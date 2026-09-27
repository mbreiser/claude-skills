from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import csv
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from kasa import Credentials, Device, DeviceConfig, Discover
from kasa.deviceconfig import DeviceConnectionParameters, DeviceEncryptionType, DeviceFamily
from kasa.exceptions import AuthenticationError

from .config import Config, StripConfig, TapoPowerError, credentials

logger = logging.getLogger("tapo_power")

CONNECT_TIMEOUT_S = 5
DISCOVERY_TIMEOUT_S = 3
SUPPORTED_MODELS = ("P304M", "P316M")
CSV_FIELDS = ["timestamp", "elapsed_s", "strip", "position", "name", "on", "power_w", "voltage_v", "current_a"]

# P304M/P316M speak KLAP v2 over HTTP. Connecting with known parameters skips
# UDP discovery, so control still works where broadcasts can't reach.
_CONNECTION = DeviceConnectionParameters(DeviceFamily.SmartTapoPlug, DeviceEncryptionType.Klap, login_version=2)

Outlet = int | str


class WaitTimeout(TapoPowerError):
    pass


@dataclass
class OutletStatus:
    position: int
    name: str
    nickname: str
    on: bool
    on_time_s: int
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    today_wh: int | None = None
    month_wh: int | None = None


@dataclass
class StripStatus:
    name: str
    model: str
    host: str
    mac: str
    fw_ver: str
    rssi: int
    outlets: list[OutletStatus] = field(default_factory=list)

    @property
    def total_w(self) -> float:
        return sum(o.power_w or 0.0 for o in self.outlets)


def _norm_mac(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


def _decode_nickname(raw: str) -> str:
    try:
        return base64.b64decode(raw, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return raw


def _run_isolated(coro):
    """Run a coroutine to completion on a fresh loop in its own thread, so this
    works even when the caller already has a running loop (Jupyter, etc.)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


class _Plug:
    """One outlet. Talks the device's own JSON methods through python-kasa's
    authenticated protocol; get_emeter_data needs energy_monitoring v2."""

    def __init__(self, child: Device):
        self._child = child

    async def _query(self, method: str) -> dict:
        return (await self._child.protocol.query(method))[method]

    async def on(self) -> None:
        await self._child.turn_on()

    async def off(self) -> None:
        await self._child.turn_off()

    async def is_on(self) -> bool:
        return (await self._query("get_device_info"))["device_on"]

    async def reading(self) -> dict:
        d = await self._query("get_emeter_data")
        return {"power_w": d["power_mw"] / 1000, "voltage_v": d["voltage_mv"] / 1000, "current_a": d["current_ma"] / 1000}

    async def energy(self) -> dict:
        d = await self._query("get_energy_usage")
        return {"today_wh": d["today_energy"], "month_wh": d["month_energy"]}


class _Connection:
    """An authenticated session with one strip — the only code that knows python-kasa."""

    def __init__(self, dev: Device):
        self._dev = dev
        self._plugs: dict[int, _Plug] = {}

    async def _query(self, method: str) -> dict:
        return (await self._dev.protocol.query(method))[method]

    async def children(self) -> list[dict]:
        kids = (await self._query("get_child_device_list"))["child_device_list"]
        for k in kids:
            if k["position"] not in self._plugs:
                self._plugs[k["position"]] = _Plug(self._dev.get_child_device(k["device_id"]))
        return [
            {
                "position": k["position"],
                "nickname": _decode_nickname(k.get("nickname", "")),
                "on": k["device_on"],
                "on_time_s": k.get("on_time", 0),
            }
            for k in kids
        ]

    async def plug(self, position: int) -> _Plug:
        if position not in self._plugs:
            await self.children()
        return self._plugs[position]

    async def info(self) -> dict:
        d = await self._query("get_device_info")
        return {"model": d["model"], "host": d["ip"], "mac": d["mac"], "fw_ver": d["fw_ver"], "rssi": d["rssi"]}

    async def close(self) -> None:
        await self._dev.disconnect()


async def _open(host: str, creds: tuple[str, str] | None) -> _Connection:
    config = DeviceConfig(
        host=host,
        credentials=Credentials(*creds) if creds else None,
        connection_type=_CONNECTION,
        timeout=CONNECT_TIMEOUT_S,
    )
    try:
        dev = await Device.connect(config=config)
    except AuthenticationError as e:
        raise TapoPowerError(
            f"Authentication failed at {host}. If the strip was added to the Tapo app, "
            f"store that account with `tapo-power login <email>`. ({e})"
        ) from e
    if not dev.model.startswith(SUPPORTED_MODELS):
        await dev.disconnect()
        raise TapoPowerError(f"{host} is a {dev.model}; only {', '.join(SUPPORTED_MODELS)} strips are supported")
    return _Connection(dev)


async def _discover(target: str, timeout_s: int) -> list[dict]:
    found = await Discover.discover(target=target, discovery_timeout=timeout_s)
    result = []
    for ip, dev in found.items():
        info = getattr(dev, "_discovery_info", None) or {}
        result.append(
            {
                "ip": ip,
                "model": info.get("device_model") or dev.model,
                "mac": dev.mac,
                "device_id": info.get("device_id"),
                "owner_bound": bool(info.get("owner")),
                "onboarded_via": info.get("obd_src"),
            }
        )
        with contextlib.suppress(Exception):
            await dev.disconnect()
    return result


def discover(target: str = "255.255.255.255", timeout_s: int = DISCOVERY_TIMEOUT_S) -> list[dict]:
    """Broadcast TP-Link discovery (UDP 20002/9999). Needs no credentials."""
    return _run_isolated(_discover(target, timeout_s))


class Strip:
    """Synchronous handle on one P316M/P304M power strip.

    Outlets can be given as a position (1-6), a local alias from the config
    file, or the outlet's Tapo nickname. Device calls are serialized, so one
    Strip can be shared between threads (e.g. a background logger).
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        host: str | None = None,
        config: Config | None = None,
    ):
        self._config = config or Config.load()
        if host:
            self._cfg = StripConfig(name=host, host=host)
            self._persist = False
        else:
            self._cfg = self._config.strip(name)
            self._persist = True
        self._creds = credentials(self._config.account)
        self._handler: _Connection | None = None
        self._kids: list[dict] = []
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="tapo-power")
        self._thread.start()

    @property
    def name(self) -> str:
        return self._cfg.name

    @property
    def host(self) -> str:
        return self._cfg.host

    def close(self) -> None:
        if not self._loop.is_running():
            return
        if self._handler is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._handler.close(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> Strip:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- plumbing -------------------------------------------------------

    def _run(self, op: Callable):
        with self._lock:
            return asyncio.run_coroutine_threadsafe(self._with_retry(op), self._loop).result()

    async def _with_retry(self, op: Callable):
        if self._handler is None:
            await self._connect()
        try:
            return await op()
        except TapoPowerError:
            raise
        except Exception as first:
            logger.info("%s: %s; reconnecting", self.name, first)
            try:
                await self._connect()
                return await op()
            except TapoPowerError:
                raise
            except Exception as e:
                raise TapoPowerError(f"{self.name}: {e}") from e

    async def _connect(self) -> None:
        if self._handler is not None:
            with contextlib.suppress(Exception):
                await self._handler.close()
        self._handler = None
        try:
            self._handler = await _open(self._cfg.host, self._creds)
            return
        except TapoPowerError:
            raise
        except Exception as e:
            err = e
        new_host = await self._rediscover()
        if new_host is None:
            raise TapoPowerError(f"Cannot reach strip {self.name!r} at {self._cfg.host}: {err}")
        self._handler = await _open(new_host, self._creds)

    async def _rediscover(self) -> str | None:
        if not self._cfg.mac:
            return None
        want = _norm_mac(self._cfg.mac)
        # Discovery is one UDP broadcast; replies occasionally drop, so try twice.
        for _ in range(2):
            ips = [d["ip"] for d in await _discover("255.255.255.255", DISCOVERY_TIMEOUT_S)
                   if _norm_mac(d["mac"] or "") == want]
            if ips:
                break
        else:
            return None
        if ips[0] == self._cfg.host:
            return None
        logger.warning("%s moved %s -> %s", self.name, self._cfg.host, ips[0])
        self._cfg.host = ips[0]
        if self._persist:
            self._config.save()
        return ips[0]

    async def _children(self) -> list[dict]:
        self._kids = await self._handler.children()
        return self._kids

    def _display_name(self, position: int, nickname: str) -> str:
        for alias, pos in self._cfg.outlets.items():
            if pos == position:
                return alias
        return nickname

    async def _resolve(self, outlet: Outlet) -> int:
        kids = self._kids or await self._children()
        positions = sorted(k["position"] for k in kids)
        if isinstance(outlet, int) or str(outlet).strip().isdigit():
            pos = int(outlet)
            if pos in positions:
                return pos
            raise TapoPowerError(f"No outlet at position {pos} (have {positions})")
        key = str(outlet).strip().lower()
        for alias, pos in self._cfg.outlets.items():
            if alias.lower() == key:
                return pos
        matches = [k["position"] for k in kids if k["nickname"].lower() == key]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise TapoPowerError(f"Outlet name {outlet!r} is ambiguous (positions {matches})")
        known = sorted(set(self._cfg.outlets) | {k["nickname"] for k in kids})
        raise TapoPowerError(f"Unknown outlet {outlet!r}. Use 1-{max(positions)} or one of: {', '.join(known)}")

    async def _resolve_many(self, outlets: Iterable[Outlet] | None) -> list[int]:
        if outlets is None:
            return sorted(k["position"] for k in (self._kids or await self._children()))
        return [await self._resolve(o) for o in outlets]

    def _status_of(self, kid: dict) -> OutletStatus:
        return OutletStatus(
            position=kid["position"],
            name=self._display_name(kid["position"], kid["nickname"]),
            nickname=kid["nickname"],
            on=kid["on"],
            on_time_s=kid["on_time_s"],
        )

    async def _read_into(self, st: OutletStatus) -> OutletStatus:
        r = await (await self._handler.plug(st.position)).reading()
        st.power_w, st.voltage_v, st.current_a = r["power_w"], r["voltage_v"], r["current_a"]
        return st

    async def _set(self, outlet: Outlet, on: bool) -> int:
        pos = await self._resolve(outlet)
        plug = await self._handler.plug(pos)
        await (plug.on() if on else plug.off())
        if await plug.is_on() != on:
            raise TapoPowerError(f"Outlet {pos} did not switch {'on' if on else 'off'}")
        return pos

    # --- public API -----------------------------------------------------

    def resolve(self, outlet: Outlet) -> int:
        """Position (1-based) for an outlet position, alias, or nickname."""
        return self._run(lambda: self._resolve(outlet))

    def outlets(self) -> list[OutletStatus]:
        """On/off state of every outlet (one device request, no power readings)."""

        async def op():
            return [self._status_of(k) for k in await self._children()]

        return self._run(op)

    def sample(self, outlets: Iterable[Outlet] | None = None) -> list[OutletStatus]:
        """State plus power (W), voltage (V) and current (A) for the given outlets (default all)."""

        async def op():
            kids = {k["position"]: k for k in await self._children()}
            return [await self._read_into(self._status_of(kids[pos])) for pos in await self._resolve_many(outlets)]

        return self._run(op)

    def status(self) -> StripStatus:
        """Strip info plus readings and today/month energy for every outlet."""

        async def op():
            info = await self._handler.info()
            result = StripStatus(name=self.name, **info)
            for kid in await self._children():
                st = await self._read_into(self._status_of(kid))
                e = await (await self._handler.plug(kid["position"])).energy()
                st.today_wh, st.month_wh = e["today_wh"], e["month_wh"]
                result.outlets.append(st)
            return result

        return self._run(op)

    def power(self, outlet: Outlet) -> float:
        """Real power draw of one outlet, in watts (mW resolution)."""

        async def op():
            plug = await self._handler.plug(await self._resolve(outlet))
            return (await plug.reading())["power_w"]

        return self._run(op)

    def is_on(self, outlet: Outlet) -> bool:
        async def op():
            return await (await self._handler.plug(await self._resolve(outlet))).is_on()

        return self._run(op)

    def on(self, outlet: Outlet) -> int:
        """Switch an outlet on and confirm it. Returns the position."""
        return self._run(lambda: self._set(outlet, True))

    def off(self, outlet: Outlet) -> int:
        """Switch an outlet off and confirm it. Returns the position."""
        return self._run(lambda: self._set(outlet, False))

    def wait_for_power(
        self,
        outlet: Outlet,
        *,
        above_w: float | None = None,
        below_w: float | None = None,
        timeout_s: float = 60.0,
        poll_s: float = 1.0,
    ) -> float:
        """Poll until the outlet's draw is > above_w and/or < below_w. Returns
        the satisfying reading; raises WaitTimeout otherwise."""
        if above_w is None and below_w is None:
            raise ValueError("give above_w and/or below_w")
        deadline = time.monotonic() + timeout_s
        while True:
            w = self.power(outlet)
            if (above_w is None or w > above_w) and (below_w is None or w < below_w):
                return w
            if time.monotonic() >= deadline:
                cond = []
                if above_w is not None:
                    cond.append(f"> {above_w} W")
                if below_w is not None:
                    cond.append(f"< {below_w} W")
                raise WaitTimeout(
                    f"{outlet}: power was {w} W, never {' and '.join(cond)} within {timeout_s} s"
                )
            time.sleep(poll_s)

    def cycle(
        self,
        outlet: Outlet,
        *,
        off_s: float = 5.0,
        wait_above_w: float | None = None,
        timeout_s: float = 60.0,
    ) -> float | None:
        """Power-cycle an outlet: off, wait off_s, on. With wait_above_w, then
        block until draw exceeds it (device booted) and return that reading."""
        pos = self.off(outlet)
        time.sleep(off_s)
        self.on(pos)
        if wait_above_w is None:
            return None
        return self.wait_for_power(pos, above_w=wait_above_w, timeout_s=timeout_s)

    def log_csv(
        self,
        dest: str | Path | TextIO,
        *,
        interval_s: float = 1.0,
        duration_s: float | None = None,
        outlets: Iterable[Outlet] | None = None,
        stop: threading.Event | None = None,
        on_sample: Callable[[list[OutletStatus]], None] | None = None,
    ) -> int:
        """Log per-outlet readings as tidy CSV (one row per outlet per sample)
        until duration_s elapses or stop is set. A path is appended to (header
        only if new); a stream is written as-is. Failed samples are logged and
        skipped. Returns the number of samples written."""
        kwargs = dict(interval_s=interval_s, duration_s=duration_s, outlets=outlets, stop=stop, on_sample=on_sample)
        if hasattr(dest, "write"):
            return self._log(dest, write_header=True, **kwargs)
        path = Path(dest)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as f:
            return self._log(f, write_header=write_header, **kwargs)

    def _log(self, f: TextIO, *, write_header, interval_s, duration_s, outlets, stop, on_sample) -> int:
        stop = stop or threading.Event()
        outlets = list(outlets) if outlets is not None else None
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_FIELDS)
        t0 = time.monotonic()
        next_t = t0
        n = 0
        while not stop.is_set():
            now = time.monotonic()
            if duration_s is not None and now - t0 >= duration_s:
                break
            ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
            try:
                rows = self.sample(outlets)
            except TapoPowerError as e:
                logger.warning("sample failed: %s", e)
            else:
                for o in rows:
                    writer.writerow([ts, f"{now - t0:.3f}", self.name, o.position, o.name, int(o.on),
                                     o.power_w, o.voltage_v, o.current_a])
                f.flush()
                n += 1
                if on_sample:
                    on_sample(rows)
            next_t += interval_s
            stop.wait(max(0.0, next_t - time.monotonic()))
        return n

    @contextlib.contextmanager
    def background_log(
        self,
        path: str | Path,
        *,
        interval_s: float = 1.0,
        outlets: Iterable[Outlet] | None = None,
    ) -> Iterator[None]:
        """Log readings to CSV in a background thread for the duration of a with-block."""
        stop = threading.Event()
        t = threading.Thread(
            target=self.log_csv,
            args=(path,),
            kwargs={"interval_s": interval_s, "outlets": outlets, "stop": stop},
            daemon=True,
            name="tapo-power-log",
        )
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join()

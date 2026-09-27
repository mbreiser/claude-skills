from __future__ import annotations

import asyncio
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

from tapo import ApiClient

from .config import Config, StripConfig, TapoPowerError, credentials

logger = logging.getLogger("tapo_power")

CONNECT_TIMEOUT_S = 8
DISCOVERY_TIMEOUT_S = 3
SUPPORTED_MODELS = ("P304M", "P316M")
CSV_FIELDS = ["timestamp", "elapsed_s", "strip", "position", "name", "on", "power_w"]

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


def _is_auth_error(e: Exception) -> bool:
    return "Unauthorized" in str(e) or "HASH_MISMATCH" in str(e)


def _run_isolated(coro):
    """Run a coroutine to completion on a fresh loop in its own thread, so this
    works even when the caller already has a running loop (Jupyter, etc.)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


async def _open(host: str, creds: list[tuple[str, str]]):
    last_auth_error = None
    for user, password in creds:
        try:
            return await ApiClient(user, password, timeout_s=CONNECT_TIMEOUT_S).p316(host)
        except Exception as e:
            if not _is_auth_error(e):
                raise
            last_auth_error = e
    raise TapoPowerError(
        f"Authentication failed at {host}. If the strip was added to the Tapo app, "
        f"store that account with `tapo-power login <email>`. ({last_auth_error})"
    )


async def _discover(target: str, timeout_s: int) -> list[dict]:
    found = []
    async for maybe in await ApiClient.discover_devices_raw(target, timeout_s):
        try:
            r = maybe.get()
        except Exception:
            continue
        res = r.message.get("result", {})
        found.append(
            {
                "ip": r.ip,
                "model": res.get("device_model"),
                "mac": res.get("mac"),
                "device_id": res.get("device_id"),
                "owner_bound": bool(res.get("owner")),
                "onboarded_via": res.get("obd_src"),
            }
        )
    return found


def discover(target: str = "255.255.255.255", timeout_s: int = DISCOVERY_TIMEOUT_S) -> list[dict]:
    """Broadcast Tapo discovery (UDP 20002). Needs no credentials."""
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
        self._handler = None
        self._plugs: dict[int, object] = {}
        self._kids: list = []
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
        if self._loop.is_running():
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
        self._handler, self._plugs = None, {}
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

    async def _children(self) -> list:
        self._kids = await self._handler.get_child_device_list()
        return self._kids

    async def _plug(self, position: int):
        if position not in self._plugs:
            self._plugs[position] = await self._handler.plug(position=position)
        return self._plugs[position]

    def _display_name(self, position: int, nickname: str) -> str:
        for alias, pos in self._cfg.outlets.items():
            if pos == position:
                return alias
        return nickname

    async def _resolve(self, outlet: Outlet) -> int:
        kids = self._kids or await self._children()
        positions = sorted(k.position for k in kids)
        if isinstance(outlet, int) or str(outlet).strip().isdigit():
            pos = int(outlet)
            if pos in positions:
                return pos
            raise TapoPowerError(f"No outlet at position {pos} (have {positions})")
        key = str(outlet).strip().lower()
        for alias, pos in self._cfg.outlets.items():
            if alias.lower() == key:
                return pos
        matches = [k.position for k in kids if k.nickname.lower() == key]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise TapoPowerError(f"Outlet name {outlet!r} is ambiguous (positions {matches})")
        known = sorted(set(self._cfg.outlets) | {k.nickname for k in kids})
        raise TapoPowerError(f"Unknown outlet {outlet!r}. Use 1-{max(positions)} or one of: {', '.join(known)}")

    async def _resolve_many(self, outlets: Iterable[Outlet] | None) -> list[int]:
        if outlets is None:
            return sorted(k.position for k in (self._kids or await self._children()))
        return [await self._resolve(o) for o in outlets]

    def _status_of(self, kid) -> OutletStatus:
        return OutletStatus(
            position=kid.position,
            name=self._display_name(kid.position, kid.nickname),
            nickname=kid.nickname,
            on=kid.device_on,
            on_time_s=kid.on_time,
        )

    async def _set(self, outlet: Outlet, on: bool) -> int:
        pos = await self._resolve(outlet)
        plug = await self._plug(pos)
        await (plug.on() if on else plug.off())
        info = await plug.get_device_info()
        if info.device_on != on:
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
        """State plus instantaneous power (W) for the given outlets (default all)."""

        async def op():
            kids = {k.position: k for k in await self._children()}
            rows = []
            for pos in await self._resolve_many(outlets):
                st = self._status_of(kids[pos])
                st.power_w = float((await (await self._plug(pos)).get_current_power()).current_power)
                rows.append(st)
            return rows

        return self._run(op)

    def status(self) -> StripStatus:
        """Strip info plus power and today/month energy for every outlet."""

        async def op():
            info = await self._handler.get_device_info()
            result = StripStatus(
                name=self.name,
                model=info.model,
                host=info.ip,
                mac=info.mac,
                fw_ver=info.fw_ver,
                rssi=info.rssi,
            )
            for kid in await self._children():
                st = self._status_of(kid)
                plug = await self._plug(kid.position)
                st.power_w = float((await plug.get_current_power()).current_power)
                usage = await plug.get_energy_usage()
                st.today_wh, st.month_wh = usage.today_energy, usage.month_energy
                result.outlets.append(st)
            return result

        return self._run(op)

    def power(self, outlet: Outlet) -> float:
        """Instantaneous power draw of one outlet, in watts (1 W resolution)."""

        async def op():
            plug = await self._plug(await self._resolve(outlet))
            return float((await plug.get_current_power()).current_power)

        return self._run(op)

    def is_on(self, outlet: Outlet) -> bool:
        async def op():
            plug = await self._plug(await self._resolve(outlet))
            return (await plug.get_device_info()).device_on

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
        """Log per-outlet power as tidy CSV (one row per outlet per sample) until
        duration_s elapses or stop is set. A path is appended to (header only if
        new); a stream is written as-is. Failed samples are logged and skipped.
        Returns the number of samples written."""
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
                    writer.writerow([ts, f"{now - t0:.3f}", self.name, o.position, o.name, int(o.on), o.power_w])
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
        """Log power to CSV in a background thread for the duration of a with-block."""
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

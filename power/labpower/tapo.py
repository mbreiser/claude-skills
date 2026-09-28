"""Tapo P304M/P316M power strips over the LAN, via python-kasa."""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import logging
import re
import socket
import threading
from collections.abc import Callable

from kasa import Credentials, Device, DeviceConfig, Discover
from kasa.deviceconfig import DeviceConnectionParameters, DeviceEncryptionType, DeviceFamily
from kasa.exceptions import AuthenticationError

from .config import Config, DeviceEntry, PowerError, tapo_credentials
from .model import ChannelStatus, DeviceStatus

logger = logging.getLogger("labpower")

CONNECT_TIMEOUT_S = 5
DISCOVERY_TIMEOUT_S = 3
MDNS_TIMEOUT_S = 2
SUPPORTED_MODELS = ("P304M", "P316M")

# Firmware updates switch these strips from KLAP to TPAP. When UDP discovery
# can't tell us which one a strip speaks, try each over plain HTTP.
_CONNECTIONS = [
    DeviceConnectionParameters(DeviceFamily.SmartTapoPlug, DeviceEncryptionType.Tpap, login_version=2),
    DeviceConnectionParameters(DeviceFamily.SmartTapoPlug, DeviceEncryptionType.Klap, login_version=2),
]


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
        if position not in self._plugs:
            raise PowerError(f"No outlet {position} (have {sorted(self._plugs)})")
        return self._plugs[position]

    async def info(self) -> dict:
        d = await self._query("get_device_info")
        return {"model": d["model"], "host": d["ip"], "mac": d["mac"], "fw_ver": d["fw_ver"], "rssi": d["rssi"]}

    async def close(self) -> None:
        await self._dev.disconnect()


def _unreachable(e: BaseException) -> bool:
    return any(isinstance(x, OSError) for x in (e, e.__cause__, *getattr(e, "args", ())))


async def _connect_device(host: str, creds: Credentials) -> Device:
    """Connect with whichever protocol the strip speaks: ask it via unicast
    discovery, and if UDP is blocked, try each known protocol in turn."""
    try:
        dev = await Discover.discover_single(
            host, credentials=creds, discovery_timeout=DISCOVERY_TIMEOUT_S, timeout=CONNECT_TIMEOUT_S
        )
    except AuthenticationError:
        raise
    except Exception:
        dev = None
    if dev is not None:
        try:
            await dev.update()
        except Exception:
            with contextlib.suppress(Exception):
                await dev.disconnect()
            raise
        return dev
    last: Exception | None = None
    for params in _CONNECTIONS:
        config = DeviceConfig(host=host, credentials=creds, connection_type=params, timeout=CONNECT_TIMEOUT_S)
        try:
            return await Device.connect(config=config)
        except AuthenticationError:
            raise
        except Exception as e:
            if _unreachable(e):
                raise
            last = e
    raise last


async def _open(host: str, creds: list[tuple[str, str]]) -> _Connection:
    last_refusal: Exception | None = None
    for user, password in creds:
        try:
            dev = await _connect_device(host, Credentials(user, password))
            break
        except AuthenticationError as e:
            last_refusal = e
        except Exception as e:
            if not ("403" in str(e) and "handshake1" in str(e)):
                raise
            last_refusal = e
    else:
        raise PowerError(
            f"The strip at {host} refused the login. If it was added to the Tapo app, store that "
            f"account with `power login <email>`. ({last_refusal})"
        )
    if not dev.model.startswith(SUPPORTED_MODELS):
        await dev.disconnect()
        raise PowerError(f"{host} is a {dev.model}; only {', '.join(SUPPORTED_MODELS)} strips are supported")
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


async def _resolve_mdns(mac: str, timeout_s: float = MDNS_TIMEOUT_S) -> str | None:
    """Matter devices keep advertising <MAC>.local over mDNS; return its current IPv4."""
    loop = asyncio.get_running_loop()
    name = f"{_norm_mac(mac).upper()}.local"
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(name, 80, family=socket.AF_INET, type=socket.SOCK_STREAM), timeout_s
        )
    except OSError:  # includes TimeoutError
        return None
    return infos[0][4][0] if infos else None


async def _find_by_mac(mac: str) -> str | None:
    """Current IP for a MAC: mDNS first (fast, reliable for Matter strips), then
    TP-Link broadcast discovery, which sometimes goes unanswered — so tried twice."""
    if ip := await _resolve_mdns(mac):
        return ip
    want = _norm_mac(mac)
    for _ in range(2):
        for d in await _discover("255.255.255.255", DISCOVERY_TIMEOUT_S):
            if _norm_mac(d["mac"] or "") == want:
                return d["ip"]
    return None


def find_by_mac(mac: str) -> str | None:
    return _run_isolated(_find_by_mac(mac))


class TapoStrip:
    """Backend for one strip. Holds one authenticated session on a private event
    loop thread; calls are serialized, reconnect once on failure, and re-find a
    strip whose IP moved (saving the new IP when a config is given)."""

    def __init__(self, entry: DeviceEntry, *, config: Config | None = None):
        self._entry = entry
        self._config = config
        self._creds = tapo_credentials(config.tapo_account if config else None)
        self._handler: _Connection | None = None
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name=f"tapo-{entry.name}")
        self._thread.start()

    @property
    def name(self) -> str:
        return self._entry.name

    @property
    def host(self) -> str:
        return self._entry.host

    def close(self) -> None:
        if not self._loop.is_running():
            return
        if self._handler is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._handler.close(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    # --- plumbing -------------------------------------------------------

    def _run(self, op: Callable):
        with self._lock:
            return asyncio.run_coroutine_threadsafe(self._with_retry(op), self._loop).result()

    async def _with_retry(self, op: Callable):
        if self._handler is None:
            await self._connect()
        try:
            return await op()
        except PowerError:
            raise
        except Exception as first:
            logger.info("%s: %s; reconnecting", self.name, first)
            try:
                await self._connect()
                return await op()
            except PowerError:
                raise
            except Exception as e:
                raise PowerError(f"{self.name}: {e}") from e

    async def _connect(self) -> None:
        if self._handler is not None:
            with contextlib.suppress(Exception):
                await self._handler.close()
        self._handler = None
        try:
            self._handler = await _open(self._entry.host, self._creds)
            return
        except PowerError:
            raise
        except Exception as e:
            err = e
        new_host = await self._rediscover()
        if new_host is None:
            raise PowerError(f"Cannot reach strip {self.name!r} at {self._entry.host}: {err}")
        try:
            self._handler = await _open(new_host, self._creds)
        except PowerError:
            raise
        except Exception as e:
            raise PowerError(f"Found strip {self.name!r} at {new_host} but could not connect: {e}") from e

    async def _rediscover(self) -> str | None:
        if not self._entry.mac:
            return None
        ip = await _find_by_mac(self._entry.mac)
        if ip is None or ip == self._entry.host:
            return None
        logger.warning("%s moved %s -> %s", self.name, self._entry.host, ip)
        self._entry.host = ip
        if self._config is not None:
            self._config.save()
        return ip

    def _status_of(self, kid: dict) -> ChannelStatus:
        return ChannelStatus(
            device=self.name, channel=kid["position"], native_name=kid["nickname"],
            on=kid["on"], on_time_s=kid["on_time_s"],
        )

    async def _read_into(self, st: ChannelStatus) -> ChannelStatus:
        r = await (await self._handler.plug(st.channel)).reading()
        st.power_w, st.voltage_v, st.current_a = r["power_w"], r["voltage_v"], r["current_a"]
        return st

    # --- Backend --------------------------------------------------------

    def channels(self) -> list[ChannelStatus]:
        async def op():
            return [self._status_of(k) for k in await self._handler.children()]

        return self._run(op)

    def sample(self, channels: list[int] | None = None) -> list[ChannelStatus]:
        async def op():
            kids = {k["position"]: k for k in await self._handler.children()}
            wanted = sorted(kids) if channels is None else channels
            missing = [c for c in wanted if c not in kids]
            if missing:
                raise PowerError(f"{self.name} has no outlet {missing[0]} (have {sorted(kids)})")
            return [await self._read_into(self._status_of(kids[c])) for c in wanted]

        return self._run(op)

    def status(self) -> DeviceStatus:
        async def op():
            result = DeviceStatus(name=self.name, type="tapo-strip", online=True, info=await self._handler.info())
            for kid in await self._handler.children():
                st = await self._read_into(self._status_of(kid))
                e = await (await self._handler.plug(kid["position"])).energy()
                st.today_wh, st.month_wh = e["today_wh"], e["month_wh"]
                result.channels.append(st)
            return result

        return self._run(op)

    def set(self, channel: int, on: bool) -> None:
        async def op():
            plug = await self._handler.plug(channel)
            await (plug.on() if on else plug.off())
            if await plug.is_on() != on:
                raise PowerError(f"{self.name} outlet {channel} did not switch {'on' if on else 'off'}")

        self._run(op)

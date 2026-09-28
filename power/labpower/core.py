from __future__ import annotations

import concurrent.futures
import contextlib
import csv
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .config import Config, DeviceEntry, PowerError, WaitTimeout
from .model import Backend, ChannelStatus, DeviceStatus

logger = logging.getLogger("labpower")

CSV_FIELDS = ["timestamp", "elapsed_s", "device", "channel", "name", "on", "power_w", "voltage_v", "current_a"]

Outlet = str | int


class Power:
    """Every configured power device behind one API.

    An outlet is named by an alias from the config (`ArenaPS`), `Device:channel`
    (`SmartPowerStrip:6`), a single-outlet device's own name (`BenchPlug`), or a
    Tapo outlet nickname. Devices connect lazily on first use; calls are
    synchronous and safe to share between threads (e.g. a background logger).
    """

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()
        self._backends: dict[str, Backend] = {}
        self._bridge = None
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="labpower")

    def close(self) -> None:
        with self._lock:
            for b in self._backends.values():
                with contextlib.suppress(Exception):
                    b.close()
            if self._bridge is not None:
                with contextlib.suppress(Exception):
                    self._bridge.close()
            self._backends.clear()
            self._bridge = None
        self._pool.shutdown(wait=False)

    def __enter__(self) -> Power:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- devices ---------------------------------------------------------

    def backend(self, device: str) -> Backend:
        entry = self.config.device(device)
        with self._lock:
            if entry.name not in self._backends:
                self._backends[entry.name] = self._make(entry)
            return self._backends[entry.name]

    def _make(self, entry: DeviceEntry) -> Backend:
        if entry.type == "tapo-strip":
            from .tapo import TapoStrip

            return TapoStrip(entry, config=self.config)
        if entry.type == "zigbee2mqtt":
            from .zigbee import Z2MBridge, Z2MPlug

            if self._bridge is None:
                self._bridge = Z2MBridge(self.config.mqtt)
            return Z2MPlug(entry, self._bridge)
        raise PowerError(f"{entry.name}: unknown device type {entry.type!r}")

    # --- naming ----------------------------------------------------------

    def _parse_target(self, target: str) -> tuple[str, int]:
        device, _, channel = target.rpartition(":")
        if not device or not channel.strip().isdigit():
            raise PowerError(f"Bad outlet {target!r}; expected Device:channel, e.g. SmartPowerStrip:6")
        return self.config.device(device).name, int(channel)

    def resolve(self, outlet: Outlet) -> tuple[str, int]:
        """(device, channel) for an alias, Device:channel, a single-outlet
        device's name, a bare channel number (only with one strip configured),
        or a Tapo outlet nickname."""
        key = str(outlet).strip()
        for alias, target in self.config.aliases.items():
            if alias.lower() == key.lower():
                return self._parse_target(target)
        if ":" in key:
            return self._parse_target(key)
        strips = [d for d in self.config.devices.values() if d.type == "tapo-strip"]
        if key.isdigit():
            if len(strips) == 1:
                return strips[0].name, int(key)
            raise PowerError(f"Outlet number {key} is ambiguous; use Device:{key}")
        for dev in self.config.devices.values():
            if dev.name.lower() == key.lower():
                if dev.type == "zigbee2mqtt":
                    return dev.name, 1
                raise PowerError(f"{dev.name} has several outlets; name one, e.g. {dev.name}:1")
        matches = []
        for dev in strips:
            with contextlib.suppress(PowerError):
                matches += [(dev.name, c.channel) for c in self.backend(dev.name).channels()
                            if c.native_name.lower() == key.lower()]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise PowerError(f"Outlet name {outlet!r} is ambiguous: {', '.join(f'{d}:{c}' for d, c in matches)}")
        raise PowerError(
            f"Unknown outlet {outlet!r}. Aliases: {', '.join(self.config.aliases) or 'none'}; "
            f"devices: {', '.join(self.config.devices) or 'none'} (use Device:channel for strips)"
        )

    def display_name(self, device: str, channel: int, native_name: str) -> str:
        for alias, target in self.config.aliases.items():
            with contextlib.suppress(PowerError):
                if self._parse_target(target) == (device, channel):
                    return alias
        return native_name

    def _named(self, rows: list[ChannelStatus]) -> list[ChannelStatus]:
        for r in rows:
            r.name = self.display_name(r.device, r.channel, r.native_name)
        return rows

    # --- reading ---------------------------------------------------------

    def _groups(self, outlets: Iterable[Outlet] | None) -> dict[str, list[int] | None]:
        if outlets is None:
            return {name: None for name in self.config.devices}
        groups: dict[str, list[int]] = {}
        for o in outlets:
            device, channel = self.resolve(o)
            chans = groups.setdefault(device, [])
            if channel not in chans:
                chans.append(channel)
        return groups

    def _sample_groups(self, groups: dict[str, list[int] | None]) -> tuple[list[ChannelStatus], dict[str, str]]:
        futures = {d: self._pool.submit(lambda d=d, c=c: self.backend(d).sample(c)) for d, c in groups.items()}
        rows, errors = [], {}
        for device, fut in futures.items():
            try:
                rows += fut.result()
            except PowerError as e:
                errors[device] = str(e)
        return self._named(rows), errors

    def sample(self, outlets: Iterable[Outlet] | None = None) -> list[ChannelStatus]:
        """State plus power (W), voltage (V) and current (A) for the given outlets
        (default: every outlet of every device). Devices are read in parallel."""
        rows, errors = self._sample_groups(self._groups(outlets))
        if errors:
            raise PowerError("; ".join(f"{d}: {e}" for d, e in errors.items()))
        return rows

    def read(self, outlet: Outlet) -> ChannelStatus:
        device, channel = self.resolve(outlet)
        return self._named(self.backend(device).sample([channel]))[0]

    def power(self, outlet: Outlet) -> float:
        """Real power draw of one outlet in watts."""
        return self.read(outlet).power_w

    def is_on(self, outlet: Outlet) -> bool:
        device, channel = self.resolve(outlet)
        return next(c.on for c in self.backend(device).channels() if c.channel == channel)

    def status(self, devices: Iterable[str] | None = None) -> list[DeviceStatus]:
        """Info, readings and energy for each device, probed in parallel. An
        unreachable device comes back with online=False and the error."""
        entries = [self.config.device(d) for d in devices] if devices is not None else list(self.config.devices.values())

        def one(entry: DeviceEntry) -> DeviceStatus:
            try:
                st = self.backend(entry.name).status()
                self._named(st.channels)
                return st
            except PowerError as e:
                return DeviceStatus(name=entry.name, type=entry.type, online=False, error=str(e))

        return list(self._pool.map(one, entries))

    # --- switching -------------------------------------------------------

    def on(self, outlet: Outlet) -> tuple[str, int]:
        """Switch an outlet on and confirm it. Returns (device, channel)."""
        device, channel = self.resolve(outlet)
        self.backend(device).set(channel, True)
        return device, channel

    def off(self, outlet: Outlet) -> tuple[str, int]:
        """Switch an outlet off and confirm it. Returns (device, channel)."""
        device, channel = self.resolve(outlet)
        self.backend(device).set(channel, False)
        return device, channel

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
                raise WaitTimeout(f"{outlet}: power was {w} W, never {' and '.join(cond)} within {timeout_s} s")
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
        target = ":".join(map(str, self.off(outlet)))
        time.sleep(off_s)
        self.on(target)
        if wait_above_w is None:
            return None
        return self.wait_for_power(target, above_w=wait_above_w, timeout_s=timeout_s)

    # --- logging ---------------------------------------------------------

    def log_csv(
        self,
        dest: str | Path | TextIO,
        *,
        interval_s: float = 1.0,
        duration_s: float | None = None,
        outlets: Iterable[Outlet] | None = None,
        stop: threading.Event | None = None,
        on_sample: Callable[[list[ChannelStatus]], None] | None = None,
    ) -> int:
        """Log readings as tidy CSV (one row per outlet per sample) until
        duration_s elapses or stop is set. A path is appended to (header only if
        new); a stream is written as-is. A device that fails a sample is logged
        and skipped for that sample. Returns the number of samples written."""
        kwargs = dict(interval_s=interval_s, duration_s=duration_s, outlets=outlets, stop=stop, on_sample=on_sample)
        if hasattr(dest, "write"):
            return self._log(dest, write_header=True, **kwargs)
        path = Path(dest)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as f:
            return self._log(f, write_header=write_header, **kwargs)

    def _log(self, f: TextIO, *, write_header, interval_s, duration_s, outlets, stop, on_sample) -> int:
        stop = stop or threading.Event()
        groups = self._groups(list(outlets) if outlets is not None else None)
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
            rows, errors = self._sample_groups(groups)
            for device, err in errors.items():
                logger.warning("sample failed for %s: %s", device, err)
            if rows:
                for r in rows:
                    writer.writerow([ts, f"{now - t0:.3f}", r.device, r.channel, r.name, int(r.on),
                                     r.power_w, r.voltage_v, r.current_a])
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
            name="labpower-log",
        )
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join()

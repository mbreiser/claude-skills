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

from .config import Config, DeviceEntry, PowerError, WaitTimeout, alias_targets
from .model import Backend, ChannelStatus, DeviceStatus

logger = logging.getLogger("labpower")

CSV_FIELDS = ["timestamp", "elapsed_s", "device", "channel", "name", "on", "power_w", "voltage_v", "current_a"]
RETRY_S = 30.0
JOIN_TIMEOUT_S = 15.0

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
        """An alias target: Device:channel, or a single-outlet device's bare name."""
        device, _, channel = target.rpartition(":")
        if not device:
            entry = self.config.device(target)
            if entry.type == "zigbee2mqtt":
                return entry.name, 1
            raise PowerError(f"Bad outlet {target!r}; expected Device:channel, e.g. {entry.name}:1")
        if not channel.strip().isdigit():
            raise PowerError(f"Bad outlet {target!r}; expected Device:channel, e.g. SmartPowerStrip:6")
        return self.config.device(device).name, int(channel)

    def resolve(self, outlet: Outlet) -> tuple[str, int]:
        """(device, channel) for an alias, Device:channel, a single-outlet
        device's name, a bare channel number (only with one strip configured),
        or a Tapo outlet nickname. An alias with several targets resolves to
        the first one whose device answers."""
        key = str(outlet).strip()
        for alias, value in self.config.aliases.items():
            if alias.lower() == key.lower():
                targets = [self._parse_target(t) for t in alias_targets(value)]
                return targets[0] if len(targets) == 1 else self._first_reachable(alias, targets)
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

    def _first_reachable(self, alias: str, targets: list[tuple[str, int]]) -> tuple[str, int]:
        errors = []
        for device, channel in targets:
            try:
                if any(c.channel == channel for c in self.backend(device).channels()):
                    return device, channel
                errors.append(f"{device}:{channel}: no such outlet")
            except PowerError as e:
                errors.append(f"{device}:{channel}: {e}")
        raise PowerError(f"No target of {alias!r} is reachable ({'; '.join(errors)})")

    def display_name(self, device: str, channel: int, native_name: str) -> str:
        for alias, value in self.config.aliases.items():
            for target in alias_targets(value):
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
        best_effort: bool = False,
        retry_s: float = RETRY_S,
    ) -> int:
        """Log readings as tidy CSV (one row per outlet per sample) until
        duration_s elapses or stop is set. A path is appended to (header only if
        new); a stream is written as-is. A device that fails a sample is logged
        and skipped for that sample. Returns the number of samples written.

        best_effort=True never raises: outlets that can't be resolved or read
        are retried every retry_s, fallback aliases fail over to their next
        target (and back to the first once it answers again), and a run with
        nothing reachable just writes the header."""
        kwargs = dict(interval_s=interval_s, duration_s=duration_s, outlets=outlets, stop=stop,
                      on_sample=on_sample, best_effort=best_effort, retry_s=retry_s)
        try:
            if hasattr(dest, "write"):
                return self._log(dest, write_header=True, **kwargs)
            path = Path(dest)
            write_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="") as f:
                return self._log(f, write_header=write_header, **kwargs)
        except Exception as e:
            if not best_effort or isinstance(e, KeyboardInterrupt):
                raise
            logger.warning("power logging stopped: %s", e)
            return 0

    def _log(self, f: TextIO, *, write_header, interval_s, duration_s, outlets, stop, on_sample,
             best_effort, retry_s) -> int:
        stop = stop or threading.Event()
        outlets = list(outlets) if outlets is not None else None
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_FIELDS)
            f.flush()
        groups: dict[str, list[int] | None] | None = None
        resolved_at = float("-inf")
        warned_at: dict[str, float] = {}

        def warn(key: str, msg: str) -> None:
            # Once per retry period per problem, so a long outage doesn't flood the log.
            if time.monotonic() - warned_at.get(key, float("-inf")) >= retry_s:
                warned_at[key] = time.monotonic()
                logger.warning("%s", msg)

        if best_effort:
            self._warm_up(outlets)
        t0 = time.monotonic()
        next_t = t0
        n = 0
        while not stop.is_set():
            now = time.monotonic()
            if duration_s is not None and now - t0 >= duration_s:
                break
            if groups is None or (best_effort and now - resolved_at >= retry_s):
                resolved_at = now
                try:
                    groups = self._groups(outlets)
                except PowerError as e:
                    if not best_effort:
                        raise
                    groups = None
                    warn("resolve", f"power logging: {e}; retrying every {retry_s:g} s")
            ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
            rows, errors = self._sample_groups(groups) if groups else ([], {})
            for device, err in errors.items():
                warn(device, f"sample failed for {device}: {err}")
            if errors and best_effort:
                resolved_at = float("-inf")  # re-resolve next time, so fallback aliases fail over
            if rows:
                for r in rows:
                    writer.writerow([ts, f"{now - t0:.3f}", r.device, r.channel, r.name, int(r.on),
                                     r.power_w, r.voltage_v, r.current_a])
                f.flush()
                n += 1
                if on_sample:
                    on_sample(rows)
            if best_effort and groups is None:  # nothing reachable: back off
                next_t = time.monotonic() + retry_s
            else:  # including after a failed sample: fail over on the next tick
                next_t += interval_s
            stop.wait(max(0.0, next_t - time.monotonic()))
        return n

    def _warm_up(self, outlets: list[Outlet] | None) -> None:
        """Connect every device a fallback alias might fail over to, in parallel,
        so a failover doesn't wait for a (slow) Tapo handshake."""
        devices = set(self.config.devices) if outlets is None else set()
        for o in outlets or []:
            for alias, value in self.config.aliases.items():
                if alias.lower() == str(o).strip().lower():
                    for t in alias_targets(value):
                        with contextlib.suppress(PowerError):
                            devices.add(self._parse_target(t)[0])

        def probe(device: str) -> None:
            with contextlib.suppress(Exception):
                self.backend(device).channels()

        list(self._pool.map(probe, devices))

    @contextlib.contextmanager
    def background_log(
        self,
        path: str | Path,
        *,
        interval_s: float = 1.0,
        outlets: Iterable[Outlet] | None = None,
        best_effort: bool = False,
        retry_s: float = RETRY_S,
    ) -> Iterator[None]:
        """Log readings to CSV in a background thread for the duration of a
        with-block. With best_effort=True the block is never affected by power
        problems (see log_csv)."""
        stop = threading.Event()
        t = threading.Thread(
            target=self.log_csv,
            args=(path,),
            kwargs={"interval_s": interval_s, "outlets": outlets, "stop": stop,
                    "best_effort": best_effort, "retry_s": retry_s},
            daemon=True,
            name="labpower-log",
        )
        t.start()
        try:
            yield
        finally:
            stop.set()
            # A best-effort logger may be mid-way through probing an unreachable
            # device; don't hold the caller up longer than one connect attempt.
            t.join(timeout=JOIN_TIMEOUT_S if best_effort else None)

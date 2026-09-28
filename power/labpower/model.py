from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ChannelStatus:
    """One switchable outlet. `name` is the alias if one points here, else `native_name`."""

    device: str
    channel: int
    native_name: str
    on: bool
    name: str = ""
    on_time_s: int | None = None
    power_w: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    today_wh: float | None = None
    month_wh: float | None = None
    total_kwh: float | None = None


@dataclass
class DeviceStatus:
    name: str
    type: str
    online: bool
    info: dict = field(default_factory=dict)
    channels: list[ChannelStatus] = field(default_factory=list)
    error: str | None = None

    @property
    def total_w(self) -> float:
        return sum(c.power_w or 0.0 for c in self.channels)


class Backend(Protocol):
    """What every device type implements. All calls are synchronous and
    thread-safe; readings are real power (W), RMS voltage (V), RMS current (A)."""

    def channels(self) -> list[ChannelStatus]: ...

    def sample(self, channels: list[int] | None = None) -> list[ChannelStatus]: ...

    def status(self) -> DeviceStatus: ...

    def set(self, channel: int, on: bool) -> None: ...

    def close(self) -> None: ...

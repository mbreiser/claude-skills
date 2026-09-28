from __future__ import annotations

import pytest

from tapo_power import strip as strip_mod
from tapo_power.config import Config, StripConfig

BENCH_MAC = "58-D8-12-14-1B-6F"
BENCH_HOST = "10.0.0.5"
REAL_RESOLVE_MDNS = strip_mod._resolve_mdns


class FakePlug:
    """Stand-in for tapo_power.strip._Plug (one outlet)."""

    def __init__(self, position, nickname, *, device_on=False, on_time=0, power=0.0, voltage=120.0):
        self.position = position
        self.nickname = nickname
        self.device_on = device_on
        self.on_time = on_time
        self.power = power
        self.voltage = voltage
        self.power_series: list[float] = []
        self.today_energy = 0
        self.month_energy = 0
        self.ignore_commands = False
        self.raise_once_on_power: Exception | None = None

    async def on(self):
        if not self.ignore_commands:
            self.device_on = True

    async def off(self):
        if not self.ignore_commands:
            self.device_on = False

    async def is_on(self):
        return self.device_on

    async def reading(self):
        if self.raise_once_on_power is not None:
            exc, self.raise_once_on_power = self.raise_once_on_power, None
            raise exc
        if len(self.power_series) > 1:
            w = self.power_series.pop(0)
        elif self.power_series:
            w = self.power_series[0]
        else:
            w = self.power
        return {"power_w": float(w), "voltage_v": self.voltage, "current_a": w / self.voltage}

    async def energy(self):
        return {"today_wh": self.today_energy, "month_wh": self.month_energy}


class FakeHandler:
    """Stand-in for tapo_power.strip._Connection (an open strip session)."""

    def __init__(self, plugs, *, model="P316M", ip=BENCH_HOST, mac=BENCH_MAC, fw_ver="1.0.0 Build 1", rssi=-50):
        self.plugs = plugs
        self.model = model
        self.ip = ip
        self.mac = mac
        self.fw_ver = fw_ver
        self.rssi = rssi
        self.closed = False

    async def children(self):
        return [
            {"position": p.position, "nickname": p.nickname, "on": p.device_on, "on_time_s": p.on_time}
            for p in sorted(self.plugs.values(), key=lambda x: x.position)
        ]

    async def plug(self, position):
        return self.plugs[position]

    async def info(self):
        return {"model": self.model, "host": self.ip, "mac": self.mac, "fw_ver": self.fw_ver, "rssi": self.rssi}

    async def close(self):
        self.closed = True


def make_plugs(nicknames: dict[int, str]) -> dict[int, FakePlug]:
    return {pos: FakePlug(pos, nickname) for pos, nickname in nicknames.items()}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No test may touch the real config file or a real device."""
    import tapo_power.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "isolated-config.json")

    async def no_network(*a, **kw):
        raise RuntimeError("test tried to reach a real device; fake _open/_discover instead")

    monkeypatch.setattr(strip_mod.Device, "connect", no_network)
    monkeypatch.setattr(strip_mod.Discover, "discover", no_network)
    monkeypatch.setattr(strip_mod.Discover, "discover_single", no_network)

    async def no_mdns(mac, timeout_s=None):
        return None

    monkeypatch.setattr(strip_mod, "_resolve_mdns", no_mdns)


@pytest.fixture
def bench_config():
    return Config(
        default="bench",
        strips={
            "bench": StripConfig(
                name="bench",
                host=BENCH_HOST,
                mac=BENCH_MAC,
                outlets={"arena": 3},
            )
        },
    )


@pytest.fixture
def plugs():
    return make_plugs(
        {
            1: "Plug 1",
            2: "Plug 2",
            3: "Arena Plug",
            4: "Plug 4",
            5: "Plug 5",
            6: "Plug 6",
        }
    )


@pytest.fixture
def handler(plugs):
    return FakeHandler(plugs)


@pytest.fixture
def open_calls():
    return {"n": 0}


@pytest.fixture
def patch_open(monkeypatch, handler, open_calls):
    async def fake_open(host, creds):
        open_calls["n"] += 1
        return handler

    monkeypatch.setattr(strip_mod, "_open", fake_open)
    return fake_open


@pytest.fixture
def patch_discover_empty(monkeypatch):
    async def fake_discover(target, timeout_s):
        return []

    monkeypatch.setattr(strip_mod, "_discover", fake_discover)
    return fake_discover


@pytest.fixture
def fast_sleep(monkeypatch):
    """Make `time.sleep` inside tapo_power.strip a no-op so poll loops (which
    use a hardcoded default poll_s) run in effectively zero wall-clock time."""
    monkeypatch.setattr(strip_mod.time, "sleep", lambda s: None)


@pytest.fixture
def strip(bench_config, patch_open, patch_discover_empty):
    with strip_mod.Strip(config=bench_config) as s:
        yield s


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    """Pre-populate a tmp config file with one strip named 'bench' and point
    tapo_power.config.CONFIG_PATH at it, so tapo_power.cli.main's internal
    Config.load() picks it up."""
    import tapo_power.config as config_mod

    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    cfg = Config(
        default="bench",
        strips={"bench": StripConfig(name="bench", host=BENCH_HOST, mac=BENCH_MAC, outlets={})},
    )
    cfg.save(path)
    return path

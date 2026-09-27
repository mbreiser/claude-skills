from __future__ import annotations

from types import SimpleNamespace

import pytest

from tapo_power import strip as strip_mod
from tapo_power.config import Config, StripConfig

BENCH_MAC = "58-D8-12-14-1B-6F"
BENCH_HOST = "10.0.0.5"


class FakePlug:
    """Stand-in for a `tapo` plug handler for one outlet."""

    def __init__(self, position, nickname, *, device_on=False, on_time=0, is_usb=False, power=0):
        self.position = position
        self.nickname = nickname
        self.device_on = device_on
        self.on_time = on_time
        self.is_usb = is_usb
        self.power = power
        self.power_series: list[int] = []
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

    async def get_device_info(self):
        return SimpleNamespace(device_on=self.device_on)

    async def get_current_power(self):
        if self.raise_once_on_power is not None:
            exc, self.raise_once_on_power = self.raise_once_on_power, None
            raise exc
        if len(self.power_series) > 1:
            v = self.power_series.pop(0)
        elif self.power_series:
            v = self.power_series[0]
        else:
            v = self.power
        return SimpleNamespace(current_power=v)

    async def get_energy_usage(self):
        return SimpleNamespace(today_energy=self.today_energy, month_energy=self.month_energy)


class FakeHandler:
    """Stand-in for a `tapo` P316M strip handler."""

    def __init__(self, plugs, *, model="P316M", ip=BENCH_HOST, mac=BENCH_MAC, fw_ver="1.0.0 Build 1", rssi=-50):
        self.plugs = plugs
        self.model = model
        self.ip = ip
        self.mac = mac
        self.fw_ver = fw_ver
        self.rssi = rssi

    async def get_child_device_list(self):
        return [
            SimpleNamespace(
                position=p.position,
                nickname=p.nickname,
                device_on=p.device_on,
                on_time=p.on_time,
                is_usb=p.is_usb,
            )
            for p in sorted(self.plugs.values(), key=lambda x: x.position)
        ]

    async def plug(self, position):
        return self.plugs[position]

    async def get_device_info(self):
        return SimpleNamespace(model=self.model, ip=self.ip, mac=self.mac, fw_ver=self.fw_ver, rssi=self.rssi)


def make_plugs(nicknames: dict[int, str]) -> dict[int, FakePlug]:
    return {pos: FakePlug(pos, nickname) for pos, nickname in nicknames.items()}


def make_fake_api_client():
    """Factory for a fake `tapo.ApiClient` with per-test-instance state, so that
    monkeypatching `tapo_power.strip.ApiClient` with the returned class lets a
    test script exactly which (user, password) pairs fail and how."""
    calls: list[tuple[str, str]] = []
    behaviors: dict[tuple[str, str], Exception] = {}

    class FakeApiClient:
        def __init__(self, user, password, timeout_s=None):
            self.user = user
            self.password = password

        async def p316(self, host):
            calls.append((self.user, self.password))
            exc = behaviors.get((self.user, self.password))
            if exc is not None:
                raise exc
            return SimpleNamespace(user=self.user, password=self.password, host=host)

    FakeApiClient.calls = calls
    FakeApiClient.behaviors = behaviors
    return FakeApiClient


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No test may touch the real config file or a real device."""
    import tapo_power.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "isolated-config.json")

    class NoNetworkApiClient:
        def __init__(self, *a, **kw):
            raise RuntimeError("test tried to build a real tapo ApiClient; fake _open/_discover instead")

    monkeypatch.setattr(strip_mod, "ApiClient", NoNetworkApiClient)


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

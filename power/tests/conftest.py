from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from labpower import config as config_mod
from labpower import core as core_mod
from labpower import tapo as tapo_mod
from labpower import zigbee as zigbee_mod
from labpower.config import Config, DeviceEntry, PowerError
from labpower.model import ChannelStatus, DeviceStatus

BENCH_MAC = "58-D8-12-14-1B-6F"
BENCH_HOST = "10.0.0.5"
REAL_RESOLVE_MDNS = tapo_mod._resolve_mdns


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No test may touch the real config, a real device, or a real broker."""
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "LEGACY_TAPO_CONFIG", tmp_path / "legacy-tapo-config.json")

    async def no_network(*a, **kw):
        raise RuntimeError("test tried to reach a real device; fake _open/_discover instead")

    monkeypatch.setattr(tapo_mod.Device, "connect", no_network)
    monkeypatch.setattr(tapo_mod.Discover, "discover", no_network)
    monkeypatch.setattr(tapo_mod.Discover, "discover_single", no_network)

    async def no_mdns(mac, timeout_s=None):
        return None

    monkeypatch.setattr(tapo_mod, "_resolve_mdns", no_mdns)

    def no_mqtt():
        raise RuntimeError("test tried to open a real MQTT connection; pass a fake client_factory")

    monkeypatch.setattr(zigbee_mod, "_make_client", no_mqtt)


# --- Tapo session fakes (stand-ins for tapo._Connection / tapo._Plug) --------


class FakePlug:
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
    def __init__(self, plugs, *, model="P316M", ip=BENCH_HOST, mac=BENCH_MAC, fw_ver="1.4.1 Build 1", rssi=-50):
        self.plugs = plugs
        self.info_dict = {"model": model, "host": ip, "mac": mac, "fw_ver": fw_ver, "rssi": rssi}
        self.closed = False

    async def children(self):
        return [
            {"position": p.position, "nickname": p.nickname, "on": p.device_on, "on_time_s": p.on_time}
            for p in sorted(self.plugs.values(), key=lambda x: x.position)
        ]

    async def plug(self, position):
        if position not in self.plugs:
            raise PowerError(f"No outlet {position} (have {sorted(self.plugs)})")
        return self.plugs[position]

    async def info(self):
        return dict(self.info_dict)

    async def close(self):
        self.closed = True


@pytest.fixture
def plugs():
    names = {1: "Plug 1", 2: "Plug 2", 3: "Arena Plug", 4: "Plug 4", 5: "Plug 5", 6: "Plug 6"}
    return {pos: FakePlug(pos, n) for pos, n in names.items()}


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

    monkeypatch.setattr(tapo_mod, "_open", fake_open)
    return fake_open


@pytest.fixture
def patch_discover_empty(monkeypatch):
    async def fake_discover(target, timeout_s):
        return []

    monkeypatch.setattr(tapo_mod, "_discover", fake_discover)


@pytest.fixture
def strip_entry():
    return DeviceEntry(name="bench", type="tapo-strip", host=BENCH_HOST, mac=BENCH_MAC)


@pytest.fixture
def strip(strip_entry, patch_open, patch_discover_empty):
    s = tapo_mod.TapoStrip(strip_entry, config=None)
    yield s
    s.close()


# --- Power-level fakes -----------------------------------------------------


class FakeBackend:
    """Stand-in for any Backend, for testing Power without device code."""

    def __init__(self, name, native_names: dict[int, str], *, type="tapo-strip"):
        self.name = name
        self.type = type
        self.native = native_names
        self.state = {c: False for c in native_names}
        self.power = {c: 0.0 for c in native_names}
        self.power_series: dict[int, list[float]] = {}
        self.calls: list[tuple[int, bool]] = []
        self.fail: str | None = None
        self.closed = False

    def _w(self, c):
        series = self.power_series.get(c)
        if series:
            return series.pop(0) if len(series) > 1 else series[0]
        return self.power[c]

    def _row(self, c, with_reading):
        row = ChannelStatus(device=self.name, channel=c, native_name=self.native[c], on=self.state[c])
        if with_reading:
            w = self._w(c)
            row.power_w, row.voltage_v, row.current_a = w, 120.0, w / 120.0
        return row

    def channels(self):
        if self.fail:
            raise PowerError(self.fail)
        return [self._row(c, False) for c in self.native]

    def sample(self, channels=None):
        if self.fail:
            raise PowerError(self.fail)
        wanted = list(self.native) if channels is None else channels
        for c in wanted:
            if c not in self.native:
                raise PowerError(f"{self.name} has no outlet {c}")
        return [self._row(c, True) for c in wanted]

    def status(self):
        return DeviceStatus(name=self.name, type=self.type, online=True, info={"model": "Fake"}, channels=self.sample())

    def set(self, channel, on):
        if self.fail:
            raise PowerError(self.fail)
        if channel not in self.native:
            raise PowerError(f"{self.name} has no outlet {channel}")
        self.calls.append((channel, on))
        self.state[channel] = on

    def close(self):
        self.closed = True


@pytest.fixture
def lab_config():
    return Config(
        devices={
            "Strip": DeviceEntry(name="Strip", type="tapo-strip", host=BENCH_HOST, mac=BENCH_MAC),
            "BenchPlug": DeviceEntry(name="BenchPlug", type="zigbee2mqtt", friendly_name="BenchPlug"),
        },
        aliases={"Arena": "Strip:3"},
    )


@pytest.fixture
def fakes():
    return {
        "Strip": FakeBackend("Strip", {1: "Plug 1", 2: "Plug 2", 3: "Arena Plug", 4: "Plug 4", 5: "Plug 5", 6: "Plug 6"}),
        "BenchPlug": FakeBackend("BenchPlug", {1: "BenchPlug"}, type="zigbee2mqtt"),
    }


@pytest.fixture
def patch_make(monkeypatch, fakes):
    monkeypatch.setattr(core_mod.Power, "_make", lambda self, entry: fakes[entry.name])
    return fakes


@pytest.fixture
def power(lab_config, patch_make):
    p = core_mod.Power(lab_config)
    yield p
    p.close()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(core_mod.time, "sleep", lambda s: None)


# --- MQTT fake for Zigbee2MQTT -----------------------------------------------


class FakeMqttClient:
    """Minimal paho-mqtt client double. `respond(topic, payload)` lets a test
    script how the fake Zigbee2MQTT answers each publish."""

    def __init__(self, *, bridge_state="online", devices=None, connect_error=None, respond=None):
        self.on_connect = None
        self.on_message = None
        self.published: list[tuple[str, dict]] = []
        self.subscribed: list = []
        self.bridge_state = bridge_state
        self.devices = devices if devices is not None else []
        self.connect_error = connect_error
        self.respond = respond
        self.stopped = False

    def connect(self, host, port, keepalive=60):
        if self.connect_error:
            raise self.connect_error

    def loop_start(self):
        self.on_connect(self, None, {}, 0, None)
        if self.bridge_state is not None:
            self.deliver("zigbee2mqtt/bridge/state", {"state": self.bridge_state})
        self.deliver("zigbee2mqtt/bridge/devices", self.devices)

    def subscribe(self, topics):
        self.subscribed.append(topics)

    def publish(self, topic, payload):
        body = json.loads(payload)
        self.published.append((topic, body))
        if self.respond:
            for t, p in self.respond(topic, body) or []:
                self.deliver(t, p)

    def deliver(self, topic, payload):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=json.dumps(payload).encode()))

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        pass


PLUG_DEVICE = {
    "friendly_name": "BenchPlug",
    "ieee_address": "0x4ce175b48e7f0000",
    "type": "Router",
    "software_build_id": "1.00.63",
    "model_id": "3RSP02064Z",
    "manufacturer": "Third Reality, Inc",
    "supported": True,
    "definition": {"model": "3RSP02064Z", "vendor": "Third Reality", "description": "Smart Plug Gen3"},
}

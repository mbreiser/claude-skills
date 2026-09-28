from __future__ import annotations

import asyncio
import json
import socket

import pytest
from kasa.exceptions import AuthenticationError, KasaException

from labpower import config as config_mod
from labpower import tapo as tapo_mod
from labpower.config import Config, DeviceEntry, PowerError

from conftest import BENCH_HOST, BENCH_MAC, REAL_RESOLVE_MDNS, FakeHandler

# --- TapoStrip backend ---------------------------------------------------


def test_channels_report_positions_nicknames_and_state(strip, plugs):
    plugs[2].device_on = True

    chans = strip.channels()

    assert [(c.channel, c.native_name, c.on) for c in chans][:3] == [(1, "Plug 1", False), (2, "Plug 2", True), (3, "Arena Plug", False)]
    assert all(c.device == "bench" for c in chans)


def test_sample_reads_requested_outlets(strip, plugs):
    plugs[3].power = 12.0

    (row,) = strip.sample([3])

    assert (row.channel, row.power_w, row.voltage_v) == (3, 12.0, 120.0)
    assert row.current_a == pytest.approx(0.1)


def test_sample_unknown_outlet_raises(strip):
    with pytest.raises(PowerError, match="no outlet 9"):
        strip.sample([9])


def test_status_includes_info_and_energy(strip, plugs):
    for pos, p in plugs.items():
        p.power, p.today_energy, p.month_energy = pos * 1.0, pos * 10, pos * 100

    st = strip.status()

    assert st.online and st.type == "tapo-strip" and st.info["model"] == "P316M"
    assert st.total_w == sum(range(1, 7))
    assert [(c.today_wh, c.month_wh) for c in st.channels][5] == (60, 600)


def test_set_switches_and_confirms(strip, plugs):
    strip.set(1, True)
    assert plugs[1].device_on is True
    strip.set(1, False)
    assert plugs[1].device_on is False


def test_set_raises_when_relay_ignores_command(strip, plugs):
    plugs[2].ignore_commands = True

    with pytest.raises(PowerError, match="did not switch on"):
        strip.set(2, True)


def test_reconnects_after_transient_error(strip, plugs, open_calls):
    strip.sample([1])
    before = open_calls["n"]
    plugs[1].power = 33.0
    plugs[1].raise_once_on_power = ConnectionError("dropped")

    (row,) = strip.sample([1])

    assert row.power_w == 33.0
    assert open_calls["n"] == before + 1


def test_close_disconnects_session(strip, handler):
    strip.channels()
    strip.close()
    assert handler.closed is True


# --- re-finding a moved strip ------------------------------------------------


def _moving_open(handler):
    async def fake_open(host, creds):
        if host == BENCH_HOST:
            raise OSError("unreachable")
        return handler

    return fake_open


def test_rediscover_prefers_mdns_and_saves_config(monkeypatch, tmp_path, plugs):
    handler = FakeHandler(plugs, ip="10.0.0.33")
    monkeypatch.setattr(tapo_mod, "_open", _moving_open(handler))

    async def fake_mdns(mac, timeout_s=None):
        assert mac == BENCH_MAC
        return "10.0.0.33"

    async def broadcast_must_not_run(target, timeout_s):
        raise AssertionError("broadcast used although mDNS answered")

    monkeypatch.setattr(tapo_mod, "_resolve_mdns", fake_mdns)
    monkeypatch.setattr(tapo_mod, "_discover", broadcast_must_not_run)
    entry = DeviceEntry(name="bench", type="tapo-strip", host=BENCH_HOST, mac=BENCH_MAC)
    cfg = Config(devices={"bench": entry})
    s = tapo_mod.TapoStrip(entry, config=cfg)
    try:
        s.channels()
        assert s.host == "10.0.0.33"
    finally:
        s.close()
    saved = json.loads(config_mod.CONFIG_PATH.read_text())
    assert saved["devices"]["bench"]["host"] == "10.0.0.33"


def test_rediscover_falls_back_to_broadcast(monkeypatch, plugs):
    monkeypatch.setattr(tapo_mod, "_open", _moving_open(FakeHandler(plugs)))

    async def fake_discover(target, timeout_s):
        return [{"ip": "10.0.0.9", "mac": "58:d8:12:14:1b:6f"}]

    monkeypatch.setattr(tapo_mod, "_discover", fake_discover)
    s = tapo_mod.TapoStrip(DeviceEntry(name="bench", type="tapo-strip", host=BENCH_HOST, mac=BENCH_MAC), config=None)
    try:
        s.channels()
        assert s.host == "10.0.0.9"
    finally:
        s.close()
    assert not config_mod.CONFIG_PATH.exists()


def test_unreachable_without_mac_is_a_clean_error(monkeypatch, plugs):
    monkeypatch.setattr(tapo_mod, "_open", _moving_open(FakeHandler(plugs)))
    s = tapo_mod.TapoStrip(DeviceEntry(name="bench", type="tapo-strip", host=BENCH_HOST), config=None)
    try:
        with pytest.raises(PowerError, match="Cannot reach strip 'bench'"):
            s.channels()
    finally:
        s.close()


def test_connect_failure_after_rediscovery_is_a_clean_error(monkeypatch):
    async def fake_open(host, creds):
        if host == BENCH_HOST:
            raise OSError("unreachable")
        raise RuntimeError("new host misbehaves")

    async def fake_mdns(mac, timeout_s=None):
        return "10.0.0.33"

    monkeypatch.setattr(tapo_mod, "_open", fake_open)
    monkeypatch.setattr(tapo_mod, "_resolve_mdns", fake_mdns)
    s = tapo_mod.TapoStrip(DeviceEntry(name="bench", type="tapo-strip", host=BENCH_HOST, mac=BENCH_MAC), config=None)
    try:
        with pytest.raises(PowerError, match="Found strip 'bench' at 10.0.0.33"):
            s.channels()
    finally:
        s.close()


def test_resolve_mdns_queries_uppercase_mac_hostname(monkeypatch):
    seen = {}

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        seen["host"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.4.33", 80))]

    monkeypatch.setattr(tapo_mod.socket, "getaddrinfo", fake_getaddrinfo)

    assert asyncio.run(REAL_RESOLVE_MDNS("58-d8-12-14-1b-6f")) == "192.168.4.33"
    assert seen["host"] == "58D812141B6F.local"


def test_resolve_mdns_returns_none_when_unresolvable(monkeypatch):
    def fail(*a, **kw):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(tapo_mod.socket, "getaddrinfo", fail)

    assert asyncio.run(REAL_RESOLVE_MDNS(BENCH_MAC)) is None


# --- python-kasa adapter: _open / _connect_device / _discover --------------------


class FakeProtocol:
    def __init__(self, responses):
        self.responses = responses

    async def query(self, method):
        return {method: self.responses[method]}


class FakeKasaDevice:
    def __init__(self, model="P316M", responses=None, children=None, mac="58:D8:12:14:1B:6F"):
        self.model = model
        self.mac = mac
        self.protocol = FakeProtocol(responses or {})
        self._children = children or {}
        self.disconnected = False
        self.updated = False
        self.update_error: Exception | None = None

    async def update(self):
        if self.update_error is not None:
            raise self.update_error
        self.updated = True

    def get_child_device(self, device_id):
        return self._children[device_id]

    async def disconnect(self):
        self.disconnected = True


def _patch_kasa(monkeypatch, *, discover, connect=None):
    calls = {"discover": [], "connect": []}

    async def fake_discover_single(host, *, credentials=None, discovery_timeout=None, timeout=None):
        calls["discover"].append(credentials.username)
        r = discover(credentials) if callable(discover) else discover
        if isinstance(r, Exception):
            raise r
        return r

    async def fake_connect(*, config):
        enc = config.connection_type.encryption_type.name
        calls["connect"].append(enc)
        r = (connect or {}).get(enc, KasaException(f"no fake for {enc}"))
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(tapo_mod.Discover, "discover_single", fake_discover_single)
    monkeypatch.setattr(tapo_mod.Device, "connect", fake_connect)
    return calls


FACTORY = [("test@tp-link.net", "test")]


def test_open_detects_protocol_via_discovery(monkeypatch):
    dev = FakeKasaDevice()
    calls = _patch_kasa(monkeypatch, discover=dev)

    conn = asyncio.run(tapo_mod._open(BENCH_HOST, [("me@x.com", "pw")]))

    assert isinstance(conn, tapo_mod._Connection) and dev.updated
    assert calls == {"discover": ["me@x.com"], "connect": []}


def test_open_without_udp_tries_tpap_then_klap(monkeypatch):
    calls = _patch_kasa(monkeypatch, discover=TimeoutError(),
                        connect={"Tpap": KasaException("pake_register failed"), "Klap": FakeKasaDevice()})

    asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))

    assert calls["connect"] == ["Tpap", "Klap"]


def test_open_stops_trying_protocols_when_host_unreachable(monkeypatch):
    calls = _patch_kasa(monkeypatch, discover=TimeoutError(),
                        connect={"Tpap": KasaException("Unable to query the device", TimeoutError())})

    with pytest.raises(KasaException):
        asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))
    assert calls["connect"] == ["Tpap"]


def test_open_falls_back_to_next_credentials_after_refusal(monkeypatch):
    dev = FakeKasaDevice()
    calls = _patch_kasa(monkeypatch, discover=lambda c: AuthenticationError("bad") if c.username == "me@x.com" else dev)

    asyncio.run(tapo_mod._open(BENCH_HOST, [("me@x.com", "wrong"), *FACTORY]))

    assert calls["discover"] == ["me@x.com", "test@tp-link.net"]


def test_open_all_refused_mentions_login(monkeypatch):
    _patch_kasa(monkeypatch, discover=AuthenticationError("bad hash"))

    with pytest.raises(PowerError, match="power login"):
        asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))


def test_open_klap_403_counts_as_refusal(monkeypatch):
    _patch_kasa(monkeypatch, discover=TimeoutError(), connect={
        "Tpap": KasaException("pake_register failed"),
        "Klap": KasaException("Device 10.0.0.5 responded with 403 to handshake1"),
    })

    with pytest.raises(PowerError, match="refused the login"):
        asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))


def test_open_update_failure_disconnects(monkeypatch):
    dev = FakeKasaDevice()
    dev.update_error = KasaException("boom")
    _patch_kasa(monkeypatch, discover=dev)

    with pytest.raises(KasaException, match="boom"):
        asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))
    assert dev.disconnected


def test_open_rejects_unsupported_model(monkeypatch):
    dev = FakeKasaDevice(model="P110M")
    _patch_kasa(monkeypatch, discover=dev)

    with pytest.raises(PowerError, match="P110M"):
        asyncio.run(tapo_mod._open(BENCH_HOST, FACTORY))
    assert dev.disconnected


def test_discover_maps_kasa_results(monkeypatch):
    dev = FakeKasaDevice()
    dev._discovery_info = {"device_model": "P316M(US)", "device_id": "abc", "owner": "", "obd_src": "matter"}

    async def fake_discover(*, target, discovery_timeout):
        return {"192.168.1.20": dev}

    monkeypatch.setattr(tapo_mod.Discover, "discover", fake_discover)

    found = asyncio.run(tapo_mod._discover("255.255.255.255", 1))

    assert found == [{"ip": "192.168.1.20", "model": "P316M(US)", "mac": "58:D8:12:14:1B:6F",
                      "device_id": "abc", "owner_bound": False, "onboarded_via": "matter"}]
    assert dev.disconnected


def _kasa_strip():
    child = FakeKasaDevice(responses={
        "get_emeter_data": {"power_mw": 5569, "voltage_mv": 121405, "current_ma": 96, "energy_wh": 16},
        "get_energy_usage": {"today_energy": 16, "month_energy": 40},
        "get_device_info": {"device_on": True},
    })
    parent = FakeKasaDevice(
        responses={
            "get_child_device_list": {"child_device_list": [
                {"position": 6, "device_id": "dev6", "nickname": "VGFwbyBTbWFydF9QbHVnXzY=", "device_on": True, "on_time": 42},
            ]},
            "get_device_info": {"model": "P316M", "ip": BENCH_HOST, "mac": BENCH_MAC, "fw_ver": "1.4.1 Build 1", "rssi": -40},
        },
        children={"dev6": child},
    )
    return tapo_mod._Connection(parent), parent


def test_connection_decodes_nicknames_and_converts_units():
    conn, parent = _kasa_strip()

    async def go():
        kids = await conn.children()
        plug = await conn.plug(6)
        return kids, await plug.reading(), await plug.energy(), await conn.info()

    kids, reading, energy, info = asyncio.run(go())

    assert kids == [{"position": 6, "nickname": "Tapo Smart_Plug_6", "on": True, "on_time_s": 42}]
    assert reading == {"power_w": 5.569, "voltage_v": 121.405, "current_a": 0.096}
    assert energy == {"today_wh": 16, "month_wh": 40}
    assert info["host"] == BENCH_HOST


def test_connection_unknown_outlet_raises():
    conn, _ = _kasa_strip()

    with pytest.raises(PowerError, match="No outlet 2"):
        asyncio.run(conn.plug(2))

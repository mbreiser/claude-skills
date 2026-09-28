from __future__ import annotations

import asyncio
import io
import json
import socket
import time

import pytest

from tapo_power import strip as strip_mod
from tapo_power.config import Config, StripConfig, TapoPowerError
from tapo_power.strip import OutletStatus, Strip, WaitTimeout

from kasa.exceptions import AuthenticationError, KasaException

from conftest import BENCH_HOST, REAL_RESOLVE_MDNS, FakeHandler

# --- outlet resolution ----------------------------------------------------


def test_resolve_int_position(strip):
    assert strip.resolve(3) == 3


def test_resolve_digit_string(strip):
    assert strip.resolve("3") == 3


def test_resolve_alias_case_insensitive(strip):
    assert strip.resolve("arena") == 3
    assert strip.resolve("ARENA") == 3


def test_resolve_nickname_case_insensitive(strip):
    assert strip.resolve("plug 1") == 1
    assert strip.resolve("PLUG 1") == 1


def test_resolve_unknown_name_lists_known_names(strip):
    with pytest.raises(TapoPowerError) as exc_info:
        strip.resolve("nope")

    msg = str(exc_info.value)
    assert "arena" in msg
    assert "Plug 1" in msg


def test_resolve_out_of_range_position_raises(strip):
    with pytest.raises(TapoPowerError):
        strip.resolve(99)


def test_resolve_ambiguous_nickname_raises(strip, plugs):
    plugs[5].nickname = "Plug 4"  # now shared with position 4

    with pytest.raises(TapoPowerError, match="ambiguous"):
        strip.resolve("Plug 4")


# --- on/off -----------------------------------------------------------------


def test_on_off_change_state_and_return_position(strip, plugs):
    assert strip.on(1) == 1
    assert plugs[1].device_on is True

    assert strip.off(1) == 1
    assert plugs[1].device_on is False


def test_on_ignored_by_device_raises(strip, plugs):
    plugs[2].ignore_commands = True

    with pytest.raises(TapoPowerError):
        strip.on(2)


def test_off_ignored_by_device_raises(strip, plugs):
    plugs[2].device_on = True
    plugs[2].ignore_commands = True

    with pytest.raises(TapoPowerError):
        strip.off(2)


# --- cycle / wait_for_power ---------------------------------------------------


def test_cycle_calls_off_then_on_in_order(strip, plugs):
    plugs[1].device_on = True
    order: list[str] = []
    orig_off, orig_on = plugs[1].off, plugs[1].on

    async def tracked_off():
        order.append("off")
        await orig_off()

    async def tracked_on():
        order.append("on")
        await orig_on()

    plugs[1].off = tracked_off
    plugs[1].on = tracked_on

    strip.cycle(1, off_s=0)

    assert order == ["off", "on"]
    assert plugs[1].device_on is True


def test_cycle_wait_above_w_returns_first_reading_above_threshold(strip, plugs, fast_sleep):
    plugs[1].power_series = [10, 20, 150]

    w = strip.cycle(1, off_s=0, wait_above_w=100, timeout_s=1.0)

    assert w == 150


def test_cycle_wait_above_w_timeout_raises_wait_timeout(strip, plugs, fast_sleep):
    plugs[1].power = 5  # never rises

    with pytest.raises(WaitTimeout):
        strip.cycle(1, off_s=0, wait_above_w=100, timeout_s=0.05)


def test_wait_timeout_is_a_tapo_power_error():
    assert issubclass(WaitTimeout, TapoPowerError)


def test_wait_for_power_below_w(strip, plugs):
    plugs[1].power = 5

    w = strip.wait_for_power(1, below_w=10, timeout_s=1.0)

    assert w == 5


def test_wait_for_power_requires_a_bound(strip):
    with pytest.raises(ValueError):
        strip.wait_for_power(1)


# --- sample / status ----------------------------------------------------------


def test_sample_returns_only_requested_outlets_with_alias_name(strip, plugs):
    plugs[3].power = 42
    plugs[1].power = 7

    rows = strip.sample([3, 1])

    by_pos = {r.position: r for r in rows}
    assert set(by_pos) == {3, 1}
    assert by_pos[3].name == "arena"
    assert by_pos[1].name == "Plug 1"
    assert by_pos[3].power_w == 42.0
    assert isinstance(by_pos[3].power_w, float)
    assert isinstance(by_pos[1].power_w, float)


def test_status_total_w_and_energy(strip, plugs):
    for pos, plug in plugs.items():
        plug.power = pos * 10
        plug.today_energy = pos * 100
        plug.month_energy = pos * 1000

    st = strip.status()

    assert st.total_w == sum(p.power for p in plugs.values())
    by_pos = {o.position: o for o in st.outlets}
    for pos, plug in plugs.items():
        assert by_pos[pos].today_wh == plug.today_energy
        assert by_pos[pos].month_wh == plug.month_energy


# --- reconnect on transient error ---------------------------------------------


def test_reconnects_and_retries_after_generic_exception(strip, plugs, open_calls):
    strip.status()  # establish the first connection
    calls_before = open_calls["n"]

    plugs[1].power = 33
    plugs[1].raise_once_on_power = ConnectionError("dropped")

    w = strip.power(1)

    assert w == 33.0
    assert open_calls["n"] == calls_before + 1


# --- rediscovery on unreachable host -------------------------------------------


def test_rediscover_switches_host_and_saves_config(monkeypatch, tmp_path, plugs):
    import tapo_power.config as config_mod

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)

    handler = FakeHandler(plugs, ip="10.0.0.9", mac="58:d8:12:14:1b:6f")

    async def fake_open(host, creds):
        if host == BENCH_HOST:
            raise OSError("unreachable")
        return handler

    async def fake_discover(target, timeout_s):
        return [
            {
                "ip": "10.0.0.9",
                "model": "P316M",
                "mac": "58:d8:12:14:1b:6f",
                "device_id": "dev1",
                "owner_bound": True,
                "onboarded_via": "app",
            }
        ]

    monkeypatch.setattr(strip_mod, "_open", fake_open)
    monkeypatch.setattr(strip_mod, "_discover", fake_discover)

    cfg = Config(
        default="bench",
        strips={"bench": StripConfig(name="bench", host=BENCH_HOST, mac="58-D8-12-14-1B-6F", outlets={"arena": 3})},
    )
    with Strip(config=cfg) as s:
        s.status()
        assert s.host == "10.0.0.9"

    saved = json.loads(config_path.read_text())
    assert saved["strips"]["bench"]["host"] == "10.0.0.9"


def test_rediscover_prefers_mdns_over_broadcast(monkeypatch, tmp_path, plugs):
    import tapo_power.config as config_mod

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)
    handler = FakeHandler(plugs, ip="10.0.0.33")

    async def fake_open(host, creds):
        if host == BENCH_HOST:
            raise OSError("unreachable")
        return handler

    async def fake_mdns(mac, timeout_s=None):
        assert mac == "58-D8-12-14-1B-6F"
        return "10.0.0.33"

    async def broadcast_must_not_run(target, timeout_s):
        raise AssertionError("broadcast discovery used although mDNS answered")

    monkeypatch.setattr(strip_mod, "_open", fake_open)
    monkeypatch.setattr(strip_mod, "_resolve_mdns", fake_mdns)
    monkeypatch.setattr(strip_mod, "_discover", broadcast_must_not_run)

    cfg = Config(default="bench", strips={"bench": StripConfig(name="bench", host=BENCH_HOST, mac="58-D8-12-14-1B-6F")})
    with Strip(config=cfg) as s:
        s.outlets()
        assert s.host == "10.0.0.33"
    assert json.loads(config_path.read_text())["strips"]["bench"]["host"] == "10.0.0.33"


def test_resolve_mdns_queries_uppercase_mac_hostname(monkeypatch):
    seen = {}

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        seen["host"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.4.33", 80))]

    monkeypatch.setattr(strip_mod.socket, "getaddrinfo", fake_getaddrinfo)

    assert asyncio.run(REAL_RESOLVE_MDNS("58-d8-12-14-1b-6f")) == "192.168.4.33"
    assert seen["host"] == "58D812141B6F.local"


def test_resolve_mdns_returns_none_when_unresolvable(monkeypatch):
    def fail(*a, **kw):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(strip_mod.socket, "getaddrinfo", fail)

    assert asyncio.run(REAL_RESOLVE_MDNS("58-D8-12-14-1B-6F")) is None


def test_adhoc_host_strip_does_not_save_config_on_rediscover(monkeypatch, tmp_path, plugs):
    import tapo_power.config as config_mod

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)

    async def fake_open(host, creds):
        raise OSError("unreachable")

    async def fake_discover(target, timeout_s):
        return [
            {
                "ip": "10.0.0.9",
                "model": "P316M",
                "mac": "58:d8:12:14:1b:6f",
                "device_id": "dev1",
                "owner_bound": True,
                "onboarded_via": "app",
            }
        ]

    monkeypatch.setattr(strip_mod, "_open", fake_open)
    monkeypatch.setattr(strip_mod, "_discover", fake_discover)

    with Strip(host=BENCH_HOST, config=Config()) as s:
        with pytest.raises(TapoPowerError):
            s.resolve(1)

    assert not config_path.exists()


# --- python-kasa adapter (_open, _discover, _Connection, _Plug) ------------------


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
    """Fake python-kasa entry points. `discover` is the result of
    Discover.discover_single (a device, an exception, or a callable taking the
    credentials); `connect` maps encryption name -> device or exception."""
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

    monkeypatch.setattr(strip_mod.Discover, "discover_single", fake_discover_single)
    monkeypatch.setattr(strip_mod.Device, "connect", fake_connect)
    return calls


def test_open_detects_protocol_via_discovery(monkeypatch):
    dev = FakeKasaDevice()
    calls = _patch_kasa(monkeypatch, discover=dev)

    conn = asyncio.run(strip_mod._open(BENCH_HOST, [("me@x.com", "pw")]))

    assert isinstance(conn, strip_mod._Connection)
    assert dev.updated is True
    assert calls == {"discover": ["me@x.com"], "connect": []}


def test_open_without_udp_tries_tpap_then_klap(monkeypatch):
    dev = FakeKasaDevice()
    calls = _patch_kasa(monkeypatch, discover=TimeoutError(),
                        connect={"Tpap": KasaException("pake_register failed"), "Klap": dev})

    conn = asyncio.run(strip_mod._open(BENCH_HOST, [("test@tp-link.net", "test")]))

    assert isinstance(conn, strip_mod._Connection)
    assert calls["connect"] == ["Tpap", "Klap"]


def test_open_stops_trying_protocols_when_host_unreachable(monkeypatch):
    calls = _patch_kasa(monkeypatch, discover=TimeoutError(),
                        connect={"Tpap": KasaException("Unable to query the device", TimeoutError())})

    with pytest.raises(KasaException):
        asyncio.run(strip_mod._open(BENCH_HOST, [("test@tp-link.net", "test")]))
    assert calls["connect"] == ["Tpap"]


def test_open_falls_back_to_next_credentials_after_refusal(monkeypatch):
    dev = FakeKasaDevice()
    calls = _patch_kasa(
        monkeypatch,
        discover=lambda creds: AuthenticationError("bad") if creds.username == "me@x.com" else dev,
    )

    asyncio.run(strip_mod._open(BENCH_HOST, [("me@x.com", "wrong"), ("test@tp-link.net", "test")]))

    assert calls["discover"] == ["me@x.com", "test@tp-link.net"]


def test_open_all_refused_mentions_login(monkeypatch):
    _patch_kasa(monkeypatch, discover=AuthenticationError("bad hash"))

    with pytest.raises(TapoPowerError, match="tapo-power login"):
        asyncio.run(strip_mod._open(BENCH_HOST, [("me@x.com", "wrong"), ("test@tp-link.net", "test")]))


def test_open_klap_403_counts_as_refusal(monkeypatch):
    _patch_kasa(monkeypatch, discover=TimeoutError(), connect={
        "Tpap": KasaException("pake_register failed"),
        "Klap": KasaException("Device 10.0.0.5 responded with 403 to handshake1"),
    })

    with pytest.raises(TapoPowerError, match="refused the login"):
        asyncio.run(strip_mod._open(BENCH_HOST, [("test@tp-link.net", "test")]))


def test_open_update_failure_disconnects(monkeypatch):
    dev = FakeKasaDevice()
    dev.update_error = KasaException("boom")
    _patch_kasa(monkeypatch, discover=dev)

    with pytest.raises(KasaException, match="boom"):
        asyncio.run(strip_mod._open(BENCH_HOST, [("test@tp-link.net", "test")]))
    assert dev.disconnected is True


def test_connect_failure_after_rediscovery_is_a_clean_error(monkeypatch, tmp_path, plugs):
    import tapo_power.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")

    async def fake_open(host, creds):
        if host == BENCH_HOST:
            raise OSError("unreachable")
        raise RuntimeError("new host misbehaves")

    async def fake_mdns(mac, timeout_s=None):
        return "10.0.0.33"

    monkeypatch.setattr(strip_mod, "_open", fake_open)
    monkeypatch.setattr(strip_mod, "_resolve_mdns", fake_mdns)

    cfg = Config(default="bench", strips={"bench": StripConfig(name="bench", host=BENCH_HOST, mac="58-D8-12-14-1B-6F")})
    with Strip(config=cfg) as s:
        with pytest.raises(TapoPowerError, match="Found strip 'bench' at 10.0.0.33"):
            s.outlets()


def test_open_rejects_unsupported_model_and_disconnects(monkeypatch):
    dev = FakeKasaDevice(model="P110M")
    _patch_kasa(monkeypatch, discover=dev)

    with pytest.raises(TapoPowerError, match="P110M"):
        asyncio.run(strip_mod._open(BENCH_HOST, [("test@tp-link.net", "test")]))
    assert dev.disconnected is True


def test_discover_maps_kasa_results_and_disconnects(monkeypatch):
    dev = FakeKasaDevice(model="P316M")
    dev._discovery_info = {"device_model": "P316M(US)", "device_id": "abc", "owner": "", "obd_src": "matter"}

    async def fake_discover(*, target, discovery_timeout):
        assert target == "255.255.255.255"
        return {"192.168.1.20": dev}

    monkeypatch.setattr(strip_mod.Discover, "discover", fake_discover)

    found = asyncio.run(strip_mod._discover("255.255.255.255", 1))

    assert found == [{
        "ip": "192.168.1.20", "model": "P316M(US)", "mac": "58:D8:12:14:1B:6F",
        "device_id": "abc", "owner_bound": False, "onboarded_via": "matter",
    }]
    assert dev.disconnected is True


def _kasa_strip():
    child = FakeKasaDevice(responses={
        "get_emeter_data": {"power_mw": 5569, "voltage_mv": 121405, "current_ma": 96, "energy_wh": 16},
        "get_energy_usage": {"today_energy": 16, "month_energy": 40},
        "get_device_info": {"device_on": True},
    })
    parent = FakeKasaDevice(
        responses={
            "get_child_device_list": {"child_device_list": [
                {"position": 6, "device_id": "dev6", "nickname": "VGFwbyBTbWFydF9QbHVnXzY=",
                 "device_on": True, "on_time": 42},
            ], "start_index": 0, "sum": 1},
            "get_device_info": {"model": "P316M", "ip": BENCH_HOST, "mac": "58-D8-12-14-1B-6F",
                                "fw_ver": "1.0.5 Build 250306", "rssi": -40},
        },
        children={"dev6": child},
    )
    return strip_mod._Connection(parent), parent


def test_connection_children_decodes_nicknames():
    conn, _ = _kasa_strip()

    kids = asyncio.run(conn.children())

    assert kids == [{"position": 6, "nickname": "Tapo Smart_Plug_6", "on": True, "on_time_s": 42}]


def test_plug_reading_converts_units():
    conn, _ = _kasa_strip()

    async def go():
        plug = await conn.plug(6)
        return await plug.reading(), await plug.energy(), await plug.is_on()

    reading, energy, on = asyncio.run(go())

    assert reading == {"power_w": 5.569, "voltage_v": 121.405, "current_a": 0.096}
    assert energy == {"today_wh": 16, "month_wh": 40}
    assert on is True


def test_connection_info_and_close():
    conn, parent = _kasa_strip()

    info = asyncio.run(conn.info())
    asyncio.run(conn.close())

    assert info == {"model": "P316M", "host": BENCH_HOST, "mac": "58-D8-12-14-1B-6F",
                    "fw_ver": "1.0.5 Build 250306", "rssi": -40}
    assert parent.disconnected is True


def test_sample_includes_voltage_and_current(strip, plugs):
    plugs[3].power = 12.0
    plugs[3].voltage = 120.0

    (row,) = strip.sample(["arena"])

    assert row.voltage_v == 120.0
    assert row.current_a == pytest.approx(0.1)


def test_close_disconnects_the_session(bench_config, patch_open, patch_discover_empty, handler):
    s = Strip(config=bench_config)
    s.outlets()
    s.close()

    assert handler.closed is True


# --- log_csv -------------------------------------------------------------------


def test_log_csv_path_writes_header_once(strip, plugs, tmp_path):
    path = tmp_path / "log.csv"

    n = strip.log_csv(path, interval_s=0.01, duration_s=0.08, outlets=[1])

    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(strip_mod.CSV_FIELDS)
    assert n >= 1


def test_log_csv_append_does_not_repeat_header(strip, plugs, tmp_path):
    path = tmp_path / "log.csv"

    strip.log_csv(path, interval_s=0.01, duration_s=0.08, outlets=[1])
    strip.log_csv(path, interval_s=0.01, duration_s=0.08, outlets=[1])

    header = ",".join(strip_mod.CSV_FIELDS)
    assert path.read_text().count(header) == 1


def test_log_csv_one_row_per_outlet_per_sample(strip, plugs, tmp_path):
    path = tmp_path / "log.csv"

    strip.log_csv(path, interval_s=0.01, duration_s=0.08, outlets=[1, 2])

    rows = path.read_text().splitlines()[1:]
    assert rows
    assert len(rows) % 2 == 0
    for row in rows:
        assert len(row.split(",")) == len(strip_mod.CSV_FIELDS)


def test_log_csv_stream_includes_header(strip, plugs):
    buf = io.StringIO()

    strip.log_csv(buf, interval_s=0.01, duration_s=0.08, outlets=[1])

    assert buf.getvalue().startswith(",".join(strip_mod.CSV_FIELDS))


def test_log_csv_duration_s_stops_the_loop(strip, plugs, tmp_path):
    path = tmp_path / "log.csv"

    started = time.monotonic()
    strip.log_csv(path, interval_s=0.01, duration_s=0.05, outlets=[1])
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_log_csv_skips_failed_sample_and_continues(strip, plugs, monkeypatch, tmp_path):
    orig_sample = strip.sample
    calls = {"n": 0}

    def flaky_sample(outlets=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TapoPowerError("sample boom")
        return orig_sample(outlets)

    monkeypatch.setattr(strip, "sample", flaky_sample)
    path = tmp_path / "log.csv"

    n = strip.log_csv(path, interval_s=0.01, duration_s=0.05, outlets=[1])

    assert calls["n"] >= 2
    assert n >= 1


def test_log_csv_calls_on_sample_with_rows(strip, plugs, tmp_path):
    received: list[list[OutletStatus]] = []
    path = tmp_path / "log.csv"

    strip.log_csv(path, interval_s=0.01, duration_s=0.08, outlets=[1], on_sample=received.append)

    assert received
    assert all(isinstance(row, OutletStatus) for rows in received for row in rows)


# --- background_log -------------------------------------------------------------


def test_background_log_writes_rows_and_stops_thread_after_block(strip, plugs, tmp_path):
    path = tmp_path / "bg.csv"

    with strip.background_log(path, interval_s=0.01, outlets=[1]):
        time.sleep(0.05)

    size_after_exit = path.stat().st_size
    assert size_after_exit > 0

    time.sleep(0.05)
    assert path.stat().st_size == size_after_exit

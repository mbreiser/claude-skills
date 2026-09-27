from __future__ import annotations

import asyncio
import io
import json
import time

import pytest

from tapo_power import strip as strip_mod
from tapo_power.config import Config, StripConfig, TapoPowerError
from tapo_power.strip import OutletStatus, Strip, WaitTimeout

from conftest import BENCH_HOST, FakeHandler, make_fake_api_client

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


# --- _open credential fallback --------------------------------------------------


def _auth_error():
    return Exception('Tapo(Unauthorized { kind: "HASH_MISMATCH" })')


def test_open_falls_back_to_next_credentials_on_auth_error(monkeypatch):
    FakeApiClient = make_fake_api_client()
    FakeApiClient.behaviors[("bad@x.com", "wrongpw")] = _auth_error()
    monkeypatch.setattr(strip_mod, "ApiClient", FakeApiClient)

    creds = [("bad@x.com", "wrongpw"), ("test@tp-link.net", "test")]
    handler = asyncio.run(strip_mod._open(BENCH_HOST, creds))

    assert handler.user == "test@tp-link.net"
    assert FakeApiClient.calls == creds


def test_open_all_credentials_fail_mentions_login(monkeypatch):
    FakeApiClient = make_fake_api_client()
    creds = [("a@x.com", "1"), ("b@x.com", "2")]
    for c in creds:
        FakeApiClient.behaviors[c] = _auth_error()
    monkeypatch.setattr(strip_mod, "ApiClient", FakeApiClient)

    with pytest.raises(TapoPowerError, match="tapo-power login"):
        asyncio.run(strip_mod._open(BENCH_HOST, creds))


def test_open_nonauth_error_propagates_without_trying_later_pairs(monkeypatch):
    FakeApiClient = make_fake_api_client()
    creds = [("a@x.com", "1"), ("b@x.com", "2")]
    FakeApiClient.behaviors[("a@x.com", "1")] = TimeoutError("timed out")
    monkeypatch.setattr(strip_mod, "ApiClient", FakeApiClient)

    with pytest.raises(TimeoutError):
        asyncio.run(strip_mod._open(BENCH_HOST, creds))

    assert FakeApiClient.calls == [("a@x.com", "1")]


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

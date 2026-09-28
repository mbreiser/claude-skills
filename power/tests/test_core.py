from __future__ import annotations

import io
import time

import pytest

from labpower.config import Config, DeviceEntry, PowerError, WaitTimeout
from labpower.core import CSV_FIELDS, Power

# --- naming ------------------------------------------------------------------


def test_resolve_alias_case_insensitive(power):
    assert power.resolve("Arena") == ("Strip", 3)
    assert power.resolve("arena") == ("Strip", 3)


def test_resolve_device_channel(power):
    assert power.resolve("Strip:6") == ("Strip", 6)
    assert power.resolve("strip:6") == ("Strip", 6)


def test_resolve_single_outlet_device_by_name(power):
    assert power.resolve("BenchPlug") == ("BenchPlug", 1)


def test_resolve_multi_outlet_device_name_asks_for_channel(power):
    with pytest.raises(PowerError, match="several outlets"):
        power.resolve("Strip")


def test_resolve_bare_number_with_one_strip(power):
    assert power.resolve("4") == ("Strip", 4)
    assert power.resolve(4) == ("Strip", 4)


def test_resolve_bare_number_ambiguous_with_two_strips(power, lab_config):
    lab_config.devices["Strip2"] = DeviceEntry(name="Strip2", type="tapo-strip", host="10.0.0.6")

    with pytest.raises(PowerError, match="ambiguous"):
        power.resolve("4")


def test_resolve_tapo_nickname(power):
    assert power.resolve("arena plug") == ("Strip", 3)


def test_resolve_unknown_lists_aliases_and_devices(power):
    with pytest.raises(PowerError) as e:
        power.resolve("nope")
    assert "Arena" in str(e.value) and "BenchPlug" in str(e.value)


def test_resolve_unknown_device_in_target(power):
    with pytest.raises(PowerError, match="Unknown device 'Nope'"):
        power.resolve("Nope:1")


def test_display_name_prefers_alias(power):
    assert power.display_name("Strip", 3, "Arena Plug") == "Arena"
    assert power.display_name("Strip", 1, "Plug 1") == "Plug 1"


# --- switching -------------------------------------------------------------


def test_on_off_route_to_the_right_backend(power, fakes):
    assert power.on("Arena") == ("Strip", 3)
    assert power.off("BenchPlug") == ("BenchPlug", 1)
    assert fakes["Strip"].calls == [(3, True)]
    assert fakes["BenchPlug"].calls == [(1, False)]


def test_is_on(power, fakes):
    fakes["BenchPlug"].state[1] = True
    assert power.is_on("BenchPlug") is True


def test_cycle_turns_off_then_on(power, fakes, no_sleep):
    power.cycle("Arena", off_s=0)
    assert fakes["Strip"].calls == [(3, False), (3, True)]


def test_cycle_waits_for_power(power, fakes, no_sleep):
    fakes["BenchPlug"].power_series[1] = [0.0, 1.0, 7.5]

    assert power.cycle("BenchPlug", off_s=0, wait_above_w=5, timeout_s=5) == 7.5


def test_wait_for_power_timeout(power, fakes, no_sleep):
    with pytest.raises(WaitTimeout):
        power.wait_for_power("BenchPlug", above_w=5, timeout_s=0.01, poll_s=0)


def test_wait_for_power_needs_a_bound(power):
    with pytest.raises(ValueError):
        power.wait_for_power("BenchPlug")


def test_backend_errors_propagate(power, fakes):
    fakes["BenchPlug"].fail = "Timed out waiting for BenchPlug"
    with pytest.raises(PowerError, match="Timed out"):
        power.on("BenchPlug")


# --- reading ---------------------------------------------------------------


def test_sample_all_devices_with_names(power, fakes):
    fakes["Strip"].power[3] = 12.0

    rows = power.sample()

    assert len(rows) == 7
    arena = next(r for r in rows if r.device == "Strip" and r.channel == 3)
    assert arena.name == "Arena" and arena.power_w == 12.0
    assert next(r for r in rows if r.device == "BenchPlug").name == "BenchPlug"


def test_sample_selected_outlets_across_devices(power):
    rows = power.sample(["Arena", "BenchPlug", "Strip:3"])

    assert [(r.device, r.channel) for r in rows] == [("Strip", 3), ("BenchPlug", 1)]


def test_sample_raises_if_a_device_fails(power, fakes):
    fakes["Strip"].fail = "Cannot reach strip"
    with pytest.raises(PowerError, match="Strip: Cannot reach strip"):
        power.sample()


def test_read_and_power(power, fakes):
    fakes["Strip"].power[3] = 2.5
    assert power.read("Arena").voltage_v == 120.0
    assert power.power("Arena") == 2.5


def test_status_marks_unreachable_devices_offline(power, fakes):
    fakes["Strip"].fail = "Cannot reach strip 'Strip'"

    statuses = {s.name: s for s in power.status()}

    assert statuses["Strip"].online is False and "Cannot reach" in statuses["Strip"].error
    assert statuses["BenchPlug"].online is True


def test_status_names_channels(power):
    (st,) = power.status(["Strip"])
    assert st.channels[2].name == "Arena"


def test_close_closes_backends(power, fakes):
    power.sample()
    power.close()
    assert fakes["Strip"].closed and fakes["BenchPlug"].closed


# --- logging ---------------------------------------------------------------


def test_log_csv_header_once_and_rows(power, tmp_path):
    path = tmp_path / "log.csv"

    power.log_csv(path, interval_s=0.01, duration_s=0.05, outlets=["Arena", "BenchPlug"])
    power.log_csv(path, interval_s=0.01, duration_s=0.05, outlets=["Arena", "BenchPlug"])

    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(CSV_FIELDS)
    assert path.read_text().count(",".join(CSV_FIELDS)) == 1
    rows = [line.split(",") for line in lines[1:]]
    assert rows and len(rows) % 2 == 0
    assert {r[4] for r in rows} == {"Arena", "BenchPlug"}
    assert all(len(r) == len(CSV_FIELDS) for r in rows)


def test_log_csv_stream_includes_header(power):
    buf = io.StringIO()
    power.log_csv(buf, interval_s=0.01, duration_s=0.03, outlets=["BenchPlug"])
    assert buf.getvalue().startswith(",".join(CSV_FIELDS))


def test_log_csv_skips_a_failing_device_and_keeps_the_rest(power, fakes, tmp_path):
    fakes["Strip"].fail = "Cannot reach strip"
    path = tmp_path / "log.csv"

    n = power.log_csv(path, interval_s=0.01, duration_s=0.05)

    rows = path.read_text().splitlines()[1:]
    assert n >= 1 and rows
    assert all(r.split(",")[2] == "BenchPlug" for r in rows)


def test_log_csv_calls_on_sample(power):
    seen = []
    power.log_csv(io.StringIO(), interval_s=0.01, duration_s=0.03, outlets=["BenchPlug"], on_sample=seen.append)
    assert seen and seen[0][0].device == "BenchPlug"


def test_background_log_stops_after_block(power, tmp_path):
    path = tmp_path / "bg.csv"

    with power.background_log(path, interval_s=0.01, outlets=["BenchPlug"]):
        time.sleep(0.05)
    size = path.stat().st_size
    time.sleep(0.05)

    assert size > 0 and path.stat().st_size == size


def test_unknown_device_type_is_an_error():
    cfg = Config(devices={"X": DeviceEntry(name="X", type="snmp-pdu")})
    with Power(cfg) as p, pytest.raises(PowerError, match="unknown device type"):
        p.backend("X")


# --- fallback aliases --------------------------------------------------------


@pytest.fixture
def g6(power, lab_config):
    lab_config.aliases["G6Arena"] = ["BenchPlug", "Strip:1"]
    return power


def test_fallback_alias_uses_first_reachable(g6, fakes):
    assert g6.resolve("G6Arena") == ("BenchPlug", 1)
    fakes["BenchPlug"].fail = "Timed out waiting for BenchPlug"
    assert g6.resolve("G6Arena") == ("Strip", 1)


def test_fallback_alias_none_reachable(g6, fakes):
    fakes["BenchPlug"].fail = fakes["Strip"].fail = "down"
    with pytest.raises(PowerError, match="No target of 'G6Arena' is reachable"):
        g6.resolve("G6Arena")


def test_fallback_alias_names_every_target(g6):
    assert g6.display_name("BenchPlug", 1, "BenchPlug") == "G6Arena"
    assert g6.display_name("Strip", 1, "Plug 1") == "G6Arena"


def test_cycle_on_fallback_stays_on_one_target(g6, fakes, no_sleep):
    fakes["BenchPlug"].fail = "down"
    g6.cycle("G6Arena", off_s=0)
    assert fakes["Strip"].calls == [(1, False), (1, True)]


# --- best-effort logging -------------------------------------------------------


def test_best_effort_log_with_nothing_reachable_writes_header_only(g6, fakes, tmp_path):
    fakes["BenchPlug"].fail = fakes["Strip"].fail = "down"
    path = tmp_path / "p.csv"

    n = g6.log_csv(path, interval_s=0.01, duration_s=0.1, outlets=["G6Arena"], best_effort=True, retry_s=0.02)

    assert n == 0
    assert path.read_text().strip() == ",".join(CSV_FIELDS)


def test_strict_log_with_nothing_reachable_raises(g6, fakes, tmp_path):
    fakes["BenchPlug"].fail = fakes["Strip"].fail = "down"
    with pytest.raises(PowerError):
        g6.log_csv(tmp_path / "p.csv", interval_s=0.01, duration_s=0.1, outlets=["G6Arena"])


def test_best_effort_log_fails_over_and_back(g6, fakes, tmp_path):
    path = tmp_path / "p.csv"
    seen = []

    def script(rows):
        seen.append(rows[0].device)
        if len(seen) == 3:
            fakes["BenchPlug"].fail = "Timed out waiting for BenchPlug"
        if len(seen) == 8:
            fakes["BenchPlug"].fail = None

    g6.log_csv(path, interval_s=0.01, duration_s=0.5, outlets=["G6Arena"], best_effort=True, retry_s=0.05,
               on_sample=script)

    assert seen[:3] == ["BenchPlug"] * 3
    assert "Strip" in seen[3:8]
    assert seen[-1] == "BenchPlug"
    devices = {line.split(",")[2] for line in path.read_text().splitlines()[1:]}
    assert devices == {"BenchPlug", "Strip"}


def test_best_effort_log_ignores_unknown_outlets(power, tmp_path):
    n = power.log_csv(tmp_path / "p.csv", interval_s=0.01, duration_s=0.05, outlets=["Nope"],
                      best_effort=True, retry_s=0.02)
    assert n == 0


def test_best_effort_background_log_never_breaks_the_block(power, tmp_path):
    bad = tmp_path / "missing-dir" / "p.csv"
    with power.background_log(bad, interval_s=0.01, outlets=["BenchPlug"], best_effort=True):
        time.sleep(0.02)
    assert not bad.exists()


def test_best_effort_log_warms_up_fallback_devices(g6, fakes, monkeypatch):
    probed = []
    for name, fake in fakes.items():
        orig = fake.channels
        monkeypatch.setattr(fake, "channels", lambda orig=orig, name=name: probed.append(name) or orig())

    g6.log_csv(io.StringIO(), interval_s=0.01, duration_s=0.02, outlets=["G6Arena"], best_effort=True)

    assert {"BenchPlug", "Strip"} <= set(probed)

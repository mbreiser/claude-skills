from __future__ import annotations

import json

import pytest

from labpower import cli
from labpower import config as config_mod
from labpower import tapo as tapo_mod
from labpower import zigbee as zigbee_mod
from labpower.cli import EXIT_ERROR, EXIT_TIMEOUT

from conftest import PLUG_DEVICE, FakeMqttClient


@pytest.fixture
def saved(lab_config, patch_make, no_sleep):
    lab_config.save()
    return config_mod.CONFIG_PATH


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_status_json_covers_all_devices(saved, capsys, fakes):
    fakes["BenchPlug"].power[1] = 3.5

    rc, out, _ = run(capsys, "--json", "status")

    data = {d["name"]: d for d in json.loads(out)}
    assert rc == 0 and set(data) == {"Strip", "BenchPlug"}
    assert data["BenchPlug"]["total_w"] == 3.5
    assert len(data["Strip"]["channels"]) == 6


def test_status_table_marks_offline_device(saved, capsys, fakes):
    fakes["Strip"].fail = "Cannot reach strip 'Strip'"

    rc, out, _ = run(capsys, "status")

    assert rc == 0
    assert "Strip  tapo-strip  OFFLINE: Cannot reach" in out
    assert "BenchPlug" in out


def test_status_fails_when_nothing_reachable(saved, capsys, fakes):
    for f in fakes.values():
        f.fail = "down"

    rc, _, err = run(capsys, "status")

    assert rc == EXIT_ERROR and "no device reachable" in err


def test_on_json_reports_device_and_alias(saved, capsys, fakes):
    rc, out, _ = run(capsys, "--json", "on", "Arena")

    assert rc == 0
    assert json.loads(out) == {"device": "Strip", "channel": 3, "name": "Arena", "state": "on"}
    assert fakes["Strip"].calls == [(3, True)]


def test_off_plain_output(saved, capsys):
    rc, out, _ = run(capsys, "off", "BenchPlug")
    assert rc == 0 and out.strip() == "BenchPlug: off"


def test_cycle_timeout_exit_code(saved, capsys):
    rc, _, err = run(capsys, "cycle", "BenchPlug", "--off-s", "0", "--wait-above-w", "100", "--timeout-s", "0.01")
    assert rc == EXIT_TIMEOUT and "never > 100" in err


def test_read_prints_w_v_a(saved, capsys, fakes):
    fakes["Strip"].power[3] = 6.0
    rc, out, _ = run(capsys, "read", "Arena")
    assert rc == 0 and out.strip() == "Arena: off  6.000 W  120.0 V  0.050 A"


def test_unknown_outlet_is_exit_1(saved, capsys):
    rc, _, err = run(capsys, "on", "nope")
    assert rc == EXIT_ERROR and "Unknown outlet" in err


def test_alias_stores_device_channel(saved, capsys):
    rc, out, _ = run(capsys, "alias", "Lamp", "Strip:5")

    assert rc == 0 and out.strip() == "Lamp -> Strip:5"
    assert json.loads(saved.read_text())["aliases"]["Lamp"] == "Strip:5"


def test_alias_replaces_previous_alias_for_same_outlet(saved, capsys):
    run(capsys, "alias", "Camera", "Arena")
    aliases = json.loads(saved.read_text())["aliases"]
    assert aliases == {"Camera": "Strip:3"}


@pytest.mark.parametrize("bad", ["7", "Strip:9", "BenchPlug"])
def test_alias_rejects_numbers_colons_and_device_names(saved, capsys, bad):
    rc, _, err = run(capsys, "alias", bad, "Strip:5")
    assert rc == EXIT_ERROR and err


def test_unalias_unknown_is_error(saved, capsys):
    rc, _, _ = run(capsys, "unalias", "nope")
    assert rc == EXIT_ERROR


def test_remove_drops_device_and_its_aliases(saved, capsys):
    rc, out, _ = run(capsys, "remove", "Strip")

    data = json.loads(saved.read_text())
    assert rc == 0 and "Strip" not in data["devices"] and data["aliases"] == {}
    assert "Arena" in out


def test_add_zigbee_checks_the_bridge(saved, capsys, monkeypatch):
    monkeypatch.setattr(zigbee_mod, "_make_client", lambda: FakeMqttClient(devices=[PLUG_DEVICE]))
    run(capsys, "remove", "BenchPlug")

    rc, out, _ = run(capsys, "add-zigbee", "BenchPlug")

    assert rc == 0 and "Smart Plug Gen3" in out
    assert json.loads(saved.read_text())["devices"]["BenchPlug"] == {"type": "zigbee2mqtt", "friendly_name": "BenchPlug"}


def test_add_zigbee_unknown_friendly_name(saved, capsys, monkeypatch):
    monkeypatch.setattr(zigbee_mod, "_make_client", lambda: FakeMqttClient(devices=[PLUG_DEVICE]))
    rc, _, err = run(capsys, "add-zigbee", "Other")
    assert rc == EXIT_ERROR and "no device named 'Other'" in err


def test_discover_json_updates_moved_strip(saved, capsys, monkeypatch):
    monkeypatch.setattr(tapo_mod, "discover", lambda **kw: [])
    monkeypatch.setattr(tapo_mod, "find_by_mac", lambda mac: "10.0.0.33")
    monkeypatch.setattr(zigbee_mod, "_make_client", lambda: FakeMqttClient(devices=[PLUG_DEVICE]))

    rc, out, _ = run(capsys, "--json", "discover")

    data = json.loads(out)
    assert rc == 0
    assert data["tapo"] == [{"ip": "10.0.0.33", "model": None, "mac": "58-D8-12-14-1B-6F", "via": "mdns",
                             "configured_as": "Strip"}]
    assert data["zigbee"][0]["configured_as"] == "BenchPlug"
    assert json.loads(saved.read_text())["devices"]["Strip"]["host"] == "10.0.0.33"


def test_discover_reports_missing_broker(saved, capsys, monkeypatch):
    monkeypatch.setattr(tapo_mod, "discover", lambda **kw: [])
    monkeypatch.setattr(tapo_mod, "find_by_mac", lambda mac: None)
    monkeypatch.setattr(zigbee_mod, "_make_client",
                        lambda: FakeMqttClient(connect_error=ConnectionRefusedError(61, "refused")))

    rc, out, _ = run(capsys, "discover")

    assert rc == 0 and "unavailable: Can't reach the MQTT broker" in out

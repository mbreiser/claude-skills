from __future__ import annotations

import json

import tapo_power.cli as cli_mod
import tapo_power.config as config_mod
from tapo_power.cli import EXIT_ERROR, EXIT_TIMEOUT
from tapo_power.config import Config, StripConfig

from conftest import BENCH_HOST, BENCH_MAC


def test_status_json_reports_total_and_six_outlets(cli_config, patch_open, patch_discover_empty, capsys):
    rc = cli_mod.main(["--json", "status"])

    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "total_w" in data
    assert len(data["outlets"]) == 6


def test_alias_updates_config_file(cli_config, patch_open, patch_discover_empty, capsys):
    rc = cli_mod.main(["alias", "arena", "3"])

    assert rc == 0
    saved = json.loads(cli_config.read_text())
    assert saved["strips"]["bench"]["outlets"]["arena"] == 3


def test_alias_numeric_name_is_rejected(cli_config, capsys):
    rc = cli_mod.main(["alias", "7", "3"])

    assert rc == EXIT_ERROR
    assert capsys.readouterr().err.strip() != ""


def test_unalias_unknown_alias_is_an_error(cli_config, capsys):
    rc = cli_mod.main(["unalias", "nope"])

    assert rc == EXIT_ERROR
    assert capsys.readouterr().err.strip() != ""


def test_cycle_wait_above_w_timeout_returns_exit_timeout(cli_config, patch_open, patch_discover_empty, plugs, fast_sleep):
    plugs[3].power = 5  # never rises above the threshold

    rc = cli_mod.main(["cycle", "3", "--off-s", "0", "--wait-above-w", "100", "--timeout-s", "0.05"])

    assert rc == EXIT_TIMEOUT


def test_discover_json_prints_discovered_list(cli_config, monkeypatch, capsys):
    fake_found = [
        {
            "ip": "10.0.0.5",
            "model": "P316M",
            "mac": BENCH_MAC,
            "device_id": "dev1",
            "owner_bound": True,
            "onboarded_via": "app",
        }
    ]
    monkeypatch.setattr(cli_mod, "discover", lambda **kwargs: fake_found)

    rc = cli_mod.main(["--json", "discover"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == fake_found


def test_on_arena_json_reports_state_and_position(monkeypatch, tmp_path, patch_open, patch_discover_empty, capsys):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)
    Config(
        default="bench",
        strips={"bench": StripConfig(name="bench", host=BENCH_HOST, mac=BENCH_MAC, outlets={"arena": 3})},
    ).save(config_path)

    rc = cli_mod.main(["--json", "on", "arena"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "on"
    assert data["position"] == 3

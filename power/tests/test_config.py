from __future__ import annotations

import json

import pytest

from labpower import config as config_mod
from labpower.config import FACTORY_CREDENTIALS, Config, DeviceEntry, PowerError, tapo_credentials


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config(
        devices={
            "Strip": DeviceEntry(name="Strip", type="tapo-strip", host="10.0.0.5", mac="58-D8-12-14-1B-6F"),
            "BenchPlug": DeviceEntry(name="BenchPlug", type="zigbee2mqtt", friendly_name="BenchPlug"),
        },
        aliases={"ArenaPS": "Strip:6"},
        tapo_account="me@x.com",
    )
    cfg.save(path)

    loaded = Config.load(path)

    assert loaded.devices == cfg.devices
    assert loaded.aliases == {"ArenaPS": "Strip:6"}
    assert loaded.tapo_account == "me@x.com"
    assert loaded.mqtt.port == 1883
    assert "friendly_name" not in json.loads(path.read_text())["devices"]["Strip"]


def test_missing_file_loads_empty(tmp_path):
    cfg = Config.load(tmp_path / "none.json")
    assert cfg.devices == {} and cfg.aliases == {}


def test_legacy_tapo_power_config_is_migrated():
    config_mod.LEGACY_TAPO_CONFIG.write_text(json.dumps({
        "default": "SmartPowerStrip",
        "account": None,
        "strips": {"SmartPowerStrip": {"host": "192.168.4.33", "mac": "58-D8-12-14-1B-6F", "outlets": {"ArenaPS": 6}}},
    }))

    cfg = Config.load()

    assert cfg.devices["SmartPowerStrip"] == DeviceEntry(
        name="SmartPowerStrip", type="tapo-strip", host="192.168.4.33", mac="58-D8-12-14-1B-6F")
    assert cfg.aliases == {"ArenaPS": "SmartPowerStrip:6"}
    assert config_mod.CONFIG_PATH.exists()


def test_device_lookup_case_insensitive_and_unknown_lists_names():
    cfg = Config(devices={"BenchPlug": DeviceEntry(name="BenchPlug", type="zigbee2mqtt")})

    assert cfg.device("benchplug").name == "BenchPlug"
    with pytest.raises(PowerError, match="configured: BenchPlug"):
        cfg.device("nope")


def test_tapo_credentials_without_account_is_factory_only():
    assert tapo_credentials(None) == [FACTORY_CREDENTIALS]


def test_tapo_credentials_with_stored_password(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, account: "s3cret" if account == "a@b.c" else None)
    assert tapo_credentials("a@b.c") == [("a@b.c", "s3cret"), FACTORY_CREDENTIALS]


def test_tapo_credentials_account_without_password(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, account: None)
    assert tapo_credentials("a@b.c") == [FACTORY_CREDENTIALS]

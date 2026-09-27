from __future__ import annotations

import pytest

from tapo_power.config import Config, StripConfig, TapoPowerError, credentials


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(
        default="bench",
        account="me@x.com",
        strips={
            "bench": StripConfig(name="bench", host="10.0.0.5", mac="58-D8-12-14-1B-6F", outlets={"arena": 3}),
        },
    )
    cfg.save(path)

    loaded = Config.load(path)

    assert loaded.default == "bench"
    assert loaded.account == "me@x.com"
    assert loaded.strips["bench"].host == "10.0.0.5"
    assert loaded.strips["bench"].mac == "58-D8-12-14-1B-6F"
    assert loaded.strips["bench"].outlets == {"arena": 3}


def test_load_missing_file_is_empty(tmp_path):
    cfg = Config.load(tmp_path / "does-not-exist.json")

    assert cfg.default is None
    assert cfg.account is None
    assert cfg.strips == {}


def test_strip_with_no_default_raises():
    cfg = Config()

    with pytest.raises(TapoPowerError):
        cfg.strip()


def test_strip_unknown_name_lists_configured_names():
    cfg = Config(strips={"bench": StripConfig(name="bench", host="10.0.0.5")})

    with pytest.raises(TapoPowerError) as exc_info:
        cfg.strip("nope")

    assert "bench" in str(exc_info.value)


def test_credentials_no_account_is_none():
    assert credentials(None) is None


def test_credentials_with_stored_password(monkeypatch):
    monkeypatch.setattr(
        "keyring.get_password",
        lambda service, account: "s3cret" if account == "a@b.c" else None,
    )

    assert credentials("a@b.c") == ("a@b.c", "s3cret")


def test_credentials_account_without_stored_password(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda service, account: None)

    assert credentials("a@b.c") is None

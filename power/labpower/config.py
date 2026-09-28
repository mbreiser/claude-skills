from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("POWER_CONFIG", "~/.config/power/config.json")).expanduser()
LEGACY_TAPO_CONFIG = Path("~/.config/tapo-power/config.json").expanduser()

KEYRING_SERVICE = "tapo-power"

# Strips onboarded via Matter only (no TP-Link account owner) accept TP-Link's
# factory-default local credentials. KLAP falls back to them on its own; TPAP
# has to be given them.
FACTORY_CREDENTIALS = ("test@tp-link.net", "test")

DEVICE_TYPES = ("tapo-strip", "zigbee2mqtt")


class PowerError(Exception):
    pass


class WaitTimeout(PowerError):
    pass


@dataclass
class DeviceEntry:
    name: str
    type: str
    host: str | None = None  # tapo-strip
    mac: str | None = None  # tapo-strip: re-finds the strip when its IP moves
    friendly_name: str | None = None  # zigbee2mqtt

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "name" and v is not None}


@dataclass
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    base_topic: str = "zigbee2mqtt"


@dataclass
class Config:
    devices: dict[str, DeviceEntry] = field(default_factory=dict)
    # alias -> "Device:channel", or an ordered list of them: the first reachable one is used
    aliases: dict[str, str | list[str]] = field(default_factory=dict)
    tapo_account: str | None = None
    mqtt: MqttConfig = field(default_factory=MqttConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or CONFIG_PATH
        if not path.exists():
            if path == CONFIG_PATH and LEGACY_TAPO_CONFIG.exists():
                cfg = cls.from_legacy(json.loads(LEGACY_TAPO_CONFIG.read_text()))
                cfg.save(path)
                return cfg
            return cls()
        data = json.loads(path.read_text())
        return cls(
            devices={n: DeviceEntry(name=n, **d) for n, d in data.get("devices", {}).items()},
            aliases=dict(data.get("aliases", {})),
            tapo_account=data.get("tapo_account"),
            mqtt=MqttConfig(**data.get("mqtt", {})),
        )

    @classmethod
    def from_legacy(cls, data: dict) -> Config:
        """Convert a tapo-power config (strips with per-strip outlet aliases)."""
        cfg = cls(tapo_account=data.get("account"))
        for name, s in data.get("strips", {}).items():
            cfg.devices[name] = DeviceEntry(name=name, type="tapo-strip", host=s["host"], mac=s.get("mac"))
            for alias, pos in s.get("outlets", {}).items():
                cfg.aliases[alias] = f"{name}:{pos}"
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": {n: d.to_json() for n, d in self.devices.items()},
            "aliases": self.aliases,
            "tapo_account": self.tapo_account,
            "mqtt": asdict(self.mqtt),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)

    def device(self, name: str) -> DeviceEntry:
        for n, d in self.devices.items():
            if n.lower() == name.lower():
                return d
        known = ", ".join(self.devices) or "none"
        raise PowerError(f"Unknown device {name!r} (configured: {known})")


def alias_targets(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def tapo_credentials(account: str | None) -> list[tuple[str, str]]:
    """Credentials to try, in order: the stored TP-Link account, then factory default."""
    creds = []
    if account:
        import keyring

        password = keyring.get_password(KEYRING_SERVICE, account)
        if password:
            creds.append((account, password))
    creds.append(FACTORY_CREDENTIALS)
    return creds


def store_tapo_password(account: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, account, password)

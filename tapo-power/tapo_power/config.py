from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("TAPO_POWER_CONFIG", "~/.config/tapo-power/config.json")
).expanduser()

KEYRING_SERVICE = "tapo-power"

# Strips onboarded via Matter only (no TP-Link account owner) accept TP-Link's
# factory-default KLAP credentials. Once bound to an account, only that
# account's credentials work.
FACTORY_CREDENTIALS = ("test@tp-link.net", "test")


class TapoPowerError(Exception):
    pass


@dataclass
class StripConfig:
    name: str
    host: str
    mac: str | None = None
    outlets: dict[str, int] = field(default_factory=dict)


@dataclass
class Config:
    default: str | None = None
    account: str | None = None
    strips: dict[str, StripConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or CONFIG_PATH
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        strips = {
            name: StripConfig(
                name=name,
                host=s["host"],
                mac=s.get("mac"),
                outlets={k: int(v) for k, v in s.get("outlets", {}).items()},
            )
            for name, s in data.get("strips", {}).items()
        }
        return cls(default=data.get("default"), account=data.get("account"), strips=strips)

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "default": self.default,
            "account": self.account,
            "strips": {
                name: {"host": s.host, "mac": s.mac, "outlets": s.outlets}
                for name, s in self.strips.items()
            },
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)

    def strip(self, name: str | None = None) -> StripConfig:
        name = name or self.default
        if name is None:
            raise TapoPowerError("No strip configured. Run: tapo-power add <name> [host]")
        try:
            return self.strips[name]
        except KeyError:
            known = ", ".join(self.strips) or "none"
            raise TapoPowerError(f"Unknown strip {name!r} (configured: {known})") from None


def credentials(account: str | None) -> list[tuple[str, str]]:
    """Credentials to try, in order: the stored TP-Link account, then factory default."""
    creds = []
    if account:
        import keyring

        password = keyring.get_password(KEYRING_SERVICE, account)
        if password:
            creds.append((account, password))
    creds.append(FACTORY_CREDENTIALS)
    return creds


def store_password(account: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, account, password)

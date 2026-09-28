"""Zigbee plugs through a Zigbee2MQTT bridge, over MQTT."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable

from .config import DeviceEntry, MqttConfig, PowerError
from .model import ChannelStatus, DeviceStatus

REQUEST_TIMEOUT_S = 3  # a W/V/A read normally takes ~0.3 s
# Zigbee2MQTT publishes the full state once per attribute it reads or the
# device reports (a W/V/A read is three messages ~40 ms apart). A request is
# answered once the burst has been quiet this long.
QUIET_S = 0.15
READING_PROPS = ["power", "voltage", "current"]


def _make_client():
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"labpower-{os.getpid()}-{id(object())}")


class Z2MBridge:
    """One MQTT session with Zigbee2MQTT, shared by every zigbee2mqtt device.

    Zigbee2MQTT publishes each device's full state on `<base>/<friendly_name>`;
    a request is answered by the next state message for that device."""

    def __init__(self, cfg: MqttConfig, *, client_factory: Callable | None = None,
                 timeout_s: float = REQUEST_TIMEOUT_S, quiet_s: float = QUIET_S):
        self._base = cfg.base_topic
        self._timeout_s = timeout_s
        self._quiet_s = quiet_s
        self._cond = threading.Condition()
        self._states: dict[str, dict] = {}
        self._seq: dict[str, int] = {}
        self._bridge_state: str | None = None
        self._devices: list[dict] | None = None
        self._client = (client_factory or _make_client)()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        try:
            self._client.connect(cfg.host, cfg.port, keepalive=30)
        except OSError as e:
            raise PowerError(
                f"Can't reach the MQTT broker at {cfg.host}:{cfg.port} ({e}). Is Mosquitto running? "
                "`brew services start mosquitto`"
            ) from e
        self._client.loop_start()
        # bridge/state and bridge/devices are retained, so they arrive right after subscribing.
        self._wait(lambda: self._bridge_state is not None and self._devices is not None, "the Zigbee2MQTT bridge")
        if self._bridge_state != "online":
            raise PowerError(
                "Zigbee2MQTT is not running (bridge state offline). Start it: "
                "launchctl kickstart -k gui/$(id -u)/io.zigbee2mqtt"
            )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe([(f"{self._base}/bridge/state", 0), (f"{self._base}/bridge/devices", 0), (f"{self._base}/+", 0)])

    def _on_message(self, client, userdata, msg):
        topic = msg.topic[len(self._base) + 1:]
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            payload = msg.payload.decode(errors="replace")
        with self._cond:
            if topic == "bridge/state":
                self._bridge_state = payload.get("state") if isinstance(payload, dict) else payload
            elif topic == "bridge/devices":
                self._devices = payload
            elif "/" not in topic and isinstance(payload, dict):
                self._states[topic] = payload
                self._seq[topic] = self._seq.get(topic, 0) + 1
            self._cond.notify_all()

    def _wait(self, pred: Callable[[], bool], what: str) -> None:
        with self._cond:
            if not self._cond.wait_for(pred, self._timeout_s):
                raise PowerError(f"Timed out after {self._timeout_s} s waiting for {what}")

    def devices(self) -> list[dict]:
        """Paired devices (Zigbee2MQTT's bridge/devices list, minus the coordinator)."""
        with self._cond:
            return [d for d in self._devices or [] if d.get("type") != "Coordinator"]

    def device(self, friendly_name: str) -> dict:
        for d in self.devices():
            if d.get("friendly_name") == friendly_name:
                return d
        known = ", ".join(d.get("friendly_name", "?") for d in self.devices()) or "none"
        raise PowerError(f"Zigbee2MQTT has no device named {friendly_name!r} (paired: {known})")

    def cached(self, friendly_name: str) -> dict | None:
        with self._cond:
            s = self._states.get(friendly_name)
            return dict(s) if s else None

    def _request(self, friendly_name: str, suffix: str, payload: dict, done: Callable[[dict], bool]) -> dict:
        """Publish, wait for a state message satisfying `done`, then for the
        burst of follow-up messages to end, and return the last state — so every
        requested value is fresh and no leftovers answer the next request."""
        with self._cond:
            if self._bridge_state != "online":
                # Zigbee2MQTT publishes a retained "offline" when it stops; fail now instead of timing out.
                raise PowerError(f"Zigbee2MQTT is {self._bridge_state or 'not answering'}; {friendly_name} is unreachable")
            seq0 = self._seq.get(friendly_name, 0)
        self._client.publish(f"{self._base}/{friendly_name}/{suffix}", json.dumps(payload))
        deadline = time.monotonic() + self._timeout_s

        def answered() -> bool:
            return self._seq.get(friendly_name, 0) > seq0 and done(self._states[friendly_name])

        self._wait(answered, f"{friendly_name} to answer (powered, paired, in range?)")
        with self._cond:
            while True:
                seq = self._seq[friendly_name]
                window = min(self._quiet_s, deadline - time.monotonic())
                if window <= 0 or not self._cond.wait_for(lambda: self._seq[friendly_name] != seq, window):
                    return dict(self._states[friendly_name])

    def get(self, friendly_name: str, props: list[str]) -> dict:
        return self._request(friendly_name, "get", {p: "" for p in props}, lambda s: True)

    def set_state(self, friendly_name: str, on: bool) -> dict:
        want = "ON" if on else "OFF"
        return self._request(friendly_name, "set", {"state": want}, lambda s: s.get("state") == want)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class Z2MPlug:
    """Backend for a single-outlet Zigbee plug (tested: ThirdReality 3RSP02064Z Smart Plug Gen3)."""

    def __init__(self, entry: DeviceEntry, bridge: Z2MBridge):
        self._entry = entry
        self._bridge = bridge
        self._friendly = entry.friendly_name or entry.name
        self._info = bridge.device(self._friendly)

    @property
    def name(self) -> str:
        return self._entry.name

    def _check(self, channels: list[int] | None) -> None:
        if channels not in (None, [1]):
            raise PowerError(f"{self.name} is a single outlet; its only channel is 1")

    def _status(self, st: dict) -> ChannelStatus:
        return ChannelStatus(
            device=self.name, channel=1, native_name=self._friendly, on=st.get("state") == "ON",
            power_w=st.get("power"), voltage_v=st.get("voltage"), current_a=st.get("current"),
        )

    def channels(self) -> list[ChannelStatus]:
        st = self._bridge.get(self._friendly, ["state"])
        return [ChannelStatus(device=self.name, channel=1, native_name=self._friendly, on=st.get("state") == "ON")]

    def sample(self, channels: list[int] | None = None) -> list[ChannelStatus]:
        self._check(channels)
        return [self._status(self._bridge.get(self._friendly, READING_PROPS))]

    def status(self) -> DeviceStatus:
        ch = self._status(self._bridge.get(self._friendly, READING_PROPS))
        st = self._bridge.get(self._friendly, ["energy"])
        ch.total_kwh = st.get("energy")
        definition = self._info.get("definition") or {}
        info = {
            "model": definition.get("model") or self._info.get("model_id"),
            "vendor": definition.get("vendor") or self._info.get("manufacturer"),
            "ieee_address": self._info.get("ieee_address"),
            "fw_ver": self._info.get("software_build_id"),
            "linkquality": st.get("linkquality"),
            "power_factor": st.get("power_factor"),
            "ac_frequency": st.get("ac_frequency"),
            "metering_only_mode": st.get("metering_only_mode"),
        }
        return DeviceStatus(name=self.name, type="zigbee2mqtt", online=True, info=info, channels=[ch])

    def set(self, channel: int, on: bool) -> None:
        self._check([channel])
        if (self._bridge.cached(self._friendly) or {}).get("metering_only_mode") == "ON":
            raise PowerError(f"{self.name} is in metering-only mode, which locks its relay on")
        self._bridge.set_state(self._friendly, on)

    def close(self) -> None:
        pass

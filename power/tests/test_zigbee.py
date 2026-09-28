from __future__ import annotations

import pytest

from labpower.config import DeviceEntry, MqttConfig, PowerError
from labpower.zigbee import Z2MBridge, Z2MPlug

from conftest import PLUG_DEVICE, FakeMqttClient

STATE = {"state": "OFF", "power": 0.0, "voltage": 121.2, "current": 0.0, "energy": 0.012,
         "linkquality": 159, "power_factor": 0.0, "ac_frequency": 60, "metering_only_mode": "OFF"}


def fake_z2m(state=None, **kw):
    """A fake Zigbee2MQTT: answers get with the current state and set by applying it."""
    state = dict(STATE if state is None else state)

    def respond(topic, body):
        if topic == "zigbee2mqtt/BenchPlug/set":
            state.update(body)
        if topic.startswith("zigbee2mqtt/BenchPlug/"):
            return [("zigbee2mqtt/BenchPlug", dict(state))]

    client = FakeMqttClient(devices=[{"type": "Coordinator"}, PLUG_DEVICE], respond=respond, **kw)
    return client, state


def bridge_for(client, timeout_s=0.2):
    return Z2MBridge(MqttConfig(), client_factory=lambda: client, timeout_s=timeout_s, quiet_s=0.01)


def test_bridge_subscribes_and_lists_devices_without_coordinator():
    client, _ = fake_z2m()
    b = bridge_for(client)

    assert [d["friendly_name"] for d in b.devices()] == ["BenchPlug"]
    topics = [t for t, _ in client.subscribed[0]]
    assert "zigbee2mqtt/bridge/devices" in topics and "zigbee2mqtt/+" in topics


def test_bridge_broker_unreachable_is_a_clean_error():
    client = FakeMqttClient(connect_error=ConnectionRefusedError(61, "Connection refused"))

    with pytest.raises(PowerError, match="brew services start mosquitto"):
        bridge_for(client)


def test_bridge_offline_is_a_clean_error():
    client, _ = fake_z2m(bridge_state="offline")

    with pytest.raises(PowerError, match="Zigbee2MQTT is not running"):
        bridge_for(client)


def test_bridge_missing_retained_state_times_out():
    client, _ = fake_z2m(bridge_state=None)

    with pytest.raises(PowerError, match="Timed out"):
        bridge_for(client)


def test_get_publishes_request_and_returns_answer():
    client, _ = fake_z2m()
    b = bridge_for(client)

    st = b.get("BenchPlug", ["power", "voltage", "current"])

    assert client.published[-1] == ("zigbee2mqtt/BenchPlug/get", {"power": "", "voltage": "", "current": ""})
    assert st["voltage"] == 121.2


def test_get_times_out_when_device_silent():
    client = FakeMqttClient(devices=[PLUG_DEVICE])
    b = bridge_for(client, timeout_s=0.05)

    with pytest.raises(PowerError, match="BenchPlug to answer"):
        b.get("BenchPlug", ["power"])


def test_set_state_waits_for_matching_state():
    client, state = fake_z2m()
    b = bridge_for(client)

    b.set_state("BenchPlug", True)

    assert client.published[-1] == ("zigbee2mqtt/BenchPlug/set", {"state": "ON"})
    assert state["state"] == "ON"


def test_set_state_times_out_if_device_never_confirms():
    def respond(topic, body):
        return [("zigbee2mqtt/BenchPlug", dict(STATE))]  # always reports OFF

    client = FakeMqttClient(devices=[PLUG_DEVICE], respond=respond)
    b = bridge_for(client, timeout_s=0.05)

    with pytest.raises(PowerError, match="Timed out"):
        b.set_state("BenchPlug", True)


def test_unknown_friendly_name_lists_paired_devices():
    client, _ = fake_z2m()
    b = bridge_for(client)

    with pytest.raises(PowerError, match="paired: BenchPlug"):
        b.device("Nope")


# --- Z2MPlug backend ---------------------------------------------------------


def plug(state=None):
    client, st = fake_z2m(state)
    entry = DeviceEntry(name="BenchPlug", type="zigbee2mqtt", friendly_name="BenchPlug")
    return Z2MPlug(entry, bridge_for(client)), client, st


def test_plug_sample_reads_power_voltage_current():
    p, _, _ = plug(dict(STATE, state="ON", power=4.2, current=0.035))

    (row,) = p.sample()

    assert (row.device, row.channel, row.on, row.power_w, row.voltage_v, row.current_a) == ("BenchPlug", 1, True, 4.2, 121.2, 0.035)


def test_plug_has_only_channel_one():
    p, _, _ = plug()

    with pytest.raises(PowerError, match="single outlet"):
        p.sample([2])
    with pytest.raises(PowerError, match="single outlet"):
        p.set(2, True)


def test_plug_status_includes_energy_and_link_info():
    p, _, _ = plug()

    st = p.status()

    assert st.type == "zigbee2mqtt" and st.online
    assert st.channels[0].total_kwh == 0.012
    assert st.info["model"] == "3RSP02064Z" and st.info["linkquality"] == 159


def test_plug_set_switches_relay():
    p, _, state = plug()

    p.set(1, True)

    assert state["state"] == "ON"
    assert p.channels()[0].on is True


def test_plug_refuses_to_switch_in_metering_only_mode():
    p, client, _ = plug(dict(STATE, metering_only_mode="ON"))
    p.sample()  # caches the state

    with pytest.raises(PowerError, match="metering-only"):
        p.set(1, False)
    assert not any(t.endswith("/set") for t, _ in client.published)


def test_get_returns_last_state_of_a_burst():
    """Zigbee2MQTT answers a W/V/A read with one message per attribute; the
    first can still hold the previous voltage."""
    def respond(topic, body):
        return [("zigbee2mqtt/BenchPlug", dict(STATE, voltage=v)) for v in (121.1, 121.3, 121.4)]

    client = FakeMqttClient(devices=[PLUG_DEVICE], respond=respond)
    b = bridge_for(client)

    assert b.get("BenchPlug", ["power", "voltage", "current"])["voltage"] == 121.4


def test_burst_leftovers_do_not_answer_the_next_request():
    replies = iter([
        [("zigbee2mqtt/BenchPlug", dict(STATE, voltage=v)) for v in (120.0, 120.1, 120.2)],
        [("zigbee2mqtt/BenchPlug", dict(STATE, voltage=121.0))],
    ])
    client = FakeMqttClient(devices=[PLUG_DEVICE], respond=lambda topic, body: next(replies))
    b = bridge_for(client)

    assert b.get("BenchPlug", ["voltage"])["voltage"] == 120.2
    assert b.get("BenchPlug", ["voltage"])["voltage"] == 121.0


def test_requests_fail_fast_once_bridge_goes_offline():
    client, _ = fake_z2m()
    b = bridge_for(client, timeout_s=5)
    client.deliver("zigbee2mqtt/bridge/state", {"state": "offline"})

    import time
    t = time.monotonic()
    with pytest.raises(PowerError, match="Zigbee2MQTT is offline"):
        b.get("BenchPlug", ["power"])
    assert time.monotonic() - t < 0.5
    assert not any(topic.endswith("/get") for topic, _ in client.published)

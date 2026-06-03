"""Tests for the SA-compatible MQTT publisher."""

import json
from unittest.mock import MagicMock

import pytest

from inverter_bridge.config import MqttCfg
from inverter_bridge.mqtt_publisher import (
    MqttPublisher,
    _key_to_topic_path,
    _key_to_unique_id,
)


@pytest.fixture
def cfg():
    return MqttCfg(
        host="broker.local",
        username="u",
        password="p",
        topic_prefix="gadi_inverters",
        discovery_prefix="homeassistant",
        retain_discovery=True,
        qos=0,
    )


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        "paho.mqtt.client.Client",
        MagicMock(return_value=client),
    )
    return client


def test_key_to_topic_path_aggregate():
    assert _key_to_topic_path("battery_state_of_charge") == "total/battery_state_of_charge"
    assert _key_to_topic_path("battery_power") == "total/battery_power"
    assert _key_to_topic_path("mode") == "total/mode"


def test_key_to_topic_path_per_inverter():
    assert _key_to_topic_path("inverter_1_pv_power") == "inverter_1/pv_power"
    assert _key_to_topic_path("inverter_2_battery_current") == "inverter_2/battery_current"
    assert _key_to_topic_path("inverter_1_pv_voltage_mppt1") == "inverter_1/pv_voltage_mppt1"


def test_key_to_unique_id_aggregate():
    assert _key_to_unique_id("battery_state_of_charge") == "total_battery_state_of_charge"


def test_key_to_unique_id_per_inverter():
    assert _key_to_unique_id("inverter_1_pv_power") == "inverter_1_pv_power"


def test_connect_sets_lwt_at_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    mock_client.will_set.assert_called_once_with(
        "gadi_inverters/availability",
        payload="offline",
        qos=0,
        retain=True,
    )


def test_publish_aggregate_value_uses_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("battery_voltage", 52.1)
    mock_client.publish.assert_any_call(
        "gadi_inverters/total/battery_voltage/state", payload="52.1", qos=0, retain=False
    )


def test_publish_per_inverter_value_uses_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("inverter_1_pv_power", 750.0)
    mock_client.publish.assert_any_call(
        "gadi_inverters/inverter_1/pv_power/state", payload="750.0", qos=0, retain=False
    )


def test_publish_discovery_uses_sa_unique_ids(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    # The discovery topic for aggregated battery SOC must match SA's
    config_calls = [c for c in mock_client.publish.call_args_list if "/config" in c.args[0]]
    soc_call = next(
        c for c in config_calls
        if c.args[0] == "homeassistant/sensor/total_battery_state_of_charge/config"
    )
    payload = json.loads(soc_call.kwargs["payload"])
    assert payload["unique_id"] == "total_battery_state_of_charge"
    assert payload["state_topic"] == "gadi_inverters/total/battery_state_of_charge/state"
    assert payload["device_class"] == "battery"
    assert payload["state_class"] == "measurement"
    assert payload["unit_of_measurement"] == "%"


def test_publish_discovery_per_inverter_unique_ids(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    config_topics = [c.args[0] for c in mock_client.publish.call_args_list if "/config" in c.args[0]]
    # Both inverters
    assert "homeassistant/sensor/inverter_1_pv_power/config" in config_topics
    assert "homeassistant/sensor/inverter_2_pv_power/config" in config_topics
    assert "homeassistant/sensor/inverter_1_temperature/config" in config_topics
    assert "homeassistant/sensor/inverter_2_temperature/config" in config_topics


def test_publish_set_online(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.set_online()
    mock_client.publish.assert_any_call(
        "gadi_inverters/availability", payload="online", qos=0, retain=True
    )


def test_disconnect_sets_offline(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.disconnect()
    mock_client.publish.assert_any_call(
        "gadi_inverters/availability", payload="offline", qos=0, retain=True
    )


# --- F-4: _meta diagnostic sensors ----------------------------------------


def test_meta_topic_path_is_top_level():
    """`_meta/*` keys must not be nested under `total/`."""
    assert _key_to_topic_path("_meta/uptime_s") == "_meta/uptime_s"
    assert _key_to_topic_path("_meta/poll_duration_ms") == "_meta/poll_duration_ms"
    assert _key_to_topic_path("_meta/crc_fails_total") == "_meta/crc_fails_total"


def test_meta_unique_id():
    """`_meta/<x>` -> `meta_<x>` (slash collapsed; unique_ids can't contain /)."""
    assert _key_to_unique_id("_meta/uptime_s") == "meta_uptime_s"
    assert _key_to_unique_id("_meta/poll_duration_ms") == "meta_poll_duration_ms"
    assert _key_to_unique_id("_meta/crc_fails_total") == "meta_crc_fails_total"


def test_meta_discovery_published(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    config_topics = [
        c.args[0] for c in mock_client.publish.call_args_list if "/config" in c.args[0]
    ]
    assert "homeassistant/sensor/meta_poll_duration_ms/config" in config_topics
    assert "homeassistant/sensor/meta_crc_fails_total/config" in config_topics
    assert "homeassistant/sensor/meta_uptime_s/config" in config_topics

    # Verify shape of one of them
    uptime_call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "homeassistant/sensor/meta_uptime_s/config"
    )
    payload = json.loads(uptime_call.kwargs["payload"])
    assert payload["unique_id"] == "meta_uptime_s"
    assert payload["state_topic"] == "gadi_inverters/_meta/uptime_s/state"
    assert payload["device_class"] == "duration"
    assert payload["state_class"] == "total_increasing"
    assert payload["unit_of_measurement"] == "s"


def test_meta_value_published_to_top_level_topic(cfg, mock_client):
    """publish_value('_meta/uptime_s', ...) must land at gadi_inverters/_meta/uptime_s/state."""
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("_meta/uptime_s", 42.0)
    mock_client.publish.assert_any_call(
        "gadi_inverters/_meta/uptime_s/state", payload="42.0", qos=0, retain=False
    )


# --- F-4: binary_sensor.inverter_bridge_online -----------------------------


def test_binary_sensor_online_discovery(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "homeassistant/binary_sensor/inverter_bridge_online/config"
    )
    payload = json.loads(call.kwargs["payload"])
    assert payload["unique_id"] == "inverter_bridge_online"
    assert payload["state_topic"] == "gadi_inverters/availability"
    assert payload["payload_on"] == "online"
    assert payload["payload_off"] == "offline"
    assert payload["device_class"] == "connectivity"
    # Same device block as sensors so it groups under "SRNE Split-phase x 2"
    assert payload["device"]["identifiers"] == ["sa_inverter"]


# --- F-6: force_update on low-granularity sensors --------------------------


def test_force_update_on_low_granularity_keys(cfg, mock_client):
    """SoC, mode, charge_state, load_percentage discovery must have force_update: true."""
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()

    def _payload(topic):
        call = next(
            c for c in mock_client.publish.call_args_list if c.args[0] == topic
        )
        return json.loads(call.kwargs["payload"])

    soc = _payload("homeassistant/sensor/total_battery_state_of_charge/config")
    assert soc.get("force_update") is True

    mode = _payload("homeassistant/sensor/total_mode/config")
    assert mode.get("force_update") is True

    dev_mode = _payload("homeassistant/sensor/inverter_1_device_mode/config")
    assert dev_mode.get("force_update") is True

    charge = _payload("homeassistant/sensor/inverter_1_charge_state/config")
    assert charge.get("force_update") is True

    load_pct = _payload("homeassistant/sensor/inverter_2_load_percentage/config")
    assert load_pct.get("force_update") is True

    # And all _meta sensors
    uptime = _payload("homeassistant/sensor/meta_uptime_s/config")
    assert uptime.get("force_update") is True


def test_force_update_off_on_continuous_keys(cfg, mock_client):
    """Continuous sensors (voltage, current, power) must NOT have force_update set."""
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()

    def _payload(topic):
        call = next(
            c for c in mock_client.publish.call_args_list if c.args[0] == topic
        )
        return json.loads(call.kwargs["payload"])

    # Aggregated continuous
    bv = _payload("homeassistant/sensor/total_battery_voltage/config")
    assert "force_update" not in bv

    bp = _payload("homeassistant/sensor/total_battery_power/config")
    assert "force_update" not in bp

    pvp = _payload("homeassistant/sensor/total_pv_power/config")
    assert "force_update" not in pvp

    # Per-inverter continuous
    temp = _payload("homeassistant/sensor/inverter_1_temperature/config")
    assert "force_update" not in temp

    pv_power_1 = _payload("homeassistant/sensor/inverter_1_pv_power/config")
    assert "force_update" not in pv_power_1


# --- F-2: energy accumulator discovery ------------------------------------


def test_energy_sensors_in_discovery(cfg, mock_client):
    """All 6 energy accumulator sensors must get discovery payloads."""
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    config_topics = [
        c.args[0] for c in mock_client.publish.call_args_list if "/config" in c.args[0]
    ]

    expected = [
        "homeassistant/sensor/total_battery_energy_in/config",
        "homeassistant/sensor/total_battery_energy_out/config",
        "homeassistant/sensor/total_pv_energy/config",
        "homeassistant/sensor/total_load_energy/config",
        "homeassistant/sensor/total_grid_energy_in/config",
        "homeassistant/sensor/total_grid_energy_out/config",
    ]
    for topic in expected:
        assert topic in config_topics, f"missing discovery for {topic}"

    # Shape check
    pv_energy_call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "homeassistant/sensor/total_pv_energy/config"
    )
    payload = json.loads(pv_energy_call.kwargs["payload"])
    assert payload["unique_id"] == "total_pv_energy"
    assert payload["state_topic"] == "gadi_inverters/total/pv_energy/state"
    assert payload["device_class"] == "energy"
    assert payload["state_class"] == "total_increasing"
    assert payload["unit_of_measurement"] == "kWh"
    # Energy accumulators need force_update: total_increasing counters stay flat
    # for long stretches (e.g. battery_energy_out while charging) and HA dedups
    # identical values, making the entity look stale to the watchdog.
    assert payload.get("force_update") is True

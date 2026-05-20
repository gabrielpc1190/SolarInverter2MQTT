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
        topic_prefix="solar_assistant",
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
    assert _key_to_topic_path("inverter_1_pv_voltage_1") == "inverter_1/pv_voltage_1"


def test_key_to_unique_id_aggregate():
    assert _key_to_unique_id("battery_state_of_charge") == "total_battery_state_of_charge"


def test_key_to_unique_id_per_inverter():
    assert _key_to_unique_id("inverter_1_pv_power") == "inverter_1_pv_power"


def test_connect_sets_lwt_at_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    mock_client.will_set.assert_called_once_with(
        "solar_assistant/availability",
        payload="offline",
        qos=0,
        retain=True,
    )


def test_publish_aggregate_value_uses_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("battery_voltage", 52.1)
    mock_client.publish.assert_any_call(
        "solar_assistant/total/battery_voltage/state", payload="52.1", qos=0, retain=False
    )


def test_publish_per_inverter_value_uses_sa_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("inverter_1_pv_power", 750.0)
    mock_client.publish.assert_any_call(
        "solar_assistant/inverter_1/pv_power/state", payload="750.0", qos=0, retain=False
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
    assert payload["state_topic"] == "solar_assistant/total/battery_state_of_charge/state"
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
        "solar_assistant/availability", payload="online", qos=0, retain=True
    )


def test_disconnect_sets_offline(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.disconnect()
    mock_client.publish.assert_any_call(
        "solar_assistant/availability", payload="offline", qos=0, retain=True
    )

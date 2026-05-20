"""Tests for the MQTT publisher (HA Discovery + LWT + state publishing)."""

import json
from unittest.mock import MagicMock

import pytest

from inverter_bridge.config import MqttCfg
from inverter_bridge.mqtt_publisher import MqttPublisher


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
    """Patch the paho mqtt Client constructor to return a MagicMock."""
    client = MagicMock()
    monkeypatch.setattr(
        "paho.mqtt.client.Client",
        MagicMock(return_value=client),
    )
    return client


def test_connect_sets_lwt(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    mock_client.will_set.assert_called_once_with(
        "gadi_inverters/availability",
        payload="offline",
        qos=0,
        retain=True,
    )
    mock_client.username_pw_set.assert_called_once_with("u", "p")
    mock_client.connect.assert_called_once_with("broker.local", 1883, keepalive=60)


def test_publish_value_writes_state_topic(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_value("battery_voltage", 52.1)
    mock_client.publish.assert_any_call(
        "gadi_inverters/battery_voltage/state", payload="52.1", qos=0, retain=False
    )


def test_publish_values_publishes_all(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.publish_values({"a": 1, "b": "x"})
    topics = [c.args[0] for c in mock_client.publish.call_args_list]
    assert "gadi_inverters/a/state" in topics
    assert "gadi_inverters/b/state" in topics


def test_publish_discovery_writes_config_topics(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    # All discovery publishes go to topics ending in /config
    config_calls = [c for c in mock_client.publish.call_args_list if "/config" in c.args[0]]
    # Should be > 50 (FIELDS x 2 + AGGREGATE + PER_INVERTER_AGGREGATES x 2)
    assert len(config_calls) > 50


def test_publish_discovery_payload_shape(cfg, mock_client):
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    # Verify shape of one specific discovery payload
    config_calls = [c for c in mock_client.publish.call_args_list if "/config" in c.args[0]]
    soc_call = next(
        c for c in config_calls if "battery_state_of_charge_2" in c.args[0]
    )
    payload = json.loads(soc_call.kwargs["payload"])
    assert payload["unique_id"].startswith("gadi_inverters_battery_state_of_charge_2")
    assert payload["state_topic"] == "gadi_inverters/battery_state_of_charge_2/state"
    assert payload["availability_topic"] == "gadi_inverters/availability"
    assert payload["device_class"] == "battery"
    assert payload["state_class"] == "measurement"
    assert payload["unit_of_measurement"] == "%"
    assert "device" in payload
    assert payload["device"]["manufacturer"] == "SunGoldPower"
    assert soc_call.kwargs["retain"] is True


def test_publish_set_online(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.set_online()
    mock_client.publish.assert_any_call(
        "gadi_inverters/availability", payload="online", qos=0, retain=True
    )


def test_disconnect_sets_offline_and_stops_loop(cfg, mock_client):
    pub = MqttPublisher(cfg)
    pub.connect()
    pub.disconnect()
    mock_client.publish.assert_any_call(
        "gadi_inverters/availability", payload="offline", qos=0, retain=True
    )
    mock_client.loop_stop.assert_called_once()
    mock_client.disconnect.assert_called_once()


def test_discovery_includes_per_inverter_keys(cfg, mock_client):
    """Verify per-inverter sensors get rendered for both inv1 and inv2."""
    pub = MqttPublisher(cfg, n_inverters=2)
    pub.connect()
    pub.publish_discovery()
    config_topics = [c.args[0] for c in mock_client.publish.call_args_list if "/config" in c.args[0]]
    # Both temperatures should exist
    assert any("inverter_1_temperature_2" in t for t in config_topics)
    assert any("inverter_2_temperature_2" in t for t in config_topics)
    # Charge state per inverter
    assert any("inverter_1_charge_state" in t for t in config_topics)
    assert any("inverter_2_charge_state" in t for t in config_topics)

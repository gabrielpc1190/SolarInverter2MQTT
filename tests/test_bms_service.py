"""Tests for the BMS MQTT service lifecycle (connect / reconnect availability)."""

from unittest.mock import MagicMock

import pytest

from inverter_bridge.bms.service import BmsService
from inverter_bridge.config import BmsCfg, MqttCfg


@pytest.fixture
def mqtt_cfg():
    return MqttCfg(host="broker.local", username="u", password="p", client_id="gadi_inverter_bridge")


@pytest.fixture
def bms_cfg():
    return BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D")


@pytest.fixture
def svc(bms_cfg, mqtt_cfg):
    s = BmsService(bms_cfg, mqtt_cfg)
    s._mqtt = MagicMock()
    return s


def test_on_connect_publishes_availability_online(svc):
    svc._handle_mqtt_connect(svc._mqtt, None, None, 0)
    svc._mqtt.publish.assert_any_call(
        "gadi_bms/availability", "online", qos=1, retain=True
    )


def test_on_connect_sets_connected_event(svc):
    assert not svc._mqtt_connected.is_set()
    svc._handle_mqtt_connect(svc._mqtt, None, None, 0)
    assert svc._mqtt_connected.is_set()


def test_reconnect_reasserts_availability_online(svc):
    # First successful connect (initial startup).
    svc._handle_mqtt_connect(svc._mqtt, None, None, 0)
    svc._mqtt.publish.reset_mock()

    # Broker dropped the connection -> LWT left "offline" retained.
    # paho auto-reconnects and fires on_connect again: availability MUST be
    # re-asserted to "online", otherwise the BMS stays offline in HA forever.
    svc._handle_mqtt_connect(svc._mqtt, None, None, 0)
    svc._mqtt.publish.assert_any_call(
        "gadi_bms/availability", "online", qos=1, retain=True
    )


def test_on_connect_failure_does_not_publish_online(svc):
    svc._handle_mqtt_connect(svc._mqtt, None, None, 5)
    online_calls = [
        c
        for c in svc._mqtt.publish.call_args_list
        if c.args[:2] == ("gadi_bms/availability", "online")
    ]
    assert online_calls == []
    assert not svc._mqtt_connected.is_set()

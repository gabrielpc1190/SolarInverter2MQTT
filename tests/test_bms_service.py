"""Tests for the BMS MQTT service lifecycle (connect / reconnect availability)."""

import asyncio
from unittest.mock import MagicMock

import pytest
from bleak.exc import BleakError

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


# ───── Poll loop self-healing (BLE reconnect) ──────────────────────────
#
# Bug (2026-06-09): when the BLE link dropped mid-loop, every poll raised
# BleakError("not connected"), but _poll_loop swallowed it per-pack and kept
# spinning forever. The reconnect path in _run_async (the `async with` re-entry
# that calls connect() again) was never reached, so the BMS stayed dead until a
# manual daemon restart. _poll_loop MUST return on a dead link so _run_async
# tears down the client and reconnects.


class _FakeClient:
    """Minimal stand-in for OctopusBleClient. BLE hardware is unavoidable to
    mock, so we fake the two poll coroutines and the is_connected flag."""

    def __init__(self, *, connected: bool, exc: Exception) -> None:
        self.is_connected = connected
        self._exc = exc
        self.poll_calls = 0

    async def poll_pia(self, pack: int):
        self.poll_calls += 1
        raise self._exc

    async def poll_pib(self, pack: int):
        self.poll_calls += 1
        raise self._exc


def _fast_cfg(**overrides) -> BmsCfg:
    base = dict(
        enabled=True,
        master_mac="C0:D6:3C:52:0F:0D",
        inter_pack_delay_s=0.0,
        poll_fast_interval_s=0.0,
        poll_slow_interval_s=1e9,  # keep PIB out of the way for these tests
    )
    base.update(overrides)
    return BmsCfg(**base)


def test_poll_loop_returns_when_ble_link_drops(mqtt_cfg):
    """Fix A: poll raises 'not connected' AND client reports disconnected ->
    _poll_loop must return promptly so _run_async can reconnect (not hang)."""
    svc = BmsService(_fast_cfg(), mqtt_cfg)
    svc._mqtt = MagicMock()
    client = _FakeClient(connected=False, exc=BleakError("not connected"))

    async def run():
        svc._stop_event = asyncio.Event()
        await asyncio.wait_for(svc._poll_loop(client), timeout=5.0)

    asyncio.run(run())  # raises TimeoutError if the loop never bails out


def test_poll_loop_returns_after_consecutive_failed_cycles(mqtt_cfg):
    """Fix B: zombie link — is_connected stays True but every poll times out.
    After N consecutive all-failed cycles, _poll_loop must give up and return
    to force a fresh reconnect rather than spin forever."""
    svc = BmsService(_fast_cfg(max_failed_cycles=3), mqtt_cfg)
    svc._mqtt = MagicMock()
    client = _FakeClient(connected=True, exc=TimeoutError("no response"))

    async def run():
        svc._stop_event = asyncio.Event()
        await asyncio.wait_for(svc._poll_loop(client), timeout=5.0)

    asyncio.run(run())

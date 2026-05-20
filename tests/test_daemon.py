"""Tests for the daemon main loop using injected fakes for serial + MQTT."""

from unittest.mock import MagicMock

import pytest

from inverter_bridge.config import BridgeConfig, InverterCfg, MqttCfg, PollingCfg
from inverter_bridge.daemon import Daemon
from inverter_bridge.modbus import ModbusException, ModbusFrame


@pytest.fixture
def cfg():
    return BridgeConfig(
        inverters=[
            InverterCfg(name="inv1", port="/dev/ttyUSB1", slave=1),
            InverterCfg(name="inv2", port="/dev/ttyUSB0", slave=2),
        ],
        mqtt=MqttCfg(host="x", username="u", password="p"),
        polling=PollingCfg(
            hot_interval_s=0.01,
            cold_interval_s=0.05,
            inter_query_delay_s=0.0,
            retry_attempts=1,
            retry_backoff_s=0.0,
        ),
    )


def _make_serial_factory(query_fn):
    """Returns a factory that always produces a Mock with `.query` = query_fn."""
    def factory(**kwargs):
        m = MagicMock()
        m.query = MagicMock(side_effect=query_fn)
        return m
    return factory


def _real_frame_for(addr: int, count: int, slave: int) -> ModbusFrame:
    """Generate a plausible ModbusFrame for the given block address."""
    if addr == 0x0100:
        return ModbusFrame(slave=slave, func=3, regs=[44, 521, 19, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0])
    if addr == 0x0210:
        return ModbusFrame(
            slave=slave, func=3,
            regs=[3, 0, 5110, 0, 0, 0, 1199, 50, 6000, 27, 0, 538, 580, 0, 0, 11, 300, 434, 500],
        )
    if addr == 0x0223:
        return ModbusFrame(
            slave=slave, func=3,
            regs=[1200, 0, 0, 0, 0, 2558, 2552, 0, 0, 1200, 0, 33, 0, 29, 0, 324, 0, 356, 0, 7, 0, 0, 0],
        )
    raise ModbusException(slave, 3, 0x02)


def test_hot_cycle_publishes_aggregated_values(cfg):
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(cfg, serial_factory=serial_factory, publisher_factory=publisher_factory)
    d.run_one_hot_cycle()
    # Verify publisher.publish_value was called for key sensors
    keys_published = [c.args[0] for c in fake_pub.publish_value.call_args_list]
    # Aggregator outputs come via publish_values, not publish_value:
    pv_calls = fake_pub.publish_values.call_args_list
    assert pv_calls, "expected at least one publish_values call (aggregator output)"
    aggregated = pv_calls[0].args[0]
    assert "battery_state_of_charge_2" in aggregated
    assert "battery_power" in aggregated
    assert "load_power_2" in aggregated
    assert "pv_power" in aggregated
    fake_pub.set_online.assert_called()
    # Meta sensors:
    meta_topics = [c.args[0] for c in fake_pub.publish_value.call_args_list]
    assert "_meta/poll_duration_ms" in meta_topics
    assert "_meta/uptime_s" in meta_topics


def test_exception_response_does_not_kill_daemon(cfg):
    """If a block returns excode 0x02, daemon must continue with other blocks."""
    def query(slave, addr, count):
        # All blocks return illegal-data-address
        raise ModbusException(slave, 3, 0x02)
    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(cfg, serial_factory=serial_factory, publisher_factory=publisher_factory)
    # Should NOT raise
    d.run_one_hot_cycle()


def test_timeout_marks_inverter_offline_after_3_consecutive(cfg):
    """All blocks fail with TimeoutError -> after 3 cycles, offline status."""
    def query(slave, addr, count):
        raise TimeoutError("no reply")
    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(cfg, serial_factory=serial_factory, publisher_factory=publisher_factory)
    for _ in range(3):
        d.run_one_hot_cycle()
    # After 3 fully failed cycles per inverter, the daemon publishes an offline marker
    offline_calls = [
        c for c in fake_pub.publish_value.call_args_list
        if "status" in c.args[0] and c.args[1] == "offline"
    ]
    assert offline_calls, "expected at least one offline status publish after 3 cycles"


def test_mixed_success_one_inverter_offline(cfg):
    """If only one inverter responds, daemon publishes what it has + marks the other offline (after 3 cycles)."""
    call_count = [0]

    def query(slave, addr, count):
        if slave == 2:
            raise TimeoutError("inv2 dead")
        return _real_frame_for(addr, count, slave)

    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(cfg, serial_factory=serial_factory, publisher_factory=publisher_factory)
    for _ in range(3):
        d.run_one_hot_cycle()
    # inv1 should still publish data; inv2 marked offline
    pv_calls = fake_pub.publish_values.call_args_list
    assert pv_calls
    last_agg = pv_calls[-1].args[0]
    # Should have inv1 data
    assert "battery_state_of_charge_2" in last_agg
    # And offline for inv2
    offline = [
        c for c in fake_pub.publish_value.call_args_list
        if "inv2" in c.args[0] and c.args[1] == "offline"
    ]
    assert offline


def test_daemon_lifecycle_calls_connect_and_disconnect(cfg, monkeypatch):
    """Verify start() calls connect+discovery+set_online (but exits on KeyboardInterrupt)."""
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)

    def fake_loop_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Daemon, "_loop_forever", fake_loop_forever)

    d = Daemon(cfg, serial_factory=serial_factory, publisher_factory=publisher_factory)
    with pytest.raises(KeyboardInterrupt):
        d.start()
    fake_pub.connect.assert_called_once()
    fake_pub.publish_discovery.assert_called_once()
    fake_pub.set_online.assert_called_once()
    fake_pub.disconnect.assert_called_once()

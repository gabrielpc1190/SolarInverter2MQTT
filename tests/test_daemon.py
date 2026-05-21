"""Tests for the daemon main loop using injected fakes for serial + MQTT."""

import contextlib
import threading
import time
from unittest.mock import MagicMock

import pytest

from inverter_bridge.config import BridgeConfig, InverterCfg, MqttCfg, PollingCfg
from inverter_bridge.daemon import Daemon
from inverter_bridge.energy_integrator import EnergyIntegrator
from inverter_bridge.modbus import ModbusException, ModbusFrame


def _make_fake_integrator() -> MagicMock:
    """A MagicMock EnergyIntegrator whose `update()` returns the canonical
    set of energy keys with zero values, so the daemon can blindly merge."""
    fake = MagicMock(spec=EnergyIntegrator)
    fake.update.return_value = {
        "battery_energy_in": 0.0,
        "battery_energy_out": 0.0,
        "pv_energy": 0.0,
        "load_energy": 0.0,
        "grid_energy_in": 0.0,
        "grid_energy_out": 0.0,
    }
    return fake


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
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    d.run_one_hot_cycle()
    # Aggregator outputs come via publish_values, not publish_value:
    pv_calls = fake_pub.publish_values.call_args_list
    assert pv_calls, "expected at least one publish_values call (aggregator output)"
    aggregated = pv_calls[0].args[0]
    assert "battery_state_of_charge" in aggregated
    assert "battery_power" in aggregated
    assert "load_power" in aggregated
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
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    # Should NOT raise
    d.run_one_hot_cycle()


def test_timeout_marks_inverter_offline_after_3_consecutive(cfg):
    """All blocks fail with TimeoutError -> after 3 cycles, offline status."""
    def query(slave, addr, count):
        raise TimeoutError("no reply")
    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
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
    def query(slave, addr, count):
        if slave == 2:
            raise TimeoutError("inv2 dead")
        return _real_frame_for(addr, count, slave)

    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    for _ in range(3):
        d.run_one_hot_cycle()
    # inv1 should still publish data; inv2 marked offline
    pv_calls = fake_pub.publish_values.call_args_list
    assert pv_calls
    last_agg = pv_calls[-1].args[0]
    # Should have inv1 data
    assert "battery_state_of_charge" in last_agg
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

    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    with pytest.raises(KeyboardInterrupt):
        d.start()
    fake_pub.connect.assert_called_once()
    fake_pub.publish_discovery.assert_called_once()
    fake_pub.set_online.assert_called_once()
    fake_pub.disconnect.assert_called_once()


def test_daemon_publishes_energy_sensors(cfg, tmp_path):
    """F-2: after a few hot cycles, the publisher should receive calls for
    the 6 energy accumulator keys via publish_values."""
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    # Use a real EnergyIntegrator so the integration math is exercised.
    integrator = EnergyIntegrator(persist_path=tmp_path / "e.json")
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=integrator,
    )
    # Three cycles: first establishes the time baseline (elapsed=0), the
    # remaining two accumulate energy.
    for _ in range(3):
        d.run_one_hot_cycle()
    pv_calls = fake_pub.publish_values.call_args_list
    assert pv_calls, "expected at least one publish_values call"
    # The last cycle's aggregated payload should contain all 6 energy keys.
    last_payload = pv_calls[-1].args[0]
    for key in (
        "battery_energy_in",
        "battery_energy_out",
        "pv_energy",
        "load_energy",
        "grid_energy_in",
        "grid_energy_out",
    ):
        assert key in last_payload, f"missing energy key: {key}"
        assert isinstance(last_payload[key], (int, float))


def test_daemon_calls_integrator_save_on_cold_cycle(cfg):
    """Cold cycle should persist the energy accumulator (so a restart near
    the cycle boundary doesn't lose minutes of integration)."""
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    fake_integrator = _make_fake_integrator()
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=fake_integrator,
    )
    fake_integrator.save.assert_not_called()
    d.run_one_cold_cycle()
    fake_integrator.save.assert_called()


def test_daemon_saves_energy_state_on_clean_shutdown(cfg, monkeypatch):
    """start() must persist energy state in its `finally:` block so a clean
    SIGTERM doesn't lose the in-memory kWh totals."""
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    fake_integrator = _make_fake_integrator()

    def fake_loop_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Daemon, "_loop_forever", fake_loop_forever)

    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=fake_integrator,
    )
    with pytest.raises(KeyboardInterrupt):
        d.start()
    # save() must have been called in the finally: block before disconnect.
    fake_integrator.save.assert_called()


def test_daemon_passes_elapsed_seconds_to_integrator(cfg, monkeypatch):
    """The integrator's update() must receive a non-negative elapsed_s on
    the second hot cycle (first cycle is elapsed=0 by design)."""
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    fake_integrator = _make_fake_integrator()

    # Drive monotonic clock deterministically.
    fake_clock = iter(
        [100.0, 100.0, 100.1, 100.1,  # cycle 1: cycle_start, now, set_online?, meta
         103.2, 103.2, 103.3, 103.3,  # cycle 2
         106.5, 106.5, 106.6, 106.6]  # cycle 3
    )

    def next_clock():
        try:
            return next(fake_clock)
        except StopIteration:
            return 999.0

    monkeypatch.setattr("inverter_bridge.daemon.time.monotonic", next_clock)

    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=fake_integrator,
    )
    d.run_one_hot_cycle()
    d.run_one_hot_cycle()

    # First call to update() should be elapsed_s = 0 (no prior baseline).
    # Second call should be > 0.
    assert fake_integrator.update.call_count >= 2
    first_kwargs = fake_integrator.update.call_args_list[0].kwargs
    second_kwargs = fake_integrator.update.call_args_list[1].kwargs
    assert first_kwargs.get("elapsed_s") == 0.0
    assert second_kwargs.get("elapsed_s", 0) > 0


# ---------- F-5: parallel per-inverter polling ----------


def test_hot_cycle_polls_in_parallel(cfg):
    """Two inverters polled concurrently: total wall time ≈ max(per-inv), not sum.

    Each query() blocks for ~0.5s. With 3 hot blocks/inverter, sequential
    would be ~3.0s (2 inv * 3 blocks * 0.5s). Parallel should be ~1.5s
    (3 blocks * 0.5s on the slower of two concurrent inverters).
    """
    per_query_delay = 0.5

    def slow_query(slave, addr, count):
        time.sleep(per_query_delay)
        return _real_frame_for(addr, count, slave)

    serial_factory = _make_serial_factory(slow_query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    t0 = time.monotonic()
    d.run_one_hot_cycle()
    elapsed = time.monotonic() - t0
    # Sequential would be 2 inverters * 3 hot blocks * 0.5s = 3.0s.
    # Parallel must be well under 3.0s (~1.5-1.7s typical). 2.5s gives a
    # generous threshold that catches a regression to sequential while
    # tolerating CI jitter.
    assert elapsed < 2.5, (
        f"hot cycle took {elapsed:.2f}s — looks sequential "
        f"(expected ~1.5s parallel, sequential would be ~3.0s)"
    )


def test_hot_cycle_thread_safety_concurrent_fails(cfg):
    """Both inverters fail simultaneously: fail_count must not double-count.

    Three cycles where every block on every inverter raises TimeoutError.
    After three cycles, each inverter's fail count must be exactly 3
    (not e.g. 6 from a race that increments both threads' counts twice).
    """
    barrier = threading.Barrier(len(cfg.inverters))

    def failing_query(slave, addr, count):
        # First block of each cycle: sync the two threads so they hit the
        # fail-count increment as close in time as possible, exposing any race.
        if addr == 0x0100:
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=2.0)
        raise TimeoutError("simulated bus failure")

    serial_factory = _make_serial_factory(failing_query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    for _ in range(3):
        barrier.reset()
        d.run_one_hot_cycle()
    # Each inverter saw 3 fully-failed cycles. No double-counting from races.
    assert d._fail_count["inv1"] == 3, d._fail_count
    assert d._fail_count["inv2"] == 3, d._fail_count


def test_executor_shutdown_on_disconnect(cfg, monkeypatch):
    """After Daemon.start() exits, the thread pool executor must be shut down.

    Guards against thread leaks on clean shutdown.
    """
    serial_factory = _make_serial_factory(
        lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)

    def fake_loop_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Daemon, "_loop_forever", fake_loop_forever)

    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    with pytest.raises(KeyboardInterrupt):
        d.start()
    # ThreadPoolExecutor exposes `_shutdown` (bool) set True by shutdown().
    assert d._executor._shutdown is True, "executor was not shut down by start()"


def test_per_inverter_timeout_doesnt_block_other_inverter(cfg, monkeypatch):
    """If one inverter's worker hangs, the cycle must complete using the
    other inverter's data within ~the worker timeout, not the hang duration."""
    # Force a short worker-level timeout so the test runs quickly.
    monkeypatch.setattr(Daemon, "_poll_worker_timeout_s", 1.0, raising=False)
    hang_event = threading.Event()

    def query(slave, addr, count):
        if slave == 1:
            # inv1 hangs forever (cancelled at process exit by daemon thread).
            hang_event.wait(timeout=20.0)
            raise TimeoutError("would have hung")
        return _real_frame_for(addr, count, slave)

    serial_factory = _make_serial_factory(query)
    fake_pub = MagicMock()
    publisher_factory = MagicMock(return_value=fake_pub)
    d = Daemon(
        cfg,
        serial_factory=serial_factory,
        publisher_factory=publisher_factory,
        integrator=_make_fake_integrator(),
    )
    # Patch the daemon's runtime worker timeout AFTER construction (since
    # __init__ computes it from cfg).
    d._poll_worker_timeout_s = 1.0
    try:
        t0 = time.monotonic()
        d.run_one_hot_cycle()
        elapsed = time.monotonic() - t0
        # Should complete within ~timeout + small overhead; well under the 20s hang.
        assert elapsed < 3.0, (
            f"cycle took {elapsed:.2f}s — the inv1 hang blocked inv2"
        )
        # inv2 data must still be present in the published aggregated payload.
        pv_calls = fake_pub.publish_values.call_args_list
        assert pv_calls
        aggregated = pv_calls[-1].args[0]
        assert "battery_state_of_charge" in aggregated
        # inv1 counted as a failed cycle (its fail_count incremented).
        assert d._fail_count["inv1"] >= 1
        # inv2 succeeded, so its fail count stays 0.
        assert d._fail_count["inv2"] == 0
    finally:
        # Wake the hanging worker so it doesn't outlive the test.
        hang_event.set()

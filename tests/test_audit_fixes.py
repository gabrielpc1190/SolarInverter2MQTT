"""Regression tests for the 2026-07-04 audit findings.

Each test is tagged with the finding ID from
docs/2026-07-04_revision_codigo-y-host.html (A1, A2, M3, M5, M7, M8, B8, B9…).
Written test-first: every test here failed before its fix landed.
"""

import asyncio
import json
import signal
import socket
from unittest.mock import MagicMock

import pytest

from inverter_bridge.bms.octopus_protocol import DecodeError
from inverter_bridge.bms.service import BmsService
from inverter_bridge.config import (
    BmsCfg,
    BridgeConfig,
    InverterCfg,
    MqttCfg,
    PollingCfg,
    load_config,
)
from inverter_bridge.crc import crc16
from inverter_bridge.daemon import Daemon
from inverter_bridge.energy_integrator import EnergyIntegrator
from inverter_bridge.mqtt_publisher import MqttPublisher
from inverter_bridge.serial_io import SerialPort, parse_frame_stream

# ───────────────────────── helpers ─────────────────────────


def _resp_frame(slave: int, regs: list[int]) -> bytes:
    """Build a valid read-holding response frame with correct CRC."""
    body = b"".join(r.to_bytes(2, "big") for r in regs)
    core = bytes([slave, 0x03, len(body)]) + body
    return core + crc16(core)


def _fake_integrator() -> MagicMock:
    fake = MagicMock(spec=EnergyIntegrator)
    fake.update.return_value = {}
    return fake


@pytest.fixture
def bridge_cfg():
    return BridgeConfig(
        inverters=[InverterCfg(name="inv1", port="/dev/ttyUSB1", slave=1)],
        mqtt=MqttCfg(host="x", username="u", password="p"),
        polling=PollingCfg(
            hot_interval_s=0.01,
            inter_query_delay_s=0.0,
            retry_attempts=1,
            retry_backoff_s=0.0,
        ),
    )


@pytest.fixture
def mqtt_cfg():
    return MqttCfg(host="broker.local", username="u", password="p")


# ───────────────────────── A1: SIGTERM → clean shutdown ─────────────────────────


@pytest.fixture
def _restore_sigterm():
    old = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, old)


def test_sigterm_raises_keyboardinterrupt(_restore_sigterm):
    """systemd stops the daemon with SIGTERM. The handler must translate it to
    KeyboardInterrupt so Daemon.start()'s finally block (persist energy, stop
    BMS, disconnect MQTT) actually runs in production."""
    from inverter_bridge.__main__ import install_signal_handlers

    install_signal_handlers()
    with pytest.raises(KeyboardInterrupt):
        signal.raise_signal(signal.SIGTERM)


def test_bms_stop_saves_energy_state(mqtt_cfg, tmp_path):
    """A1: BmsService.stop() must persist the Wh accumulators — without this,
    every daemon restart throws away the energy integrated since the last
    periodic save."""
    svc = BmsService(BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D"), mqtt_cfg)
    svc._mqtt = MagicMock()
    svc._energy_persist = tmp_path / "bms-energy.json"
    svc._energy_in_Wh = 123.5
    svc._energy_out_Wh = 45.25
    svc._thread = MagicMock()  # simulate a running service thread

    svc.stop()

    data = json.loads((tmp_path / "bms-energy.json").read_text())
    assert data["energy_in_Wh"] == 123.5
    assert data["energy_out_Wh"] == 45.25


# ───────────────────────── A2: DecodeError must not tear down BLE ─────────────────────────


def test_poll_loop_survives_decode_error(mqtt_cfg):
    """A2: a corrupted BLE frame (CRC mismatch → DecodeError) is an EXPECTED
    event under RF noise. It must count as a failed poll — not escape the poll
    loop and force a full BLE disconnect/reconnect."""
    cfg = BmsCfg(
        enabled=True,
        master_mac="C0:D6:3C:52:0F:0D",
        pack_count=1,
        inter_pack_delay_s=0.0,
        poll_fast_interval_s=0.0,
        max_failed_cycles=2,
    )
    svc = BmsService(cfg, mqtt_cfg)
    svc._mqtt = MagicMock()

    client = MagicMock()
    client.is_connected = True

    async def bad_poll(pack):
        raise DecodeError("crc mismatch")

    client.poll_pia = bad_poll
    client.poll_pib = bad_poll

    async def run():
        svc._stop_event = asyncio.Event()
        # Must RETURN cleanly after max_failed_cycles (link-zombie path),
        # never propagate DecodeError.
        await asyncio.wait_for(svc._poll_loop(client), timeout=5.0)

    asyncio.run(run())


# ───────────────────────── M5: time-based energy persistence ─────────────────────────


def test_bms_energy_persist_is_time_based(mqtt_cfg, tmp_path, monkeypatch):
    """M5: persistence must trigger on 'more than 30s since the last save',
    not on the lottery of int(monotonic) % 30 == 0."""
    svc = BmsService(BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D"), mqtt_cfg)
    svc._mqtt = MagicMock()
    svc._energy_persist = tmp_path / "e.json"

    # Freeze the clock at a value where the old modulo trick does NOT fire
    # (int(1000.5) % 30 == 10) but "31.5s since last save" must.
    monkeypatch.setattr("inverter_bridge.bms.service.time.monotonic", lambda: 1000.5)
    svc._last_energy_sample_t = 995.0
    svc._last_energy_save_t = 969.0

    agg = MagicMock(power_charging_W=100.0, power_discharging_W=0.0)
    svc._integrate_energy(agg)
    assert (tmp_path / "e.json").exists(), "31.5s since last save → must persist"

    # Immediately again (0s since the save above): must NOT persist.
    (tmp_path / "e.json").unlink()
    svc._integrate_energy(agg)
    assert not (tmp_path / "e.json").exists()


# ───────────────────────── M3: honest CRC-failure counter ─────────────────────────


def test_parse_frame_stream_reports_crc_errors():
    """M3: a plausible response frame whose CRC check fails must be reported
    via the on_crc_error callback (it used to be dropped silently)."""
    good = _resp_frame(1, [0x1234])
    corrupted = bytearray(good)
    corrupted[3] ^= 0xFF  # flip a payload byte → CRC mismatch
    hits: list[int] = []

    frames = parse_frame_stream(bytes(corrupted), on_crc_error=lambda: hits.append(1))

    assert frames == []
    assert len(hits) == 1


class _FakeSerial:
    """Stands in for serial.Serial: replays a canned RX buffer."""

    rx = b""
    kwargs: dict | None = None

    def __init__(self, *args, **kwargs):
        type(self).kwargs = kwargs
        self._buf = type(self).rx

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data):
        pass

    def flush(self):
        pass

    def read(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def close(self):
        pass


@pytest.fixture
def fake_serial(monkeypatch):
    monkeypatch.setattr("inverter_bridge.serial_io.serial.Serial", _FakeSerial)
    return _FakeSerial


def test_query_returns_matching_frame_skipping_chatter(fake_serial):
    """Coverage gap: SerialPort.query() happy path — LCD chatter (same slave,
    different register count) must be skipped, our response returned."""
    chatter = _resp_frame(1, [1, 2])  # count=2 ≠ ours
    ours = _resp_frame(1, [0x0042])  # count=1
    fake_serial.rx = chatter + ours

    port = SerialPort(device="/dev/null", timeout_s=0.2)
    frame = port.query(slave=1, addr=0x0100, count=1)

    assert frame.regs == [0x0042]


def test_query_counts_crc_errors_and_times_out(fake_serial):
    """M3: a corrupted response in the buffer increments the CRC counter via
    the port's on_crc_error callback, and query raises TimeoutError."""
    good = _resp_frame(1, [0x0042])
    corrupted = bytearray(good)
    corrupted[3] ^= 0xFF
    fake_serial.rx = bytes(corrupted)
    hits: list[int] = []

    port = SerialPort(device="/dev/null", timeout_s=0.2, on_crc_error=lambda: hits.append(1))
    with pytest.raises(TimeoutError):
        port.query(slave=1, addr=0x0100, count=1)

    assert len(hits) == 1


def test_query_opens_port_with_write_timeout(fake_serial):
    """M1-min: without write_timeout a wedged USB-serial driver can block
    s.write()/s.flush() forever (reads have a timeout; writes didn't)."""
    fake_serial.rx = _resp_frame(1, [0x0042])
    port = SerialPort(device="/dev/null", timeout_s=1.5)
    port.query(slave=1, addr=0x0100, count=1)

    assert fake_serial.kwargs is not None
    assert fake_serial.kwargs.get("write_timeout") == 1.5


def test_daemon_wires_crc_callback_into_serial_ports(bridge_cfg):
    """M3: the daemon must hand its CRC counter to each SerialPort so
    _meta/crc_fails_total counts real CRC failures."""
    captured: dict = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    d = Daemon(
        bridge_cfg,
        serial_factory=factory,
        publisher_factory=MagicMock(),
        integrator=_fake_integrator(),
        energy_persist_path=None,
    )
    cb = captured.get("on_crc_error")
    assert callable(cb)
    cb()
    assert d._crc_fails_total == 1


# ───────────────────────── M7: inverter publisher reconnect visibility ─────────────────────────


@pytest.fixture
def mock_paho_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("paho.mqtt.client.Client", MagicMock(return_value=client))
    return client


def test_publisher_wires_connect_and_disconnect_handlers(mqtt_cfg, mock_paho_client):
    """M7: the inverter-side publisher had no on_connect/on_disconnect at all —
    a prolonged broker outage left zero trace in the journal."""
    pub = MqttPublisher(mqtt_cfg)
    pub.connect()
    assert mock_paho_client.on_connect == pub._handle_connect
    assert mock_paho_client.on_disconnect == pub._handle_disconnect


def test_publisher_reconnect_republishes_discovery_and_online(mqtt_cfg, mock_paho_client):
    """M7: on (re)connect the publisher must republish discovery + availability,
    mirroring what the BMS side already does."""
    pub = MqttPublisher(mqtt_cfg)
    pub.connect()
    mock_paho_client.reset_mock()

    pub._handle_connect(mock_paho_client, None, None, 0)

    topics = [c.args[0] for c in mock_paho_client.publish.call_args_list if c.args]
    assert f"{mqtt_cfg.topic_prefix}/availability" in topics
    assert any(t.startswith(f"{mqtt_cfg.discovery_prefix}/") for t in topics)


# ───────────────────────── M8 / B8: config validation ─────────────────────────


_MINIMAL_YAML = """
inverters:
  - {{name: inv1, port: /dev/ttyUSB1, slave: 1}}
mqtt:
  host: broker
  username: u
  password_file: {pw_file}
{extra}
"""


def _write_cfg(tmp_path, pw_file: str, extra: str = ""):
    p = tmp_path / "config.yaml"
    p.write_text(_MINIMAL_YAML.format(pw_file=pw_file, extra=extra))
    return p


def test_missing_password_file_raises(tmp_path):
    """M8: a typo'd/missing password_file used to silently degrade to an empty
    password — the failure then surfaced as an MQTT auth error far from the
    cause. It must fail loudly at load time."""
    cfg_path = _write_cfg(tmp_path, pw_file=str(tmp_path / "does-not-exist"))
    with pytest.raises(ValueError, match="password_file"):
        load_config(cfg_path)


def test_absent_password_file_key_raises(tmp_path):
    """M8: a missing mqtt.password_file key must be a clear config error, not
    a bare KeyError."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "inverters:\n  - {name: inv1, port: /dev/ttyUSB1, slave: 1}\n"
        "mqtt:\n  host: broker\n  username: u\n"
    )
    with pytest.raises(ValueError, match="password_file"):
        load_config(p)


def test_bms_pack_count_capped_at_4(tmp_path):
    """B8: config accepted pack_count up to 16 but the BLE client rejects >4
    with a ValueError that would loop the reconnect forever. Align at load."""
    pw = tmp_path / "pw"
    pw.write_text("secret")
    cfg_path = _write_cfg(
        tmp_path,
        pw_file=str(pw),
        extra='bms: {enabled: true, master_mac: "C0:D6:3C:52:0F:0D", pack_count: 5}',
    )
    with pytest.raises(ValueError, match="pack_count"):
        load_config(cfg_path)


# ───────────────────────── B9: persistence durability ─────────────────────────


def test_bms_save_fsyncs_before_rename(mqtt_cfg, tmp_path, monkeypatch):
    """B9: the BMS energy save had no fsync — a power cut right after the
    rename could leave a stale file. Must fsync like the inverter-side does."""
    synced: list[int] = []
    monkeypatch.setattr(
        "inverter_bridge.bms.service.os.fsync", lambda fd: synced.append(fd)
    )
    svc = BmsService(BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D"), mqtt_cfg)
    svc._energy_persist = tmp_path / "e.json"

    svc._save_energy_state()

    assert synced, "fsync must run before the atomic rename"
    assert json.loads((tmp_path / "e.json").read_text())["energy_in_Wh"] == 0.0
    assert list(tmp_path.glob("*.tmp")) == []


def test_integrator_save_cleans_orphan_tmp_on_failure(tmp_path, monkeypatch):
    """B9: if the write fails midway (e.g. disk full at fsync), the unique-name
    tmp file must not be left behind to accumulate in /var/lib."""

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr("inverter_bridge.energy_integrator.os.fsync", boom)
    integ = EnergyIntegrator(persist_path=tmp_path / "energy.json")

    integ.save()  # must not raise

    assert list(tmp_path.glob("*.tmp")) == []
    assert not (tmp_path / "energy.json").exists()


# ───────────────────────── Watchdog: sd_notify ─────────────────────────


def test_sd_notify_sends_to_socket(tmp_path, monkeypatch):
    sock_path = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(sock_path))
    srv.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))

    from inverter_bridge.sdnotify import sd_notify

    sd_notify("READY=1")
    assert srv.recv(64) == b"READY=1"
    srv.close()


def test_sd_notify_is_noop_without_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    from inverter_bridge.sdnotify import sd_notify

    sd_notify("READY=1")  # must not raise


def test_daemon_start_sends_ready(bridge_cfg, monkeypatch):
    pings: list[str] = []
    monkeypatch.setattr("inverter_bridge.daemon.sd_notify", lambda m: pings.append(m))

    def fake_loop(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Daemon, "_loop_forever", fake_loop)
    d = Daemon(
        bridge_cfg,
        serial_factory=MagicMock(),
        publisher_factory=MagicMock(),
        integrator=_fake_integrator(),
        energy_persist_path=None,
    )
    with pytest.raises(KeyboardInterrupt):
        d.start()
    assert "READY=1" in pings


def test_daemon_loop_sends_watchdog_ping(bridge_cfg, monkeypatch):
    pings: list[str] = []
    monkeypatch.setattr("inverter_bridge.daemon.sd_notify", lambda m: pings.append(m))

    def fake_cycle(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(Daemon, "run_one_hot_cycle", fake_cycle)
    d = Daemon(
        bridge_cfg,
        serial_factory=MagicMock(),
        publisher_factory=MagicMock(),
        integrator=_fake_integrator(),
        energy_persist_path=None,
    )
    with pytest.raises(KeyboardInterrupt):
        d._loop_forever()
    assert "WATCHDOG=1" in pings


# ═════════════════ Ronda 2 (backlog de la auditoría) ═════════════════


def _pia(pack: int, current_A: float = 1.0):
    from inverter_bridge.bms.octopus_protocol import PiaData

    return PiaData(
        pack=pack,
        voltage_V=53.0,
        current_A=current_A,
        remaining_Ah=100.0,
        nominal_Ah=100.0,
        soc_pct=50.0,
        soh_pct=100.0,
        cycles=10,
    )


# ───── M6: packs congelados no siguen sumando ─────


def test_fresh_states_excludes_frozen_packs(mqtt_cfg, monkeypatch):
    """M6: a pack that stopped responding must drop out of the bank aggregates
    (and the energy integration) instead of contributing its last frozen
    current/voltage forever."""
    svc = BmsService(BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D"), mqtt_cfg)
    monkeypatch.setattr("inverter_bridge.bms.service.time.monotonic", lambda: 1000.0)
    svc._pia_state = {1: _pia(1), 2: _pia(2)}
    svc._pia_seen_t = {1: 995.0, 2: 900.0}  # pack2: 100 s sin responder (> 60 default)

    fresh_pia, _fresh_pib = svc._fresh_states()

    assert set(fresh_pia) == {1}


def test_fresh_states_readmits_recovered_pack(mqtt_cfg, monkeypatch):
    svc = BmsService(BmsCfg(enabled=True, master_mac="C0:D6:3C:52:0F:0D"), mqtt_cfg)
    monkeypatch.setattr("inverter_bridge.bms.service.time.monotonic", lambda: 1000.0)
    svc._pia_state = {2: _pia(2)}
    svc._pia_seen_t = {2: 999.0}  # fresco otra vez

    fresh_pia, _ = svc._fresh_states()

    assert set(fresh_pia) == {2}


# ───── M4: marcador online/offline por inversor, completo ─────


@pytest.fixture
def bridge_cfg2():
    return BridgeConfig(
        inverters=[
            InverterCfg(name="inv1", port="/dev/ttyUSB1", slave=1),
            InverterCfg(name="inv2", port="/dev/ttyUSB0", slave=2),
        ],
        mqtt=MqttCfg(host="x", username="u", password="p"),
        polling=PollingCfg(
            hot_interval_s=0.01,
            inter_query_delay_s=0.0,
            retry_attempts=1,
            retry_backoff_s=0.0,
        ),
    )


def _daemon_with_query(cfg, query_fn):
    def factory(**kwargs):
        m = MagicMock()
        m.query = MagicMock(side_effect=query_fn)
        return m

    pub = MagicMock()
    d = Daemon(
        cfg,
        serial_factory=factory,
        publisher_factory=MagicMock(return_value=pub),
        integrator=_fake_integrator(),
        energy_persist_path=None,
    )
    return d, pub


def _status_calls(pub):
    return [
        (c.args[0], c.args[1])
        for c in pub.publish_value.call_args_list
        if c.args and "status" in str(c.args[0])
    ]


def test_status_online_published_with_index_naming(bridge_cfg2):
    """M4: healthy polling must publish 'online' (recovery/steady marker) and
    the key must follow the inverter_<N>_ convention (it used inverter_<name>_
    before, which broke topic derivation)."""
    from tests.test_daemon import _real_frame_for

    def query(slave, addr, count):
        return _real_frame_for(addr, count, slave)

    d, pub = _daemon_with_query(bridge_cfg2, query)
    d.run_one_hot_cycle()

    calls = _status_calls(pub)
    assert ("inverter_1_status", "online") in calls
    assert ("inverter_2_status", "online") in calls
    d._executor.shutdown(wait=True)


def test_status_offline_after_3_failed_cycles_index_naming(bridge_cfg2):
    def bad_query(slave, addr, count):
        raise TimeoutError("no response")

    d, pub = _daemon_with_query(bridge_cfg2, bad_query)
    for _ in range(3):
        d.run_one_hot_cycle()

    calls = _status_calls(pub)
    assert ("inverter_1_status", "offline") in calls
    assert ("inverter_2_status", "offline") in calls
    assert all("inv1" not in k and "inv2" not in k for k, _ in calls)
    d._executor.shutdown(wait=True)


def test_status_has_discovery():
    from inverter_bridge.mqtt_publisher import PER_INVERTER_SENSORS

    assert "status" in PER_INVERTER_SENSORS


# ───── B2: códigos de falla del inversor publicados ─────


def test_aggregator_publishes_fault_bits():
    """B2: the faults block (0x0204) was polled and thrown away. Publish the
    raw registers as hex so HA can alert on any non-zero fault bit."""
    from inverter_bridge.aggregator import aggregate_inverters
    from inverter_bridge.parsers import ParsedBlock

    faults = ParsedBlock(
        block_addr=0x0204,
        block_name="faults",
        slave=1,
        regs_raw=(0x0000, 0x0004, 0x0000, 0x0000, 0x0000, 0x0000),
        fields={},
    )
    out = aggregate_inverters([{"faults": faults}])

    assert out["inverter_1_fault_bits"] == "0000 0004 0000 0000 0000 0000"


def test_fault_bits_has_discovery():
    from inverter_bridge.mqtt_publisher import PER_INVERTER_SENSORS

    assert "fault_bits" in PER_INVERTER_SENSORS


def test_dead_cold_blocks_pruned():
    """B2: fw_build_date/fw_model/fw_serial/config/thresholds were polled every
    60 s (with retries, on a 16.7%-fail bus) and nobody consumed them."""
    from inverter_bridge.srne_map import BLOCKS

    names = {b.name for b in BLOCKS}
    assert not names & {"fw_build_date", "fw_model", "fw_serial", "config", "thresholds"}
    assert "faults" in names  # este sí quedó cableado


# ───── M1 completo: worker colgado no se re-encola ─────


def test_wedged_worker_not_resubmitted(bridge_cfg2):
    """M1: if inv1's poll worker is still stuck from the previous cycle, the
    daemon must NOT queue another query for the same port (two concurrent
    queries on one tty = garbage) and inv2 must keep being polled."""
    import threading

    from tests.test_daemon import _real_frame_for

    release = threading.Event()
    calls_lock = threading.Lock()
    calls = {"inv1": 0, "inv2": 0}

    def query(slave, addr, count):
        with calls_lock:
            calls["inv1" if slave == 1 else "inv2"] += 1
        if slave == 1:
            release.wait(timeout=10)  # inv1 wedged
            raise TimeoutError("wedged")
        return _real_frame_for(addr, count, slave)

    d, _pub = _daemon_with_query(bridge_cfg2, query)
    d._poll_worker_timeout_s = 0.05
    try:
        d.run_one_hot_cycle()
        d.run_one_hot_cycle()
        with calls_lock:
            assert calls["inv1"] == 1, "wedged inv1 must not be re-queued"
            assert calls["inv2"] >= 2, "healthy inv2 must not be starved"
    finally:
        release.set()
        d._executor.shutdown(wait=True)


# ───── B4 / B7: metadatos y discovery faltantes ─────


def test_meta_per_inverter_duration_is_json(bridge_cfg2):
    from tests.test_daemon import _real_frame_for

    d, pub = _daemon_with_query(
        bridge_cfg2, lambda slave, addr, count: _real_frame_for(addr, count, slave)
    )
    d.run_one_hot_cycle()
    payloads = [
        c.args[1]
        for c in pub.publish_value.call_args_list
        if c.args and c.args[0] == "_meta/poll_duration_per_inverter_ms"
    ]
    assert payloads
    parsed = json.loads(payloads[0])  # repr() de dict Python NO es JSON
    assert set(parsed) == {"inv1", "inv2"}
    d._executor.shutdown(wait=True)


def test_apparent_power_legs_have_discovery():
    from inverter_bridge.mqtt_publisher import PER_INVERTER_SENSORS

    assert "load_apparent_power_l1" in PER_INVERTER_SENSORS
    assert "load_apparent_power_l2" in PER_INVERTER_SENSORS


# ═════════════════ M2: matching por dirección (anti-chatter del LCD) ═════════════════
#
# Captura real del bus (2026-07-04, tools/capture_fixtures.py) confirmó que en el
# cable compartido cada RESPUESTA viene precedida por su REQUEST (que sí lleva la
# dirección), y que NUESTRA propia request NO hace eco en el RX (nuestra respuesta
# queda "huérfana", sin request delante). El fix: rechazar una respuesta del mismo
# tamaño cuya request-previa sea de OTRA dirección (chatter del LCD a otro bloque),
# y aceptar solo la de nuestra dirección o la huérfana (la nuestra).


def _req_frame(slave: int, addr: int, count: int) -> bytes:
    core = bytes([slave, 0x03, (addr >> 8) & 0xFF, addr & 0xFF,
                  (count >> 8) & 0xFF, count & 0xFF])
    return core + crc16(core)


def test_query_rejects_same_count_response_for_other_address(fake_serial):
    """M2: the LCD reads a DIFFERENT block with the same register count right
    before our real response lands. Matching by count alone (old behavior)
    would grab the decoy; address-aware matching must skip it and return OUR
    orphan response instead. (count=6 so decoy + ours fit the read window.)"""
    decoy = _req_frame(1, 0x0300, 6) + _resp_frame(1, [0xDEAD] * 6)
    ours = _resp_frame(1, list(range(0x1000, 0x1006)))  # orphan (our req doesn't echo)
    fake_serial.rx = decoy + ours

    port = SerialPort(device="/dev/null", timeout_s=0.2)
    frame = port.query(slave=1, addr=0x0204, count=6)

    assert frame.regs[0] == 0x1000, "must return OUR block, not the same-count decoy"
    assert 0xDEAD not in frame.regs


def test_query_pairs_response_to_its_preceding_request(fake_serial):
    """M2: when the LCD reads OUR exact address, the response paired to that
    request is valid data for us — return it (same address = same data)."""
    paired = _req_frame(1, 0x0204, 6) + _resp_frame(1, list(range(0x2000, 0x2006)))
    other = _req_frame(1, 0x0300, 6) + _resp_frame(1, [0xBEEF] * 6)
    fake_serial.rx = paired + other  # paired first so it's within the read window

    port = SerialPort(device="/dev/null", timeout_s=0.2)
    frame = port.query(slave=1, addr=0x0204, count=6)

    assert frame.regs[0] == 0x2000
    assert 0xBEEF not in frame.regs


def test_query_still_returns_orphan_when_lcd_silent(fake_serial):
    """No regression: for a block the LCD never polls (e.g. 0xF000), only our
    own orphan response exists — it must still be returned."""
    ours = _resp_frame(1, list(range(8)))
    fake_serial.rx = ours

    port = SerialPort(device="/dev/null", timeout_s=0.2)
    frame = port.query(slave=1, addr=0xF000, count=8)

    assert frame.regs == list(range(8))


def test_real_bus_capture_pairs_addresses():
    """Golden: on the REAL captured bus stream, address-aware pairing correctly
    associates the battery block (0x0100 → 15 regs) and state block (0x0210 →
    19 regs) with their preceding LCD requests — proving the pairing works
    against genuine chatter, not just synthetic frames. The passive capture is
    pure LCD traffic (no orphan of ours), so we assert the pairing directly."""
    import pathlib

    from inverter_bridge.serial_io import responses_with_context

    cap = (pathlib.Path(__file__).parent / "fixtures"
           / "bus_capture_25s_slave01_m2_20260704.hex").read_text().strip()
    paired = responses_with_context(bytes.fromhex(cap))

    by_addr = {ctx: len(f.regs) for f, ctx in paired if ctx is not None}
    assert by_addr.get(0x0100) == 15, "battery block paired to a 15-reg response"
    assert by_addr.get(0x0210) == 19, "state block paired to a 19-reg response"
    # Every paired response's reg-count matches a plausible LCD read; none of
    # our block addresses got mis-paired to the wrong size.
    assert by_addr.get(0x0223) == 23

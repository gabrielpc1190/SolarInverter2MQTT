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

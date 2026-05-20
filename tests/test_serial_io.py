"""Tests for the bus-noise-tolerant Modbus frame stream parser."""

from pathlib import Path

from inverter_bridge.serial_io import parse_frame_stream

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_empty_stream():
    assert parse_frame_stream(b"") == []


def test_parse_single_valid_response():
    """Slave 1, fc 3, bc 8 (4 regs), valid CRC."""
    frame = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    result = parse_frame_stream(frame)
    assert len(result) == 1
    assert result[0].slave == 0x01
    assert result[0].regs == [0x002c, 0x0208, 0x00bb, 0x0000]


def test_parse_skips_exception_frames():
    """Exception frames are not "successful responses" — parser should skip them.

    But it should still consume them and continue, not get stuck.
    """
    exc = bytes.fromhex("01 83 02 c0 f1".replace(" ", ""))  # exception
    valid = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    combined = exc + valid
    result = parse_frame_stream(combined)
    assert len(result) == 1
    assert result[0].slave == 0x01


def test_parse_stream_with_garbage_between_frames():
    """Garbage bytes between valid frames should be skipped, not break parsing."""
    valid = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    garbage = bytes.fromhex("ff ff de ad be ef".replace(" ", ""))
    combined = garbage + valid + garbage + valid + garbage
    result = parse_frame_stream(combined)
    assert len(result) == 2


def test_parse_stream_skips_request_frames():
    """Modbus REQUEST frames (8 bytes) should be consumed but not emitted as responses."""
    # Captured request frame from inv1's LCD bus
    req = bytes.fromhex("01 03 02 10 00 13 04 7a".replace(" ", ""))
    resp = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    combined = req + resp
    result = parse_frame_stream(combined)
    assert len(result) == 1
    assert result[0].slave == 0x01


def test_parse_real_60s_capture_slave02():
    """Feed real captured 60s stream from inv2 (LCD-internal master polling)."""
    raw = bytes.fromhex(
        (FIXTURES / "bus_capture_60s_slave02_day_pv_active.hex").read_text().strip()
    )
    result = parse_frame_stream(raw)
    # The capture contains both master-query frames AND slave responses.
    # We expect parse_frame_stream to yield only response frames (with regs).
    # At minimum it should NOT crash and should find SOME valid responses.
    assert len(result) >= 1
    assert all(f.slave == 0x02 for f in result)
    assert all(len(f.regs) >= 1 for f in result)


def test_parse_real_60s_capture_slave01_has_traffic():
    """The slave 0x01 capture is busier (LCD polls more actively)."""
    raw = bytes.fromhex(
        (FIXTURES / "bus_capture_60s_slave01_day_pv_active.hex").read_text().strip()
    )
    result = parse_frame_stream(raw)
    # Should be at least some traffic on inv1 bus
    assert len(result) >= 1
    assert all(f.slave == 0x01 for f in result)

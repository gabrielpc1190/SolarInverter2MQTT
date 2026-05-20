"""Tests for Modbus RTU request/response framing (fc 0x03 read holding registers)."""

import pytest

from inverter_bridge.modbus import (
    ModbusException,
    ModbusFrame,
    build_read_holding_request,
    parse_response,
)


def test_build_read_request_slave1_block_0100_count_15():
    """Verify byte-for-byte request frame matches what SA's LCD sends."""
    frame = build_read_holding_request(slave=0x01, addr=0x0100, count=15)
    # slave=01 fc=03 addr_hi=01 addr_lo=00 count_hi=00 count_lo=0F + 2 CRC bytes
    assert frame[:6] == bytes([0x01, 0x03, 0x01, 0x00, 0x00, 0x0F])
    assert len(frame) == 8


def test_build_read_request_slave2_block_0210_count_19():
    frame = build_read_holding_request(slave=0x02, addr=0x0210, count=19)
    assert frame[:6] == bytes([0x02, 0x03, 0x02, 0x10, 0x00, 0x13])
    assert len(frame) == 8


def test_build_read_request_invalid_count_raises():
    with pytest.raises(ValueError, match="count"):
        build_read_holding_request(slave=0x01, addr=0x0100, count=0)
    with pytest.raises(ValueError, match="count"):
        build_read_holding_request(slave=0x01, addr=0x0100, count=200)


def test_build_read_request_invalid_addr_raises():
    with pytest.raises(ValueError, match="addr"):
        build_read_holding_request(slave=0x01, addr=-1, count=1)
    with pytest.raises(ValueError, match="addr"):
        build_read_holding_request(slave=0x01, addr=0x10000, count=1)


def test_parse_normal_response_real_capture():
    """Captured response, slave 1, addr 0x0100 count=4. 13 bytes total."""
    # Frame: slave(1) fc(1) bc(1) + 8 bytes data + crc(2) = 13 bytes
    raw = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    result = parse_response(raw, expected_slave=0x01, expected_count=4)
    assert isinstance(result, ModbusFrame)
    assert result.slave == 0x01
    assert result.regs == [0x002c, 0x0208, 0x00bb, 0x0000]


def test_parse_exception_illegal_address():
    """Captured exception: slave=0x01 fc=0x83 excode=0x02 (illegal-data-address)."""
    raw = bytes.fromhex("018302c0f1")
    with pytest.raises(ModbusException) as exc_info:
        parse_response(raw, expected_slave=0x01, expected_count=4)
    assert exc_info.value.excode == 0x02
    assert exc_info.value.slave == 0x01
    assert exc_info.value.func == 0x03


def test_parse_response_bad_crc_raises():
    """Tamper last byte of a valid frame -> CRC mismatch."""
    raw = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 ff".replace(" ", ""))
    with pytest.raises(ValueError, match="CRC"):
        parse_response(raw, expected_slave=0x01, expected_count=4)


def test_parse_response_wrong_slave_raises():
    """We expected slave 2 but got a frame from slave 1."""
    raw = bytes.fromhex("01 03 08 00 2c 02 08 00 bb 00 00 e8 13".replace(" ", ""))
    with pytest.raises(ValueError, match="slave"):
        parse_response(raw, expected_slave=0x02, expected_count=4)


def test_parse_response_truncated_raises():
    raw = bytes([0x01, 0x03, 0x08, 0x00])  # too short
    with pytest.raises(ValueError, match="too short"):
        parse_response(raw, expected_slave=0x01, expected_count=4)


def test_parse_response_byte_count_mismatch():
    """Frame declares bc=4 but we expected count=15."""
    raw = bytes.fromhex("01030400 010002000300048a1c")  # bc=4 (2 regs) but expected_count=15
    with pytest.raises(ValueError, match="byte count"):
        parse_response(raw, expected_slave=0x01, expected_count=15)

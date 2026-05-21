"""Tests for CRC16-Modbus implementation."""

from inverter_bridge.crc import crc16


# Standard libmodbus / Modbus RTU test vectors
def test_crc_empty():
    """CRC of empty input is 0xFFFF (initial, no bytes processed). Wire order LSB first."""
    assert crc16(b"") == b"\xff\xff"


def test_crc_single_byte_zero():
    """Known Modbus CRC of [0x00] is 0xBF40 -> wire bytes 0xBF 0x40."""
    assert crc16(b"\x00") == b"\xbf\x40"


def test_crc_modbus_read_request_slave1():
    """slave=1, fc=3, addr=0x0100, count=15 -> Modbus CRC 0x3204 -> wire 0x04 0x32."""
    req = bytes([0x01, 0x03, 0x01, 0x00, 0x00, 0x0F])
    assert crc16(req) == bytes([0x04, 0x32])


# Real-world captures from a SunGoldPower split-phase inverter (2026-05-20)
def test_crc_real_response_slave1_block_0100_count4():
    """Captured response body from /dev/ttyUSB1 slave=0x01 addr=0x0100 count=4."""
    body = bytes([0x01, 0x03, 0x08, 0x00, 0x2c, 0x02, 0x08, 0x00, 0xbb, 0x00, 0x00])
    assert crc16(body) == bytes([0xe8, 0x13])


def test_crc_real_exception_slave1_illegal_address():
    """Captured exception: slave 0x01 fc 0x83 excode 0x02."""
    body = bytes([0x01, 0x83, 0x02])
    assert crc16(body) == bytes([0xc0, 0xf1])


def test_crc_byte_order_little_endian_on_wire():
    """CRC bytes go LOW byte first on the wire (per Modbus RTU spec)."""
    req = bytes([0x01, 0x03, 0x01, 0x00, 0x00, 0x0F])
    result = crc16(req)
    # Standard Modbus CRC of this request = 0x3204 -> wire order 0x04 then 0x32
    assert result[0] == 0x04  # low byte first
    assert result[1] == 0x32  # high byte second


def test_crc_real_captured_request_slave1_state_block():
    """Validates implementation against a request frame ACTUALLY observed on the bus
    (bus_capture_60s_slave01_day_pv_active.hex captured from inv1 LCD master)."""
    req = bytes([0x01, 0x03, 0x02, 0x10, 0x00, 0x13])
    # In the raw bus capture, this request was followed by 0x04 0x7a (CRC bytes in wire order).
    assert crc16(req) == bytes([0x04, 0x7a])

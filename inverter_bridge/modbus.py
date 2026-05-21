"""Modbus RTU framing for function code 0x03 (read holding registers).

We intentionally implement only the slice we need (read-holding-regs + exception
parsing), rather than depending on pymodbus. Total surface: ~80 lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .crc import crc16

FC_READ_HOLDING = 0x03
EXCEPTION_BIT = 0x80


@dataclass(frozen=True, slots=True)
class ModbusFrame:
    """A successful Modbus RTU response (read holding registers)."""

    slave: int
    func: int
    regs: list[int]


class ModbusException(Exception):
    """Raised when slave returns an exception response (fc | 0x80)."""

    EXCODES: ClassVar[dict[int, str]] = {
        0x01: "illegal_function",
        0x02: "illegal_data_address",
        0x03: "illegal_data_value",
        0x04: "slave_device_failure",
        0x05: "acknowledge",
        0x06: "slave_device_busy",
    }

    def __init__(self, slave: int, func: int, excode: int) -> None:
        self.slave = slave
        self.func = func
        self.excode = excode
        name = self.EXCODES.get(excode, f"unknown_0x{excode:02x}")
        super().__init__(
            f"slave 0x{slave:02x} fc 0x{func:02x} exception 0x{excode:02x} ({name})"
        )


def build_read_holding_request(slave: int, addr: int, count: int) -> bytes:
    """Build an 8-byte Modbus RTU request for function code 0x03 (read holding regs)."""
    if not (1 <= count <= 125):
        raise ValueError(f"count must be 1..125, got {count}")
    if not (0 <= addr <= 0xFFFF):
        raise ValueError(f"addr must be 0..0xFFFF, got 0x{addr:04x}")
    req = bytes(
        [
            slave,
            FC_READ_HOLDING,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    return req + crc16(req)


def parse_response(buf: bytes, *, expected_slave: int, expected_count: int) -> ModbusFrame:
    """Parse a complete Modbus RTU response or exception.

    Raises:
        ModbusException: if the slave returned a function-bit-set exception frame.
        ValueError: on any other anomaly (truncated, bad CRC, wrong slave, etc.).
    """
    if len(buf) < 5:
        raise ValueError(f"frame too short: {len(buf)} bytes")
    slave = buf[0]
    if slave != expected_slave:
        raise ValueError(f"wrong slave: got 0x{slave:02x}, expected 0x{expected_slave:02x}")
    func = buf[1]
    if func & EXCEPTION_BIT:
        # Exception frame: slave(1) func(1) excode(1) crc(2) = 5 bytes
        frame = buf[:5]
        if crc16(frame[:-2]) != frame[-2:]:
            raise ValueError(f"CRC mismatch on exception: {frame.hex()}")
        raise ModbusException(slave, func & 0x7F, frame[2])
    if func != FC_READ_HOLDING:
        raise ValueError(f"unexpected function: 0x{func:02x}")
    bc = buf[2]
    if bc != 2 * expected_count:
        raise ValueError(f"byte count {bc} != 2 * expected_count {2 * expected_count}")
    expected_len = 5 + bc
    if len(buf) < expected_len:
        raise ValueError(f"frame too short: {len(buf)} < {expected_len}")
    frame = buf[:expected_len]
    if crc16(frame[:-2]) != frame[-2:]:
        raise ValueError(f"CRC mismatch on response: {frame.hex()}")
    body = frame[3 : 3 + bc]
    regs = [(body[i] << 8) | body[i + 1] for i in range(0, bc, 2)]
    return ModbusFrame(slave=slave, func=func, regs=regs)

"""CRC16-Modbus per Modbus RTU spec.

Reflected polynomial 0xA001 (= reverse of 0x8005), init 0xFFFF.
Output bytes on the wire are little-endian: low byte first, then high byte.
"""

from __future__ import annotations

import struct


def crc16(data: bytes) -> bytes:
    """Return Modbus CRC16 as 2 bytes in wire order (low byte first)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)

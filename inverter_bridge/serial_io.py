"""pyserial wrapper + bus-noise-tolerant Modbus frame stream parser.

The two inverters expose what is effectively a debug tap on the inverter's
internal Modbus bus between the LCD and the power-board MCU. We can see the
LCD polling its own slave, interspersed with our queries. So instead of
assuming `1 request = 1 buffer = 1 response`, we treat the serial buffer as
a stream and pull out frames with valid CRC.

Measured empirically on our installation:
- ttyUSB1 (inv1 slave 0x01): ~3 frames/sec of LCD chatter
- ttyUSB0 (inv2 slave 0x02): ~0.7 frames/sec of LCD chatter
- Bus reliability: 3.3% read fail rate on inv2, 16.7% on inv1.
"""

from __future__ import annotations

import logging
import time

import serial

from .crc import crc16
from .modbus import FC_READ_HOLDING, ModbusException, ModbusFrame, build_read_holding_request

log = logging.getLogger(__name__)


def parse_frame_stream(
    stream: bytes, on_crc_error=None
) -> list[ModbusFrame]:
    """Walk `stream`, extract every read-holding-response frame with valid CRC.

    Skips:
        - Bytes that don't start a recognizable frame
        - Exception frames (those are surfaced via `SerialPort.query()` only)
        - Modbus REQUEST frames (they have a CRC too, but no register payload)

    Args:
        on_crc_error: optional zero-arg callback invoked once per plausible
            response frame (valid header + full length) whose CRC check fails —
            feeds the real `_meta/crc_fails_total` diagnostic (audit M3; these
            used to be dropped silently and the counter never moved).

    Returns successful response frames in the order found.
    """
    frames: list[ModbusFrame] = []
    i = 0
    n = len(stream)
    while i < n - 4:
        slave = stream[i]
        func = stream[i + 1]
        # only slaves 0x01-0xF7 are valid in Modbus RTU
        if not (1 <= slave <= 0xF7):
            i += 1
            continue
        # Try as response: slave fc bc <bc bytes> crc(2)
        if func == FC_READ_HOLDING and i + 5 <= n:
            bc = stream[i + 2]
            if 2 <= bc <= 250 and bc % 2 == 0:
                total = 5 + bc
                if i + total <= n:
                    frame_bytes = stream[i : i + total]
                    if crc16(frame_bytes[:-2]) == frame_bytes[-2:]:
                        body = frame_bytes[3 : 3 + bc]
                        regs = [(body[k] << 8) | body[k + 1] for k in range(0, bc, 2)]
                        frames.append(ModbusFrame(slave=slave, func=func, regs=regs))
                        i += total
                        continue
                    # A REQUEST frame (ours echoed, or LCD chatter) with an even
                    # addr-high byte also lands here and fails the response-CRC —
                    # don't count it as corruption if it validates as a request
                    # (the request branch below will consume it).
                    is_valid_request = (
                        i + 8 <= n and crc16(stream[i : i + 6]) == stream[i + 6 : i + 8]
                    )
                    if on_crc_error is not None and not is_valid_request:
                        on_crc_error()
        # Try as request: slave fc addr(2) count(2) crc(2) = 8 bytes
        if func == FC_READ_HOLDING and i + 8 <= n:
            req = stream[i : i + 8]
            if crc16(req[:-2]) == req[-2:]:
                i += 8
                continue
        # Try as exception: slave (fc | 0x80) excode crc(2) = 5 bytes
        if func & 0x80 and i + 5 <= n:
            exc = stream[i : i + 5]
            if crc16(exc[:-2]) == exc[-2:]:
                i += 5
                continue
        i += 1
    return frames


class SerialPort:
    """Thin pyserial wrapper with stream-tolerant `query()`.

    Each `query()` opens a fresh pyserial session — reduces state issues if
    the OS or USB driver buffers anything stale between calls.
    """

    def __init__(
        self,
        device: str,
        baud: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout_s: float = 1.5,
        on_crc_error=None,
    ) -> None:
        self.device = device
        self.baud = baud
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout_s = timeout_s
        self.on_crc_error = on_crc_error

    def query(self, slave: int, addr: int, count: int) -> ModbusFrame:
        """Send a read-holding-regs request and return the matching response.

        Raises:
            ModbusException: slave returned an exception response.
            TimeoutError: no valid frame for our slave+count read within timeout.
        """
        s = serial.Serial(
            self.device,
            self.baud,
            self.bytesize,
            self.parity,
            self.stopbits,
            timeout=self.timeout_s,
            # Without write_timeout, a wedged USB-serial driver can block
            # write()/flush() forever (reads time out; writes didn't) — M1.
            write_timeout=self.timeout_s,
        )
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
            req = build_read_holding_request(slave, addr, count)
            s.write(req)
            s.flush()
            expected = 5 + 2 * count
            buf = b""
            # monotonic: an NTP step must not stretch/shrink the read window (B1)
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline and len(buf) < expected + 32:
                chunk = s.read(expected + 32 - len(buf))
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        # Pull our response from the buffer (ignoring LCD-injected traffic)
        for f in parse_frame_stream(buf, on_crc_error=self.on_crc_error):
            if f.slave == slave and len(f.regs) == count:
                return f
        # Look for an exception frame matching our slave
        marker = bytes([slave, 0x83])
        idx = buf.find(marker)
        while idx != -1 and idx + 5 <= len(buf):
            exc = buf[idx : idx + 5]
            if crc16(exc[:-2]) == exc[-2:]:
                raise ModbusException(slave, 0x03, exc[2])
            idx = buf.find(marker, idx + 1)
        raise TimeoutError(
            f"no valid response from slave 0x{slave:02x} on {self.device} "
            f"(read {len(buf)} B in {self.timeout_s}s)"
        )

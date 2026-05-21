#!/usr/bin/env python3
"""Capture binary Modbus responses from the connected inverters.

Run on the SBC that's wired to the inverters. Each captured `.hex` is a
verbatim wire-format Modbus RTU response — feed into `tests/fixtures/` for
golden tests of `parsers.py`.
"""

from __future__ import annotations

import argparse
import struct
import time
from pathlib import Path

import serial


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def query(port: str, slave: int, addr: int, count: int, timeout: float = 1.5) -> bytes:
    s = serial.Serial(port, 9600, 8, "N", 1, timeout=timeout)
    s.reset_input_buffer()
    s.reset_output_buffer()
    req_no_crc = bytes(
        [slave, 0x03, (addr >> 8) & 0xFF, addr & 0xFF, (count >> 8) & 0xFF, count & 0xFF]
    )
    frame = req_no_crc + crc16(req_no_crc)
    s.write(frame)
    s.flush()
    time.sleep(0.05)
    expected = 5 + 2 * count
    resp = b""
    deadline = time.time() + timeout
    while len(resp) < expected + 32 and time.time() < deadline:
        chunk = s.read(expected + 32 - len(resp))
        if not chunk:
            break
        resp += chunk
    s.close()
    return resp


def passive_listen(port: str, secs: float = 60.0) -> bytes:
    s = serial.Serial(port, 9600, 8, "N", 1, timeout=0.5)
    s.reset_input_buffer()
    deadline = time.time() + secs
    buf = bytearray()
    while time.time() < deadline:
        chunk = s.read(4096)
        if chunk:
            buf.extend(chunk)
    s.close()
    return bytes(buf)


BLOCKS = [
    (0x0100, 15, "battery"),
    (0x010F, 3, "bms"),
    (0x0204, 6, "faults"),
    (0x0210, 19, "state"),
    (0x0223, 23, "pv_temps_l2"),
    (0x0035, 20, "fw_ascii"),
    (0xE116, 11, "config"),
    (0xE000, 8, "thresholds"),
    (0xF000, 8, "runtime_counters"),
    (0xF02C, 18, "daily_stats"),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=Path("/tmp/fixtures"))
    p.add_argument("--ports", nargs=2, default=["/dev/ttyUSB0", "/dev/ttyUSB1"])
    p.add_argument("--slaves", nargs=2, type=lambda s: int(s, 0), default=[0x02, 0x01])
    p.add_argument("--state-tag", default="day_pv_active",
                   help="suffix to add to filenames, identifying conditions (e.g. day_pv_active, night_idle)")
    p.add_argument("--bus-capture-secs", type=float, default=60.0)
    p.add_argument("--retries", type=int, default=3)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"Output: {args.output}")
    print(f"Ports + slaves: {list(zip(args.ports, args.slaves))}")
    print(f"State tag: {args.state_tag}")
    print()

    success = 0
    skipped = 0
    failed = 0

    for port, slave in zip(args.ports, args.slaves, strict=True):
        print(f"\n=== Capturing from {port} (slave 0x{slave:02x}) ===")
        for addr, count, label in BLOCKS:
            resp = b""
            for attempt in range(args.retries):
                resp = query(port, slave, addr, count)
                if resp:
                    break
                time.sleep(0.2)
            if not resp:
                print(f"  [FAIL] {label} 0x{addr:04x}: no reply after {args.retries} retries")
                failed += 1
                continue
            # Sanity: did we get an exception?
            buf_start = resp[:5]
            if len(buf_start) >= 2 and (buf_start[1] & 0x80):
                if buf_start[2] == 0x02:
                    print(f"  [SKIP] {label} 0x{addr:04x}: exception 0x02 (not implemented)")
                    skipped += 1
                    continue
            fname = (
                f"block_{addr:04x}_count{count:02d}_slave{slave:02x}_"
                f"{label}_{args.state_tag}.hex"
            )
            (args.output / fname).write_text(resp.hex() + "\n")
            print(f"  [OK]   {fname}: {len(resp)} B")
            success += 1
            time.sleep(0.1)

    # Bus capture for stream-parser tests
    print(f"\n=== Bus capture {args.bus_capture_secs}s on each port ===")
    for port, slave in zip(args.ports, args.slaves, strict=True):
        print(f"  Listening on {port}...")
        data = passive_listen(port, args.bus_capture_secs)
        fname = f"bus_capture_{int(args.bus_capture_secs)}s_slave{slave:02x}_{args.state_tag}.hex"
        (args.output / fname).write_text(data.hex() + "\n")
        print(f"  [OK]   {fname}: {len(data)} B captured")

    print(f"\n=== SUMMARY ===")
    print(f"  Block reads: {success} OK, {skipped} skipped (excp), {failed} failed")


if __name__ == "__main__":
    main()

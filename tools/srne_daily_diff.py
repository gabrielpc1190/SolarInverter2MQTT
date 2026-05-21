#!/usr/bin/env python3
"""Capture a SRNE cold-block snapshot for daily-stat decoding.

Run twice (once now, once N hours later) and diff -- registers that
delta correlate with PV generation are the Wh-accumulators.

Usage:
    python srne_daily_diff.py --label morning > /tmp/srne_morning.json
    # ... wait some hours, then:
    python srne_daily_diff.py --label evening > /tmp/srne_evening.json
    python srne_daily_diff.py --diff /tmp/srne_morning.json /tmp/srne_evening.json
"""
from __future__ import annotations
import argparse, glob, json, struct, sys, time
import serial

sys.path.insert(0, "/opt/inverter-bridge/src")
from inverter_bridge.crc import crc16  # type: ignore

FC_READ_HOLDING = 0x03
INV_PORTS = [
    ("/dev/serial/by-path/platform-5311400.usb-usb-0:1:1.0-port0", 0x01, "inv1"),
    ("/dev/serial/by-path/platform-xhci-hcd.1.auto-usb-0:1:1.0-port0", 0x02, "inv2"),
]
BLOCKS = [
    (0x0100, 15, "battery"),
    (0x0210, 19, "state"),
    (0x0223, 23, "pv_temps_l2"),
    (0xE000, 8, "thresholds"),
    (0xE008, 10, "thresholds_ext"),
    (0xE020, 10, "thresholds_3"),
    (0xE100, 10, "config_pre"),
    (0xE116, 11, "config"),
    (0xE121, 15, "config_post"),
    (0xE200, 16, "config_io"),
    (0xF000, 8, "counters"),
    (0xF008, 16, "counters_ext"),
    (0xF018, 16, "counters_ext2"),
    (0xF02C, 18, "daily_stats"),
    (0xF03E, 16, "daily_stats_ext"),
]

def query(port: str, slave: int, addr: int, count: int) -> bytes:
    s = serial.Serial(port, 9600, 8, "N", 1, timeout=1.5)
    s.reset_input_buffer(); s.reset_output_buffer()
    req = bytes([slave, FC_READ_HOLDING, (addr>>8)&0xFF, addr&0xFF, (count>>8)&0xFF, count&0xFF])
    s.write(req + crc16(req)); s.flush(); time.sleep(0.05)
    expected = 5 + 2*count
    resp = b""
    deadline = time.time() + 2.0
    while len(resp) < expected and time.time() < deadline:
        chunk = s.read(expected - len(resp))
        if not chunk: break
        resp += chunk
    s.close()
    return resp

def snapshot(label: str) -> dict:
    out = {"label": label, "ts": time.time(), "inverters": {}}
    for port, slave, name in INV_PORTS:
        out["inverters"][name] = {}
        for addr, count, bname in BLOCKS:
            r = query(port, slave, addr, count)
            key = f"0x{addr:04x}_{bname}"
            if len(r) >= 5 + 2*count and not (r[1] & 0x80):
                regs = struct.unpack(f">{count}H", r[3:3+2*count])
                out["inverters"][name][key] = list(regs)
            time.sleep(0.05)
    return out

def diff(a_path: str, b_path: str):
    a = json.load(open(a_path))
    b = json.load(open(b_path))
    dt_hours = (b["ts"] - a["ts"]) / 3600
    print(f"# Diff: {a['label']} ({time.strftime('%H:%M', time.localtime(a['ts']))}) vs {b['label']} ({time.strftime('%H:%M', time.localtime(b['ts']))}) — {dt_hours:.1f} h apart")
    for inv in sorted(set(a["inverters"]) & set(b["inverters"])):
        print(f"\n## {inv}")
        for block in sorted(set(a["inverters"][inv]) & set(b["inverters"][inv])):
            base = int(block.split('_')[0], 16)
            ra = a["inverters"][inv][block]
            rb = b["inverters"][inv][block]
            changes = [(i, ra[i], rb[i], rb[i] - ra[i]) for i in range(min(len(ra), len(rb))) if ra[i] != rb[i]]
            if not changes: continue
            print(f"  {block}:")
            for off, va, vb, d in changes:
                marker = ""
                if abs(d) > 100: marker = "  ← BIG"
                if d > 0 and va > 0: marker += "  (monotonic up — accumulator?)"
                print(f"    0x{base+off:04x}  {va:>6} -> {vb:>6}  delta {d:+d}{marker}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="snap")
    ap.add_argument("--diff", nargs=2)
    args = ap.parse_args()
    if args.diff:
        diff(args.diff[0], args.diff[1])
    else:
        print(json.dumps(snapshot(args.label), indent=2))

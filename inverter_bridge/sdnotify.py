"""Minimal sd_notify(3) client — talks to systemd's NOTIFY_SOCKET directly.

No dependency on libsystemd/python-systemd: the protocol is a single datagram
on an AF_UNIX socket. Enables `Type=notify` + `WatchdogSec=` in the unit so
systemd restarts the daemon if the main loop ever wedges.
"""

from __future__ import annotations

import os
import socket


def sd_notify(state: str) -> None:
    """Send a notification (e.g. "READY=1", "WATCHDOG=1") to systemd.

    No-op when NOTIFY_SOCKET is unset (dev runs, tests). Best-effort: a
    notification failure must never take the daemon down.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode(), addr)
    except OSError:
        pass

#!/usr/bin/env python3
"""Smoke test: run the daemon for N hot cycles against the REAL inverters,
publishing to a stub MQTT instead of a real broker.

Use this when developing locally without an MQTT broker to validate that:
- Serial config is right
- Both inverters respond
- Aggregator produces expected sensor set
- Bus retry logic works

Usage on the OPi (after installing the package):
    python tools/smoke_test.py --config /tmp/inverter-bridge-smoke.yaml --cycles 3
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from inverter_bridge.config import load_config
from inverter_bridge.daemon import Daemon


class StubPublisher:
    """No-op MQTT publisher that records publishes for later inspection."""

    def __init__(self, cfg, n_inverters: int = 2, **_) -> None:
        self.published: dict[str, object] = {}

    def connect(self) -> None:
        log = logging.getLogger("stub")
        log.info("[stub MQTT] connect (no-op)")

    def disconnect(self) -> None:
        pass

    def set_online(self) -> None:
        pass

    def publish_discovery(self) -> None:
        log = logging.getLogger("stub")
        log.info("[stub MQTT] discovery (no-op)")

    def publish_value(self, k: str, v: object) -> None:
        self.published[k] = v

    def publish_values(self, kv: dict[str, object]) -> None:
        self.published.update(kv)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--cycles", type=int, default=3, help="number of hot cycles to run")
    p.add_argument("--interval", type=float, default=1.0, help="sleep between cycles (s)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s :: %(message)s",
    )

    cfg = load_config(args.config)
    daemon = Daemon(cfg, publisher_factory=StubPublisher)
    daemon.publisher.connect()

    log = logging.getLogger("smoke")
    log.info("running %d hot cycles", args.cycles)
    for i in range(args.cycles):
        log.info("--- cycle %d/%d ---", i + 1, args.cycles)
        start = time.monotonic()
        daemon.run_one_hot_cycle()
        elapsed_ms = (time.monotonic() - start) * 1000
        log.info("cycle done in %.0f ms, %d sensors", elapsed_ms, len(daemon.publisher.published))
        if i + 1 < args.cycles:
            time.sleep(args.interval)

    print("\n=== Final published sensors ===\n")
    for k, v in sorted(daemon.publisher.published.items()):
        print(f"  {k:55s} = {v}")
    print(f"\nTotal: {len(daemon.publisher.published)} sensors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

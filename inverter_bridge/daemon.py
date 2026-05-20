"""Main daemon loops: hot (~3s) + cold (~60s) polling per spec §5.6.

Single-threaded; the polling cadence is slow enough that we don't need async.
Resilient to per-block exceptions (skips block, keeps polling others) and
to per-inverter timeouts (publishes offline marker after 3 consecutive fails).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from .aggregator import aggregate_inverters
from .config import BridgeConfig, InverterCfg
from .modbus import ModbusException
from .mqtt_publisher import MqttPublisher
from .parsers import ParsedBlock, parse_block
from .serial_io import SerialPort
from .srne_map import BLOCKS, BlockTier

log = logging.getLogger(__name__)


class Daemon:
    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        serial_factory=SerialPort,
        publisher_factory=MqttPublisher,
    ) -> None:
        """Construct daemon.

        Args:
            cfg: validated BridgeConfig
            serial_factory / publisher_factory: injectable for testing.
        """
        self.cfg = cfg
        self.ports: dict[str, SerialPort] = {
            i.name: serial_factory(device=i.port, timeout_s=cfg.polling.serial_timeout_s)
            for i in cfg.inverters
        }
        self.publisher = publisher_factory(cfg.mqtt, n_inverters=len(cfg.inverters))
        self._fail_count: dict[str, int] = defaultdict(int)
        self._meta_start = time.monotonic()
        self._crc_fails_total = 0

    # ----- lifecycle -----

    def start(self) -> None:
        """Run forever. Returns only via KeyboardInterrupt or fatal error."""
        self.publisher.connect()
        self.publisher.publish_discovery()
        self.publisher.set_online()
        try:
            self._loop_forever()
        finally:
            self.publisher.disconnect()

    def _loop_forever(self) -> None:
        # Initialize last_cold to "now" so the first cold cycle runs cold_interval_s
        # AFTER startup, not immediately. The first iteration is hot-only, ensuring
        # MQTT publishes start within ~3s of daemon start (not delayed by the slow
        # cold-block polling on the noisy inv1 bus).
        last_cold = time.monotonic()
        while True:
            cycle_start = time.monotonic()
            try:
                self.run_one_hot_cycle()
                if time.monotonic() - last_cold >= self.cfg.polling.cold_interval_s:
                    self.run_one_cold_cycle()
                    last_cold = time.monotonic()
            except Exception:
                log.exception("unexpected error in main loop")
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, self.cfg.polling.hot_interval_s - elapsed)
            time.sleep(sleep_for)

    # ----- one cycle -----

    def run_one_hot_cycle(self) -> None:
        """Poll all hot-tier blocks on both inverters and publish."""
        cycle_start = time.monotonic()
        per_inverter: list[dict[str, ParsedBlock]] = []
        for inv in self.cfg.inverters:
            blocks = self._poll_inverter(inv, tier=BlockTier.HOT)
            per_inverter.append(blocks)
        aggregated = aggregate_inverters(per_inverter)
        self.publisher.publish_values(aggregated)
        if any(blocks for blocks in per_inverter):
            self.publisher.set_online()
        # Meta sensors
        elapsed_ms = round((time.monotonic() - cycle_start) * 1000, 1)
        self.publisher.publish_value("_meta/poll_duration_ms", elapsed_ms)
        self.publisher.publish_value("_meta/crc_fails_total", self._crc_fails_total)
        self.publisher.publish_value(
            "_meta/uptime_s", round(time.monotonic() - self._meta_start, 1)
        )

    def run_one_cold_cycle(self) -> None:
        """Poll all cold-tier blocks on both inverters. Doesn't publish (only refreshes internals)."""
        for inv in self.cfg.inverters:
            self._poll_inverter(inv, tier=BlockTier.COLD)

    # ----- internals -----

    def _poll_inverter(
        self, inv: InverterCfg, *, tier: BlockTier
    ) -> dict[str, ParsedBlock]:
        port = self.ports[inv.name]
        out: dict[str, ParsedBlock] = {}
        any_success = False
        for block in BLOCKS:
            if block.tier != tier:
                continue
            frame = None
            for attempt in range(self.cfg.polling.retry_attempts):
                try:
                    frame = port.query(slave=inv.slave, addr=block.addr, count=block.count)
                    break
                except ModbusException as e:
                    if e.excode == 0x02:
                        log.debug(
                            "inv %s block 0x%04x not implemented (excode 0x02)",
                            inv.name, block.addr,
                        )
                    else:
                        log.warning("inv %s block 0x%04x exception %r", inv.name, block.addr, e)
                    break  # exceptions are deterministic — don't retry
                except TimeoutError as e:
                    if attempt + 1 < self.cfg.polling.retry_attempts:
                        time.sleep(self.cfg.polling.retry_backoff_s * (2 ** attempt))
                        continue
                    log.warning(
                        "inv %s block 0x%04x timeout after %d attempts: %s",
                        inv.name, block.addr, attempt + 1, e,
                    )
                except Exception:
                    log.exception("inv %s block 0x%04x unexpected error", inv.name, block.addr)
                    self._crc_fails_total += 1
                    break
            if frame is not None:
                try:
                    parsed = parse_block(block_addr=block.addr, frame=frame)
                    out[block.name] = parsed
                    any_success = True
                except Exception:
                    log.exception("inv %s block 0x%04x parse error", inv.name, block.addr)
            time.sleep(self.cfg.polling.inter_query_delay_s)
        if any_success:
            self._fail_count[inv.name] = 0
        else:
            self._fail_count[inv.name] += 1
            if self._fail_count[inv.name] >= 3:
                log.error("inv %s offline (3 consecutive cycles failed)", inv.name)
                self.publisher.publish_value(f"inverter_{inv.name}_status", "offline")
        return out

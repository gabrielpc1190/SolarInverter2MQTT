"""Main daemon loops: hot (~3s) + cold (~60s) polling per spec §5.6.

Per-inverter polling runs in parallel via a ThreadPoolExecutor (one thread per
inverter, each owning its own serial port — no bus contention). MQTT publish
and energy integration stay on the main thread after both workers join.
Resilient to per-block exceptions (skips block, keeps polling others),
per-inverter timeouts (publishes offline marker after 3 consecutive fails),
and per-thread timeouts (a stuck inverter doesn't block the other).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from .aggregator import aggregate_inverters
from .config import BridgeConfig, InverterCfg
from .energy_integrator import EnergyIntegrator
from .modbus import ModbusException
from .mqtt_publisher import MqttPublisher
from .parsers import ParsedBlock, parse_block
from .serial_io import SerialPort
from .srne_map import BLOCKS, BlockTier

log = logging.getLogger(__name__)

_DEFAULT_ENERGY_PERSIST_PATH = Path("/var/lib/inverter-bridge/energy.json")


class Daemon:
    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        serial_factory=SerialPort,
        publisher_factory=MqttPublisher,
        integrator: EnergyIntegrator | None = None,
        energy_persist_path: Path | None = _DEFAULT_ENERGY_PERSIST_PATH,
    ) -> None:
        """Construct daemon.

        Args:
            cfg: validated BridgeConfig
            serial_factory / publisher_factory: injectable for testing.
            integrator: optional pre-built EnergyIntegrator (e.g. a mock).
                If None, a real EnergyIntegrator is constructed with
                `persist_path=energy_persist_path`.
            energy_persist_path: file path used by the default EnergyIntegrator
                to persist accumulated energy across restarts. Pass None to
                disable persistence (useful in tests).
        """
        self.cfg = cfg
        self.ports: dict[str, SerialPort] = {
            i.name: serial_factory(device=i.port, timeout_s=cfg.polling.serial_timeout_s)
            for i in cfg.inverters
        }
        self.publisher = publisher_factory(cfg.mqtt, n_inverters=len(cfg.inverters))
        self.integrator: EnergyIntegrator = (
            integrator
            if integrator is not None
            else EnergyIntegrator(persist_path=energy_persist_path)
        )
        self._fail_count: dict[str, int] = defaultdict(int)
        # Protects `_fail_count` and `_crc_fails_total` against concurrent
        # mutation from the per-inverter worker threads (F-5).
        self._fail_count_lock = threading.Lock()
        self._meta_start = time.monotonic()
        self._crc_fails_total = 0
        # Set on first hot cycle; until then `elapsed_s` is treated as 0.
        self._last_hot_cycle_monotonic: float | None = None
        # One worker per inverter; threads named for easier debugging in logs.
        # Each inverter has its own /dev/ttyUSB*, so they can be polled
        # simultaneously without serial-bus contention.
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, len(cfg.inverters)),
            thread_name_prefix="poll",
        )
        # Per-inverter worker timeout, derived from polling config so that
        # the slowest legal hot/cold cycle still completes before we kill the
        # thread. 10 s headroom on top of (timeout * retries * blocks).
        hot_block_count = sum(1 for b in BLOCKS if b.tier == BlockTier.HOT)
        cold_block_count = sum(1 for b in BLOCKS if b.tier == BlockTier.COLD)
        block_count_max = max(hot_block_count, cold_block_count, 1)
        self._poll_worker_timeout_s = 2 * (
            cfg.polling.serial_timeout_s
            * max(1, cfg.polling.retry_attempts)
            * block_count_max
            + 10
        )

    # ----- lifecycle -----

    def start(self) -> None:
        """Run forever. Returns only via KeyboardInterrupt or fatal error."""
        self.publisher.connect()
        self.publisher.publish_discovery()
        self.publisher.set_online()
        try:
            self._loop_forever()
        finally:
            # Persist energy state on clean shutdown so the next start picks up
            # the accumulator where we left off, then close the MQTT session.
            try:
                self.save_energy_state()
            except Exception:
                log.exception("failed to persist energy state on shutdown")
            self.publisher.disconnect()
            # Drain the executor so in-flight serial reads finish cleanly
            # (don't cancel — pyserial.close is fast, and cancelling could
            # leave the port half-open on the OS side).
            try:
                self._executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                log.exception("failed to shut down poll executor")

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
        """Poll all hot-tier blocks on both inverters (in parallel) and publish."""
        cycle_start = time.monotonic()
        per_inverter, per_inverter_durations_ms = self._poll_all_parallel(
            tier=BlockTier.HOT
        )
        aggregated = aggregate_inverters(per_inverter)
        # Integrate energy (kWh) from instantaneous power readings. First cycle
        # has no elapsed delta yet, so dt = 0 (no accumulation that cycle).
        now = time.monotonic()
        if self._last_hot_cycle_monotonic is None:
            elapsed_s = 0.0
        else:
            elapsed_s = max(0.0, now - self._last_hot_cycle_monotonic)
        energy_values = self.integrator.update(
            aggregated=aggregated, elapsed_s=elapsed_s
        )
        aggregated.update(energy_values)
        self._last_hot_cycle_monotonic = now
        self.publisher.publish_values(aggregated)
        if any(blocks for blocks in per_inverter):
            self.publisher.set_online()
        # Meta sensors. With parallel polling, poll_duration_ms ≈ max(per-inv)
        # not sum(per-inv) — the whole point of F-5.
        elapsed_ms = round((time.monotonic() - cycle_start) * 1000, 1)
        self.publisher.publish_value("_meta/poll_duration_ms", elapsed_ms)
        self.publisher.publish_value(
            "_meta/poll_duration_per_inverter_ms", per_inverter_durations_ms
        )
        self.publisher.publish_value("_meta/crc_fails_total", self._crc_fails_total)
        self.publisher.publish_value(
            "_meta/uptime_s", round(time.monotonic() - self._meta_start, 1)
        )

    def run_one_cold_cycle(self) -> None:
        """Poll all cold-tier blocks on both inverters in parallel. Doesn't publish."""
        self._poll_all_parallel(tier=BlockTier.COLD)
        # Persist accumulated energy on every cold cycle so a restart near a
        # cycle boundary doesn't lose minutes of integration.
        self.save_energy_state()

    def save_energy_state(self) -> None:
        """Persist the energy integrator to disk. Safe to call repeatedly."""
        try:
            self.integrator.save()
        except Exception:
            log.exception("failed to persist energy state")

    # ----- internals -----

    def _poll_all_parallel(
        self, *, tier: BlockTier
    ) -> tuple[list[dict[str, ParsedBlock]], dict[str, float]]:
        """Submit one polling task per inverter and gather results in cfg order.

        Returns:
            (per_inverter_blocks, per_inverter_durations_ms) where the blocks
            list mirrors `cfg.inverters` order — `per_inverter[0]` is inv1 etc.
            Inverters that hit the worker-level timeout are reported as `{}`
            blocks (same shape as a fully-failed sequential poll).
        """
        per_inverter: list[dict[str, ParsedBlock]] = [
            {} for _ in self.cfg.inverters
        ]
        per_inverter_durations_ms: dict[str, float] = {}
        # Submit in cfg order, but stash an index so results land in slot[i].
        futures = []
        for idx, inv in enumerate(self.cfg.inverters):
            started = time.monotonic()
            fut = self._executor.submit(self._poll_inverter, inv, tier=tier)
            futures.append((idx, inv, fut, started))
        for idx, inv, fut, started in futures:
            try:
                per_inverter[idx] = fut.result(timeout=self._poll_worker_timeout_s)
            except FuturesTimeoutError:
                log.error(
                    "inv %s poll worker exceeded %.1fs timeout (tier=%s); "
                    "treating as empty blocks for this cycle",
                    inv.name, self._poll_worker_timeout_s, tier.value,
                )
                # Count this as a failed cycle for the offline-marker logic so
                # a permanently-wedged port still surfaces as offline.
                with self._fail_count_lock:
                    self._fail_count[inv.name] += 1
                    fails = self._fail_count[inv.name]
                if fails >= 3:
                    self.publisher.publish_value(
                        f"inverter_{inv.name}_status", "offline"
                    )
                per_inverter[idx] = {}
            except Exception:
                log.exception("inv %s poll worker crashed (tier=%s)",
                              inv.name, tier.value)
                per_inverter[idx] = {}
            per_inverter_durations_ms[inv.name] = round(
                (time.monotonic() - started) * 1000, 1
            )
        return per_inverter, per_inverter_durations_ms

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
                    with self._fail_count_lock:
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
        # `_fail_count` is shared with the other worker thread (F-5) — lock the
        # read-modify-write so concurrent failures don't race.
        with self._fail_count_lock:
            if any_success:
                self._fail_count[inv.name] = 0
                fails = 0
            else:
                self._fail_count[inv.name] += 1
                fails = self._fail_count[inv.name]
        if fails >= 3:
            log.error("inv %s offline (3 consecutive cycles failed)", inv.name)
            self.publisher.publish_value(f"inverter_{inv.name}_status", "offline")
        return out

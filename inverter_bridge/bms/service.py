"""BMS service: ata todo — BLE polling + decode + aggregate + MQTT discovery + publish.

Diseñado para correr en un thread dedicado del daemon principal con su propio
asyncio loop. Maneja:
  - Connect MQTT (paho), LWT availability=offline
  - Publish 86 discovery payloads (retained) al iniciar
  - Publish availability=online
  - Loop async: conecta BLE, polls fast (PIA) + slow (PIB) cada 4 packs, agrega,
    publica state values, persiste energy accumulators
  - Reconnect BLE con exponential backoff ante BleakError
  - Stop limpio via cancel + LWT-aware disconnect

Lifecycle desde el daemon:
    svc = BmsService(cfg.bms, cfg.mqtt)
    svc.start()  # spawns thread
    ...
    svc.stop()   # signals + joins
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from bleak.exc import BleakError

from ..config import BmsCfg, MqttCfg
from .aggregator import BankAggregates, aggregate_bank
from .ble_client import OctopusBleClient
from .discovery import (
    EntityDef,
    all_bms_entities,
    build_discovery_payload,
    discovery_topic_for,
    state_topic_for,
)
from .octopus_protocol import PiaData, PibData

log = logging.getLogger(__name__)


class BmsService:
    """Servicio integrado del BMS. Corre BLE polling en async loop dentro de un thread."""

    def __init__(self, bms_cfg: BmsCfg, mqtt_cfg: MqttCfg) -> None:
        self.bms_cfg = bms_cfg
        self.mqtt_cfg = mqtt_cfg
        self.entities: list[EntityDef] = all_bms_entities()
        self._availability_topic = f"{bms_cfg.mqtt_topic_prefix}/availability"

        # MQTT client (separado del inverter publisher para aislamiento)
        self._mqtt: mqtt.Client | None = None
        self._mqtt_connected = threading.Event()
        self._client_id = f"{mqtt_cfg.client_id}_bms"

        # Async machinery
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None  # creado dentro del loop

        # State cache: ultima lectura por pack para agregar cuando PIB llega entre PIAs
        self._pia_state: dict[int, PiaData] = {}
        self._pib_state: dict[int, PibData] = {}

        # Energy integrators (Wh acumulado). Seed desde HA REST al iniciar (TODO en v2);
        # por ahora fallback a persistencia local o cero.
        self._energy_in_Wh: float = 0.0
        self._energy_out_Wh: float = 0.0
        self._energy_persist = Path(bms_cfg.energy_persist_path)
        self._last_energy_sample_t: float | None = None

        # Telemetry: counter de parses exitosos (PIA + PIB). Incrementa cada
        # decode OK; expuesto como diagnostic para watchdog en HA.
        self._parses_ok: int = 0
        # Per-pack parse counters: distinguen LIVE vs FROZEN vs NO-RESPONSE.
        self._parses_by_pack: dict[int, int] = {p: 0 for p in range(1, 5)}

    # ───── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BmsService ya está iniciado")
        if not self.bms_cfg.enabled:
            log.info("BmsService: bms.enabled=false — no se inicia")
            return
        log.info("Iniciando BmsService (master_mac=%s)…", self.bms_cfg.master_mac)
        self._load_energy_state()
        self._connect_mqtt()
        self._thread = threading.Thread(
            target=self._thread_main, name="bms-service", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        if self._thread is None:
            return
        log.info("Deteniendo BmsService…")
        if self._loop is not None and self._stop_event is not None:
            # Programar cancellation desde fuera del loop
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=timeout_s)
        if self._mqtt is not None:
            try:
                self._mqtt.publish(self._availability_topic, "offline", qos=0, retain=True)
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception as e:
                log.warning("Error cerrando MQTT en stop(): %s", e)
        self._thread = None

    # ───── Energy state persistence ───────────────────────────────

    def _load_energy_state(self) -> None:
        if self._energy_persist.exists():
            try:
                d = json.loads(self._energy_persist.read_text())
                self._energy_in_Wh = float(d.get("energy_in_Wh", 0.0))
                self._energy_out_Wh = float(d.get("energy_out_Wh", 0.0))
                log.info(
                    "Energy state cargado de %s: in=%.2f Wh, out=%.2f Wh",
                    self._energy_persist, self._energy_in_Wh, self._energy_out_Wh,
                )
            except Exception as e:
                log.warning("No pude cargar energy state (%s): %s. Empiezo en 0.", self._energy_persist, e)
        else:
            log.info("Energy state file no existe (%s). Empiezo en 0.", self._energy_persist)

    def _save_energy_state(self) -> None:
        try:
            self._energy_persist.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "energy_in_Wh": self._energy_in_Wh,
                "energy_out_Wh": self._energy_out_Wh,
                "saved_at_unix": time.time(),
            }
            tmp = self._energy_persist.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._energy_persist)
        except Exception as e:
            log.warning("Error guardando energy state: %s", e)

    # ───── MQTT ───────────────────────────────────────────────────

    def _connect_mqtt(self) -> None:
        # client_id distinto del inverter publisher
        self._mqtt = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            clean_session=True,
        )
        self._mqtt.username_pw_set(self.mqtt_cfg.username, self.mqtt_cfg.password)
        self._mqtt.will_set(self._availability_topic, "offline", qos=1, retain=True)
        self._mqtt.on_connect = self._handle_mqtt_connect
        self._mqtt.connect(self.mqtt_cfg.host, self.mqtt_cfg.port, keepalive=60)
        self._mqtt.loop_start()
        # Espera hasta 10s a que conecte
        if not self._mqtt_connected.wait(timeout=10.0):
            raise RuntimeError("MQTT no se conectó en 10s")

    def _handle_mqtt_connect(self, _client, _ud, _flags, rc, _props=None) -> None:
        """on_connect handler — re-asienta discovery + availability=online en CADA
        (re)conexión.

        paho auto-reconnecta vía loop_start(); cuando la conexión se cae (p.ej. un
        reinicio del router) el broker publica el LWT 'offline' retenido. Si no
        republicamos aquí, tras el corte el BMS queda 'offline' permanente en HA
        hasta reiniciar el proceso. El inverter publisher hace lo análogo.
        """
        if rc != 0:
            log.error("MQTT (bms) conexión falló rc=%s", rc)
            return
        log.info("MQTT conectado (bms client_id=%s)", self._client_id)
        try:
            self._publish_discovery()
        except Exception:
            log.exception("Error republicando discovery/availability del BMS en on_connect")
        self._mqtt_connected.set()

    def _publish_discovery(self) -> None:
        assert self._mqtt is not None
        log.info("Publicando %d discovery payloads…", len(self.entities))
        for entity in self.entities:
            topic = discovery_topic_for(entity)
            payload = build_discovery_payload(
                entity,
                topic_prefix=self.bms_cfg.mqtt_topic_prefix,
                device_id=self.bms_cfg.mqtt_device_id,
                device_name=self.bms_cfg.mqtt_device_name,
                availability_topic=self._availability_topic,
            )
            self._mqtt.publish(topic, json.dumps(payload), qos=1, retain=True)
        # Mark availability
        self._mqtt.publish(self._availability_topic, "online", qos=1, retain=True)
        # Pack serials (hardcoded en config — no son legibles via BLE Octopus para
        # packs 2-4). Publicados retained una sola vez para que sobrevivan a daemon
        # restart sin necesidad de republish.
        for i, serial in enumerate(self.bms_cfg.pack_serials, start=1):
            object_id = f"bluesun_pack{i:02d}_serial"
            self._mqtt.publish(
                f"{self.bms_cfg.mqtt_topic_prefix}/{object_id}/state",
                payload=serial, qos=1, retain=True,
            )
        log.info("Discovery + serials publicados.")

    def _publish_state(self, entity: EntityDef, value: Any) -> None:
        """Publica un single state. value=None se skipea (sensor mantiene valor previo)."""
        assert self._mqtt is not None
        if value is None:
            return
        topic = state_topic_for(entity, topic_prefix=self.bms_cfg.mqtt_topic_prefix)
        payload = f"{value:.4f}" if isinstance(value, float) else str(value)
        self._mqtt.publish(topic, payload, qos=0, retain=False)

    # ───── Async loop main ────────────────────────────────────────

    def _thread_main(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop_event = asyncio.Event()
            # Discovery + availability=online se publican en _handle_mqtt_connect
            # (en cada (re)conexión), no aquí, para que sobrevivan a reconexiones MQTT.
            self._loop.run_until_complete(self._run_async())
        except Exception:
            log.exception("BmsService thread crashed")
        finally:
            log.info("BmsService thread exiting")
            if self._loop is not None:
                self._loop.close()

    async def _run_async(self) -> None:
        backoff = self.bms_cfg.reconnect_initial_backoff_s
        while not self._stop_event.is_set():
            try:
                async with OctopusBleClient(
                    mac=self.bms_cfg.master_mac,
                    connect_timeout_s=self.bms_cfg.connect_timeout_s,
                ) as client:
                    log.info("BLE conectado, entrando a poll loop")
                    backoff = self.bms_cfg.reconnect_initial_backoff_s  # reset
                    await self._poll_loop(client)
            except asyncio.CancelledError:
                raise
            except BleakError as e:
                log.warning("BleakError: %s — reconectando en %.1fs", e, backoff)
            except Exception as e:
                log.exception("Error inesperado en poll loop: %s", e)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                break  # stop event set
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.bms_cfg.reconnect_max_backoff_s)

    async def _poll_loop(self, client: OctopusBleClient) -> None:
        last_slow = 0.0
        slow_interval = self.bms_cfg.poll_slow_interval_s
        fast_interval = self.bms_cfg.poll_fast_interval_s
        pack_count = self.bms_cfg.pack_count
        inter_pack = self.bms_cfg.inter_pack_delay_s
        max_failed_cycles = self.bms_cfg.max_failed_cycles
        # Ciclos consecutivos sin NINGÚN parse OK. Cuando llega a
        # max_failed_cycles, salimos para que _run_async reconecte (link zombie).
        failed_cycles = 0

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            cycle_ok = 0  # parses exitosos en este ciclo (PIA + PIB)
            # PIA round: todos los packs
            for pack in range(1, pack_count + 1):
                try:
                    pia = await client.poll_pia(pack)
                    self._pia_state[pack] = pia
                    self._parses_ok += 1
                    self._parses_by_pack[pack] = self._parses_by_pack.get(pack, 0) + 1
                    cycle_ok += 1
                    log.debug(
                        "PIA pack=%d V=%.2f I=%+.2f SoC=%.1f",
                        pack, pia.voltage_V, pia.current_A, pia.soc_pct,
                    )
                    self._publish_pia_state(pia)
                except (TimeoutError, BleakError) as e:
                    log.warning("PIA pack=%d FAIL: %s", pack, e)
                    # Si el link BLE se cayó, no tiene sentido seguir polleando
                    # un cliente muerto: salimos para que _run_async reconecte.
                    if not client.is_connected:
                        log.warning("BLE desconectado (PIA pack=%d) — saliendo del poll loop para reconectar", pack)
                        return
                await asyncio.sleep(inter_pack)
                if self._stop_event.is_set():
                    return

            # PIB round si toca
            if cycle_start - last_slow >= slow_interval:
                last_slow = cycle_start
                for pack in range(1, pack_count + 1):
                    try:
                        pib = await client.poll_pib(pack)
                        self._pib_state[pack] = pib
                        self._parses_ok += 1
                        self._parses_by_pack[pack] = self._parses_by_pack.get(pack, 0) + 1
                        cycle_ok += 1
                        self._publish_pib_state(pib)
                    except (TimeoutError, BleakError) as e:
                        log.warning("PIB pack=%d FAIL: %s", pack, e)
                        if not client.is_connected:
                            log.warning("BLE desconectado (PIB pack=%d) — saliendo del poll loop para reconectar", pack)
                            return
                    await asyncio.sleep(inter_pack)
                    if self._stop_event.is_set():
                        return

            # Watchdog de link zombie: si el ciclo completo no logró ningún parse
            # (is_connected miente, todo da timeout) contamos; al llegar al umbral
            # forzamos reconexión fresca en vez de girar para siempre.
            if cycle_ok == 0:
                failed_cycles += 1
                if failed_cycles >= max_failed_cycles:
                    log.warning(
                        "%d ciclos consecutivos sin parse OK — forzando reconexión BLE",
                        failed_cycles,
                    )
                    return
            else:
                failed_cycles = 0

            # Aggregate + publish bank-level
            agg = aggregate_bank(self._pia_state, self._pib_state)
            self._publish_bank_state(agg)

            # Energy integration
            self._integrate_energy(agg)

            # Telemetry
            self._pub("bluesun_octopus_parses_ok", self._parses_ok)
            for pack_num, parses in self._parses_by_pack.items():
                self._pub(f"bluesun_pack{pack_num:02d}_parses", parses)

            # Wait hasta cumplir el fast interval (slow polls a costa del wall-clock)
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, fast_interval - elapsed)
            if remaining > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
                    return
                except TimeoutError:
                    pass

    # ───── Publishing helpers ─────────────────────────────────────

    def _entity_by_object(self, object_id: str) -> EntityDef | None:
        for e in self.entities:
            if e.object_id == object_id:
                return e
        return None

    def _pub(self, object_id: str, value: Any) -> None:
        e = self._entity_by_object(object_id)
        if e is None:
            log.warning("entity object_id=%s no está en el catálogo", object_id)
            return
        self._publish_state(e, value)

    def _publish_pia_state(self, p: PiaData) -> None:
        pp = f"pack{p.pack:02d}"
        self._pub(f"bluesun_{pp}_voltage", p.voltage_V)
        self._pub(f"bluesun_{pp}_current", p.current_A)
        self._pub(f"bluesun_{pp}_soc", p.soc_pct)
        self._pub(f"bluesun_{pp}_soh", p.soh_pct)
        self._pub(f"bluesun_{pp}_cycles", p.cycles)
        self._pub(f"bluesun_{pp}_remaining_ah", p.remaining_Ah)
        self._pub(f"bluesun_{pp}_nominal_ah", p.nominal_Ah)

    def _publish_pib_state(self, p: PibData) -> None:
        pp = f"pack{p.pack:02d}"
        self._pub(f"bluesun_{pp}_cell_v_min", p.cell_v_min_mV)
        self._pub(f"bluesun_{pp}_cell_v_max", p.cell_v_max_mV)
        self._pub(f"bluesun_{pp}_cell_v_avg", p.cell_v_avg_mV)
        self._pub(f"bluesun_{pp}_cell_v_delta", p.cell_v_delta_mV)
        for i, t in enumerate(p.cell_temp_C, start=1):
            self._pub(f"bluesun_{pp}_cell_temp_{i}", t)
        self._pub(f"bluesun_{pp}_env_temp", p.env_temp_C)
        self._pub(f"bluesun_{pp}_pcb_temp", p.pcb_temp_C)

    def _publish_bank_state(self, agg: BankAggregates) -> None:
        self._pub("bluesun_bank_voltage_avg", agg.voltage_avg_V)
        self._pub("bluesun_bank_current_total", agg.current_total_A)
        self._pub("bluesun_bank_soc_avg", agg.soc_avg_pct)
        self._pub("bluesun_bank_soc_spread", agg.soc_spread_pct)
        self._pub("bluesun_bank_power", agg.power_W)
        self._pub("bluesun_bank_power_charging", agg.power_charging_W)
        self._pub("bluesun_bank_power_discharging", agg.power_discharging_W)
        self._pub("bluesun_bank_remaining_ah", agg.remaining_Ah)
        self._pub("bluesun_bank_nominal_ah", agg.nominal_Ah)
        self._pub("bluesun_bank_min_soh", agg.min_soh_pct)
        self._pub("bluesun_bank_max_cycles", agg.max_cycles)
        self._pub("bluesun_bank_max_cell_temp", agg.max_cell_temp_C)
        self._pub("bluesun_bank_charge_current_total", agg.charge_current_total_A)
        self._pub("bluesun_bank_discharge_current_total", agg.discharge_current_total_A)
        self._pub("bluesun_bank_charge_power_total", agg.charge_power_total_W)
        self._pub("bluesun_bank_discharge_power_total", agg.discharge_power_total_W)

    def _integrate_energy(self, agg: BankAggregates) -> None:
        """Integra power_charging/discharging en Wh acumulado."""
        now = time.monotonic()
        if self._last_energy_sample_t is None or agg.power_charging_W is None or agg.power_discharging_W is None:
            self._last_energy_sample_t = now
            self._pub("bluesun_battery_energy_in", self._energy_in_Wh)
            self._pub("bluesun_battery_energy_out", self._energy_out_Wh)
            return
        dt_s = now - self._last_energy_sample_t
        self._last_energy_sample_t = now
        if dt_s > 0 and dt_s < 60.0:  # ignora dt > 60s (probable reconnect)
            dt_h = dt_s / 3600.0
            self._energy_in_Wh += agg.power_charging_W * dt_h
            self._energy_out_Wh += agg.power_discharging_W * dt_h
        self._pub("bluesun_battery_energy_in", self._energy_in_Wh)
        self._pub("bluesun_battery_energy_out", self._energy_out_Wh)
        # Persist cada N samples (cada ciclo es ~5s; persistir cada ~30s)
        if int(now) % 30 == 0:
            self._save_energy_state()

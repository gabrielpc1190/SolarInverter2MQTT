"""paho-mqtt publisher with HA Discovery + LWT — Solar Assistant compatible.

Designed to MATCH Solar Assistant's MQTT topology so existing HA entities
(created from SA's retained discovery payloads) keep updating without
requiring dashboard/automation changes.

SA convention (verified 2026-05-20 by inspecting retained discovery payloads
on our MQTT broker by inspecting retained discovery payloads before SA was disconnected):

- Topic prefix: `solar_assistant`
- Aggregated sensors: state at `solar_assistant/total/<key>/state`,
  unique_id `total_<key>` (e.g. `total_battery_state_of_charge`).
- Per-inverter sensors: state at `solar_assistant/inverter_<N>/<key>/state`,
  unique_id `inverter_<N>_<key>` (e.g. `inverter_1_pv_voltage`).
- Discovery topic: `homeassistant/sensor/<unique_id>/config` (retained).
- Device identifiers: `["sa_inverter"]`, model "SRNE Split-phase".

Extensions beyond SA (audit findings F-2, F-4, F-6):

- `_meta/*` diagnostic sensors: topic `solar_assistant/_meta/<key>/state`,
  unique_id `meta_<key>` (slash collapsed) — top-level, not under `total/`.
- `binary_sensor.inverter_bridge_online`: derives state from the
  `availability_topic` (LWT) for the spec §12.3 watchdog automation.
- `force_update: true` on low-granularity sensors (SoC, mode, etc.) so HA
  recorder bumps `last_updated` on every received message even when the
  value did not change (fixes F-6 stale SoC).
- Energy accumulator discovery (`battery_energy_in/out`, `pv_energy`,
  `load_energy`, `grid_energy_in/out`) — values are published by the
  energy_integrator module; this publisher only emits discovery so HA
  knows the entities exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from .config import MqttCfg

log = logging.getLogger(__name__)


# Aggregated sensors (no inverter_N_ prefix on the published key).
# Maps key -> (unit, device_class, state_class).
AGGREGATE_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "battery_state_of_charge":  ("%",   "battery",       "measurement"),
    "battery_voltage":          ("V",   "voltage",       "measurement"),
    "battery_power":            ("W",   "power",         "measurement"),
    "bus_voltage":              ("V",   "voltage",       "measurement"),
    "load_power":               ("W",   "power",         "measurement"),
    "pv_power":                 ("W",   "power",         "measurement"),
    "grid_power":               ("W",   "power",         "measurement"),
    "grid_voltage":             ("V",   "voltage",       "measurement"),
    "grid_frequency":           ("Hz",  "frequency",     "measurement"),
    "mode":                     ("",    None,            None),
    "capacity":                 ("kWh", "energy",        None),
}

# Per-inverter sensors (without the `inverter_N_` prefix; that is added at publish time).
PER_INVERTER_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "battery_current":          ("A",   "current",       "measurement"),
    "battery_voltage":          ("V",   "voltage",       "measurement"),
    "bus_voltage":              ("V",   "voltage",       "measurement"),
    "load_power":               ("W",   "power",         "measurement"),
    "load_power_l1":            ("W",   "power",         "measurement"),
    "load_power_l2":            ("W",   "power",         "measurement"),
    "load_apparent_power":      ("VA",  "apparent_power","measurement"),
    "load_percentage":          ("%",   None,            "measurement"),
    "ac_output_voltage":        ("V",   "voltage",       "measurement"),
    "ac_output_frequency":      ("Hz",  "frequency",     "measurement"),
    "grid_voltage":             ("V",   "voltage",       "measurement"),
    "grid_power":               ("W",   "power",         "measurement"),
    "grid_frequency":           ("Hz",  "frequency",     "measurement"),
    "temperature":              ("°C",  "temperature",   "measurement"),
    "temperature_dc_dc":        ("°C",  "temperature",   "measurement"),
    "temperature_dc_ac":        ("°C",  "temperature",   "measurement"),
    "temperature_transformer":  ("°C",  "temperature",   "measurement"),
    "pv_voltage_mppt1":         ("V",   "voltage",       "measurement"),
    "pv_voltage_mppt2":         ("V",   "voltage",       "measurement"),
    "pv_current":               ("A",   "current",       "measurement"),
    "pv_current_mppt1":         ("A",   "current",       "measurement"),
    "pv_current_mppt2":         ("A",   "current",       "measurement"),
    "pv_power":                 ("W",   "power",         "measurement"),
    "pv_power_mppt1":           ("W",   "power",         "measurement"),
    "pv_power_mppt2":           ("W",   "power",         "measurement"),
    "device_mode":              ("",    None,            None),
    "charge_state":             ("",    None,            None),
}

# Energy accumulators (F-2 discovery). Values are produced by the
# energy_integrator module; the publisher only emits discovery. They are
# aggregated (no `inverter_N_` prefix), so they share the `total_<key>`
# unique_id / `total/<key>` topic convention.
ENERGY_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "battery_energy_in":   ("kWh", "energy", "total_increasing"),
    "battery_energy_out":  ("kWh", "energy", "total_increasing"),
    "pv_energy":           ("kWh", "energy", "total_increasing"),
    "load_energy":         ("kWh", "energy", "total_increasing"),
    "grid_energy_in":      ("kWh", "energy", "total_increasing"),
    "grid_energy_out":     ("kWh", "energy", "total_increasing"),
}

# Diagnostic sensors (F-4). Published top-level under `solar_assistant/_meta/`,
# not under `total/`. unique_id is `meta_<key>` (slash replaced).
META_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "_meta/poll_duration_ms":  ("ms", None,       "measurement"),
    "_meta/crc_fails_total":   ("",   None,       "total_increasing"),
    "_meta/uptime_s":          ("s",  "duration", "total_increasing"),
}

# Low-granularity sensors that should carry `force_update: true` in their
# discovery payload so HA recorder bumps `last_updated` on every received
# message (F-6 fix: SoC was 17 min stale because the value rarely changes).
# Matched by suffix (so `inverter_1_load_percentage` also matches).
FORCE_UPDATE_KEY_SUFFIXES: tuple[str, ...] = (
    "battery_state_of_charge",
    "mode",
    "device_mode",
    "charge_state",
    "load_percentage",
    "inverter_state_code",
    # Grid sensors are always 0 in an offgrid setup; force_update keeps them
    # from going stale in HA (they'd otherwise appear `unavailable` after the
    # default 5 min staleness threshold).
    "grid_voltage",
    "grid_frequency",
    "grid_power",
    "grid_energy_in",
    "grid_energy_out",
    # Battery and PV/load energy accumulators are `state_class: total_increasing`
    # so they only emit DB rows when they increment. When a counter is flat
    # (e.g. `battery_energy_out` while the bank is charging) HA shows the entity
    # as stale because last_updated doesn't refresh. force_update bumps
    # last_updated on every received MQTT message so the watchdog sees activity.
    "battery_energy_in",
    "battery_energy_out",
    "pv_energy",
    "load_energy",
)


def _device_block() -> dict[str, Any]:
    """SA-compatible device block — keeps existing HA device intact."""
    return {
        "identifiers": ["sa_inverter"],
        "name": "SRNE Split-phase x 2",
        "manufacturer": "SRNE",
        "model": "SRNE Split-phase",
    }


def _key_to_topic_path(key: str) -> str:
    """Map an aggregator key to SA's topic path component.

    Aggregated keys (no `inverter_N_` prefix) -> `total/<key>`.
    Per-inverter keys `inverter_N_<rest>` -> `inverter_N/<rest>`.
    `_meta/*` diagnostic keys stay top-level (no `total/` prefix).

    Examples:
        battery_state_of_charge -> total/battery_state_of_charge
        inverter_1_pv_power     -> inverter_1/pv_power
        _meta/uptime_s          -> _meta/uptime_s
    """
    if key.startswith("_meta/"):
        return key
    if key.startswith("inverter_") and "_" in key[len("inverter_"):]:
        # inverter_1_xxx_yyy -> inverter_1/xxx_yyy
        n, rest = key[len("inverter_"):].split("_", 1)
        return f"inverter_{n}/{rest}"
    return f"total/{key}"


def _key_to_unique_id(key: str) -> str:
    """Map an aggregator key to SA's unique_id format.

    Aggregated -> `total_<key>`. Per-inverter -> `inverter_N_<rest>` (unchanged).
    `_meta/<x>` -> `meta_<x>` (slash collapsed; unique_ids can't contain `/`).
    """
    if key.startswith("_meta/"):
        return "meta_" + key[len("_meta/"):]
    if key.startswith("inverter_"):
        return key  # already in `inverter_N_<rest>` form
    return f"total_{key}"


def _needs_force_update(key: str) -> bool:
    """Return True if this key's discovery should set `force_update: true`.

    Used for low-granularity sensors whose value changes rarely (SoC %, mode
    text, charge state, etc.) so HA recorder updates `last_updated` even when
    the new message has the same value as the previous one. See F-6.
    """
    if key.startswith("_meta/"):
        return True
    # Exact match for aggregate keys, or `inverter_N_<suffix>` for per-inverter.
    return any(
        key == suffix or key.endswith(f"_{suffix}")
        for suffix in FORCE_UPDATE_KEY_SUFFIXES
    )


class MqttPublisher:
    def __init__(
        self,
        cfg: MqttCfg,
        *,
        n_inverters: int = 2,
    ) -> None:
        self.cfg = cfg
        self.n_inverters = n_inverters
        self._device = _device_block()
        self._client = mqtt.Client(
            client_id=cfg.client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        # Availability is at the configured prefix root (matches SA: solar_assistant/availability)
        self._availability_topic = f"{cfg.topic_prefix}/availability"
        self._connected = False

    def connect(self) -> None:
        self._client.username_pw_set(self.cfg.username, self.cfg.password)
        self._client.will_set(
            self._availability_topic, payload="offline", qos=self.cfg.qos, retain=True
        )
        self._client.connect(self.cfg.host, self.cfg.port, keepalive=60)
        self._client.loop_start()
        self._connected = True

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self._client.publish(
                self._availability_topic, payload="offline", qos=self.cfg.qos, retain=True
            )
        finally:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    def set_online(self) -> None:
        self._client.publish(
            self._availability_topic, payload="online", qos=self.cfg.qos, retain=True
        )

    def publish_value(self, sensor_key: str, value: float | str) -> None:
        """Publish a value using SA's topic format.

        `sensor_key` is the aggregator-side key (e.g. `battery_voltage` or
        `inverter_1_pv_power`). Topic is derived per SA convention.
        """
        topic = f"{self.cfg.topic_prefix}/{_key_to_topic_path(sensor_key)}/state"
        self._client.publish(topic, payload=f"{value}", qos=self.cfg.qos, retain=False)

    def publish_values(self, kv: dict[str, float | str]) -> None:
        for k, v in kv.items():
            self.publish_value(k, v)

    def publish_discovery(self) -> None:
        """Publish HA Discovery payloads matching SA's unique_id format.

        Overwrites SA's retained discoveries on the same topic, so HA's existing
        entities (registered against SA's unique_ids) keep their entity_id and
        resume receiving state updates from us.

        Also publishes:
        - 6 energy-accumulator sensors (F-2) under aggregate convention.
        - 3 `_meta/*` diagnostic sensors (F-4) top-level.
        - 1 binary_sensor.inverter_bridge_online (F-4) derived from LWT.
        """
        # 1) Aggregated sensors
        for key, (unit, dc, sc) in AGGREGATE_SENSORS.items():
            self._publish_discovery_one(
                key=key,
                unit=unit,
                device_class=dc,
                state_class=sc,
                display_name=key.replace("_", " ").capitalize(),
            )
        # 2) Energy accumulators (F-2)
        for key, (unit, dc, sc) in ENERGY_SENSORS.items():
            self._publish_discovery_one(
                key=key,
                unit=unit,
                device_class=dc,
                state_class=sc,
                display_name=key.replace("_", " ").capitalize(),
            )
        # 3) Per-inverter sensors x N
        for i in range(1, self.n_inverters + 1):
            for sub_key, (unit, dc, sc) in PER_INVERTER_SENSORS.items():
                full_key = f"inverter_{i}_{sub_key}"
                self._publish_discovery_one(
                    key=full_key,
                    unit=unit,
                    device_class=dc,
                    state_class=sc,
                    display_name=f"Inverter {i} - {sub_key.replace('_', ' ').capitalize()}",
                )
        # 4) Meta diagnostic sensors (F-4)
        for key, (unit, dc, sc) in META_SENSORS.items():
            # Strip `_meta/` for the display name.
            short = key[len("_meta/"):]
            self._publish_discovery_one(
                key=key,
                unit=unit,
                device_class=dc,
                state_class=sc,
                display_name=f"Meta {short.replace('_', ' ')}",
            )
        # 5) Binary sensor for daemon online status (F-4, spec §12.3)
        self.publish_binary_sensor_discovery()

    def publish_binary_sensor_discovery(self) -> None:
        """Publish a binary_sensor whose state mirrors the LWT availability topic.

        Allows HA automations to react to the daemon disappearing (spec §12.3
        watchdog) without needing a template sensor. Unique_id is stable so the
        same entity is reused across daemon restarts.
        """
        unique = "inverter_bridge_online"
        config_topic = f"{self.cfg.discovery_prefix}/binary_sensor/{unique}/config"
        payload: dict[str, Any] = {
            "name": "Inverter bridge online",
            "unique_id": unique,
            "object_id": unique,
            "state_topic": self._availability_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "availability_topic": self._availability_topic,
            "device": self._device,
        }
        self._client.publish(
            config_topic,
            payload=json.dumps(payload),
            qos=self.cfg.qos,
            retain=self.cfg.retain_discovery,
        )

    def _publish_discovery_one(
        self,
        *,
        key: str,
        unit: str,
        device_class: str | None,
        state_class: str | None,
        display_name: str,
        force_update: bool | None = None,
    ) -> None:
        unique = _key_to_unique_id(key)
        config_topic = f"{self.cfg.discovery_prefix}/sensor/{unique}/config"
        state_topic = f"{self.cfg.topic_prefix}/{_key_to_topic_path(key)}/state"
        payload: dict[str, Any] = {
            "name": display_name,
            "unique_id": unique,
            "object_id": unique,
            "state_topic": state_topic,
            "availability_topic": self._availability_topic,
            "device": self._device,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if state_class:
            payload["state_class"] = state_class
        # `force_update` decision: caller override > auto by key.
        if force_update is None:
            force_update = _needs_force_update(key)
        if force_update:
            payload["force_update"] = True
        self._client.publish(
            config_topic,
            payload=json.dumps(payload),
            qos=self.cfg.qos,
            retain=self.cfg.retain_discovery,
        )

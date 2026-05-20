"""paho-mqtt publisher with HA Discovery + LWT — Solar Assistant compatible.

Designed to MATCH Solar Assistant's MQTT topology so existing HA entities
(created from SA's retained discovery payloads) keep updating without
requiring dashboard/automation changes.

SA convention (verified 2026-05-20 by inspecting retained discovery payloads
on the home broker before SA was disconnected):

- Topic prefix: `solar_assistant`
- Aggregated sensors: state at `solar_assistant/total/<key>/state`,
  unique_id `total_<key>` (e.g. `total_battery_state_of_charge`).
- Per-inverter sensors: state at `solar_assistant/inverter_<N>/<key>/state`,
  unique_id `inverter_<N>_<key>` (e.g. `inverter_1_pv_voltage`).
- Discovery topic: `homeassistant/sensor/<unique_id>/config` (retained).
- Device identifiers: `["sa_inverter"]`, model "SRNE Split-phase".
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
}

# Per-inverter sensors (without the `inverter_N_` prefix; that is added at publish time).
PER_INVERTER_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "battery_current":          ("A",   "current",       "measurement"),
    "battery_voltage":          ("V",   "voltage",       "measurement"),
    "bus_voltage":              ("V",   "voltage",       "measurement"),
    "load_power":               ("W",   "power",         "measurement"),
    "load_power_1":             ("W",   "power",         "measurement"),
    "load_power_2":             ("W",   "power",         "measurement"),
    "load_apparent_power":      ("VA",  "apparent_power","measurement"),
    "load_percentage":          ("%",   None,            "measurement"),
    "ac_output_voltage":        ("V",   "voltage",       "measurement"),
    "ac_output_frequency":      ("Hz",  "frequency",     "measurement"),
    "grid_voltage_1":           ("V",   "voltage",       "measurement"),
    "grid_frequency":           ("Hz",  "frequency",     "measurement"),
    "temperature":              ("°C",  "temperature",   "measurement"),
    "temperature_dc_dc":        ("°C",  "temperature",   "measurement"),
    "temperature_dc_ac":        ("°C",  "temperature",   "measurement"),
    "temperature_transformer":  ("°C",  "temperature",   "measurement"),
    "pv1_voltage":              ("V",   "voltage",       "measurement"),
    "pv2_voltage":              ("V",   "voltage",       "measurement"),
    "pv_voltage_1":             ("V",   "voltage",       "measurement"),
    "pv_voltage_2":             ("V",   "voltage",       "measurement"),
    "pv_current_1":             ("A",   "current",       "measurement"),
    "pv_current_2":             ("A",   "current",       "measurement"),
    "pv_power":                 ("W",   "power",         "measurement"),
    "pv_power_1":               ("W",   "power",         "measurement"),
    "pv_power_2":               ("W",   "power",         "measurement"),
    "device_mode":              ("",    None,            None),
    "charge_state":             ("",    None,            None),
}


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

    Examples:
        battery_state_of_charge -> total/battery_state_of_charge
        inverter_1_pv_power     -> inverter_1/pv_power
    """
    if key.startswith("inverter_") and "_" in key[len("inverter_"):]:
        # inverter_1_xxx_yyy -> inverter_1/xxx_yyy
        n, rest = key[len("inverter_"):].split("_", 1)
        return f"inverter_{n}/{rest}"
    return f"total/{key}"


def _key_to_unique_id(key: str) -> str:
    """Map an aggregator key to SA's unique_id format.

    Aggregated -> `total_<key>`. Per-inverter -> `inverter_N_<rest>` (unchanged).
    """
    if key.startswith("inverter_"):
        return key  # already in `inverter_N_<rest>` form
    return f"total_{key}"


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
        # 2) Per-inverter sensors x N
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

    def _publish_discovery_one(
        self,
        *,
        key: str,
        unit: str,
        device_class: str | None,
        state_class: str | None,
        display_name: str,
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
        self._client.publish(
            config_topic,
            payload=json.dumps(payload),
            qos=self.cfg.qos,
            retain=self.cfg.retain_discovery,
        )

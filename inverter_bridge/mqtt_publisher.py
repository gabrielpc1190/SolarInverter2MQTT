"""paho-mqtt publisher with HA Discovery + LWT (Last Will & Testament).

Implements the MQTT side of the spec §6 schema. Publishes:
- One discovery payload per sensor (retained) on connect
- One state topic per sensor on each hot/cold cycle
- LWT-driven availability topic (retained, "online" while running, "offline" via will)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from .config import MqttCfg
from .srne_map import FIELDS

log = logging.getLogger(__name__)


# Aggregated/derived sensors (not in FIELDS) we also publish discovery for.
# Schema: {key: (unit, device_class, state_class)}
AGGREGATE_SENSORS: dict[str, tuple[str, str | None, str | None]] = {
    "battery_state_of_charge_2":        ("%",   "battery",       "measurement"),
    "battery_voltage":                  ("V",   "voltage",       "measurement"),
    "battery_power":                    ("W",   "power",         "measurement"),
    "load_power_2":                     ("W",   "power",         "measurement"),
    "pv_power":                         ("W",   "power",         "measurement"),
    "mode":                             ("",    None,            None),
    # Per-inverter aggregated (multi-instance, generated below)
}

# Per-inverter aggregated sensor templates (rendered for each inverter index).
PER_INVERTER_AGGREGATES: dict[str, tuple[str, str | None, str | None]] = {
    "battery_current_2":        ("A",   "current",       "measurement"),
    "battery_voltage_2":        ("V",   "voltage",       "measurement"),
    "bus_voltage_2":            ("V",   "voltage",       "measurement"),
    "load_power":               ("W",   "power",         "measurement"),
    "load_power_1_2":           ("W",   "power",         "measurement"),
    "load_power_2_2":           ("W",   "power",         "measurement"),
    "load_apparent_power_2":    ("VA",  "apparent_power","measurement"),
    "load_percentage_2":        ("%",   None,            "measurement"),
    "ac_output_voltage":        ("V",   "voltage",       "measurement"),
    "ac_output_frequency_2":    ("Hz",  "frequency",     "measurement"),
    "grid_voltage_1_2":         ("V",   "voltage",       "measurement"),
    "grid_frequency":           ("Hz",  "frequency",     "measurement"),
    "temperature_2":            ("°C",  "temperature",   "measurement"),
    "temperature_dc_dc":        ("°C",  "temperature",   "measurement"),
    "temperature_dc_ac":        ("°C",  "temperature",   "measurement"),
    "temperature_transformer":  ("°C",  "temperature",   "measurement"),
    "pv1_voltage":              ("V",   "voltage",       "measurement"),
    "pv2_voltage":              ("V",   "voltage",       "measurement"),
    "pv_voltage_1_2":           ("V",   "voltage",       "measurement"),
    "pv_voltage_2_2":           ("V",   "voltage",       "measurement"),
    "pv_current_1_2":           ("A",   "current",       "measurement"),
    "pv_current_2_2":           ("A",   "current",       "measurement"),
    "pv_power":                 ("W",   "power",         "measurement"),
    "pv_power_1_2":             ("W",   "power",         "measurement"),
    "pv_power_2_2":             ("W",   "power",         "measurement"),
    "device_mode":              ("",    None,            None),
    "charge_state":             ("",    None,            None),
}


def _device_block(model: str, sw_version: str) -> dict[str, Any]:
    return {
        "identifiers": ["gadi_inverters"],
        "name": "home Inverters",
        "manufacturer": "SunGoldPower",
        "model": model,
        "sw_version": sw_version,
    }


class MqttPublisher:
    def __init__(
        self,
        cfg: MqttCfg,
        *,
        n_inverters: int = 2,
        device_model: str = "SR-24031501 split-phase",
        device_sw_version: str = "fw build TBD",
    ) -> None:
        self.cfg = cfg
        self.n_inverters = n_inverters
        self._device = _device_block(device_model, device_sw_version)
        self._client = mqtt.Client(
            client_id=cfg.client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
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
        topic = f"{self.cfg.topic_prefix}/{sensor_key}/state"
        payload = f"{value}"
        self._client.publish(topic, payload=payload, qos=self.cfg.qos, retain=False)

    def publish_values(self, kv: dict[str, float | str]) -> None:
        for k, v in kv.items():
            self.publish_value(k, v)

    def publish_discovery(self) -> None:
        """Publish HA Discovery payloads for every known sensor (retained)."""
        # 1) Raw per-block fields per inverter (so engineers can debug individual blocks)
        for f in FIELDS:
            for i in range(1, self.n_inverters + 1):
                self._publish_discovery_one(
                    key=f"inverter_{i}_{f.key}",
                    unit=f.unit,
                    device_class=f.device_class,
                    state_class=f.state_class,
                )
        # 2) Aggregated single-instance sensors
        for key, (unit, dc, sc) in AGGREGATE_SENSORS.items():
            self._publish_discovery_one(key=key, unit=unit, device_class=dc, state_class=sc)
        # 3) Per-inverter aggregated sensors (×N inverters)
        for key, (unit, dc, sc) in PER_INVERTER_AGGREGATES.items():
            for i in range(1, self.n_inverters + 1):
                self._publish_discovery_one(
                    key=f"inverter_{i}_{key}",
                    unit=unit,
                    device_class=dc,
                    state_class=sc,
                )

    def _publish_discovery_one(
        self,
        *,
        key: str,
        unit: str,
        device_class: str | None,
        state_class: str | None,
    ) -> None:
        unique = f"{self.cfg.topic_prefix}_{key}"
        config_topic = f"{self.cfg.discovery_prefix}/sensor/{unique}/config"
        payload: dict[str, Any] = {
            "name": key.replace("_", " ").title(),
            "unique_id": unique,
            "object_id": unique,
            "state_topic": f"{self.cfg.topic_prefix}/{key}/state",
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

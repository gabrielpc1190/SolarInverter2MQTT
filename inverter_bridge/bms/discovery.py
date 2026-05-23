"""MQTT discovery payload generators para las entidades BMS BlueSun.

Genera 86 payloads de discovery para HA bajo prefix `bluesun_*` (cleaner que
el viejo `panel_cuartoelectrico_bluesun_*` del firmware Panel S3). Los viejos
quedan orphan/unavailable en HA hasta limpieza manual; los dashboards y
automations se migran a referenciar `sensor.bluesun_*` por aparte.

Estructura del catálogo:
  - PIA per pack (7 * 4 = 28): V/I/SoC/SoH/cycles/remaining_Ah/nominal_Ah
  - PIB per pack (10 * 4 = 40): cell V min/max/avg/delta + 4 cell temps + env + pcb
  - Bank aggregates (12): avg V, sum I, SoC avg/spread, power total/+/-, remaining/nominal Ah, min SoH, max cycles, max cell temp
  - Integration energy (2): battery_energy_in/out (`total_increasing`)
  - Serials (4): pack01..04_serial (text-like)

Topic layout:
  Discovery:    homeassistant/sensor/<object_id>/config
  State:        <topic_prefix>/<object_id>/state
  Availability: <topic_prefix>/availability  (online/offline LWT)

object_id ya viene con prefix `bluesun_` → entity_id resultante:
  sensor.bluesun_pack01_voltage, sensor.bluesun_bank_soc_avg, etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EntityDef:
    """Definición de una entidad MQTT discovery para el BMS."""
    object_id: str                       # `bluesun_pack01_voltage`, etc — match el slug HA actual
    name: str                            # friendly name (`BlueSun Pack01 Voltage`)
    unit: str | None = None              # unit_of_measurement
    device_class: str | None = None      # voltage, current, battery, power, energy, temperature
    state_class: str | None = "measurement"  # measurement | total_increasing
    icon: str | None = None              # mdi:xxx si no hay device_class
    accuracy: int | None = None          # decimales (suggested_display_precision)
    force_update: bool = False           # útil para low-granularity sensors
    entity_category: str | None = None   # diagnostic | config


# ─── Generadores per-pack ────────────────────────────────────────────


def _pia_entities_for_pack(p: int) -> list[EntityDef]:
    """7 entidades PIA por pack."""
    pp = f"pack{p:02d}"
    PP = f"Pack{p:02d}"
    return [
        EntityDef(f"bluesun_{pp}_voltage", f"{PP} Voltage", "V", "voltage", accuracy=2),
        EntityDef(f"bluesun_{pp}_current", f"{PP} Current", "A", "current", accuracy=2),
        EntityDef(f"bluesun_{pp}_soc", f"{PP} SoC", "%", "battery", accuracy=1),
        EntityDef(
            f"bluesun_{pp}_soh", f"{PP} SoH", "%", None,
            icon="mdi:battery-heart-variant", accuracy=1,
        ),
        EntityDef(
            f"bluesun_{pp}_cycles", f"{PP} Cycles", None, None,
            state_class="total_increasing", icon="mdi:counter", accuracy=0,
        ),
        EntityDef(
            f"bluesun_{pp}_remaining_ah", f"{PP} Remaining Ah", "Ah", None,
            icon="mdi:battery-outline", accuracy=2,
        ),
        EntityDef(
            f"bluesun_{pp}_nominal_ah", f"{PP} Nominal Ah", "Ah", None,
            icon="mdi:battery-high", accuracy=2,
        ),
    ]


def _pib_entities_for_pack(p: int) -> list[EntityDef]:
    """10 entidades PIB por pack: 4 cell V stats + 4 cell temps + env + pcb."""
    pp = f"pack{p:02d}"
    PP = f"Pack{p:02d}"
    base: list[EntityDef] = [
        EntityDef(
            f"bluesun_{pp}_cell_v_min", f"{PP} Cell V Min", "mV", "voltage", accuracy=0,
        ),
        EntityDef(
            f"bluesun_{pp}_cell_v_max", f"{PP} Cell V Max", "mV", "voltage", accuracy=0,
        ),
        EntityDef(
            f"bluesun_{pp}_cell_v_avg", f"{PP} Cell V Avg", "mV", "voltage", accuracy=0,
        ),
        EntityDef(
            f"bluesun_{pp}_cell_v_delta", f"{PP} Cell V Delta", "mV", None,
            icon="mdi:arrow-expand-vertical", accuracy=0,
        ),
    ]
    # 4 cell temps
    for i in range(1, 5):
        base.append(EntityDef(
            f"bluesun_{pp}_cell_temp_{i}", f"{PP} Cell Temp {i}", "°C", "temperature",
            accuracy=1,
        ))
    base.append(EntityDef(
        f"bluesun_{pp}_env_temp", f"{PP} Env Temp", "°C", "temperature", accuracy=1,
    ))
    base.append(EntityDef(
        f"bluesun_{pp}_pcb_temp", f"{PP} PCB Temp", "°C", "temperature", accuracy=1,
    ))
    return base


def _serial_entities() -> list[EntityDef]:
    """4 entidades serial (text-like, sin unit/device_class)."""
    return [
        EntityDef(
            f"bluesun_pack{p:02d}_serial", f"Pack{p:02d} Serial", None, None,
            state_class=None, icon="mdi:identifier",
        )
        for p in range(1, 5)
    ]


def _bank_entities() -> list[EntityDef]:
    """12 entidades bank-level."""
    return [
        EntityDef("bluesun_bank_voltage_avg", "Bank Voltage Avg", "V", "voltage", accuracy=2),
        EntityDef("bluesun_bank_current_total", "Bank Current Total", "A", "current", accuracy=2),
        EntityDef("bluesun_bank_soc_avg", "Bank SoC Avg", "%", "battery", accuracy=1),
        EntityDef(
            "bluesun_bank_soc_spread", "Bank SoC Spread", "%", None,
            icon="mdi:arrow-expand-horizontal", accuracy=1,
        ),
        EntityDef("bluesun_bank_power", "Bank Power", "W", "power", accuracy=0),
        EntityDef(
            "bluesun_bank_power_charging", "Bank Power Charging", "W", "power",
            accuracy=0, icon="mdi:battery-plus-variant",
        ),
        EntityDef(
            "bluesun_bank_power_discharging", "Bank Power Discharging", "W", "power",
            accuracy=0, icon="mdi:battery-minus-variant",
        ),
        EntityDef(
            "bluesun_bank_remaining_ah", "Bank Remaining Ah", "Ah", None,
            icon="mdi:battery-outline", accuracy=2,
        ),
        EntityDef(
            "bluesun_bank_nominal_ah", "Bank Nominal Ah", "Ah", None,
            icon="mdi:battery-high", accuracy=2,
        ),
        EntityDef(
            "bluesun_bank_min_soh", "Bank Min SoH", "%", None,
            icon="mdi:battery-heart-variant", accuracy=1,
        ),
        EntityDef(
            "bluesun_bank_max_cycles", "Bank Max Cycles", None, None,
            icon="mdi:counter", accuracy=0,
        ),
        EntityDef(
            "bluesun_bank_max_cell_temp", "Bank Max Cell Temp", "°C", "temperature",
            accuracy=1,
        ),
    ]


def _energy_entities() -> list[EntityDef]:
    """2 entidades de integration energy (Wh acumulado)."""
    return [
        EntityDef(
            "bluesun_battery_energy_in", "Battery Energy In", "Wh", "energy",
            state_class="total_increasing", icon="mdi:battery-arrow-up", accuracy=2,
        ),
        EntityDef(
            "bluesun_battery_energy_out", "Battery Energy Out", "Wh", "energy",
            state_class="total_increasing", icon="mdi:battery-arrow-down", accuracy=2,
        ),
    ]


def _telemetry_entities() -> list[EntityDef]:
    """Entidades diagnósticas para monitorear salud del daemon BMS."""
    return [
        EntityDef(
            "bluesun_octopus_parses_ok", "Octopus Parses OK", None, None,
            state_class="total_increasing", icon="mdi:counter",
            accuracy=0, entity_category="diagnostic",
        ),
    ]


def all_bms_entities() -> list[EntityDef]:
    """Catálogo: 28 PIA + 40 PIB + 4 serial + 12 bank + 2 energy + 1 telemetry = 87."""
    out: list[EntityDef] = []
    for p in (1, 2, 3, 4):
        out.extend(_pia_entities_for_pack(p))
    for p in (1, 2, 3, 4):
        out.extend(_pib_entities_for_pack(p))
    out.extend(_serial_entities())
    out.extend(_bank_entities())
    out.extend(_energy_entities())
    out.extend(_telemetry_entities())
    return out


# ─── Discovery payload builder ───────────────────────────────────────


def build_discovery_payload(
    entity: EntityDef,
    *,
    topic_prefix: str,
    device_id: str,
    device_name: str,
    availability_topic: str,
) -> dict[str, Any]:
    """Construye el payload JSON de discovery HA para una entidad.

    entity.object_id ya viene como slug HA completo (ej. `bluesun_pack01_voltage`).

    Discovery topic: homeassistant/sensor/<object_id>/config
    State topic:     <topic_prefix>/<object_id>/state
    Entity_id resultante: sensor.<object_id>
    """
    payload: dict[str, Any] = {
        "name": entity.name,
        "unique_id": entity.object_id,
        "object_id": entity.object_id,
        "state_topic": f"{topic_prefix}/{entity.object_id}/state",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [device_id],
            "name": device_name,
            "manufacturer": "BlueSun / XZH-ElecTech",
            "model": "BMS16S200A-SP05B (4-pack 16s LiFePO4)",
            "configuration_url": "https://github.com/gabrielpc1190/SolarInverter2MQTT",
        },
    }
    if entity.unit is not None:
        payload["unit_of_measurement"] = entity.unit
    if entity.device_class is not None:
        payload["device_class"] = entity.device_class
    if entity.state_class is not None:
        payload["state_class"] = entity.state_class
    if entity.icon is not None:
        payload["icon"] = entity.icon
    if entity.accuracy is not None:
        payload["suggested_display_precision"] = entity.accuracy
    if entity.force_update:
        payload["force_update"] = True
    if entity.entity_category is not None:
        payload["entity_category"] = entity.entity_category
    return payload


def discovery_topic_for(entity: EntityDef, *, ha_prefix: str = "homeassistant") -> str:
    return f"{ha_prefix}/sensor/{entity.object_id}/config"


def state_topic_for(entity: EntityDef, *, topic_prefix: str) -> str:
    return f"{topic_prefix}/{entity.object_id}/state"

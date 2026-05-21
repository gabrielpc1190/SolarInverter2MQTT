"""Central register map for SRNE split-phase inverters (model SR-24031501).

Each entry: (block_addr, offset_in_block) -> (sensor_key, scale, signed, unit, ...).
Block constants are at the top; the BLOCKS list is the source of truth for
which blocks the daemon polls.

Source of truth for the mapping: spec section 5.6-5.10 in
docs/superpowers/specs/2026-05-19-inverter-bridge-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BlockTier(Enum):
    HOT = "hot"      # poll every 3 s
    COLD = "cold"    # poll every 60 s


@dataclass(frozen=True, slots=True)
class Block:
    addr: int
    count: int
    name: str
    tier: BlockTier


@dataclass(frozen=True, slots=True)
class Field:
    """Maps an offset inside a block to a sensor."""

    block_addr: int
    offset: int
    key: str
    scale: float
    signed: bool
    unit: str
    device_class: str | None
    state_class: str | None


BLOCKS: list[Block] = [
    # Hot — polled every 3 s
    Block(0x0100, 15, "battery",       BlockTier.HOT),
    Block(0x0210, 19, "state",         BlockTier.HOT),
    Block(0x0223, 23, "phase_b",       BlockTier.HOT),  # Inverter/load phase B (L2). Renamed 2026-05-21 from "pv_temps_l2" — none of the offsets were actually PV per V1.96 spec.
    # Cold — polled every 60 s
    Block(0x0014, 10, "device_info",   BlockTier.COLD),
    Block(0x0020, 16, "fw_build_date", BlockTier.COLD),  # ASCII "Apr 18 2025 09:27:26"
    Block(0x0030, 16, "fw_model",      BlockTier.COLD),  # ASCII "SR-24031501..."
    Block(0x0040, 8,  "fw_serial",     BlockTier.COLD),  # ASCII continuation
    Block(0x010F, 3,  "pv2",           BlockTier.HOT),  # PV2 V/I/P per V1.96 spec (NOT BMS as previously mislabeled). Promoted to HOT so PV2 readings refresh every 3s.
    Block(0x0204, 6,  "faults",        BlockTier.COLD),
    Block(0xE116, 11, "config",        BlockTier.COLD),
    Block(0xE000, 8,  "thresholds",    BlockTier.COLD),
    Block(0xF000, 8,  "runtime_ctrs",  BlockTier.COLD),
    Block(0xF02C, 18, "daily_stats",   BlockTier.COLD),
]


FIELDS: list[Field] = [
    # battery block 0x0100..0x010E
    Field(0x0100, 0,  "battery_state_of_charge", 1.0,  False, "%",  "battery",  "measurement"),
    Field(0x0100, 1,  "battery_voltage",         0.1,  False, "V",  "voltage",  "measurement"),
    # Sign convention STANDARD (positive = charging, negative = discharging).
    # Cross-validated against SA's historical battery_power sensor empirically.
    Field(0x0100, 2,  "battery_current",         0.1,  True,  "A",  "current",  "measurement"),
    # PV1 — registers 0x0107..0x0109 per SRNE V1.96 spec. Direct V/I/P from
    # firmware MPPT measurement. Verified empirically 2026-05-21: at night
    # all three regs report 0 (matches reality, no sun).
    Field(0x0100, 7,  "pv1_voltage",             0.1,  False, "V",  "voltage",  "measurement"),  # 0x0107
    Field(0x0100, 8,  "pv1_current",             0.1,  False, "A",  "current",  "measurement"),  # 0x0108
    Field(0x0100, 9,  "pv1_power",               1.0,  False, "W",  "power",    "measurement"),  # 0x0109
    Field(0x0100, 11, "charge_state_code",       1.0,  False, "",   None,       None),  # 0x010B

    # state block 0x0210..0x0222
    Field(0x0210, 0,  "inverter_state_code",     1.0,  False, "",   None,       None),
    Field(0x0210, 1,  "inverter_substate_code",  1.0,  False, "",   None,       None),
    # 0x0212 raw=5220 → 522.0 V is the HV DC bus (post-boost converter, NOT battery V).
    # SA historical range 495-570V (mean 518V) confirms scale 0.1 is correct.
    # The earlier interpretation "bus = battery V" was a coincidence: raw x 0.01 happens
    # to equal battery V only because boost ratio ≈ 10. Fixed 2026-05-20 (F-8).
    Field(0x0210, 2,  "bus_voltage",             0.1,  False, "V",  "voltage",  "measurement"),
    Field(0x0210, 3,  "grid_voltage_l1",         0.1,  False, "V",  "voltage",  "measurement"),
    Field(0x0210, 4,  "grid_current_l1",         0.1,  False, "A",  "current",  "measurement"),
    Field(0x0210, 5,  "grid_frequency",          0.01, False, "Hz", "frequency","measurement"),
    Field(0x0210, 6,  "ac_output_voltage_l1",    0.1,  False, "V",  "voltage",  "measurement"),
    Field(0x0210, 7,  "ac_output_current_l1",    0.1,  False, "A",  "current",  "measurement"),
    Field(0x0210, 8,  "ac_output_frequency",     0.01, False, "Hz", "frequency","measurement"),
    Field(0x0210, 9,  "load_percent_alt",        1.0,  False, "%",  None,       "measurement"),  # 0x0219
    # 0x021B is LOAD PHASE A active power ONLY (per V1.96 spec), NOT a sum.
    # Bug found 2026-05-21: comment "sum L1+L2" was wrong; bridge was under-
    # reporting site load by ~50% on balanced 240V loads. Fix: read 0x0232
    # (Load Phase B active) separately and sum in aggregator.
    Field(0x0210, 11, "load_active_phase_a",     1.0,  False, "W",  "power",    "measurement"),  # 0x021B
    Field(0x0210, 12, "load_apparent_phase_a",   1.0,  False, "VA", "apparent_power","measurement"),  # 0x021C
    Field(0x0210, 15, "load_percent",            1.0,  False, "%",  None,       "measurement"),  # 0x021F
    Field(0x0210, 16, "temperature_dc_dc",       0.1,  False, "°C", "temperature","measurement"),
    Field(0x0210, 17, "temperature_dc_ac",       0.1,  False, "°C", "temperature","measurement"),
    Field(0x0210, 18, "temperature_transformer", 0.1,  False, "°C", "temperature","measurement"),

    # L2 / phase B block 0x0223..0x0239 — corrected mapping per SRNE V1.96 PDF
    # (2026-05-21). Previously this block was named "pv_temps_l2" because the
    # bridge incorrectly interpreted offsets as PV V/I:
    #   - 0x0228/0x0229 were called "pv1_voltage"/"pv2_voltage" — but they are
    #     NOT in V1.96 spec. Values (~253V at night) appear to track
    #     bus_voltage / 2 (internal HV DC rail), not PV.
    #   - 0x0232/0x0234 were called "pv1_current"/"pv2_current" scale 0.01 —
    #     but per spec they are Load Phase B active and apparent power in W/VA.
    # The "verified empirically via energy balance within 3%" claim was the
    # coincidental result of 4 simultaneous bugs that cancelled: load
    # under-counted by 2x, battery sign inverted, "PV current" actually L2
    # active W, "PV voltage" actually internal bus V. Real PV V/I/P live in
    # block 0x0100 (PV1) and 0x010F (PV2) per spec.
    Field(0x0223, 9,  "ac_output_voltage_l2",    0.1,  False, "V",  "voltage",  "measurement"),   # 0x022C inverter phase_B out V
    Field(0x0223, 11, "ac_output_current_l2",    0.1,  False, "A",  "current",  "measurement"),   # 0x022E inverter phase_B inductive I
    Field(0x0223, 13, "load_current_phase_b",    0.1,  False, "A",  "current",  "measurement"),   # 0x0230 Load Phase B current
    Field(0x0223, 15, "load_active_phase_b",     1.0,  False, "W",  "power",    "measurement"),   # 0x0232 Load Phase B active power
    Field(0x0223, 17, "load_apparent_phase_b",   1.0,  False, "VA", "apparent_power","measurement"),# 0x0234 Load Phase B apparent power

    # PV2 block 0x010F..0x0111 — per V1.96 spec. Block name was "bms" in the
    # BLOCKS list but the address range is PV2, not BMS. Renamed 2026-05-21.
    Field(0x010F, 0,  "pv2_voltage",             0.1,  False, "V",  "voltage",  "measurement"),   # 0x010F
    Field(0x010F, 1,  "pv2_current",             0.1,  False, "A",  "current",  "measurement"),   # 0x0110
    Field(0x010F, 2,  "pv2_power",               1.0,  True,  "W",  "power",    "measurement"),   # 0x0111 (signed for safety)

    # device_info block 0x0014..0x001D — diagnostic, polled cold every 60 s.
    # Verified 2026-05-20 against SRNE V1.96 PDF (raw 818 -> "V8.18", etc.).
    Field(0x0014, 0,  "firmware_version",        0.01, False, "",   None,       None),     # 0x0014 SoftWareVersion
    Field(0x0014, 3,  "hardware_version",        0.01, False, "",   None,       None),     # 0x0017 HardWareVersion power-board

    # runtime_counters block 0xF000..0xF007 — daily PV history (kWh), last 7 days.
    # Verified 2026-05-20 against SRNE V1.96 PDF.
    Field(0xF000, 0,  "pv_energy_yesterday",     0.1,  False, "kWh","energy",   "measurement"),  # 0xF000
    Field(0xF000, 1,  "pv_energy_2_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF001
    Field(0xF000, 2,  "pv_energy_3_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF002
    Field(0xF000, 3,  "pv_energy_4_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF003
    Field(0xF000, 4,  "pv_energy_5_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF004
    Field(0xF000, 5,  "pv_energy_6_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF005
    Field(0xF000, 6,  "pv_energy_7_days_ago",    0.1,  False, "kWh","energy",   "measurement"),  # 0xF006

    # daily_stats block 0xF02C..0xF03D — today's accumulators, reset daily.
    # Verified 2026-05-20 against SRNE V1.96 PDF; values match observed
    # magnitudes (e.g. raw 163 -> "163 Ah charged today", raw 183 -> "18.3 kWh PV today").
    Field(0xF02C, 1,  "battery_charge_ah_today",     1.0, False, "Ah", None,      "total_increasing"),  # 0xF02D
    Field(0xF02C, 2,  "battery_discharge_ah_today",  1.0, False, "Ah", None,      "total_increasing"),  # 0xF02E
    Field(0xF02C, 3,  "pv_energy_today",             0.1, False, "kWh","energy",  "total_increasing"),  # 0xF02F
    Field(0xF02C, 4,  "load_energy_today",           0.1, False, "kWh","energy",  "total_increasing"),  # 0xF030
]


def fields_for(block_addr: int) -> list[Field]:
    """Return all fields defined for a given block address."""
    return [f for f in FIELDS if f.block_addr == block_addr]


# State code lookup ("MachineState" per timbit123/srne-modbus modbus.py).
# Per spec §5.9: vendor labels mapped to SA-friendly labels.
INVERTER_STATE_LOOKUP: dict[int, str] = {
    0: "Initialization",
    1: "Standby",
    2: "Grid",     # vendor: "AC power operation"
    3: "Battery",  # vendor: "Inverter operation" (confirmed empirically 2026-05-20)
}


# Charge state code lookup (0x010B). Per spec §5.11.
# Only `1` observed empirically (PV mid-day); rest inferred.
CHARGE_STATE_LOOKUP: dict[int, str] = {
    0: "Idle",
    1: "PV charging",
    2: "Grid charging",
    3: "Float",
}


def ascii_decode_regs(regs: list[int]) -> str:
    """Decode register words as ASCII chars (1 char per reg, low byte = ASCII).

    Used for blocks 0x0020 (build date), 0x0030 (model+serial), 0x0040 (serial cont).
    """
    chars: list[str] = []
    for r in regs:
        b = r & 0xFF
        if 0x20 <= b <= 0x7E:
            chars.append(chr(b))
    return "".join(chars).strip()

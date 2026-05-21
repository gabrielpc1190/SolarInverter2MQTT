"""Aggregate per-inverter ParsedBlock dicts into per-inverter + total sensors.

Output keys follow an explicit suffix convention so they aren't ambiguous:

- Aggregated (whole site): `battery_voltage`, `pv_power`, `load_power`, etc.
- Per-inverter: `inverter_<N>_<key>`.
- AC split-phase suffix: `_l1` / `_l2` (e.g. `load_power_l1`, `load_power_l2`).
- PV string suffix: `_mppt1` / `_mppt2` (e.g. `pv_voltage_mppt1`).

The earlier `_1` / `_2` suffix scheme was ambiguous (meant phase for AC and
string for PV) and got fully replaced 2026-05-20.
"""

from __future__ import annotations

from .parsers import ParsedBlock
from .srne_map import CHARGE_STATE_LOOKUP, INVERTER_STATE_LOOKUP


def aggregate_inverters(
    per_inverter: list[dict[str, ParsedBlock]],
) -> dict[str, float | str]:
    """Combine readings from N inverters into the SA-compatible sensor set."""
    out: dict[str, float | str] = {}

    # Battery (shared bank)
    socs: list[float] = []
    vs: list[float] = []
    bank_currents: list[float] = []
    for i, inv in enumerate(per_inverter, start=1):
        b = inv.get("battery")
        if b is None:
            continue
        soc = b.fields["battery_state_of_charge"]
        v = b.fields["battery_voltage"]
        i_a = b.fields["battery_current"]
        socs.append(soc)
        vs.append(v)
        bank_currents.append(i_a)
        out[f"inverter_{i}_battery_current"] = i_a
        out[f"inverter_{i}_battery_voltage"] = v
        cs_code = int(b.fields.get("charge_state_code", -1))
        out[f"inverter_{i}_charge_state"] = CHARGE_STATE_LOOKUP.get(
            cs_code, f"unknown_{cs_code}"
        )
    if socs:
        out["battery_state_of_charge"] = round(sum(socs) / len(socs), 1)
        out["battery_voltage"] = round(sum(vs) / len(vs), 2)
        avg_v = sum(vs) / len(vs)
        total_i = sum(bank_currents)
        # SA convention: positive = charging
        out["battery_power"] = round(avg_v * total_i, 1)

    # State block: load Phase A active/apparent + grid (Phase A) + temps + mode.
    # NB: per V1.96 spec, register 0x021B is Load *Phase A* active power, NOT
    # the L1+L2 sum (the previous comment was wrong, causing the bridge to
    # under-report site load by ~50% on balanced 240V loads). Phase B active
    # power comes from the separate phase_b block below; per-inverter
    # `load_power` is the sum of both phases.
    inv1_state: ParsedBlock | None = None
    site_load_active_total = 0.0
    site_load_any = False
    for i, inv in enumerate(per_inverter, start=1):
        s = inv.get("state")
        if s is None:
            continue
        if i == 1:
            inv1_state = s
        active_a = s.fields["load_active_phase_a"]
        # Sum L1 + L2 active to get this inverter's TOTAL real load delivery.
        phase_b = inv.get("phase_b")
        active_b = phase_b.fields["load_active_phase_b"] if phase_b is not None else 0.0
        total_active = active_a + active_b
        out[f"inverter_{i}_load_power"] = round(total_active, 1)
        out[f"inverter_{i}_load_power_l1"] = round(active_a, 1)
        out[f"inverter_{i}_load_power_l2"] = round(active_b, 1)
        site_load_active_total += total_active
        site_load_any = True
        # Apparent power: per-leg from registers (when both available).
        apparent_a = s.fields["load_apparent_phase_a"]
        out[f"inverter_{i}_load_apparent_power_l1"] = round(apparent_a, 1)
        if phase_b is not None and "load_apparent_phase_b" in phase_b.fields:
            apparent_b = phase_b.fields["load_apparent_phase_b"]
            out[f"inverter_{i}_load_apparent_power_l2"] = round(apparent_b, 1)
            out[f"inverter_{i}_load_apparent_power"] = round(apparent_a + apparent_b, 1)
        else:
            out[f"inverter_{i}_load_apparent_power"] = round(apparent_a, 1)
        out[f"inverter_{i}_load_percentage"] = s.fields["load_percent"]
        out[f"inverter_{i}_ac_output_frequency"] = s.fields["ac_output_frequency"]
        out[f"inverter_{i}_grid_frequency"] = s.fields["grid_frequency"]
        # Grid: inverter only exposes Phase A. Keep single per-inverter sensor.
        out[f"inverter_{i}_grid_voltage"] = s.fields["grid_voltage_l1"]
        out[f"inverter_{i}_grid_power"] = round(
            s.fields["grid_voltage_l1"] * s.fields["grid_current_l1"], 1
        )
        # Temperatures
        t_dc_dc = s.fields["temperature_dc_dc"]
        t_dc_ac = s.fields["temperature_dc_ac"]
        t_trans = s.fields["temperature_transformer"]
        out[f"inverter_{i}_temperature"] = round(max(t_dc_dc, t_dc_ac, t_trans), 1)
        out[f"inverter_{i}_temperature_dc_dc"] = t_dc_dc
        out[f"inverter_{i}_temperature_dc_ac"] = t_dc_ac
        out[f"inverter_{i}_temperature_transformer"] = t_trans
        # device_mode text
        code = int(s.fields["inverter_state_code"])
        out[f"inverter_{i}_device_mode"] = INVERTER_STATE_LOOKUP.get(
            code, f"unknown_code_{code}"
        )
        out[f"inverter_{i}_bus_voltage"] = s.fields["bus_voltage"]
        # Combined L1+L2 AC output voltage (~240 V)
        if phase_b is not None and "ac_output_voltage_l2" in phase_b.fields:
            v_l1 = s.fields["ac_output_voltage_l1"]
            v_l2 = phase_b.fields["ac_output_voltage_l2"]
            out[f"inverter_{i}_ac_output_voltage"] = round(v_l1 + v_l2, 1)
    if site_load_any:
        out["load_power"] = round(site_load_active_total, 1)
        # Site-wide mode = inv1's mode (they sync in split-phase)
        if "inverter_1_device_mode" in out:
            out["mode"] = out["inverter_1_device_mode"]

    # Aggregated grid/output (from inv1's state — phases sync)
    if inv1_state is not None:
        out["grid_voltage"] = inv1_state.fields["grid_voltage_l1"]
        out["grid_frequency"] = inv1_state.fields["grid_frequency"]
        out["grid_power"] = round(
            inv1_state.fields["grid_voltage_l1"] * inv1_state.fields["grid_current_l1"], 1
        )
        out["bus_voltage"] = inv1_state.fields["bus_voltage"]

    # PV — read DIRECTLY from firmware MPPT registers per V1.96 spec.
    #   PV1: block "battery" (0x0100), offsets 7/8/9 = 0x0107/0x0108/0x0109 (V/I/P)
    #   PV2: block "pv2"     (0x010F), offsets 0/1/2 = 0x010F/0x0110/0x0111 (V/I/P)
    # Previously the bridge multiplied (bus_voltage/2) x (L2 active power)
    # which yielded ~1500W phantom PV at night and required a charge_state
    # gate as a band-aid. Real registers report 0 at night naturally, so
    # no gate is needed.
    pv_total = 0.0
    pv_any = False
    for i, inv in enumerate(per_inverter, start=1):
        bat = inv.get("battery")
        pv2_block = inv.get("pv2")
        pv1_v = bat.fields.get("pv1_voltage", 0.0) if bat is not None else 0.0
        pv1_i = bat.fields.get("pv1_current", 0.0) if bat is not None else 0.0
        pv1_p = bat.fields.get("pv1_power", 0.0) if bat is not None else 0.0
        pv2_v = pv2_block.fields.get("pv2_voltage", 0.0) if pv2_block is not None else 0.0
        pv2_i = pv2_block.fields.get("pv2_current", 0.0) if pv2_block is not None else 0.0
        pv2_p = pv2_block.fields.get("pv2_power", 0.0) if pv2_block is not None else 0.0
        # pv2_power is signed in the register (defensive); negative or sentinel
        # values mean idle MPPT — clamp to 0 so dashboards aren't surprised.
        if pv2_p < 0:
            pv2_p = 0.0
        if bat is not None or pv2_block is not None:
            pv_any = True
        pv_total += pv1_p + pv2_p
        out[f"inverter_{i}_pv_voltage_mppt1"] = pv1_v
        out[f"inverter_{i}_pv_voltage_mppt2"] = pv2_v
        out[f"inverter_{i}_pv_current_mppt1"] = pv1_i
        out[f"inverter_{i}_pv_current_mppt2"] = pv2_i
        out[f"inverter_{i}_pv_current"] = round(pv1_i + pv2_i, 2)
        out[f"inverter_{i}_pv_power_mppt1"] = round(pv1_p, 1)
        out[f"inverter_{i}_pv_power_mppt2"] = round(pv2_p, 1)
        out[f"inverter_{i}_pv_power"] = round(pv1_p + pv2_p, 1)
    if pv_any:
        out["pv_power"] = round(pv_total, 1)

    # NOTE: `capacity` (rated battery kWh) is intentionally NOT published.
    # Removed 2026-05-20 because:
    #   1. SA published it as a hardcoded static value (72.6) — never a real
    #      measurement, just a config setting users entered in SA's web UI.
    #   2. The value is wrong for most installs (Gabriel's bank is 57 kWh
    #      actual; 72.6 was a never-corrected SA factory hint).
    #   3. Without a state_class HA Statistics throws "no longer has state
    #      class" warnings on the orphaned long-term stats.
    #   4. The authoritative bank-capacity reference belongs in HA as an
    #      `input_number.capacidad_bateria_kwh` helper that your automations
    #      can edit; the inverter has no business owning that number.
    # If you really need a static capacity entity in HA, define it client-side
    # as an `input_number` or `template` sensor.

    # Daily stats + device info + 7-day historical PV — cold block extractions.
    # Each block is optional; when not present (because we're in a hot-only
    # cycle), those keys are simply not emitted. Per-inverter keys + total sums.
    daily_keys = ("battery_charge_ah_today", "battery_discharge_ah_today",
                  "pv_energy_today", "load_energy_today")
    history_keys = tuple(f"pv_energy_{d}" for d in
                         ("yesterday", "2_days_ago", "3_days_ago", "4_days_ago",
                          "5_days_ago", "6_days_ago", "7_days_ago"))
    diag_keys = ("firmware_version", "hardware_version")
    daily_sums: dict[str, float] = {}

    for i, inv in enumerate(per_inverter, start=1):
        daily = inv.get("daily_stats")
        if daily is not None:
            for k in daily_keys:
                if k in daily.fields:
                    v = daily.fields[k]
                    out[f"inverter_{i}_{k}"] = v
                    daily_sums[k] = daily_sums.get(k, 0.0) + v
        ctrs = inv.get("runtime_ctrs")
        if ctrs is not None:
            for k in history_keys:
                if k in ctrs.fields:
                    out[f"inverter_{i}_{k}"] = ctrs.fields[k]
        info = inv.get("device_info")
        if info is not None:
            for k in diag_keys:
                if k in info.fields:
                    out[f"inverter_{i}_{k}"] = info.fields[k]
    for k, total in daily_sums.items():
        out[k] = round(total, 2)

    return out

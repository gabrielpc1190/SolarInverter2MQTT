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

    # State block: load + temps + AC output + per-inverter mode
    inv_active_powers: list[float] = []
    inv1_state: ParsedBlock | None = None
    for i, inv in enumerate(per_inverter, start=1):
        s = inv.get("state")
        if s is None:
            continue
        if i == 1:
            inv1_state = s
        active_p = s.fields["inverter_active_power"]
        inv_active_powers.append(active_p)
        out[f"inverter_{i}_load_power"] = active_p
        out[f"inverter_{i}_load_apparent_power"] = s.fields["inverter_apparent_power_l1"]
        out[f"inverter_{i}_load_percentage"] = s.fields["load_percent"]
        out[f"inverter_{i}_ac_output_frequency"] = s.fields["ac_output_frequency"]
        out[f"inverter_{i}_grid_frequency"] = s.fields["grid_frequency"]
        # Grid: inverter only exposes the L1 phase reading, so we publish a
        # single per-inverter `grid_voltage` and `grid_power` (no phase suffix).
        out[f"inverter_{i}_grid_voltage"] = s.fields["grid_voltage_l1"]
        out[f"inverter_{i}_grid_power"] = round(
            s.fields["grid_voltage_l1"] * s.fields["grid_current_l1"], 1
        )
        # AC output per phase (V_phase * I_phase)
        out[f"inverter_{i}_load_power_l1"] = round(
            s.fields["ac_output_voltage_l1"] * s.fields["ac_output_current_l1"], 1
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
    if inv_active_powers:
        out["load_power"] = round(sum(inv_active_powers), 1)
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

    # PV + L2 (split-phase) + per-phase L2 apparent
    # Note: PV1/PV2 are stored as CURRENTS (A) in the inverter registers; we compute
    # power as V x I. Confirmed empirically 2026-05-20 (energy balance).
    #
    # Night-time gate (added 2026-05-20): when the inverter is NOT charging from
    # PV (charge_state_code ∉ {1=PV, 3=Float}), the pv_voltage / pv_current
    # registers report fake values (pv_voltage tracks bus_voltage/2, pv_current
    # is a stray reading from the boost converter, not a real solar measurement).
    # Without this gate the bridge reports ~1500-1700 W of phantom PV at night,
    # which breaks energy balance and trips `binary_sensor.solar_excess_stable`.
    PV_ACTIVE_CHARGE_CODES = {1, 3}  # 1 = PV charging, 3 = Float (PV-driven)
    pv_total = 0.0
    pv_any = False
    for i, inv in enumerate(per_inverter, start=1):
        pv = inv.get("pv_temps_l2")
        if pv is None:
            continue
        pv_any = True
        # Gate from battery block's charge_state_code; default to inactive if missing.
        b = inv.get("battery")
        cs_code = int(b.fields.get("charge_state_code", 0)) if b is not None else 0
        pv_active = cs_code in PV_ACTIVE_CHARGE_CODES
        pv1_v = pv.fields["pv1_voltage"] if pv_active else 0.0
        pv2_v = pv.fields["pv2_voltage"] if pv_active else 0.0
        pv1_i = pv.fields["pv1_current"] if pv_active else 0.0
        pv2_i = pv.fields["pv2_current"] if pv_active else 0.0
        p1 = round(pv1_v * pv1_i, 1)  # PV1 power = V x I
        p2 = round(pv2_v * pv2_i, 1)
        v_l2 = pv.fields["ac_output_voltage_l2"]
        i_l2 = pv.fields["ac_output_current_l2"]
        pv_total += p1 + p2
        out[f"inverter_{i}_pv_voltage_mppt1"] = pv1_v
        out[f"inverter_{i}_pv_voltage_mppt2"] = pv2_v
        out[f"inverter_{i}_pv_current_mppt1"] = pv1_i
        out[f"inverter_{i}_pv_current_mppt2"] = pv2_i
        out[f"inverter_{i}_pv_current"] = round(pv1_i + pv2_i, 2)
        out[f"inverter_{i}_pv_power_mppt1"] = p1
        out[f"inverter_{i}_pv_power_mppt2"] = p2
        out[f"inverter_{i}_pv_power"] = round(p1 + p2, 1)
        out[f"inverter_{i}_load_power_l2"] = round(v_l2 * i_l2, 1)
        # Combined L1+L2 AC output voltage (SA shows ~240 V)
        s = inv.get("state")
        if s is not None:
            v_l1 = s.fields["ac_output_voltage_l1"]
            out[f"inverter_{i}_ac_output_voltage"] = round(v_l1 + v_l2, 1)
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

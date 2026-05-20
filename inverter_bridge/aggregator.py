"""Aggregate per-inverter ParsedBlock dicts into the home-wide sensor set.

Input: list of per-inverter dicts, each `{block_name: ParsedBlock}`.
Output: flat dict `{sensor_key: value}` matching the SA sensor schema.

Convention follows spec §5.8 (HA-side names with _2 suffix preserved for compat).
"""

from __future__ import annotations

from .parsers import ParsedBlock
from .srne_map import CHARGE_STATE_LOOKUP, INVERTER_STATE_LOOKUP


def aggregate_inverters(
    per_inverter: list[dict[str, ParsedBlock]],
) -> dict[str, float | str]:
    """Combine readings from N inverters into the published sensor set.

    Returns a flat dict ready to be published (one MQTT topic per key).
    Per-inverter sensors are prefixed `inverter_{N}_` where N is 1-indexed.
    Aggregate sensors have no prefix.
    """
    out: dict[str, float | str] = {}

    # Battery (shared bank): SOC + V averaged, currents summed
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
        out[f"inverter_{i}_battery_current_2"] = i_a
        out[f"inverter_{i}_battery_voltage_2"] = v
        # Charge state -> text label
        cs_code = int(b.fields.get("charge_state_code", -1))
        out[f"inverter_{i}_charge_state"] = CHARGE_STATE_LOOKUP.get(
            cs_code, f"unknown_{cs_code}"
        )
    if socs:
        out["battery_state_of_charge_2"] = round(sum(socs) / len(socs), 1)
        out["battery_voltage"] = round(sum(vs) / len(vs), 2)
        # battery_power = V_avg * sum(currents); SA convention: positive = charging
        avg_v = sum(vs) / len(vs)
        total_i = sum(bank_currents)
        out["battery_power"] = round(avg_v * total_i, 1)

    # State + load + temps + AC output
    inv_active_powers: list[float] = []
    for i, inv in enumerate(per_inverter, start=1):
        s = inv.get("state")
        if s is None:
            continue
        active_p = s.fields["inverter_active_power"]
        inv_active_powers.append(active_p)
        out[f"inverter_{i}_load_power"] = active_p
        out[f"inverter_{i}_load_apparent_power_2"] = s.fields["inverter_apparent_power_l1"]
        out[f"inverter_{i}_load_percentage_2"] = s.fields["load_percent"]
        out[f"inverter_{i}_ac_output_frequency_2"] = s.fields["ac_output_frequency"]
        out[f"inverter_{i}_grid_frequency"] = s.fields["grid_frequency"]
        out[f"inverter_{i}_grid_voltage_1_2"] = s.fields["grid_voltage_l1"]
        # Per-phase L1 apparent (V_L1 * I_L1, despite being W per SA naming)
        v_l1 = s.fields["ac_output_voltage_l1"]
        i_l1 = s.fields["ac_output_current_l1"]
        out[f"inverter_{i}_load_power_1_2"] = round(v_l1 * i_l1, 1)
        # Temperatures: max + 3 individual
        t_dc_dc = s.fields["temperature_dc_dc"]
        t_dc_ac = s.fields["temperature_dc_ac"]
        t_trans = s.fields["temperature_transformer"]
        out[f"inverter_{i}_temperature_2"] = round(max(t_dc_dc, t_dc_ac, t_trans), 1)
        out[f"inverter_{i}_temperature_dc_dc"] = t_dc_dc
        out[f"inverter_{i}_temperature_dc_ac"] = t_dc_ac
        out[f"inverter_{i}_temperature_transformer"] = t_trans
        # device_mode text from state code
        code = int(s.fields["inverter_state_code"])
        out[f"inverter_{i}_device_mode"] = INVERTER_STATE_LOOKUP.get(
            code, f"unknown_code_{code}"
        )
        out[f"inverter_{i}_bus_voltage_2"] = s.fields["bus_voltage"]
    if inv_active_powers:
        out["load_power_2"] = round(sum(inv_active_powers), 1)
        # `mode` (aggregated) = inv1's device_mode (they sync in split-phase)
        if "inverter_1_device_mode" in out:
            out["mode"] = out["inverter_1_device_mode"]

    # PV + L2 (split-phase) + per-phase L2 apparent
    pv_total = 0.0
    pv_any = False
    grid_voltage_l2_seen: list[float] = []
    ac_output_v_per_inv: list[tuple[float, float]] = []  # (v_l1, v_l2)
    for i, inv in enumerate(per_inverter, start=1):
        pv = inv.get("pv_temps_l2")
        if pv is None:
            continue
        pv_any = True
        p1 = pv.fields["pv1_power"]
        p2 = pv.fields["pv2_power"]
        pv1_v = pv.fields["pv1_voltage"]
        pv2_v = pv.fields["pv2_voltage"]
        v_l2 = pv.fields["ac_output_voltage_l2"]
        i_l2 = pv.fields["ac_output_current_l2"]
        pv_total += p1 + p2
        out[f"inverter_{i}_pv1_voltage"] = pv1_v
        out[f"inverter_{i}_pv2_voltage"] = pv2_v
        out[f"inverter_{i}_pv_voltage_1_2"] = pv1_v
        out[f"inverter_{i}_pv_voltage_2_2"] = pv2_v
        out[f"inverter_{i}_pv_power_1_2"] = p1
        out[f"inverter_{i}_pv_power_2_2"] = p2
        out[f"inverter_{i}_pv_power"] = round(p1 + p2, 1)
        # PV current = P / V (clamp tiny denominators to avoid /0 noise)
        out[f"inverter_{i}_pv_current_1_2"] = round(p1 / pv1_v, 2) if pv1_v > 5 else 0.0
        out[f"inverter_{i}_pv_current_2_2"] = round(p2 / pv2_v, 2) if pv2_v > 5 else 0.0
        # Per-phase L2 apparent (V_L2 * I_L2)
        out[f"inverter_{i}_load_power_2_2"] = round(v_l2 * i_l2, 1)
        # L2 grid V (probably same register as L1 grid in offgrid: always 0 in home)
        # Just track per-inv for now; L2 grid V mapping TBD.
        grid_voltage_l2_seen.append(0.0)
        # Cross-inv L1+L2 voltage
        s = inv.get("state")
        if s is not None:
            v_l1 = s.fields["ac_output_voltage_l1"]
            ac_output_v_per_inv.append((v_l1, v_l2))
            out[f"inverter_{i}_ac_output_voltage"] = round(v_l1 + v_l2, 1)
    if pv_any:
        out["pv_power"] = round(pv_total, 1)

    return out

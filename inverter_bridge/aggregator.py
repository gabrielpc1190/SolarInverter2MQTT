"""Aggregate per-inverter ParsedBlock dicts into Solar Assistant-compatible sensors.

Output keys follow SA's convention so that the MQTT publisher can map them to
the right topic + unique_id (see mqtt_publisher.py):

- Aggregated (whole site): `battery_voltage`, `pv_power`, `load_power`, etc.
- Per-inverter: `inverter_<N>_<key>`.

NO `_2` suffix on keys (that was an HA-side artifact, not part of SA's wire format).
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
        out[f"inverter_{i}_grid_voltage_1"] = s.fields["grid_voltage_l1"]
        # Grid power per phase (offgrid: always 0; here for SA compat)
        out[f"inverter_{i}_grid_power_1"] = round(
            s.fields["grid_voltage_l1"] * s.fields["grid_current_l1"], 1
        )
        out[f"inverter_{i}_grid_power"] = out[f"inverter_{i}_grid_power_1"]  # sum across phases = L1 only here
        # Per-phase L1 apparent (V_L1 * I_L1)
        out[f"inverter_{i}_load_power_1"] = round(
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
    pv_total = 0.0
    pv_any = False
    for i, inv in enumerate(per_inverter, start=1):
        pv = inv.get("pv_temps_l2")
        if pv is None:
            continue
        pv_any = True
        pv1_v = pv.fields["pv1_voltage"]
        pv2_v = pv.fields["pv2_voltage"]
        pv1_i = pv.fields["pv1_current"]
        pv2_i = pv.fields["pv2_current"]
        p1 = round(pv1_v * pv1_i, 1)  # PV1 power = V x I
        p2 = round(pv2_v * pv2_i, 1)
        v_l2 = pv.fields["ac_output_voltage_l2"]
        i_l2 = pv.fields["ac_output_current_l2"]
        pv_total += p1 + p2
        out[f"inverter_{i}_pv1_voltage"] = pv1_v
        out[f"inverter_{i}_pv2_voltage"] = pv2_v
        out[f"inverter_{i}_pv_voltage_1"] = pv1_v
        out[f"inverter_{i}_pv_voltage_2"] = pv2_v
        out[f"inverter_{i}_pv_current_1"] = pv1_i
        out[f"inverter_{i}_pv_current_2"] = pv2_i
        out[f"inverter_{i}_pv_current"] = round(pv1_i + pv2_i, 2)
        out[f"inverter_{i}_pv_power_1"] = p1
        out[f"inverter_{i}_pv_power_2"] = p2
        out[f"inverter_{i}_pv_power"] = round(p1 + p2, 1)
        out[f"inverter_{i}_load_power_2"] = round(v_l2 * i_l2, 1)
        # Combined L1+L2 AC output voltage (SA shows ~240 V)
        s = inv.get("state")
        if s is not None:
            v_l1 = s.fields["ac_output_voltage_l1"]
            out[f"inverter_{i}_ac_output_voltage"] = round(v_l1 + v_l2, 1)
    if pv_any:
        out["pv_power"] = round(pv_total, 1)

    # F-7 fix: split-phase system has only L1+L2. Solar Assistant historically
    # exposed `_3` (third-phase) sensors as compat-zero placeholders. We publish
    # them explicitly as 0.0 (rather than omitting them) so the existing HA
    # entities keep getting fresh updates (force_update: true bumps last_updated
    # even when value is unchanged), instead of going stale.
    for i in range(1, len(per_inverter) + 1):
        out[f"inverter_{i}_load_power_3"] = 0.0
        out[f"inverter_{i}_grid_power_3"] = 0.0
        out[f"inverter_{i}_grid_voltage_3"] = 0.0
        out[f"inverter_{i}_pv_power_3"] = 0.0

    # Rated installed battery capacity (kWh).
    # SA historically published this as a static value derived from the
    # inverter config block 0xE116. The exact register offset hasn't been
    # confirmed, so we publish a hardcoded value here for SA-compatibility
    # of the entity name. Override in your fork if your installation differs;
    # the real (degraded) bank capacity should live in HA as an
    # `input_number.*` helper that your automations actually consume.
    out["capacity"] = 72.6

    return out

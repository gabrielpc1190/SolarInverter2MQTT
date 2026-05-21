"""Tests for the multi-inverter aggregator (SA-compatible key naming)."""

from inverter_bridge.aggregator import aggregate_inverters
from inverter_bridge.parsers import ParsedBlock


def _make_battery(
    soc: float, v: float, i: float, charge_state: int = 1,
    pv1_v: float = 0.0, pv1_i: float = 0.0, pv1_p: float | None = None,
) -> ParsedBlock:
    """Battery block (0x0100). Also carries PV1 V/I/P at offsets 7/8/9 per
    V1.96 spec — accept optional pv1_* args."""
    p = pv1_p if pv1_p is not None else round(pv1_v * pv1_i, 1)
    return ParsedBlock(
        block_addr=0x0100,
        block_name="battery",
        slave=0,
        regs_raw=(),
        fields={
            "battery_state_of_charge": soc,
            "battery_voltage": v,
            "battery_current": i,
            "charge_state_code": charge_state,
            "pv1_voltage": pv1_v,
            "pv1_current": pv1_i,
            "pv1_power": p,
        },
    )


def _make_state(active_p: float, temp_max: float = 40.0) -> ParsedBlock:
    """Build a state block fixture. `active_p` is interpreted as Phase A active
    power (register 0x021B). Phase B active power lives in the separate phase_b
    block — use `_make_phase_b()` if you need to set it explicitly. Tests that
    only care about the L1 leg can ignore phase_b and the aggregator will treat
    L2 active as 0."""
    return ParsedBlock(
        block_addr=0x0210,
        block_name="state",
        slave=0,
        regs_raw=(),
        fields={
            "inverter_state_code": 3.0,
            "inverter_substate_code": 0,
            "bus_voltage": 52.0,
            "grid_voltage_l1": 0.0,
            "grid_current_l1": 0.0,
            "grid_frequency": 0.0,
            "ac_output_voltage_l1": 120.0,
            "ac_output_current_l1": active_p / 120.0 / 2,
            "ac_output_frequency": 60.0,
            "load_active_phase_a": active_p,
            "load_apparent_phase_a": active_p * 1.05,
            "load_percent": 13.0,
            "temperature_dc_dc": 30.0,
            "temperature_dc_ac": 38.0,
            "temperature_transformer": temp_max,
        },
    )


def _make_phase_b(active_p: float = 0.0, v_l2: float = 120.0) -> ParsedBlock:
    """Phase B block (0x0223). Provides L2 load active/apparent + AC output L2
    voltage. Defaults to a balanced 120V leg with zero load."""
    return ParsedBlock(
        block_addr=0x0223,
        block_name="phase_b",
        slave=0,
        regs_raw=(),
        fields={
            "ac_output_voltage_l2": v_l2,
            "ac_output_current_l2": active_p / max(v_l2, 1.0),
            "load_current_phase_b": active_p / max(v_l2, 1.0),
            "load_active_phase_b": active_p,
            "load_apparent_phase_b": active_p * 1.05,
        },
    )


def _make_pv1(v: float, i: float, p: float | None = None) -> dict[str, float]:
    """Return PV1 fields to merge into the `battery` block fixture (PV1 lives
    in the 0x0100 battery block at offsets 7/8/9 per V1.96 spec)."""
    return {"pv1_voltage": v, "pv1_current": i, "pv1_power": p if p is not None else round(v * i, 1)}


def _make_pv2(v: float, i: float, p: float | None = None) -> ParsedBlock:
    """PV2 block (0x010F)."""
    return ParsedBlock(
        block_addr=0x010F,
        block_name="pv2",
        slave=0,
        regs_raw=(),
        fields={"pv2_voltage": v, "pv2_current": i, "pv2_power": p if p is not None else round(v * i, 1)},
    )


def _make_daily_stats(pv_kwh: float, load_kwh: float, ah_in: float, ah_out: float) -> ParsedBlock:
    return ParsedBlock(
        block_addr=0xF02C,
        block_name="daily_stats",
        slave=0,
        regs_raw=(),
        fields={
            "battery_charge_ah_today": ah_in,
            "battery_discharge_ah_today": ah_out,
            "pv_energy_today": pv_kwh,
            "load_energy_today": load_kwh,
        },
    )


def _make_runtime_ctrs(daily_history: list[float]) -> ParsedBlock:
    fields = {f"pv_energy_{d}": v for d, v in zip(
        ("yesterday","2_days_ago","3_days_ago","4_days_ago","5_days_ago","6_days_ago","7_days_ago"),
        daily_history, strict=True)}
    return ParsedBlock(block_addr=0xF000, block_name="runtime_ctrs", slave=0, regs_raw=(), fields=fields)


def _make_device_info(fw: float, hw: float) -> ParsedBlock:
    return ParsedBlock(
        block_addr=0x0014,
        block_name="device_info",
        slave=0,
        regs_raw=(),
        fields={"firmware_version": fw, "hardware_version": hw},
    )


def _make_pv_setup(pv1_w: float, pv2_w: float, *, charge_state: int = 1,
                   soc: float = 50.0, bat_v: float = 52.0, bat_i: float = 0.0,
                   active_p: float = 0.0, active_p_l2: float | None = None,
                   temp_max: float = 40.0) -> dict[str, ParsedBlock]:
    """Build a full per-inverter inv dict with PV1 (in battery block), PV2 (own
    block), state (Phase A active), and phase_b (Phase B active + L2 output).

    PV voltage assumed 260V (typical MPPT operating point). pv1/pv2 power
    direct from firmware registers per V1.96 spec.

    Use like:
        inv1 = _make_pv_setup(pv1_w=300, pv2_w=400, soc=44, ..., active_p=500)
    """
    if active_p_l2 is None:
        active_p_l2 = active_p
    return {
        "battery": _make_battery(soc=soc, v=bat_v, i=bat_i, charge_state=charge_state,
                                  pv1_v=260.0 if pv1_w else 0.0,
                                  pv1_i=pv1_w / 260.0 if pv1_w else 0.0,
                                  pv1_p=pv1_w),
        "pv2": _make_pv2(v=260.0 if pv2_w else 0.0,
                        i=pv2_w / 260.0 if pv2_w else 0.0,
                        p=pv2_w),
        "state": _make_state(active_p=active_p, temp_max=temp_max),
        "phase_b": _make_phase_b(active_p=active_p_l2),
    }


def test_aggregate_uses_sa_keys_without_suffix():
    """Aggregated keys should NOT have the _2 suffix."""
    inv1 = _make_pv_setup(pv1_w=300, pv2_w=400, soc=44, bat_v=52.5, bat_i=10.0,
                          active_p=500, temp_max=45.0)
    inv2 = _make_pv_setup(pv1_w=350, pv2_w=380, soc=44, bat_v=52.5, bat_i=12.0,
                          active_p=600, temp_max=48.0)
    out = aggregate_inverters([inv1, inv2])
    assert "battery_state_of_charge" in out
    assert "battery_voltage" in out
    assert "battery_power" in out
    assert "load_power" in out
    assert "pv_power" in out
    assert "grid_voltage" in out
    assert "grid_frequency" in out
    # Per-phase load_power_l1/l2 are per-inverter only, never aggregate
    assert "load_power_l1" not in out
    assert "load_power_l2" not in out


def test_battery_power_positive_when_charging():
    inv1 = _make_pv_setup(pv1_w=400, pv2_w=400, soc=50, bat_v=52.0, bat_i=10.0, active_p=500)
    inv2 = _make_pv_setup(pv1_w=500, pv2_w=500, soc=50, bat_v=52.0, bat_i=15.0, active_p=600)
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_power"] == 52.0 * 25.0
    assert out["battery_power"] > 0


def test_battery_power_negative_when_discharging():
    inv1 = _make_pv_setup(pv1_w=0, pv2_w=0, soc=80, bat_v=51.0, bat_i=-5.0, active_p=400)
    inv2 = _make_pv_setup(pv1_w=0, pv2_w=0, soc=80, bat_v=51.0, bat_i=-3.0, active_p=350)
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_power"] == 51.0 * -8.0  # -408
    assert out["battery_power"] < 0


def test_aggregate_per_inverter_keys_balanced_load():
    """Per-inverter total load_power = phase_a + phase_b (sum L1+L2 per inverter,
    per V1.96 spec). `active_p` in the fixture sets BOTH phase_a and phase_b
    by default (balanced 240V load assumption)."""
    inv1 = _make_pv_setup(pv1_w=300, pv2_w=400, soc=44, bat_v=52.5, bat_i=10.0,
                          active_p=500, temp_max=45.0)
    inv2 = _make_pv_setup(pv1_w=350, pv2_w=380, soc=44, bat_v=52.5, bat_i=12.0,
                          active_p=600, temp_max=48.0)
    out = aggregate_inverters([inv1, inv2])
    assert out["inverter_1_temperature"] == 45.0
    assert out["inverter_2_temperature"] == 48.0
    # Phase A 500W + Phase B 500W = 1000W per inverter (balanced)
    assert out["inverter_1_load_power"] == 1000
    assert out["inverter_2_load_power"] == 1200
    assert out["inverter_1_load_power_l1"] == 500
    assert out["inverter_1_load_power_l2"] == 500
    # PV is direct register sum, no V x I math
    assert out["inverter_1_pv_power"] == 700.0  # 300 + 400
    assert out["inverter_2_pv_power"] == 730.0  # 350 + 380
    assert out["inverter_1_device_mode"] == "Battery"
    assert out["mode"] == "Battery"


def test_unbalanced_split_phase_load():
    """Realistic case: 120V-only load on L1, no load on L2."""
    inv1 = _make_pv_setup(pv1_w=0, pv2_w=0, soc=50, bat_v=52.0, bat_i=-10.0,
                          active_p=600, active_p_l2=0)
    out = aggregate_inverters([inv1])
    assert out["inverter_1_load_power_l1"] == 600
    assert out["inverter_1_load_power_l2"] == 0
    assert out["inverter_1_load_power"] == 600


def test_charge_state_text_label():
    inv1 = _make_pv_setup(pv1_w=400, pv2_w=400, soc=50, bat_v=52.0, bat_i=10.0,
                          active_p=500, charge_state=1)
    out = aggregate_inverters([inv1])
    assert out["inverter_1_charge_state"] == "PV charging"


def test_missing_state_block_gracefully_degraded():
    inv1 = {"battery": _make_battery(soc=60, v=52.0, i=10.0)}
    inv2 = {"battery": _make_battery(soc=60, v=52.0, i=10.0)}
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_state_of_charge"] == 60.0
    assert "load_power" not in out


def test_pv_power_read_directly_from_firmware_registers():
    """PV power comes from registers 0x0109 (PV1) + 0x0111 (PV2) directly,
    no V x I math. Per V1.96 spec; verified empirically 2026-05-21."""
    inv1 = _make_pv_setup(pv1_w=520, pv2_w=260, soc=50, bat_v=52.0, bat_i=0.0,
                          active_p=500)
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power_mppt1"] == 520.0
    assert out["inverter_1_pv_power_mppt2"] == 260.0
    assert out["inverter_1_pv_power"] == 780.0
    assert out["pv_power"] == 780.0


def test_pv_zero_at_night_without_gate():
    """Night: real PV registers (0x0107-0x0109, 0x010F-0x0111) report 0 V/0 A/0 W
    naturally. No charge_state gate needed — the gate was a band-aid for the
    previous bug where the bridge read wrong registers."""
    inv1 = _make_pv_setup(pv1_w=0, pv2_w=0, soc=46, bat_v=52.4, bat_i=11.0,
                          charge_state=0, active_p=362)
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power"] == 0.0
    assert out["inverter_1_pv_power_mppt1"] == 0.0
    assert out["inverter_1_pv_power_mppt2"] == 0.0
    assert out["pv_power"] == 0.0


def test_pv_negative_power_clamped_to_zero():
    """pv2_power register is signed; if it ever returns negative (sentinel /
    idle MPPT artifact) the aggregator clamps to 0 so dashboards aren't
    surprised."""
    bat = _make_battery(soc=50, v=52.0, i=0.0, pv1_v=0.0, pv1_i=0.0, pv1_p=0.0)
    pv2 = _make_pv2(v=0.0, i=0.0, p=-50.0)  # spurious negative
    inv1 = {"battery": bat, "pv2": pv2, "state": _make_state(active_p=0),
            "phase_b": _make_phase_b()}
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power_mppt2"] == 0.0


def test_ac_output_voltage_is_split_phase_sum():
    inv1 = _make_pv_setup(pv1_w=0, pv2_w=0, soc=50, bat_v=52.0, bat_i=0.0, active_p=0)
    out = aggregate_inverters([inv1])
    assert out["inverter_1_ac_output_voltage"] == 240.0


def test_no_phantom_third_phase_keys():
    """Split-phase systems only have L1+L2. Aggregator must NOT emit
    `_3`-suffixed phantom-zero keys (removed 2026-05-20)."""
    inv1 = _make_pv_setup(pv1_w=300, pv2_w=400, soc=44, bat_v=52.5, bat_i=10.0,
                          active_p=500, temp_max=45.0)
    out = aggregate_inverters([inv1])
    forbidden = [k for k in out if k.endswith("_3")]
    assert forbidden == [], f"phantom _3 keys present: {forbidden}"


def test_capacity_is_not_published():
    """Retirado 2026-05-20: `capacity` era un static config setting de SA."""
    inv1 = _make_pv_setup(pv1_w=300, pv2_w=400, soc=44, bat_v=52.5, bat_i=10.0,
                          active_p=500)
    out = aggregate_inverters([inv1])
    assert "capacity" not in out
    assert "capacity" not in aggregate_inverters([{}])


# --- Cold-block sensors (daily stats + 7-day history + diagnostics) ----------

def test_daily_stats_per_inverter_and_aggregate_sum():
    """Daily stats fields publish per-inverter and as `total_*` aggregate sums."""
    inv1 = {"daily_stats": _make_daily_stats(pv_kwh=18.3, load_kwh=16.3, ah_in=163, ah_out=167)}
    inv2 = {"daily_stats": _make_daily_stats(pv_kwh=15.9, load_kwh=17.4, ah_in=159, ah_out=174)}
    out = aggregate_inverters([inv1, inv2])
    # per-inverter
    assert out["inverter_1_pv_energy_today"] == 18.3
    assert out["inverter_1_load_energy_today"] == 16.3
    assert out["inverter_1_battery_charge_ah_today"] == 163
    assert out["inverter_2_pv_energy_today"] == 15.9
    # aggregate = sum
    assert out["pv_energy_today"] == 34.2
    assert out["load_energy_today"] == 33.7
    assert out["battery_charge_ah_today"] == 322
    assert out["battery_discharge_ah_today"] == 341


def test_daily_stats_missing_block_omits_keys():
    """If the cold poll skipped daily_stats, the aggregator must not emit those keys."""
    inv1 = {"battery": _make_battery(soc=50, v=52.0, i=0.0)}
    out = aggregate_inverters([inv1])
    assert "pv_energy_today" not in out
    assert "inverter_1_pv_energy_today" not in out


def test_pv_history_per_inverter():
    """7-day PV history publishes per-inverter (no aggregate sum — those would be
    misleading since each day is already a kWh total per-inverter)."""
    inv1 = {"runtime_ctrs": _make_runtime_ctrs([27.3, 35.8, 27.4, 33.4, 42.4, 28.9, 36.6])}
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_energy_yesterday"] == 27.3
    assert out["inverter_1_pv_energy_7_days_ago"] == 36.6
    # not aggregated
    assert "pv_energy_yesterday" not in out


def test_device_info_diagnostics():
    """firmware_version + hardware_version publish per-inverter."""
    inv1 = {"device_info": _make_device_info(fw=8.18, hw=3.04)}
    out = aggregate_inverters([inv1])
    assert out["inverter_1_firmware_version"] == 8.18
    assert out["inverter_1_hardware_version"] == 3.04

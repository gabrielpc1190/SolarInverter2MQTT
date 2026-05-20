"""Tests for the multi-inverter aggregator (SA-compatible key naming)."""

from inverter_bridge.aggregator import aggregate_inverters
from inverter_bridge.parsers import ParsedBlock


def _make_battery(soc: float, v: float, i: float, charge_state: int = 1) -> ParsedBlock:
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
        },
    )


def _make_state(active_p: float, temp_max: float = 40.0) -> ParsedBlock:
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
            "load_percent_alt": 27.0,
            "inverter_active_power": active_p,
            "inverter_apparent_power_l1": active_p * 1.05,
            "load_percent": 13.0,
            "temperature_dc_dc": 30.0,
            "temperature_dc_ac": 38.0,
            "temperature_transformer": temp_max,
        },
    )


def _make_pv(pv1_w: float, pv2_w: float) -> ParsedBlock:
    """Build a PV block fixture; convert input watts to the underlying current
    (registers store I, aggregator computes P = V × I with V = 260)."""
    return ParsedBlock(
        block_addr=0x0223,
        block_name="pv_temps_l2",
        slave=0,
        regs_raw=(),
        fields={
            "pv1_voltage": 260.0,
            "pv2_voltage": 260.0,
            "ac_output_voltage_l2": 120.0,
            "ac_output_current_l2": (pv1_w + pv2_w) / 120.0 / 2,
            "pv1_current": pv1_w / 260.0,
            "pv2_current": pv2_w / 260.0,
        },
    )


def test_aggregate_uses_sa_keys_without_suffix():
    """Aggregated keys should NOT have the _2 suffix."""
    inv1 = {
        "battery": _make_battery(soc=44, v=52.5, i=10.0),
        "state": _make_state(active_p=500, temp_max=45.0),
        "pv_temps_l2": _make_pv(pv1_w=300, pv2_w=400),
    }
    inv2 = {
        "battery": _make_battery(soc=44, v=52.5, i=12.0),
        "state": _make_state(active_p=600, temp_max=48.0),
        "pv_temps_l2": _make_pv(pv1_w=350, pv2_w=380),
    }
    out = aggregate_inverters([inv1, inv2])
    # Aggregated keys (no _2 suffix)
    assert "battery_state_of_charge" in out
    assert "battery_voltage" in out
    assert "battery_power" in out
    assert "load_power" in out
    assert "pv_power" in out
    assert "grid_voltage" in out
    assert "grid_frequency" in out
    # No _2 suffixes
    assert "battery_state_of_charge_2" not in out
    assert "load_power_2" not in out


def test_battery_power_positive_when_charging():
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=10.0),
        "state": _make_state(active_p=500),
        "pv_temps_l2": _make_pv(pv1_w=400, pv2_w=400),
    }
    inv2 = {
        "battery": _make_battery(soc=50, v=52.0, i=15.0),
        "state": _make_state(active_p=600),
        "pv_temps_l2": _make_pv(pv1_w=500, pv2_w=500),
    }
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_power"] == 52.0 * 25.0
    assert out["battery_power"] > 0


def test_battery_power_negative_when_discharging():
    inv1 = {
        "battery": _make_battery(soc=80, v=51.0, i=-5.0),
        "state": _make_state(active_p=400),
        "pv_temps_l2": _make_pv(pv1_w=0, pv2_w=0),
    }
    inv2 = {
        "battery": _make_battery(soc=80, v=51.0, i=-3.0),
        "state": _make_state(active_p=350),
        "pv_temps_l2": _make_pv(pv1_w=0, pv2_w=0),
    }
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_power"] == 51.0 * -8.0  # -408
    assert out["battery_power"] < 0


def test_aggregate_per_inverter_keys():
    inv1 = {
        "battery": _make_battery(soc=44, v=52.5, i=10.0),
        "state": _make_state(active_p=500, temp_max=45.0),
        "pv_temps_l2": _make_pv(pv1_w=300, pv2_w=400),
    }
    inv2 = {
        "battery": _make_battery(soc=44, v=52.5, i=12.0),
        "state": _make_state(active_p=600, temp_max=48.0),
        "pv_temps_l2": _make_pv(pv1_w=350, pv2_w=380),
    }
    out = aggregate_inverters([inv1, inv2])
    # Per-inverter keys (no _2 suffix)
    assert out["inverter_1_temperature"] == 45.0
    assert out["inverter_2_temperature"] == 48.0
    assert out["inverter_1_load_power"] == 500
    assert out["inverter_2_load_power"] == 600
    assert out["inverter_1_pv_power"] == 700.0
    assert out["inverter_2_pv_power"] == 730.0
    # Device mode from state code
    assert out["inverter_1_device_mode"] == "Battery"
    assert out["mode"] == "Battery"


def test_charge_state_text_label():
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=10.0, charge_state=1),
        "state": _make_state(active_p=500),
        "pv_temps_l2": _make_pv(pv1_w=400, pv2_w=400),
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_charge_state"] == "PV charging"


def test_missing_state_block_gracefully_degraded():
    inv1 = {"battery": _make_battery(soc=60, v=52.0, i=10.0)}
    inv2 = {"battery": _make_battery(soc=60, v=52.0, i=10.0)}
    out = aggregate_inverters([inv1, inv2])
    assert out["battery_state_of_charge"] == 60.0
    assert "load_power" not in out


def test_pv_power_computed_from_voltage_and_current():
    """PV power = V × I (registers store current; we compute power)."""
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=0.0),
        "state": _make_state(active_p=500),
        "pv_temps_l2": _make_pv(pv1_w=520, pv2_w=260),
    }
    out = aggregate_inverters([inv1])
    # _make_pv stored pv1_current = 520/260 = 2.0 A, pv2_current = 260/260 = 1.0 A
    assert out["inverter_1_pv_current_1"] == 2.0
    assert out["inverter_1_pv_current_2"] == 1.0
    # Aggregator computes power from V × I: 260 × 2 = 520W, 260 × 1 = 260W
    assert out["inverter_1_pv_power_1"] == 520.0
    assert out["inverter_1_pv_power_2"] == 260.0
    assert out["inverter_1_pv_power"] == 780.0
    assert out["pv_power"] == 780.0


def test_pv_current_clamped_when_voltage_zero():
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=0.0),
        "state": _make_state(active_p=0),
    }
    pv = ParsedBlock(
        block_addr=0x0223,
        block_name="pv_temps_l2",
        slave=0,
        regs_raw=(),
        fields={
            "pv1_voltage": 0.0,
            "pv2_voltage": 0.0,
            "ac_output_voltage_l2": 120.0,
            "ac_output_current_l2": 0.0,
            "pv1_current": 0.0,
            "pv2_current": 0.0,
        },
    )
    inv1["pv_temps_l2"] = pv
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_current_1"] == 0.0
    assert out["inverter_1_pv_current_2"] == 0.0


def test_ac_output_voltage_is_split_phase_sum():
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=0.0),
        "state": _make_state(active_p=0),
        "pv_temps_l2": _make_pv(pv1_w=0, pv2_w=0),
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_ac_output_voltage"] == 240.0

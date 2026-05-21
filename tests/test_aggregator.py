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


def _make_pv(pv1_w: float, pv2_w: float) -> ParsedBlock:
    """Build a PV block fixture; convert input watts to the underlying current
    (registers store I, aggregator computes P = V x I with V = 260)."""
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
    # Per-phase load_power_l1/l2 are per-inverter only, never aggregate
    assert "load_power_l1" not in out
    assert "load_power_l2" not in out


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
    """PV power = V x I (registers store current; we compute power)."""
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=0.0),
        "state": _make_state(active_p=500),
        "pv_temps_l2": _make_pv(pv1_w=520, pv2_w=260),
    }
    out = aggregate_inverters([inv1])
    # _make_pv stored pv1_current = 520/260 = 2.0 A, pv2_current = 260/260 = 1.0 A
    assert out["inverter_1_pv_current_mppt1"] == 2.0
    assert out["inverter_1_pv_current_mppt2"] == 1.0
    # Aggregator computes power from V x I: 260 x 2 = 520W, 260 x 1 = 260W
    assert out["inverter_1_pv_power_mppt1"] == 520.0
    assert out["inverter_1_pv_power_mppt2"] == 260.0
    assert out["inverter_1_pv_power"] == 780.0
    assert out["pv_power"] == 780.0


def test_pv_zeroed_when_charge_state_idle():
    """Night: inverter's PV V/I registers report fake values (pv_voltage tracks
    bus_voltage/2, pv_current is a stray reading). When charge_state_code = 0
    (Idle), the aggregator must zero PV power so dashboards and solar_excess
    sensors don't see phantom generation."""
    inv1 = {
        "battery": _make_battery(soc=46, v=52.4, i=11.0, charge_state=0),  # 0 = Idle
        "state": _make_state(active_p=362),
        "pv_temps_l2": _make_pv(pv1_w=520, pv2_w=260),  # fake-looking data on the wire
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power"] == 0.0
    assert out["inverter_1_pv_power_mppt1"] == 0.0
    assert out["inverter_1_pv_power_mppt2"] == 0.0
    assert out["inverter_1_pv_current"] == 0.0
    assert out["inverter_1_pv_voltage_mppt1"] == 0.0
    assert out["pv_power"] == 0.0


def test_pv_zeroed_when_charge_state_grid():
    """charge_state_code = 2 (Grid charging) also implies PV is not active."""
    inv1 = {
        "battery": _make_battery(soc=30, v=52.0, i=20.0, charge_state=2),  # 2 = Grid
        "state": _make_state(active_p=200),
        "pv_temps_l2": _make_pv(pv1_w=400, pv2_w=300),
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power"] == 0.0
    assert out["pv_power"] == 0.0


def test_pv_active_when_charge_state_float():
    """charge_state_code = 3 (Float) is PV-driven; PV registers ARE trustworthy."""
    inv1 = {
        "battery": _make_battery(soc=99, v=54.0, i=2.0, charge_state=3),  # 3 = Float
        "state": _make_state(active_p=200),
        "pv_temps_l2": _make_pv(pv1_w=300, pv2_w=200),
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_pv_power"] == 500.0


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
    assert out["inverter_1_pv_current_mppt1"] == 0.0
    assert out["inverter_1_pv_current_mppt2"] == 0.0


def test_ac_output_voltage_is_split_phase_sum():
    inv1 = {
        "battery": _make_battery(soc=50, v=52.0, i=0.0),
        "state": _make_state(active_p=0),
        "pv_temps_l2": _make_pv(pv1_w=0, pv2_w=0),
    }
    out = aggregate_inverters([inv1])
    assert out["inverter_1_ac_output_voltage"] == 240.0


def test_no_phantom_third_phase_keys():
    """Split-phase systems only have L1+L2. The aggregator must NOT emit
    `_3`-suffixed phantom-zero keys (removed 2026-05-20 along with the SA
    compat naming)."""
    inv1 = {
        "battery": _make_battery(soc=44, v=52.5, i=10.0),
        "state": _make_state(active_p=500, temp_max=45.0),
        "pv_temps_l2": _make_pv(pv1_w=300, pv2_w=400),
    }
    out = aggregate_inverters([inv1])
    forbidden = [k for k in out if k.endswith("_3")]
    assert forbidden == [], f"phantom _3 keys present: {forbidden}"


def test_capacity_is_not_published():
    """Retirado 2026-05-20: `capacity` era un static config setting de SA
    (hardcoded 72.6 kWh, no medición), generaba warnings de HA Statistics
    por falta de state_class, y la autoridad real vive HA-side en
    `input_number.capacidad_bateria_kwh`. Aggregator no debe emitirlo."""
    inv1 = {
        "battery": _make_battery(soc=44, v=52.5, i=10.0),
        "state": _make_state(active_p=500),
        "pv_temps_l2": _make_pv(pv1_w=300, pv2_w=400),
    }
    out = aggregate_inverters([inv1])
    assert "capacity" not in out
    # Defense in depth: even with all blocks empty, no capacity key.
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

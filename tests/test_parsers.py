"""Golden tests for register parsers against real captured fixtures."""

from pathlib import Path

import pytest

from inverter_bridge.modbus import ModbusFrame
from inverter_bridge.parsers import parse_block
from inverter_bridge.serial_io import parse_frame_stream

FIXTURES = Path(__file__).parent / "fixtures"


def load_frame_from_hex(hex_path: Path, count: int, slave: int) -> ModbusFrame:
    raw = bytes.fromhex(hex_path.read_text().strip())
    frames = parse_frame_stream(raw)
    matching = [f for f in frames if len(f.regs) == count and f.slave == slave]
    assert matching, f"no frame with count={count} slave=0x{slave:02x} in {hex_path.name}"
    return matching[0]


def test_parse_battery_block_slave01():
    """Real captured battery block: SOC=34%, V=52.8, I_raw=-146 (S16, discharging)."""
    frame = load_frame_from_hex(
        FIXTURES / "block_0100_count15_slave01_battery_day_pv_active.hex",
        count=15,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_state_of_charge"] == 34.0
    assert parsed.fields["battery_voltage"] == 52.8
    # S16: 65390 - 65536 = -146, x0.1 = -14.6 A (negative = discharging)
    assert parsed.fields["battery_current"] == pytest.approx(-14.6, abs=0.001)
    assert parsed.fields["charge_state_code"] == 1.0


def test_parse_battery_block_slave02_has_similar_values():
    """Both inverters share the same battery -> very similar values."""
    f1 = load_frame_from_hex(
        FIXTURES / "block_0100_count15_slave01_battery_day_pv_active.hex", 15, 0x01
    )
    f2 = load_frame_from_hex(
        FIXTURES / "block_0100_count15_slave02_battery_day_pv_active.hex", 15, 0x02
    )
    p1 = parse_block(block_addr=0x0100, frame=f1)
    p2 = parse_block(block_addr=0x0100, frame=f2)
    # SOC must be identical (same bank)
    assert p1.fields["battery_state_of_charge"] == p2.fields["battery_state_of_charge"]
    # Battery V should be within 0.5 V of each other (resistive drop / sensor noise)
    assert abs(p1.fields["battery_voltage"] - p2.fields["battery_voltage"]) <= 0.5


def test_parse_state_block_slave01():
    """Real state: code=3 (Battery), output 120.1V/60Hz, active 258W, temps 39.5/46.1/49.3C."""
    frame = load_frame_from_hex(
        FIXTURES / "block_0210_count19_slave01_state_day_pv_active.hex",
        count=19,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0210, frame=frame)
    assert parsed.fields["inverter_state_code"] == 3.0
    assert parsed.fields["bus_voltage"] == pytest.approx(52.20, abs=0.01)
    assert parsed.fields["ac_output_voltage_l1"] == 120.1
    assert parsed.fields["ac_output_frequency"] == 60.0
    assert parsed.fields["inverter_active_power"] == 258.0
    assert parsed.fields["temperature_dc_dc"] == pytest.approx(39.5, abs=0.05)
    assert parsed.fields["temperature_dc_ac"] == pytest.approx(46.1, abs=0.05)
    assert parsed.fields["temperature_transformer"] == pytest.approx(49.3, abs=0.05)


def test_parse_pv_block_slave01():
    """Real PV: PV1=260.9V/46W, PV2=260.8V/121W, L2 output 120.1V."""
    frame = load_frame_from_hex(
        FIXTURES / "block_0223_count23_slave01_pv_temps_l2_day_pv_active.hex",
        count=23,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0223, frame=frame)
    assert parsed.fields["pv1_voltage"] == pytest.approx(260.9, abs=0.05)
    assert parsed.fields["pv2_voltage"] == pytest.approx(260.8, abs=0.05)
    assert parsed.fields["ac_output_voltage_l2"] == pytest.approx(120.1, abs=0.05)
    assert parsed.fields["ac_output_current_l2"] == pytest.approx(2.1, abs=0.05)
    # 0x0232 and 0x0234 are PV CURRENT in 0.01A units (verified empirically 2026-05-20).
    # The original fixture had raw=46 and raw=121 → 0.46 A and 1.21 A.
    assert parsed.fields["pv1_current"] == pytest.approx(0.46, abs=0.001)
    assert parsed.fields["pv2_current"] == pytest.approx(1.21, abs=0.001)


def test_parse_signed_battery_current_negative():
    """Battery current is S16; verify negative interpretation."""
    # 0xFFE0 = -32 (S16), * 0.1 = -3.2 A
    frame = ModbusFrame(slave=1, func=3, regs=[44, 521, 0xFFE0, *([0] * 12)])
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_current"] == pytest.approx(-3.2, abs=0.01)


def test_parse_signed_battery_current_positive():
    """Positive raw = charging per standard convention."""
    frame = ModbusFrame(slave=1, func=3, regs=[80, 530, 200, *([0] * 12)])
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_current"] == 20.0  # +20A charging


def test_parse_wrong_register_count_raises():
    """If frame has wrong number of regs, parse must raise."""
    frame = ModbusFrame(slave=1, func=3, regs=[1, 2, 3])  # only 3, expects 15
    with pytest.raises(ValueError, match="expects"):
        parse_block(block_addr=0x0100, frame=frame)


def test_parse_unknown_block_raises():
    frame = ModbusFrame(slave=1, func=3, regs=[1, 2, 3])
    with pytest.raises(ValueError, match="unknown block"):
        parse_block(block_addr=0xBEEF, frame=frame)

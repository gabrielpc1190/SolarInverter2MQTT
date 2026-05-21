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
    """Real captured battery block: SOC=34%, V=52.8, I_raw=-146 (S16).
    Fixture captured during PV-active daytime; charge_state_code=1 ("PV
    charging") confirms the bank WAS being charged. Per V1.96 firmware
    convention, raw NEGATIVE register = charging (current flowing INTO bank).
    Bridge applies scale -0.1 to flip into our convention (positive = charging).
    So raw -146 → exposed +14.6 A charging."""
    frame = load_frame_from_hex(
        FIXTURES / "block_0100_count15_slave01_battery_day_pv_active.hex",
        count=15,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_state_of_charge"] == 34.0
    assert parsed.fields["battery_voltage"] == 52.8
    # S16: raw 65390 = -146; with scale -0.1 → +14.6 A (bridge: positive = charging)
    assert parsed.fields["battery_current"] == pytest.approx(14.6, abs=0.001)
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
    # 0x0212 is the HV DC bus (post-boost), not battery V. Scale 0.1: raw 5220 → 522.0 V.
    assert parsed.fields["bus_voltage"] == pytest.approx(522.0, abs=0.1)
    assert parsed.fields["ac_output_voltage_l1"] == 120.1
    assert parsed.fields["ac_output_frequency"] == 60.0
    # 0x021B is Load Phase A active power (NOT total — see srne_map.py).
    # Total per inverter = phase_a + phase_b; phase_b in separate 0x0223 block.
    assert parsed.fields["load_active_phase_a"] == 258.0
    assert parsed.fields["temperature_dc_dc"] == pytest.approx(39.5, abs=0.05)
    assert parsed.fields["temperature_dc_ac"] == pytest.approx(46.1, abs=0.05)
    assert parsed.fields["temperature_transformer"] == pytest.approx(49.3, abs=0.05)


def test_parse_phase_b_block_slave01():
    """Block 0x0223 is Phase B (L2 inverter output + L2 load), NOT PV.
    Fixture was captured 2026-05-20 with a day-PV-active label, but the
    register interpretations have been corrected per V1.96 spec:
      - 0x022C = Inverter Phase B output voltage (was right)
      - 0x022E = Inverter Phase B inductive current (was right)
      - 0x0232 = Load Phase B active power in W (NOT pv1 current x 0.01)
      - 0x0234 = Load Phase B apparent power in VA (NOT pv2 current x 0.01)
    """
    frame = load_frame_from_hex(
        FIXTURES / "block_0223_count23_slave01_pv_temps_l2_day_pv_active.hex",
        count=23,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0223, frame=frame)
    assert parsed.fields["ac_output_voltage_l2"] == pytest.approx(120.1, abs=0.05)
    assert parsed.fields["ac_output_current_l2"] == pytest.approx(2.1, abs=0.05)
    # Raw 46 at 0x0232 was previously misinterpreted as 0.46 A (PV); per spec
    # it's 46 W Load Phase B active power. The fixture was captured during a
    # low-L2-load moment.
    assert parsed.fields["load_active_phase_b"] == 46.0
    assert parsed.fields["load_apparent_phase_b"] == 121.0


def test_parse_pv1_in_battery_block_slave01():
    """PV1 V/I/P live in the battery block (0x0100 offsets 7/8/9) per V1.96."""
    frame = load_frame_from_hex(
        FIXTURES / "block_0100_count15_slave01_battery_day_pv_active.hex",
        count=15,
        slave=0x01,
    )
    parsed = parse_block(block_addr=0x0100, frame=frame)
    # PV1 fields must be present (values depend on the captured moment)
    assert "pv1_voltage" in parsed.fields
    assert "pv1_current" in parsed.fields
    assert "pv1_power" in parsed.fields


def test_parse_signed_battery_current_negative_raw_becomes_positive_charging():
    """Raw NEGATIVE register = charging per firmware convention.
    Bridge flips sign via scale -0.1 to expose positive = charging.
    raw 0xFFE0 = -32 (S16), x -0.1 → +3.2 A (charging)."""
    frame = ModbusFrame(slave=1, func=3, regs=[44, 521, 0xFFE0, *([0] * 12)])
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_current"] == pytest.approx(3.2, abs=0.01)


def test_parse_signed_battery_current_positive_raw_becomes_negative_discharging():
    """Raw POSITIVE register = discharging per firmware convention.
    Bridge flips sign via scale -0.1 to expose negative = discharging.
    raw +200 x -0.1 → -20 A (discharging)."""
    frame = ModbusFrame(slave=1, func=3, regs=[80, 530, 200, *([0] * 12)])
    parsed = parse_block(block_addr=0x0100, frame=frame)
    assert parsed.fields["battery_current"] == -20.0  # discharging


def test_parse_wrong_register_count_raises():
    """If frame has wrong number of regs, parse must raise."""
    frame = ModbusFrame(slave=1, func=3, regs=[1, 2, 3])  # only 3, expects 15
    with pytest.raises(ValueError, match="expects"):
        parse_block(block_addr=0x0100, frame=frame)


def test_parse_unknown_block_raises():
    frame = ModbusFrame(slave=1, func=3, regs=[1, 2, 3])
    with pytest.raises(ValueError, match="unknown block"):
        parse_block(block_addr=0xBEEF, frame=frame)

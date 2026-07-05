"""Structural tests for the SRNE register map."""

from inverter_bridge.srne_map import (
    BLOCKS,
    CHARGE_STATE_LOOKUP,
    FIELDS,
    INVERTER_STATE_LOOKUP,
    BlockTier,
    ascii_decode_regs,
    fields_for,
)


def test_blocks_have_unique_addresses():
    addrs = [b.addr for b in BLOCKS]
    assert len(addrs) == len(set(addrs)), "duplicate block address"


def test_blocks_have_at_least_one_hot_block():
    assert any(b.tier == BlockTier.HOT for b in BLOCKS)


def test_field_keys_unique():
    keys = [f.key for f in FIELDS]
    assert len(keys) == len(set(keys)), "duplicate sensor key"


def test_field_offsets_within_block():
    """Every Field's offset must be < its block's count."""
    blocks_by_addr = {b.addr: b for b in BLOCKS}
    for f in FIELDS:
        assert f.block_addr in blocks_by_addr, f"field {f.key} references unknown block 0x{f.block_addr:04x}"
        assert (
            f.offset < blocks_by_addr[f.block_addr].count
        ), f"field {f.key} offset {f.offset} >= block count {blocks_by_addr[f.block_addr].count}"


def test_critical_sensors_present():
    """Sanity: the 3 most critical sensors must be mapped."""
    keys = {f.key for f in FIELDS}
    assert "battery_state_of_charge" in keys
    assert "battery_voltage" in keys
    assert "battery_current" in keys


def test_battery_block_fields():
    """Battery block 0x0100 includes battery state + PV1 (per V1.96 spec,
    PV1 V/I/P live at offsets 7/8/9 of this block, not in a separate block)."""
    fs = fields_for(0x0100)
    assert {f.key for f in fs} == {
        "battery_state_of_charge",
        "battery_voltage",
        "battery_current",
        "charge_state_code",
        "pv1_voltage",
        "pv1_current",
        "pv1_power",
    }


def test_battery_current_is_signed():
    bc = next(f for f in FIELDS if f.key == "battery_current")
    assert bc.signed is True


def test_state_lookup_has_battery_at_3():
    """Empirically confirmed 2026-05-20."""
    assert INVERTER_STATE_LOOKUP[3] == "Battery"


def test_charge_state_lookup_has_pv_charging_at_1():
    assert CHARGE_STATE_LOOKUP[1] == "PV charging"


def test_charge_state_lookup_code2_is_boost_not_grid():
    # 2026-07-05: code 2 was observed for hours on a fully offgrid site
    # (grid 0.0 V all day) with the battery voltage regulated to the 56.0 V
    # boost setpoint at high charge current — i.e. constant-voltage / "Boost"
    # charging per the SRNE SPH10048 manual, NOT "Grid charging".
    assert CHARGE_STATE_LOOKUP[2] == "Boost charging"


def test_ascii_decode_extracts_text():
    # Real captured regs from 0x0030 fw_model block: "SR-24031501..."
    # Each reg = char in low byte, high byte = 0
    regs_for_sr = [0x0053, 0x0052, 0x002D, 0x0032, 0x0034, 0x0030, 0x0033, 0x0031, 0x0035, 0x0030, 0x0031]
    assert ascii_decode_regs(regs_for_sr) == "SR-24031501"


def test_ascii_decode_skips_non_printable():
    regs = [0x0048, 0x0000, 0x0069, 0x00FF, 0x002E]
    assert ascii_decode_regs(regs) == "Hi."

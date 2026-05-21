"""Unit tests for `tools/rename_ha_entities.py`.

Pure-function coverage of:

  * `parse_pairs` and `load_mapping_file` — CLI/JSON input adapters,
  * `validate_renames` — same-domain / collision / not-found rules,
  * `build_audit_f7_renames` — auto-mapping for the F-7 audit fix,
  * `ha_base_to_ws_url` — URL translation.

No real WebSocket roundtrip. Tests construct `RegistryEntry` records by
hand and assert that the planner picks the right ones.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_TOOL_PATH = _HERE.parent / "tools" / "rename_ha_entities.py"


def _load_module():
    """Import tools/rename_ha_entities.py as a module.

    Same trick used by tests/test_purge_ha_zombies.py — register in
    sys.modules before exec so `@dataclasses.dataclass(frozen=True)` can
    resolve the host module.
    """
    spec = importlib.util.spec_from_file_location(
        "rename_ha_entities", _TOOL_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rename_ha_entities"] = mod
    spec.loader.exec_module(mod)
    return mod


rn_mod = _load_module()


# --- Fixtures ------------------------------------------------------------

def _entry(entity_id: str, *, unique_id: str = "", platform: str = "mqtt"):
    return rn_mod.RegistryEntry(
        entity_id=entity_id,
        unique_id=unique_id or entity_id.replace(".", "_"),
        platform=platform,
        raw={"entity_id": entity_id, "unique_id": unique_id or "", "platform": platform},
    )


# --- parse_pairs ---------------------------------------------------------

def test_parse_pairs_basic():
    """Comma-free 'old=new' pairs parse into Rename records."""
    out = rn_mod.parse_pairs(["sensor.a=sensor.b", "sensor.c=sensor.d"])
    assert [(r.old, r.new) for r in out] == [
        ("sensor.a", "sensor.b"),
        ("sensor.c", "sensor.d"),
    ]


def test_parse_pairs_strips_whitespace():
    """Whitespace around old/new is tolerated."""
    out = rn_mod.parse_pairs(["  sensor.a   =  sensor.b  "])
    assert (out[0].old, out[0].new) == ("sensor.a", "sensor.b")


def test_parse_pairs_rejects_missing_equals():
    """An entry without `=` is a hard error."""
    with pytest.raises(ValueError):
        rn_mod.parse_pairs(["sensor.a sensor.b"])


def test_parse_pairs_rejects_empty_side():
    """`old=` or `=new` is rejected."""
    with pytest.raises(ValueError):
        rn_mod.parse_pairs(["sensor.a="])
    with pytest.raises(ValueError):
        rn_mod.parse_pairs(["=sensor.b"])


# --- load_mapping_file ---------------------------------------------------

def test_load_mapping_file_roundtrip(tmp_path):
    """A JSON object {old: new} loads as a list of Renames."""
    p = tmp_path / "rn.json"
    p.write_text(json.dumps({
        "sensor.a": "sensor.b",
        "sensor.x": "sensor.y",
    }))
    out = rn_mod.load_mapping_file(str(p))
    pairs = sorted((r.old, r.new) for r in out)
    assert pairs == [("sensor.a", "sensor.b"), ("sensor.x", "sensor.y")]


def test_load_mapping_file_rejects_list(tmp_path):
    """A JSON array is not a valid mapping file."""
    p = tmp_path / "rn.json"
    p.write_text(json.dumps([["a", "b"]]))
    with pytest.raises(ValueError):
        rn_mod.load_mapping_file(str(p))


def test_load_mapping_file_rejects_non_string(tmp_path):
    """Numeric values are rejected."""
    p = tmp_path / "rn.json"
    p.write_text(json.dumps({"sensor.a": 42}))
    with pytest.raises(ValueError):
        rn_mod.load_mapping_file(str(p))


# --- validate_renames: domain rules --------------------------------------

def test_validate_same_domain_rename_is_ok():
    """sensor.* -> sensor.* with the old in registry and new free -> ok."""
    reg = [_entry("sensor.foo")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.foo", new="sensor.bar")],
        reg,
    )
    assert len(plan) == 1
    assert plan[0].ok
    assert plan[0].status == "ok"


def test_validate_cross_domain_rename_rejected():
    """sensor.* -> binary_sensor.* is rejected."""
    reg = [_entry("sensor.foo")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.foo", new="binary_sensor.foo")],
        reg,
    )
    assert plan[0].status == "domain-mismatch"


def test_validate_cross_domain_rename_rejected_alt():
    """light.* -> switch.* is also rejected."""
    reg = [_entry("light.kitchen")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="light.kitchen", new="switch.kitchen")],
        reg,
    )
    assert plan[0].status == "domain-mismatch"


# --- validate_renames: collision rules -----------------------------------

def test_validate_target_already_taken_is_rejected():
    """If new entity_id is owned by some OTHER entry, reject."""
    reg = [
        _entry("sensor.foo_3", unique_id="canonical_uid"),
        _entry("sensor.foo", unique_id="some_other_uid"),
    ]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.foo_3", new="sensor.foo")],
        reg,
    )
    assert plan[0].status == "would-collide"
    assert "some_other_uid" in plan[0].detail


def test_validate_old_not_in_registry_is_rejected():
    """Renaming an entity_id that isn't registered fails fast."""
    reg = [_entry("sensor.exists")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.ghost", new="sensor.exists2")],
        reg,
    )
    assert plan[0].status == "not-found"


def test_validate_noop_rename_is_flagged():
    """old == new is a noop, flagged as such."""
    reg = [_entry("sensor.same")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.same", new="sensor.same")],
        reg,
    )
    assert plan[0].status == "noop"


def test_validate_target_free_when_old_owns_no_one_collides():
    """If the new entity_id doesn't appear in the registry at all, ok."""
    reg = [_entry("sensor.foo_3", unique_id="canonical_uid")]
    plan = rn_mod.validate_renames(
        [rn_mod.Rename(old="sensor.foo_3", new="sensor.foo")],
        reg,
    )
    assert plan[0].status == "ok"


def test_validate_multiple_renames_mixed_outcomes():
    """Verify each rename is classified independently."""
    reg = [
        _entry("sensor.a", unique_id="ua"),
        _entry("sensor.b", unique_id="ub"),
        _entry("sensor.c_3", unique_id="uc"),
        _entry("sensor.x", unique_id="ux"),  # target for would-collide
    ]
    renames = [
        rn_mod.Rename(old="sensor.c_3", new="sensor.c"),  # ok
        rn_mod.Rename(old="sensor.a", new="sensor.x"),    # would-collide (x taken by ux)
        rn_mod.Rename(old="sensor.ghost", new="sensor.found"),  # not-found
        rn_mod.Rename(old="sensor.b", new="binary_sensor.b"),  # domain-mismatch
    ]
    plan = rn_mod.validate_renames(renames, reg)
    statuses = [p.status for p in plan]
    assert statuses == ["ok", "would-collide", "not-found", "domain-mismatch"]


# --- build_audit_f7_renames ----------------------------------------------

def test_audit_f7_proposes_rename_for_canonical_uid_with_underscore3():
    """An entity_id `sensor.gadi_inverters_inverter_2_load_power_3` whose
    unique_id is the canonical `inverter_2_load_power` should be proposed
    for rename to `sensor.gadi_inverters_inverter_2_load_power`.
    """
    reg = [
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power_3",
            unique_id="inverter_2_load_power",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    assert len(out) == 1
    assert out[0].old == "sensor.gadi_inverters_inverter_2_load_power_3"
    assert out[0].new == "sensor.gadi_inverters_inverter_2_load_power"


def test_audit_f7_skips_if_target_name_already_exists():
    """If the proposed target entity_id already belongs to another entry,
    the rename must NOT be proposed.
    """
    reg = [
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power_3",
            unique_id="inverter_2_load_power",
        ),
        # Some OTHER entry owns the clean name (worst case: another integration)
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power",
            unique_id="something_else_entirely",
            platform="template",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    assert out == []


def test_audit_f7_skips_when_unique_id_is_not_canonical():
    """Trailing `_3` in entity_id doesn't imply rename — only when the
    unique_id is the canonical `inverter_<n>_<base>`. Otherwise it's a
    legitimately distinct entity that happens to end in `_3`.
    """
    reg = [
        _entry(
            "sensor.gadi_inverters_inverter_2_battery_voltage_3",
            unique_id="some_legit_distinct_uid",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    assert out == []


def test_audit_f7_handles_inv1_and_inv2():
    """Both inv1 and inv2 are scanned. Multiple entries proposed."""
    reg = [
        _entry(
            "sensor.gadi_inverters_inverter_1_pv_power_3",
            unique_id="inverter_1_pv_power",
        ),
        _entry(
            "sensor.gadi_inverters_inverter_2_pv_power_3",
            unique_id="inverter_2_pv_power",
        ),
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power_3",
            unique_id="inverter_2_load_power",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    pairs = sorted((r.old, r.new) for r in out)
    assert pairs == sorted([
        ("sensor.gadi_inverters_inverter_1_pv_power_3",
         "sensor.gadi_inverters_inverter_1_pv_power"),
        ("sensor.gadi_inverters_inverter_2_pv_power_3",
         "sensor.gadi_inverters_inverter_2_pv_power"),
        ("sensor.gadi_inverters_inverter_2_load_power_3",
         "sensor.gadi_inverters_inverter_2_load_power"),
    ])


def test_audit_f7_skips_unrelated_prefixes():
    """Entities outside the inverter-bridge family must be ignored
    even if they have `_3` and a similar-looking unique_id pattern.
    """
    reg = [
        _entry(
            "sensor.other_integration_inverter_2_foo_3",
            unique_id="inverter_2_foo",
        ),
        _entry(
            "binary_sensor.gadi_inverters_inverter_2_bar_3",
            unique_id="inverter_2_bar",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    assert out == []


def test_audit_f7_skips_entries_without_trailing_underscore3():
    """Clean entity_ids (already correct) should not be touched."""
    reg = [
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power",
            unique_id="inverter_2_load_power",
        ),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    assert out == []


def test_audit_f7_realistic_mixed_registry():
    """Realistic scenario: F-7 fix should rename inv2's `_3` orphans
    while leaving inv1 alone (no `_3` there) and ignoring non-gadi
    entities.
    """
    reg = [
        # Inv1: clean (canonical, no _3)
        _entry(
            "sensor.gadi_inverters_inverter_1_load_power",
            unique_id="inverter_1_load_power",
        ),
        _entry(
            "sensor.gadi_inverters_inverter_1_battery_voltage",
            unique_id="inverter_1_battery_voltage",
        ),
        # Inv2: bad — has _3 with canonical unique_id (the F-7 victims)
        _entry(
            "sensor.gadi_inverters_inverter_2_load_power_3",
            unique_id="inverter_2_load_power",
        ),
        _entry(
            "sensor.gadi_inverters_inverter_2_battery_voltage_3",
            unique_id="inverter_2_battery_voltage",
        ),
        # Non-gadi: ignored
        _entry("light.living_room"),
        _entry("sensor.weather_outdoor"),
    ]
    out = rn_mod.build_audit_f7_renames(reg)
    pairs = sorted((r.old, r.new) for r in out)
    assert pairs == sorted([
        ("sensor.gadi_inverters_inverter_2_load_power_3",
         "sensor.gadi_inverters_inverter_2_load_power"),
        ("sensor.gadi_inverters_inverter_2_battery_voltage_3",
         "sensor.gadi_inverters_inverter_2_battery_voltage"),
    ])


# --- ha_base_to_ws_url (sanity check, parallel to purge tests) -----------

def test_ha_base_to_ws_url_https_to_wss():
    assert rn_mod.ha_base_to_ws_url("https://ha.example.com") == \
        "wss://ha.example.com/api/websocket"


def test_ha_base_to_ws_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        rn_mod.ha_base_to_ws_url("ftp://ha.example.com")


# --- main() CLI guards ---------------------------------------------------

def test_main_rejects_when_no_source_specified(capsys):
    """Without --mapping, --pairs or --audit-f7, exit non-zero."""
    rc = rn_mod.main([
        "--ha-base", "https://ha.example.com",
        "--ha-token", "irrelevant",
    ])
    assert rc == 1


def test_main_rejects_mapping_plus_pairs(capsys):
    """argparse should reject --mapping + --pairs at the same time
    (they're in a mutually exclusive group)."""
    with pytest.raises(SystemExit):
        rn_mod.main([
            "--ha-base", "https://ha.example.com",
            "--ha-token", "irrelevant",
            "--mapping", "foo.json",
            "--pairs", "a=b",
        ])

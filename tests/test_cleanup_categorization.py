"""Unit tests for the categorization logic in tools/cleanup_orphan_discoveries.py.

No actual MQTT roundtrip — these tests construct `Discovery` records by
hand and assert that `categorize()` puts each one in the expected bucket.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_TOOL_PATH = _HERE.parent / "tools" / "cleanup_orphan_discoveries.py"


def _load_cleanup_module():
    """Import tools/cleanup_orphan_discoveries.py as a module.

    The `tools/` directory isn't part of the inverter_bridge package, so we
    load by file path. We also register the module in `sys.modules` BEFORE
    executing it, because `@dataclasses.dataclass` looks up the host module
    in `sys.modules` during class construction.
    """
    spec = importlib.util.spec_from_file_location(
        "cleanup_orphan_discoveries", _TOOL_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cleanup_orphan_discoveries"] = mod
    spec.loader.exec_module(mod)
    return mod


cleanup = _load_cleanup_module()


# --- Fixtures ------------------------------------------------------------

@pytest.fixture
def spec():
    """A canonical spec matching the shipped MQTT_CANONICAL.json (n=2)."""
    canonical_path = _HERE.parent / "docs" / "MQTT_CANONICAL.json"
    return cleanup.load_canonical_spec(canonical_path, n_inverters=2)


def _make_sensor(uid: str, state_topic: str = "", identifiers: list[str] | None = None):
    """Construct a Discovery record for a sensor with the given fields."""
    return cleanup.Discovery(
        component="sensor",
        topic=f"homeassistant/sensor/{uid}/config",
        object_id=uid,
        unique_id=uid,
        state_topic=state_topic,
        device_identifiers=identifiers or [],
        raw={
            "unique_id": uid,
            "state_topic": state_topic,
            "device": {"identifiers": identifiers or []},
        },
    )


def _make_binary(uid: str, state_topic: str = "", identifiers: list[str] | None = None):
    return cleanup.Discovery(
        component="binary_sensor",
        topic=f"homeassistant/binary_sensor/{uid}/config",
        object_id=uid,
        unique_id=uid,
        state_topic=state_topic,
        device_identifiers=identifiers or [],
        raw={
            "unique_id": uid,
            "state_topic": state_topic,
            "device": {"identifiers": identifiers or []},
        },
    )


# --- Spec loading --------------------------------------------------------

def test_spec_loads_expected_entities(spec):
    # A few sanity checks against the shipped JSON file.
    assert "total_battery_state_of_charge" in spec.aggregates
    assert "total_pv_energy" in spec.aggregates
    assert "inverter_1_pv_power" in spec.per_inverter_unique_ids
    assert "inverter_2_temperature" in spec.per_inverter_unique_ids
    assert "meta_uptime_s" in spec.meta
    assert "inverter_bridge_online" in spec.binary_sensors


def test_spec_loader_missing_file(tmp_path, caplog):
    missing = tmp_path / "nope.json"
    s = cleanup.load_canonical_spec(missing)
    assert s.aggregates == set()
    assert s.per_inverter_unique_ids == set()
    assert s.meta == set()
    assert s.binary_sensors == set()


def test_spec_loader_partial_keys(tmp_path):
    """Missing keys default to empty list — no crash."""
    f = tmp_path / "partial.json"
    f.write_text(json.dumps({"version": 1, "aggregates": ["total_x"]}))
    s = cleanup.load_canonical_spec(f, n_inverters=2)
    assert s.aggregates == {"total_x"}
    assert s.per_inverter_unique_ids == set()
    assert s.meta == set()


def test_spec_loader_malformed_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json at all")
    s = cleanup.load_canonical_spec(f)
    assert s.aggregates == set()


# --- Canonical ----------------------------------------------------------

def test_canonical_aggregate(spec):
    d = _make_sensor("total_battery_state_of_charge",
                     state_topic="solar_assistant/total/battery_state_of_charge/state",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL


def test_canonical_per_inverter(spec):
    d = _make_sensor("inverter_1_pv_power",
                     state_topic="solar_assistant/inverter_1/pv_power/state",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL


def test_canonical_meta(spec):
    d = _make_sensor("meta_uptime_s",
                     state_topic="solar_assistant/_meta/uptime_s/state",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL


def test_canonical_energy_aggregate(spec):
    d = _make_sensor("total_pv_energy",
                     state_topic="solar_assistant/total/pv_energy/state",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL


def test_canonical_binary_sensor(spec):
    d = _make_binary("inverter_bridge_online",
                     state_topic="solar_assistant/availability",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL


def test_binary_sensor_not_canonical_when_uid_unknown(spec):
    """A binary_sensor with a non-canonical uid shouldn't be CANONICAL even
    if its base name matches a sensor uid."""
    d = _make_binary("total_battery_state_of_charge")
    # It's not a known binary_sensor uid; falls through to UNRELATED.
    assert cleanup.categorize(d, spec) != cleanup.CAT_CANONICAL


# --- ORPHAN_V1 -----------------------------------------------------------

def test_orphan_v1_prefix(spec):
    d = _make_sensor("gadi_inverters_battery_state_of_charge",
                     state_topic="gadi_inverters/total/battery_state_of_charge/state")
    assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_V1


def test_orphan_v1_prefix_with_per_inverter(spec):
    d = _make_sensor("gadi_inverters_inverter_1_pv_power",
                     state_topic="gadi_inverters/inverter_1/pv_power/state")
    assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_V1


def test_orphan_v1_prefix_binary(spec):
    d = _make_binary("gadi_inverters_inverter_bridge_online")
    assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_V1


# --- ORPHAN_COLLISION ---------------------------------------------------

def test_orphan_collision_single_suffix(spec):
    """`total_battery_state_of_charge_2` is HA-side duplicate of a canonical uid."""
    d = _make_sensor("total_battery_state_of_charge_2",
                     state_topic="solar_assistant/total/battery_state_of_charge/state",
                     identifiers=["sa_inverter"])
    assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_COLLISION


def test_orphan_collision_chained_suffix(spec):
    """`_2_2` and `_2_3` chains are also HA-side collisions."""
    for uid in ("total_battery_state_of_charge_2_2",
                "total_pv_power_3",
                "inverter_1_pv_power_2_2",
                "inverter_2_temperature_2"):
        d = _make_sensor(uid)
        assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_COLLISION, uid


def test_orphan_collision_binary(spec):
    d = _make_binary("inverter_bridge_online_2")
    assert cleanup.categorize(d, spec) == cleanup.CAT_ORPHAN_COLLISION


def test_canonical_l1_l2_mppt_suffixes_not_treated_as_collision(spec):
    """Canonical uids ending in `_l1`/`_l2`/`_mppt1`/`_mppt2` (the new explicit
    phase / string suffix scheme) must stay CANONICAL. Only trailing `_N` or
    `_N_M` digit-only suffixes are HA collision markers."""
    for uid in ("inverter_1_load_power_l1",
                "inverter_1_load_power_l2",
                "inverter_1_pv_voltage_mppt1",
                "inverter_1_pv_voltage_mppt2",
                "inverter_2_pv_power_mppt1",
                "inverter_2_pv_current_mppt2"):
        d = _make_sensor(uid)
        assert cleanup.categorize(d, spec) == cleanup.CAT_CANONICAL, uid


# --- SA_STATIC ----------------------------------------------------------

def test_sa_static_inverter_serial_number(spec):
    """An SA discovery for a static config sensor we don't publish."""
    d = _make_sensor(
        "inverter_1_serial_number",
        state_topic="solar_assistant/inverter_1/serial_number/state",
        identifiers=["sa_inverter"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_SA_STATIC


def test_sa_static_battery_capacity(spec):
    d = _make_sensor(
        "battery_1_capacity",
        state_topic="solar_assistant/battery_1/capacity/state",
        identifiers=["sa_inverter"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_SA_STATIC


def test_sa_static_charger_source_priority(spec):
    """The exact example called out in the task: charger_source_priority."""
    d = _make_sensor(
        "inverter_1_charger_source_priority",
        state_topic="solar_assistant/inverter_1/charger_source_priority/state",
        identifiers=["sa_inverter"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_SA_STATIC


def test_sa_static_max_charge_current(spec):
    d = _make_sensor(
        "inverter_2_max_charge_current",
        state_topic="solar_assistant/inverter_2/max_charge_current/state",
        identifiers=["sa_inverter"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_SA_STATIC


# --- UNRELATED ----------------------------------------------------------

def test_unrelated_zigbee2mqtt(spec):
    """zigbee2mqtt-published discoveries must never be touched."""
    d = _make_sensor(
        "0x00158d0001234567_temperature",
        state_topic="zigbee2mqtt/Living Room Sensor",
        identifiers=["zigbee2mqtt_bridge"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_UNRELATED


def test_unrelated_esphome(spec):
    d = _make_sensor(
        "esp32-relay-3_input",
        state_topic="esphome/relay-3/sensor/input/state",
        identifiers=["esphome_relay3"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_UNRELATED


def test_unrelated_random_third_party(spec):
    d = _make_sensor(
        "some_unrelated_thing",
        state_topic="otherdomain/foo",
        identifiers=["other"],
    )
    assert cleanup.categorize(d, spec) == cleanup.CAT_UNRELATED


# --- Helpers ------------------------------------------------------------

def test_strip_collision_suffix_no_suffix():
    base, had = cleanup._strip_collision_suffix("total_battery_voltage")
    assert base == "total_battery_voltage"
    assert had is False


def test_strip_collision_suffix_single_digit():
    base, had = cleanup._strip_collision_suffix("total_battery_voltage_2")
    assert base == "total_battery_voltage"
    assert had is True


def test_strip_collision_suffix_chained():
    base, had = cleanup._strip_collision_suffix("total_battery_voltage_2_3")
    assert base == "total_battery_voltage"
    assert had is True


def test_strip_collision_suffix_pv1(spec):
    """`pv1_voltage` ends with no digit, so no stripping."""
    base, had = cleanup._strip_collision_suffix("inverter_1_pv1_voltage")
    # `pv1_voltage` ends with `_voltage` not `_<digit>`, so no suffix found.
    assert had is False
    assert base == "inverter_1_pv1_voltage"


def test_parse_discovery_uniq_id_alias():
    """HA Discovery allows `uniq_id` short form. Make sure we read it."""
    payload = json.dumps({
        "uniq_id": "total_battery_voltage",
        "stat_t": "solar_assistant/total/battery_voltage/state",
    }).encode("utf-8")
    d = cleanup.parse_discovery(
        "sensor",
        "homeassistant/sensor/total_battery_voltage/config",
        payload,
    )
    assert d is not None
    assert d.unique_id == "total_battery_voltage"
    assert d.state_topic == "solar_assistant/total/battery_voltage/state"


def test_parse_discovery_empty_payload():
    """Empty payload (cleared retained) returns None — no entry to act on."""
    d = cleanup.parse_discovery(
        "sensor", "homeassistant/sensor/x/config", b"",
    )
    assert d is None


def test_parse_discovery_handles_missing_device():
    payload = json.dumps({"unique_id": "abc", "state_topic": "y/z"}).encode("utf-8")
    d = cleanup.parse_discovery("sensor", "homeassistant/sensor/abc/config", payload)
    assert d is not None
    assert d.device_identifiers == []


def test_parse_discovery_identifiers_string():
    """If identifiers is a single string, wrap into a list."""
    payload = json.dumps({
        "unique_id": "abc",
        "device": {"identifiers": "single_string"},
    }).encode("utf-8")
    d = cleanup.parse_discovery("sensor", "homeassistant/sensor/abc/config", payload)
    assert d is not None
    assert d.device_identifiers == ["single_string"]


def test_collect_topics_to_clean_excludes_sa_static_by_default(spec):
    """SA_STATIC stays untouched unless --include-sa-static is set."""
    v1 = _make_sensor("gadi_inverters_battery_voltage")
    collision = _make_sensor("total_battery_voltage_2",
                             state_topic="solar_assistant/total/battery_voltage/state")
    sa = _make_sensor(
        "inverter_1_serial_number",
        state_topic="solar_assistant/inverter_1/serial_number/state",
        identifiers=["sa_inverter"],
    )
    canonical = _make_sensor("total_battery_voltage",
                             state_topic="solar_assistant/total/battery_voltage/state")
    buckets = cleanup.build_report([v1, collision, sa, canonical], spec)
    assert buckets[cleanup.CAT_ORPHAN_V1] == [v1]
    assert buckets[cleanup.CAT_ORPHAN_COLLISION] == [collision]
    assert buckets[cleanup.CAT_SA_STATIC] == [sa]
    assert buckets[cleanup.CAT_CANONICAL] == [canonical]

    default = cleanup.collect_topics_to_clean(buckets, include_sa_static=False)
    assert v1.topic in default
    assert collision.topic in default
    assert sa.topic not in default
    assert canonical.topic not in default

    full = cleanup.collect_topics_to_clean(buckets, include_sa_static=True)
    assert sa.topic in full
    assert canonical.topic not in full


def test_build_report_buckets_complete(spec):
    """Every bucket key must be present even if empty."""
    buckets = cleanup.build_report([], spec)
    assert set(buckets.keys()) == {
        cleanup.CAT_CANONICAL,
        cleanup.CAT_ORPHAN_V1,
        cleanup.CAT_ORPHAN_COLLISION,
        cleanup.CAT_SA_STATIC,
        cleanup.CAT_UNRELATED,
    }
    for v in buckets.values():
        assert v == []

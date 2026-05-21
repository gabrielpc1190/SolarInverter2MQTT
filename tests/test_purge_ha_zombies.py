"""Unit tests for the candidate-selection logic in
`tools/purge_ha_zombies.py`.

No actual WS roundtrip and no REST calls. Tests construct `RegistryEntry`
and `StateRecord` records by hand and assert that `select_candidates()`
picks the right ones for various combinations of:

  * entity_id prefix match / no match,
  * fresh vs stale state,
  * recently vs long-ago `last_updated`.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_TOOL_PATH = _HERE.parent / "tools" / "purge_ha_zombies.py"


def _load_purge_module():
    """Import tools/purge_ha_zombies.py as a module.

    Same trick used by tests/test_cleanup_categorization.py — register in
    sys.modules before exec so `@dataclasses.dataclass(frozen=True)` can
    resolve the host module.
    """
    spec = importlib.util.spec_from_file_location(
        "purge_ha_zombies", _TOOL_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["purge_ha_zombies"] = mod
    spec.loader.exec_module(mod)
    return mod


purge = _load_purge_module()


# --- Fixtures ------------------------------------------------------------

DEFAULT_DOMAINS = (
    "sensor.gadi_inverters_",
    "binary_sensor.gadi_inverters_",
    "binary_sensor.inverter_bridge_",
)

# A fixed "now" so the tests are deterministic regardless of clock.
NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)


def _entry(entity_id: str, *, unique_id: str = "", platform: str = "mqtt"):
    return purge.RegistryEntry(
        entity_id=entity_id,
        unique_id=unique_id or entity_id.replace(".", "_"),
        platform=platform,
        raw={"entity_id": entity_id, "unique_id": unique_id, "platform": platform},
    )


def _state(entity_id: str, *, state: str, age_s: float | None):
    last_updated: datetime | None = (
        None if age_s is None else NOW - timedelta(seconds=age_s)
    )
    return purge.StateRecord(
        entity_id=entity_id,
        state=state,
        last_updated=last_updated,
    )


# --- Test 1: outside domain prefixes is NOT selected ----------------------

def test_entity_outside_domain_prefixes_is_ignored():
    """A registry entity whose entity_id doesn't match any --domain prefix
    must never be selected, even if it's stale for hours."""
    reg = [_entry("light.kitchen_main")]
    states = [_state("light.kitchen_main", state="unavailable", age_s=24 * 3600)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


def test_multiple_entities_outside_domain_all_ignored():
    """Several non-matching entities — none selected."""
    reg = [
        _entry("switch.front_door"),
        _entry("climate.living"),
        _entry("sensor.weather_outdoor_temp"),  # in `sensor.` but no inverter prefix
        _entry("binary_sensor.motion_garage"),
    ]
    states = [
        _state(e.entity_id, state="unavailable", age_s=10 * 3600) for e in reg
    ]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


# --- Test 2: in domain BUT state OK is NOT selected -----------------------

def test_entity_in_domain_with_healthy_state_is_ignored():
    """Even if the prefix matches, a healthy (non-unavailable/unknown) state
    must not be selected."""
    reg = [_entry("sensor.gadi_inverters_battery_voltage")]
    states = [_state("sensor.gadi_inverters_battery_voltage",
                     state="51.2", age_s=30)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


def test_in_domain_with_various_healthy_states_ignored():
    """Sample a few realistic non-stale states."""
    reg = [
        _entry("sensor.gadi_inverters_pv_power"),
        _entry("sensor.gadi_inverters_battery_soc"),
        _entry("binary_sensor.inverter_bridge_online"),
    ]
    states = [
        _state("sensor.gadi_inverters_pv_power", state="1234", age_s=5),
        _state("sensor.gadi_inverters_battery_soc", state="78", age_s=10),
        _state("binary_sensor.inverter_bridge_online", state="on", age_s=0),
    ]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


# --- Test 3: unavailable for >=1 h is selected ---------------------------

def test_entity_unavailable_for_1h_is_selected_with_default_threshold():
    """Exactly 1 h old `unavailable` -> selected with default 3600 s."""
    reg = [_entry("sensor.gadi_inverters_old_zombie")]
    states = [_state("sensor.gadi_inverters_old_zombie",
                     state="unavailable", age_s=3600)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_old_zombie"]


def test_entity_unavailable_for_24h_is_selected():
    """Day-old stale entity is also selected."""
    reg = [_entry("binary_sensor.inverter_bridge_old_flag")]
    states = [_state("binary_sensor.inverter_bridge_old_flag",
                     state="unavailable", age_s=24 * 3600)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == [
        "binary_sensor.inverter_bridge_old_flag"
    ]


def test_unknown_state_treated_same_as_unavailable():
    """`unknown` is also stale per HA semantics."""
    reg = [_entry("sensor.gadi_inverters_legacy_thing")]
    states = [_state("sensor.gadi_inverters_legacy_thing",
                     state="unknown", age_s=2 * 3600)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_legacy_thing"]


# --- Test 4: unavailable for 30 min is NOT selected ----------------------

def test_entity_unavailable_for_30min_is_not_selected_with_default_threshold():
    """30 min stale < 1 h default threshold -> NOT selected (still healing?)."""
    reg = [_entry("sensor.gadi_inverters_just_dropped")]
    states = [_state("sensor.gadi_inverters_just_dropped",
                     state="unavailable", age_s=1800)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


def test_threshold_boundary_just_under_is_not_selected():
    """A few seconds shy of the threshold -> not selected."""
    reg = [_entry("sensor.gadi_inverters_almost")]
    states = [_state("sensor.gadi_inverters_almost",
                     state="unavailable", age_s=3599)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert selected == []


def test_threshold_boundary_exactly_at_threshold_is_selected():
    """At exactly the threshold (>=) -> selected."""
    reg = [_entry("sensor.gadi_inverters_at_boundary")]
    states = [_state("sensor.gadi_inverters_at_boundary",
                     state="unavailable", age_s=3600)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_at_boundary"]


def test_custom_threshold_is_respected():
    """With min_age_s=10, a 30-min stale entity should be selected."""
    reg = [_entry("sensor.gadi_inverters_recent_drop")]
    states = [_state("sensor.gadi_inverters_recent_drop",
                     state="unavailable", age_s=1800)]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=10,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_recent_drop"]


# --- Test 5: multiple entities batch correctly --------------------------

def test_mixed_batch_correctly_partitioned():
    """A realistic batch of registry entries + states. Verify ONLY the
    in-domain, stale, old-enough entries are selected.
    """
    reg = [
        # Outside domain — never selected.
        _entry("light.kitchen"),
        _entry("sensor.outdoor_temp"),
        # In domain, healthy — never selected.
        _entry("sensor.gadi_inverters_pv_power"),
        _entry("binary_sensor.inverter_bridge_online"),
        # In domain, stale, old enough — SELECTED.
        _entry("sensor.gadi_inverters_zombie_a"),
        _entry("sensor.gadi_inverters_zombie_b"),
        _entry("binary_sensor.gadi_inverters_dead_flag"),
        _entry("binary_sensor.inverter_bridge_dead_flag"),
        # In domain, stale, but RECENT — never selected.
        _entry("sensor.gadi_inverters_just_offline"),
        # In domain, healthy with an old timestamp — never selected
        # (state is the gate; old-but-healthy is fine).
        _entry("sensor.gadi_inverters_old_but_alive"),
    ]
    states = [
        _state("light.kitchen", state="on", age_s=5),
        _state("sensor.outdoor_temp", state="20", age_s=60),
        _state("sensor.gadi_inverters_pv_power", state="500", age_s=2),
        _state("binary_sensor.inverter_bridge_online", state="on", age_s=2),
        _state("sensor.gadi_inverters_zombie_a", state="unavailable", age_s=2 * 3600),
        _state("sensor.gadi_inverters_zombie_b", state="unknown", age_s=5 * 3600),
        _state("binary_sensor.gadi_inverters_dead_flag", state="unavailable", age_s=10 * 3600),
        _state("binary_sensor.inverter_bridge_dead_flag", state="unavailable", age_s=24 * 3600),
        _state("sensor.gadi_inverters_just_offline", state="unavailable", age_s=600),
        _state("sensor.gadi_inverters_old_but_alive", state="42", age_s=24 * 3600),
    ]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    selected_ids = sorted(e.entity_id for e in selected)
    assert selected_ids == sorted([
        "sensor.gadi_inverters_zombie_a",
        "sensor.gadi_inverters_zombie_b",
        "binary_sensor.gadi_inverters_dead_flag",
        "binary_sensor.inverter_bridge_dead_flag",
    ])


# --- Edge cases ----------------------------------------------------------

def test_entity_missing_from_states_is_treated_as_zombie():
    """An entity registered but absent from /api/states is the worst kind
    of zombie — selected unconditionally (so long as the prefix matches).
    """
    reg = [_entry("sensor.gadi_inverters_no_state_ever")]
    states: list = []  # /api/states doesn't have it
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_no_state_ever"]


def test_stale_state_without_timestamp_is_selected():
    """If `last_updated` cannot be parsed but state is unavailable, the
    entity is a zombie regardless of age.
    """
    reg = [_entry("sensor.gadi_inverters_no_ts")]
    states = [purge.StateRecord(
        entity_id="sensor.gadi_inverters_no_ts",
        state="unavailable",
        last_updated=None,
    )]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=DEFAULT_DOMAINS,
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.gadi_inverters_no_ts"]


def test_custom_domain_prefixes_only_match_themselves():
    """If the user passes a narrower prefix list, only those match."""
    reg = [
        _entry("sensor.gadi_inverters_zombie_a"),       # default-list zombie
        _entry("binary_sensor.inverter_bridge_zombie"), # default-list zombie
        _entry("sensor.zigbee_temp_zombie"),            # custom prefix zombie
    ]
    states = [
        _state(e.entity_id, state="unavailable", age_s=10 * 3600) for e in reg
    ]
    selected = purge.select_candidates(
        reg, states,
        domain_prefixes=("sensor.zigbee_",),  # custom narrower list
        min_age_s=3600,
        now=NOW,
    )
    assert [e.entity_id for e in selected] == ["sensor.zigbee_temp_zombie"]


def test_ha_base_to_ws_url_https_to_wss():
    """`https://host` -> `wss://host/api/websocket`."""
    assert purge.ha_base_to_ws_url("https://ha.example.com") == \
        "wss://ha.example.com/api/websocket"


def test_ha_base_to_ws_url_http_to_ws():
    assert purge.ha_base_to_ws_url("http://192.0.2.1:8123") == \
        "ws://192.0.2.1:8123/api/websocket"


def test_ha_base_to_ws_url_strips_trailing_slash():
    assert purge.ha_base_to_ws_url("https://ha.example.com/") == \
        "wss://ha.example.com/api/websocket"


def test_ha_base_to_ws_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        purge.ha_base_to_ws_url("ftp://ha.example.com")

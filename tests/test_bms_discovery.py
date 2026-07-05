"""Tests del catálogo de discovery del BMS (all_bms_entities)."""
from inverter_bridge.bms.discovery import all_bms_entities


def _by_object_id() -> dict[str, object]:
    return {e.object_id: e for e in all_bms_entities()}


def test_catalog_count_is_100():
    # 28 PIA + 40 PIB + 4 serial + 17 bank + 2 energy + 9 telemetry
    assert len(all_bms_entities()) == 100


def test_object_ids_unique():
    ents = all_bms_entities()
    ids = [e.object_id for e in ents]
    assert len(ids) == len(set(ids))


def test_bank_soc_min_present():
    e = _by_object_id().get("bluesun_bank_soc_min")
    assert e is not None
    assert e.unit == "%"
    assert e.device_class == "battery"


def test_soc_polls_heartbeat_present_for_each_pack():
    ents = _by_object_id()
    for p in range(1, 5):
        e = ents.get(f"bluesun_pack{p:02d}_soc_polls")
        assert e is not None, f"falta soc_polls del pack {p}"
        assert e.state_class == "total_increasing"
        assert e.entity_category == "diagnostic"

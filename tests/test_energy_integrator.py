"""Tests for the client-side Wh accumulator (post-deploy finding F-2)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from inverter_bridge.energy_integrator import EnergyIntegrator

# The 6 keys update() must return.
EXPECTED_KEYS = {
    "battery_energy_in",
    "battery_energy_out",
    "pv_energy",
    "load_energy",
    "grid_energy_in",
    "grid_energy_out",
}


def _zero_aggregated() -> dict[str, float | str]:
    return {
        "battery_power": 0.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }


def test_zero_power_no_integration() -> None:
    """All zero power values -> all accumulators remain at 0."""
    integ = EnergyIntegrator()
    out = integ.update(aggregated=_zero_aggregated(), elapsed_s=60.0)
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0


def test_battery_charging_increments_in() -> None:
    """battery_power=+500W, elapsed=60s -> battery_energy_in = 8.333... Wh = 0.008 kWh."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": 500.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=60.0)
    # 500 W * 60 s / 3600 s/h = 8.3333... Wh = 0.008333... kWh -> rounded to 0.008
    assert out["battery_energy_in"] == 0.008
    # No other accumulator should have moved.
    assert out["battery_energy_out"] == 0.0
    assert out["pv_energy"] == 0.0
    assert out["load_energy"] == 0.0
    assert out["grid_energy_in"] == 0.0
    assert out["grid_energy_out"] == 0.0


def test_battery_discharging_increments_out() -> None:
    """battery_power=-1000W, elapsed=30s -> battery_energy_out = 8.333... Wh."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": -1000.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=30.0)
    # |-1000| W * 30 s / 3600 = 8.3333... Wh = 0.008 kWh
    assert out["battery_energy_out"] == 0.008
    assert out["battery_energy_in"] == 0.0


def test_pv_load_grid_integrate_correctly() -> None:
    """Multiple non-zero signals integrate independently in one step."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": 0.0,
        "load_power": 1200.0,
        "pv_power": 3600.0,
        "grid_power": 0.0,
    }
    # 600 s = 1/6 h (max allowed elapsed_s)
    out = integ.update(aggregated=aggregated, elapsed_s=600.0)
    # pv: 3600 W * 600 s / 3600 = 600 Wh = 0.600 kWh
    assert out["pv_energy"] == 0.600
    # load: 1200 W * 600 s / 3600 = 200 Wh = 0.200 kWh
    assert out["load_energy"] == 0.200
    # battery & grid untouched
    assert out["battery_energy_in"] == 0.0
    assert out["battery_energy_out"] == 0.0
    assert out["grid_energy_in"] == 0.0
    assert out["grid_energy_out"] == 0.0


def test_grid_export_uses_grid_out() -> None:
    """grid_power=-200W (exporting) -> only grid_energy_out increments."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": 0.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": -200.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=600.0)
    # |-200| W * 600 s / 3600 = 33.333... Wh = 0.033 kWh
    assert out["grid_energy_out"] == 0.033
    assert out["grid_energy_in"] == 0.0


def test_grid_import_uses_grid_in() -> None:
    """grid_power=+500W (importing) -> only grid_energy_in increments."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": 0.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 500.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=600.0)
    # 500 W * 600 s / 3600 = 83.333... Wh = 0.083 kWh
    assert out["grid_energy_in"] == 0.083
    assert out["grid_energy_out"] == 0.0


def test_persist_and_restore(tmp_path: Path) -> None:
    """Save state then reload into a fresh instance -> values match."""
    persist = tmp_path / "energy.json"
    integ = EnergyIntegrator(persist_path=persist)
    aggregated: dict[str, float | str] = {
        "battery_power": 800.0,
        "load_power": 400.0,
        "pv_power": 1200.0,
        "grid_power": -50.0,
    }
    integ.update(aggregated=aggregated, elapsed_s=600.0)
    snapshot_before = integ.update(aggregated=aggregated, elapsed_s=600.0)
    integ.save()
    assert persist.exists()

    # Fresh instance pointing at the same file -> __init__ calls load().
    fresh = EnergyIntegrator(persist_path=persist)
    snapshot_after = fresh.update(aggregated=_zero_aggregated(), elapsed_s=0.001)
    # elapsed_s = 0.001 contributes a negligible amount that rounds to 0.000 in kWh,
    # so the snapshots should match exactly.
    for key in EXPECTED_KEYS:
        assert snapshot_after[key] == snapshot_before[key]


def test_load_missing_file_starts_fresh(tmp_path: Path) -> None:
    """load() with a non-existent path is a silent no-op; accumulators stay at 0."""
    persist = tmp_path / "does_not_exist.json"
    integ = EnergyIntegrator(persist_path=persist)
    # Explicit load() call should also not raise.
    integ.load()
    out = integ.update(aggregated=_zero_aggregated(), elapsed_s=60.0)
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0


def test_load_corrupt_json_starts_fresh(tmp_path: Path) -> None:
    """Corrupt JSON in persist file -> no exception, accumulators at 0."""
    persist = tmp_path / "energy.json"
    persist.write_text("{not valid json at all", encoding="utf-8")
    # Must not raise.
    integ = EnergyIntegrator(persist_path=persist)
    out = integ.update(aggregated=_zero_aggregated(), elapsed_s=60.0)
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0


def test_skips_invalid_elapsed() -> None:
    """elapsed_s <= 0 or > 600 s -> integration skipped, no exception, current values returned."""
    integ = EnergyIntegrator()
    # Prime with one good step so totals are nonzero.
    aggregated: dict[str, float | str] = {
        "battery_power": 500.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }
    primed = integ.update(aggregated=aggregated, elapsed_s=60.0)
    primed_in = primed["battery_energy_in"]
    assert primed_in > 0

    # Negative elapsed_s -> skip.
    out_neg = integ.update(aggregated=aggregated, elapsed_s=-1.0)
    assert out_neg["battery_energy_in"] == primed_in

    # Zero elapsed_s -> skip (no contribution either way).
    out_zero = integ.update(aggregated=aggregated, elapsed_s=0.0)
    assert out_zero["battery_energy_in"] == primed_in

    # 1000 s > 600 s -> skip.
    out_huge = integ.update(aggregated=aggregated, elapsed_s=1000.0)
    assert out_huge["battery_energy_in"] == primed_in

    # NaN -> skip.
    out_nan = integ.update(aggregated=aggregated, elapsed_s=float("nan"))
    assert out_nan["battery_energy_in"] == primed_in


def test_handles_missing_power_keys() -> None:
    """Empty aggregated dict -> no error, returns 0 values for all accumulators."""
    integ = EnergyIntegrator()
    out = integ.update(aggregated={}, elapsed_s=60.0)
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0


def test_handles_non_numeric_power_value() -> None:
    """A string value in aggregated (e.g. mode='Battery') must not crash integration."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "mode": "Battery",            # string, must be ignored even though it's not a power key
        "battery_power": "Battery",   # string in a power slot -> coerced to 0
        "load_power": 600.0,
        "pv_power": 1200.0,
        "grid_power": 0.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=600.0)
    # battery_power was a string -> treated as 0, no battery accumulator moves.
    assert out["battery_energy_in"] == 0.0
    assert out["battery_energy_out"] == 0.0
    # load: 600 W * 600 s / 3600 = 100 Wh = 0.100 kWh
    assert out["load_energy"] == 0.100
    # pv: 1200 W * 600 s / 3600 = 200 Wh = 0.200 kWh
    assert out["pv_energy"] == 0.200


def test_handles_non_finite_power_value() -> None:
    """NaN/Inf power values must be treated as 0, not propagated into accumulators."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": float("nan"),
        "load_power": float("inf"),
        "pv_power": -float("inf"),
        "grid_power": 0.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=60.0)
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0
        assert math.isfinite(out[key])


def test_output_in_kwh_rounded() -> None:
    """Returned values are kWh (Wh/1000), rounded to 3 decimal places."""
    integ = EnergyIntegrator()
    # 1234 W * 60 s = 1234 * 60 / 3600 = 20.5666... Wh = 0.0205666... kWh -> 0.021
    aggregated: dict[str, float | str] = {
        "battery_power": 0.0,
        "load_power": 1234.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }
    out = integ.update(aggregated=aggregated, elapsed_s=60.0)
    assert out["load_energy"] == 0.021
    # All values must be floats and look like 3-decimal-rounded numbers.
    for key in EXPECTED_KEYS:
        v = out[key]
        assert isinstance(v, float)
        # Equality between repeated round(...) calls confirms we already rounded.
        assert round(v, 3) == v


def test_returned_keys_match_contract() -> None:
    """update() must return exactly the 6 documented keys -- no extras, no internal Wh names."""
    integ = EnergyIntegrator()
    out = integ.update(aggregated=_zero_aggregated(), elapsed_s=60.0)
    assert set(out.keys()) == EXPECTED_KEYS
    # Make sure no internal _wh keys leak.
    for key in out:
        assert not key.endswith("_wh")


def test_save_noop_without_persist_path(tmp_path: Path) -> None:
    """save() with persist_path=None is a no-op and doesn't raise."""
    integ = EnergyIntegrator()  # no path
    integ.update(
        aggregated={"battery_power": 500.0, "load_power": 0.0, "pv_power": 0.0, "grid_power": 0.0},
        elapsed_s=60.0,
    )
    # Should be silently fine.
    integ.save()
    # tmp_path should still be empty -- nothing got written.
    assert list(tmp_path.iterdir()) == []


def test_persist_file_is_versioned_json(tmp_path: Path) -> None:
    """save() writes a JSON object with a version field and the 6 Wh keys."""
    persist = tmp_path / "energy.json"
    integ = EnergyIntegrator(persist_path=persist)
    integ.update(
        aggregated={"battery_power": 500.0, "load_power": 0.0, "pv_power": 0.0, "grid_power": 0.0},
        elapsed_s=60.0,
    )
    integ.save()
    data = json.loads(persist.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "version" in data
    assert data["version"] == 1
    for key in (
        "battery_energy_in_wh",
        "battery_energy_out_wh",
        "pv_energy_wh",
        "load_energy_wh",
        "grid_energy_in_wh",
        "grid_energy_out_wh",
    ):
        assert key in data


def test_version_mismatch_starts_fresh(tmp_path: Path) -> None:
    """A persist file with the wrong version -> warn + start at zero, no raise."""
    persist = tmp_path / "energy.json"
    persist.write_text(
        json.dumps(
            {
                "version": 999,
                "battery_energy_in_wh": 12345.0,
                "battery_energy_out_wh": 0,
                "pv_energy_wh": 0,
                "load_energy_wh": 0,
                "grid_energy_in_wh": 0,
                "grid_energy_out_wh": 0,
            }
        ),
        encoding="utf-8",
    )
    integ = EnergyIntegrator(persist_path=persist)
    out = integ.update(aggregated=_zero_aggregated(), elapsed_s=60.0)
    # Nothing should have been restored from the stale-version file.
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0


def test_accumulators_persist_across_multiple_updates() -> None:
    """Multiple update() calls accumulate -- no clobber, no decay."""
    integ = EnergyIntegrator()
    aggregated: dict[str, float | str] = {
        "battery_power": 3600.0,
        "load_power": 0.0,
        "pv_power": 0.0,
        "grid_power": 0.0,
    }
    # 10 steps of 600 s each at 3600 W -> 3600 * 600 * 10 / 3600 = 6000 Wh = 6.0 kWh.
    out: dict[str, float] = {}
    for _ in range(10):
        out = integ.update(aggregated=aggregated, elapsed_s=600.0)
    assert out["battery_energy_in"] == 6.000


@pytest.mark.parametrize(
    "elapsed_s",
    [-100.0, -1.0, 0.0, 600.001, 1000.0, float("inf"), float("nan")],
)
def test_defensive_elapsed_bounds(elapsed_s: float) -> None:
    """Property-style: every out-of-band elapsed_s value is a safe no-op."""
    integ = EnergyIntegrator()
    out = integ.update(
        aggregated={
            "battery_power": 1000.0,
            "load_power": 1000.0,
            "pv_power": 1000.0,
            "grid_power": 1000.0,
        },
        elapsed_s=elapsed_s,
    )
    for key in EXPECTED_KEYS:
        assert out[key] == 0.0

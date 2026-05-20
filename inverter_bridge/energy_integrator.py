"""Client-side Wh accumulator for energy sensors.

Addresses post-deploy finding F-2 (see
`docs/superpowers/inverter-bridge-2026-05-20-deficiencias-postdeploy.md`):
the 6 energy accumulator sensors (`battery_energy_in/out`, `load_energy`,
`pv_energy`, `grid_energy_in/out`) were left in `unknown` state because
no client-side integration was wired in.

This module samples the aggregated power values produced by
`inverter_bridge.aggregator.aggregate_inverters()` and integrates each
relevant signal across time:

    Wh += W * dt / 3600

State is held in watt-hours (float) internally; `update()` returns the 6
accumulators converted to kWh and rounded to 3 decimals — that is the
shape the MQTT publisher will hand to Home Assistant.

State is persisted as JSON (versioned) and writes are atomic
(`.tmp` + fsync + rename) so daemon restarts don't reset the counters.

Spec references: §5.8 (sensor → register mapping, energy sensors) and
§13 OQ #6 (client-side integration was explicitly left as pending work).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)


# Internal accumulator keys (watt-hours, full precision).
_WH_KEYS: Final[tuple[str, ...]] = (
    "battery_energy_in_wh",
    "battery_energy_out_wh",
    "pv_energy_wh",
    "load_energy_wh",
    "grid_energy_in_wh",
    "grid_energy_out_wh",
)

# Mapping from internal Wh key to the public kWh key exposed via update()
# and ultimately published to MQTT.
_WH_TO_KWH_KEY: Final[dict[str, str]] = {
    "battery_energy_in_wh": "battery_energy_in",
    "battery_energy_out_wh": "battery_energy_out",
    "pv_energy_wh": "pv_energy",
    "load_energy_wh": "load_energy",
    "grid_energy_in_wh": "grid_energy_in",
    "grid_energy_out_wh": "grid_energy_out",
}

# JSON persistence schema version. Bump on incompatible changes.
_PERSIST_VERSION: Final[int] = 1

# Defensive bounds for elapsed_s. Hot cycle is ~3 s; anything > 10 min is
# almost certainly clock weirdness (NTP step, suspend, debug pause).
_MAX_ELAPSED_S: Final[float] = 600.0


def _coerce_power(value: object) -> float:
    """Return a finite float, or 0.0 if value is missing/non-numeric/non-finite.

    The aggregated dict mixes `float` and `str` values (e.g. `mode = "Battery"`).
    A string in a power slot, NaN, or +/-inf all degrade to 0.0 so this step
    contributes nothing to the integral and we keep marching forward.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; reject it defensively.
        return 0.0
    if isinstance(value, int | float):
        f = float(value)
        if not math.isfinite(f):
            log.warning("energy_integrator: non-finite power value %r treated as 0", value)
            return 0.0
        return f
    return 0.0


class EnergyIntegrator:
    """Client-side Wh accumulator for energy sensors.

    Tracks 6 accumulators in watt-hours:
      - battery_energy_in_wh    (battery charging)
      - battery_energy_out_wh   (battery discharging)
      - pv_energy_wh            (PV production)
      - load_energy_wh          (load consumption)
      - grid_energy_in_wh       (grid import)
      - grid_energy_out_wh      (grid export)

    On update(), takes the current aggregated power values and adds
    W * dt / 3600 to each appropriate accumulator. Persists state to disk
    on save() so a daemon restart doesn't reset counters.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        """Initialize. If persist_path is given and the file exists, load state from it."""
        self.persist_path: Path | None = persist_path
        self._wh: dict[str, float] = {k: 0.0 for k in _WH_KEYS}
        if self.persist_path is not None and self.persist_path.exists():
            self.load()

    # --- integration ----------------------------------------------------

    def update(
        self,
        *,
        aggregated: dict[str, float | str],
        elapsed_s: float,
    ) -> dict[str, float]:
        """Integrate one step.

        Returns the 6 accumulator values in kWh (Wh / 1000, rounded to 3
        decimals) — that's the publish-ready shape, not the internal state.

        `aggregated` is the dict produced by
        `inverter_bridge.aggregator.aggregate_inverters()`. Keys consumed:
          - "battery_power"  (signed: + = charging, - = discharging)
          - "load_power"     (always >= 0)
          - "pv_power"       (always >= 0)
          - "grid_power"     (signed: + = import from grid, - = export)

        Missing/non-numeric/non-finite values contribute 0 W this step.
        `elapsed_s` <= 0 or > 600 s is treated as a clock anomaly: the
        integration is skipped for that step (warning logged) but the
        current totals are still returned so the caller can publish.
        """
        if not math.isfinite(elapsed_s) or elapsed_s <= 0 or elapsed_s > _MAX_ELAPSED_S:
            log.warning(
                "energy_integrator: skipping integration for invalid elapsed_s=%r "
                "(must be 0 < elapsed_s <= %.0f)",
                elapsed_s,
                _MAX_ELAPSED_S,
            )
            return self._snapshot_kwh()

        battery_power = _coerce_power(aggregated.get("battery_power"))
        load_power = _coerce_power(aggregated.get("load_power"))
        pv_power = _coerce_power(aggregated.get("pv_power"))
        grid_power = _coerce_power(aggregated.get("grid_power"))

        # dt in hours; multiplying by power (W) yields Wh directly.
        dt_h = elapsed_s / 3600.0

        # Battery: signed -> split into in/out by sign.
        if battery_power > 0:
            self._wh["battery_energy_in_wh"] += battery_power * dt_h
        elif battery_power < 0:
            self._wh["battery_energy_out_wh"] += -battery_power * dt_h

        # PV & load are always >= 0 per spec, but guard against bad data.
        if pv_power > 0:
            self._wh["pv_energy_wh"] += pv_power * dt_h
        if load_power > 0:
            self._wh["load_energy_wh"] += load_power * dt_h

        # Grid: signed -> split into in/out by sign. Offgrid both stay 0.
        if grid_power > 0:
            self._wh["grid_energy_in_wh"] += grid_power * dt_h
        elif grid_power < 0:
            self._wh["grid_energy_out_wh"] += -grid_power * dt_h

        return self._snapshot_kwh()

    def _snapshot_kwh(self) -> dict[str, float]:
        """Return the 6 accumulators in kWh, rounded to 3 decimals."""
        return {
            _WH_TO_KWH_KEY[wh_key]: round(self._wh[wh_key] / 1000.0, 3)
            for wh_key in _WH_KEYS
        }

    # --- persistence ----------------------------------------------------

    def save(self) -> None:
        """Persist current accumulators to persist_path (JSON). No-op if None.

        Writes atomically: tmp file in the same directory, fsync, rename.
        Never raises — persistence failures are logged but don't take the
        daemon down (the in-memory counters keep working).
        """
        if self.persist_path is None:
            return

        payload: dict[str, object] = {"version": _PERSIST_VERSION}
        payload.update(self._wh)

        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            # NamedTemporaryFile in the same dir guarantees the rename is atomic
            # on POSIX (same filesystem).
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.persist_path.parent),
                prefix=self.persist_path.name + ".",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, self.persist_path)
        except OSError:
            log.exception(
                "energy_integrator: failed to persist accumulators to %s",
                self.persist_path,
            )

    def load(self) -> None:
        """Restore accumulators from persist_path if it exists.

        Tolerant of:
          - missing file: no-op.
          - malformed JSON: log error, start from zeros.
          - version mismatch: log warning, start from zeros.
          - missing keys in the payload: those default to 0.
        """
        if self.persist_path is None or not self.persist_path.exists():
            return

        try:
            raw = self.persist_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            log.exception(
                "energy_integrator: persist file %s is unreadable/corrupt; "
                "starting accumulators at zero",
                self.persist_path,
            )
            self._wh = {k: 0.0 for k in _WH_KEYS}
            return

        if not isinstance(data, dict):
            log.warning(
                "energy_integrator: persist file %s does not contain a JSON object; "
                "starting at zero",
                self.persist_path,
            )
            self._wh = {k: 0.0 for k in _WH_KEYS}
            return

        version = data.get("version")
        if version != _PERSIST_VERSION:
            log.warning(
                "energy_integrator: persist file %s has version=%r, expected %d; "
                "starting at zero",
                self.persist_path,
                version,
                _PERSIST_VERSION,
            )
            self._wh = {k: 0.0 for k in _WH_KEYS}
            return

        restored: dict[str, float] = {}
        for key in _WH_KEYS:
            value = data.get(key, 0.0)
            if isinstance(value, bool) or not isinstance(value, int | float):
                log.warning(
                    "energy_integrator: persist key %s has non-numeric value %r; "
                    "defaulting to 0",
                    key,
                    value,
                )
                restored[key] = 0.0
                continue
            f = float(value)
            if not math.isfinite(f) or f < 0:
                log.warning(
                    "energy_integrator: persist key %s has invalid value %r; "
                    "defaulting to 0",
                    key,
                    value,
                )
                restored[key] = 0.0
                continue
            restored[key] = f
        self._wh = restored

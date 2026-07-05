"""Tests for inverter_bridge.bms.aggregator — bank-level math."""
from __future__ import annotations

import pytest

from inverter_bridge.bms.aggregator import aggregate_bank
from inverter_bridge.bms.octopus_protocol import PiaData, PibData


def _pia(
    pack: int,
    voltage: float = 52.5,
    current: float = -10.0,
    remaining: float = 200.0,
    nominal: float = 280.0,
    soc: float = 70.0,
    soh: float = 99.0,
    cycles: int = 50,
) -> PiaData:
    return PiaData(
        pack=pack,
        voltage_V=voltage,
        current_A=current,
        remaining_Ah=remaining,
        nominal_Ah=nominal,
        soc_pct=soc,
        soh_pct=soh,
        cycles=cycles,
    )


def _pib(
    pack: int,
    cells_mV: list[int] | None = None,
    cell_temps_C: tuple[float | None, ...] = (25.0, 25.5, 25.2, 25.3),
    env_C: float | None = 24.5,
    pcb_C: float | None = 28.0,
) -> PibData:
    if cells_mV is None:
        cells_mV = [3275] * 16
    cell_v_min = min(cells_mV)
    cell_v_max = max(cells_mV)
    cell_v_avg = sum(cells_mV) // 16
    cell_v_delta = cell_v_max - cell_v_min
    return PibData(
        pack=pack,
        cell_v_mV=tuple(cells_mV),
        cell_v_min_mV=cell_v_min,
        cell_v_max_mV=cell_v_max,
        cell_v_avg_mV=cell_v_avg,
        cell_v_delta_mV=cell_v_delta,
        cell_temp_C=cell_temps_C,
        env_temp_C=env_C,
        pcb_temp_C=pcb_C,
    )


# ─── Happy path: 4 packs balanced ─────────────────────────────────


def test_full_bank_balanced_discharging():
    """4 packs balanceados descargando — totales coinciden con expectativa manual."""
    pia = {
        1: _pia(pack=1, voltage=52.40, current=-8.00, remaining=180.0, nominal=280.0, soc=64.3, soh=99.2, cycles=47),
        2: _pia(pack=2, voltage=52.42, current=-8.05, remaining=181.0, nominal=280.0, soc=64.6, soh=99.3, cycles=48),
        3: _pia(pack=3, voltage=52.38, current=-7.95, remaining=176.0, nominal=274.4, soc=64.1, soh=99.1, cycles=49),
        4: _pia(pack=4, voltage=52.45, current=-8.10, remaining=182.0, nominal=280.0, soc=64.7, soh=99.4, cycles=46),
    }
    pib = {
        1: _pib(pack=1, cell_temps_C=(25.0, 25.5, 25.2, 25.3), env_C=24.5, pcb_C=28.0),
        2: _pib(pack=2, cell_temps_C=(25.1, 25.4, 25.3, 25.2), env_C=24.6, pcb_C=28.1),
        3: _pib(pack=3, cell_temps_C=(25.5, 26.0, 25.8, 25.7), env_C=24.7, pcb_C=28.5),  # más caliente
        4: _pib(pack=4, cell_temps_C=(25.0, 25.3, 25.1, 25.0), env_C=24.4, pcb_C=28.0),
    }
    agg = aggregate_bank(pia, pib)

    # Voltage_avg
    assert agg.voltage_avg_V == pytest.approx((52.40 + 52.42 + 52.38 + 52.45) / 4)
    # Current_total
    assert agg.current_total_A == pytest.approx(-8.00 - 8.05 - 7.95 - 8.10)
    # Power = V_avg * I_total (NO sum(V_i * I_i))
    assert agg.power_W == pytest.approx(agg.voltage_avg_V * agg.current_total_A)
    # Descargando → discharging positive, charging zero
    assert agg.power_charging_W == 0.0
    assert agg.power_discharging_W == -agg.power_W
    # SoC avg
    assert agg.soc_avg_pct == pytest.approx((64.3 + 64.6 + 64.1 + 64.7) / 4)
    assert agg.soc_spread_pct == pytest.approx(64.7 - 64.1)
    # SoC min — el pack más bajo manda para autonomía/energía (no el promedio,
    # que se infla con packs apagados/desbalanceados).
    assert agg.soc_min_pct == pytest.approx(64.1)
    # Remaining / nominal sumados
    assert agg.remaining_Ah == pytest.approx(180.0 + 181.0 + 176.0 + 182.0)
    assert agg.nominal_Ah == pytest.approx(280.0 + 280.0 + 274.4 + 280.0)
    # Min SoH
    assert agg.min_soh_pct == pytest.approx(99.1)
    # Max cycles
    assert agg.max_cycles == 49
    # Max cell temp → pack 3 con 26.0
    assert agg.max_cell_temp_C == pytest.approx(26.0)


def test_full_bank_charging():
    """Carga: current positivo → charging>0, discharging=0."""
    pia = {p: _pia(pack=p, voltage=53.0, current=+10.0, soc=80.0) for p in (1, 2, 3, 4)}
    agg = aggregate_bank(pia, pib_by_pack=None)
    assert agg.power_W > 0
    assert agg.power_charging_W == agg.power_W
    assert agg.power_discharging_W == 0.0


def test_zero_current_idle():
    pia = {p: _pia(pack=p, voltage=52.5, current=0.0, soc=70.0) for p in (1, 2, 3, 4)}
    agg = aggregate_bank(pia, pib_by_pack=None)
    assert agg.power_W == 0.0
    assert agg.power_charging_W == 0.0
    assert agg.power_discharging_W == 0.0


# ─── Edge cases ──────────────────────────────────────────────────


def test_empty_bank():
    """Sin packs → todo None."""
    agg = aggregate_bank({}, {})
    assert agg.voltage_avg_V is None
    assert agg.current_total_A is None
    assert agg.soc_avg_pct is None
    assert agg.soc_min_pct is None
    assert agg.soc_spread_pct is None
    assert agg.power_W is None
    assert agg.remaining_Ah is None
    assert agg.nominal_Ah is None
    assert agg.min_soh_pct is None
    assert agg.max_cycles is None
    assert agg.max_cell_temp_C is None
    assert agg.power_charging_W is None
    assert agg.power_discharging_W is None


def test_single_pack_only():
    """Un solo pack — voltage_avg = ese pack, current_total = ese pack."""
    pia = {1: _pia(pack=1, voltage=52.4, current=-5.0)}
    agg = aggregate_bank(pia, {})
    assert agg.voltage_avg_V == pytest.approx(52.4)
    assert agg.current_total_A == pytest.approx(-5.0)
    assert agg.power_W == pytest.approx(52.4 * -5.0)
    assert agg.soc_spread_pct == 0.0


def test_pib_none_omits_max_cell_temp():
    """Sin PIB data → max_cell_temp es None, demás se calculan normalmente."""
    pia = {p: _pia(pack=p) for p in (1, 2, 3, 4)}
    agg = aggregate_bank(pia, pib_by_pack=None)
    assert agg.max_cell_temp_C is None
    # PIA-derived sí están
    assert agg.voltage_avg_V is not None
    assert agg.current_total_A is not None


def test_pib_all_temps_sentinel_omits_max_cell_temp():
    """Si todas las cell temps son None (sensor missing), max_cell_temp_C is None."""
    pia = {1: _pia(pack=1)}
    pib = {1: _pib(pack=1, cell_temps_C=(None, None, None, None))}
    agg = aggregate_bank(pia, pib)
    assert agg.max_cell_temp_C is None


def test_pib_mixed_sentinel_uses_valid_temps():
    """Mix de None y valid → max sobre solo los valid."""
    pia = {1: _pia(pack=1), 2: _pia(pack=2)}
    pib = {
        1: _pib(pack=1, cell_temps_C=(25.0, None, None, None)),
        2: _pib(pack=2, cell_temps_C=(None, None, 27.5, None)),
    }
    agg = aggregate_bank(pia, pib)
    assert agg.max_cell_temp_C == pytest.approx(27.5)


def test_min_soh_takes_lowest():
    pia = {
        1: _pia(pack=1, soh=99.5),
        2: _pia(pack=2, soh=98.2),  # peor
        3: _pia(pack=3, soh=99.8),
    }
    agg = aggregate_bank(pia, {})
    assert agg.min_soh_pct == pytest.approx(98.2)


def test_max_cycles_takes_highest():
    pia = {
        1: _pia(pack=1, cycles=10),
        2: _pia(pack=2, cycles=47),
        3: _pia(pack=3, cycles=25),
    }
    agg = aggregate_bank(pia, {})
    assert agg.max_cycles == 47

"""Bank-level aggregates a partir de per-pack PiaData + PibData.

Port directo de los template sensors `bank_*` definidos en
`panel-s3-step5-ui.yaml` líneas 894-937 (ESPHome lambdas C++).

Convención de signo (gotcha #3 HA): positivo = carga, negativo = descarga.
"""
from __future__ import annotations

from dataclasses import dataclass

from .octopus_protocol import PiaData, PibData


@dataclass(frozen=True)
class BankAggregates:
    """10 + 2 (splits) + 4 (per-pack splits) aggregates bank-level del banco BlueSun.

    Todos son `None` cuando ningún pack tiene data suficiente para calcular.
    """
    # PIA-derived
    voltage_avg_V: float | None
    current_total_A: float | None
    soc_avg_pct: float | None
    soc_min_pct: float | None          # min de packs frescos (señal honesta p/ autonomía)
    soc_spread_pct: float | None
    power_W: float | None              # voltage_avg * current_total
    remaining_Ah: float | None         # sum
    nominal_Ah: float | None           # sum
    min_soh_pct: float | None
    max_cycles: int | None

    # PIB-derived
    max_cell_temp_C: float | None      # max sobre 16 cell temps (4 cells * 4 packs)

    # Split power (derivados de power_W, never None si power_W no es None)
    power_charging_W: float | None     # max(power_W, 0)
    power_discharging_W: float | None  # max(-power_W, 0)

    # Split per-pack current/power. Útiles cuando hay balanceo interno
    # (un pack carga mientras otro descarga → net engaña). Calculados sobre
    # I_pack individual con signo.
    charge_current_total_A: float | None     # Σ I_pack donde I>0
    discharge_current_total_A: float | None  # Σ |I_pack| donde I<0
    charge_power_total_W: float | None       # Σ V_pack * I_pack donde I>0
    discharge_power_total_W: float | None    # Σ V_pack * abs(I_pack) donde I<0


def _avg_or_none(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _sum_or_none(values: list[float]) -> float | None:
    return sum(values) if values else None


def _max_or_none(values: list[float]) -> float | None:
    return max(values) if values else None


def _min_or_none(values: list[float]) -> float | None:
    return min(values) if values else None


def aggregate_bank(
    pia_by_pack: dict[int, PiaData],
    pib_by_pack: dict[int, PibData] | None = None,
) -> BankAggregates:
    """Calcula los 12 aggregates bank-level a partir de per-pack data.

    Args:
        pia_by_pack: dict pack_num (1..4) → PiaData. Packs sin data se omiten.
        pib_by_pack: dict pack_num (1..4) → PibData. Opcional; sin él,
            `max_cell_temp_C` queda en None.

    Returns:
        BankAggregates — campos en `None` cuando ningún pack contribuye.
    """
    if pib_by_pack is None:
        pib_by_pack = {}

    # PIA-derived ─────────────────────────────────────────────────
    voltages = [p.voltage_V for p in pia_by_pack.values()]
    currents = [p.current_A for p in pia_by_pack.values()]
    socs = [p.soc_pct for p in pia_by_pack.values()]
    remainings = [p.remaining_Ah for p in pia_by_pack.values()]
    nominals = [p.nominal_Ah for p in pia_by_pack.values()]
    sohs = [p.soh_pct for p in pia_by_pack.values()]
    cycles = [p.cycles for p in pia_by_pack.values()]

    voltage_avg = _avg_or_none(voltages)
    current_total = _sum_or_none(currents)
    soc_avg = _avg_or_none(socs)
    soc_min = _min_or_none(socs)
    soc_spread = (max(socs) - min(socs)) if socs else None
    remaining = _sum_or_none(remainings)
    nominal = _sum_or_none(nominals)
    min_soh = _min_or_none(sohs)
    max_cycles = max(cycles) if cycles else None

    # Power = V_avg * I_total (replica del firmware; NO es sum(V_i * I_i))
    power = (
        voltage_avg * current_total
        if voltage_avg is not None and current_total is not None
        else None
    )

    # Splits charging/discharging (signo: +carga / -descarga)
    if power is None:
        power_charging = None
        power_discharging = None
    else:
        power_charging = power if power > 0 else 0.0
        power_discharging = -power if power < 0 else 0.0

    # Per-pack splits (más finos que el net: revelan balanceo interno)
    pack_pairs = [(p.voltage_V, p.current_A) for p in pia_by_pack.values()]
    if pack_pairs:
        charge_currents = [cur for _, cur in pack_pairs if cur > 0]
        discharge_currents = [-cur for _, cur in pack_pairs if cur < 0]
        charge_powers = [volt * cur for volt, cur in pack_pairs if cur > 0]
        discharge_powers = [volt * -cur for volt, cur in pack_pairs if cur < 0]
        charge_current_total = sum(charge_currents) if charge_currents else 0.0
        discharge_current_total = sum(discharge_currents) if discharge_currents else 0.0
        charge_power_total = sum(charge_powers) if charge_powers else 0.0
        discharge_power_total = sum(discharge_powers) if discharge_powers else 0.0
    else:
        charge_current_total = None
        discharge_current_total = None
        charge_power_total = None
        discharge_power_total = None

    # PIB-derived ─────────────────────────────────────────────────
    cell_temps = []
    for pib in pib_by_pack.values():
        for t in pib.cell_temp_C:
            if t is not None:  # sentinel-filtered already
                cell_temps.append(t)
    max_cell_temp = _max_or_none(cell_temps)

    return BankAggregates(
        voltage_avg_V=voltage_avg,
        current_total_A=current_total,
        soc_avg_pct=soc_avg,
        soc_min_pct=soc_min,
        soc_spread_pct=soc_spread,
        power_W=power,
        remaining_Ah=remaining,
        nominal_Ah=nominal,
        min_soh_pct=min_soh,
        max_cycles=max_cycles,
        max_cell_temp_C=max_cell_temp,
        power_charging_W=power_charging,
        power_discharging_W=power_discharging,
        charge_current_total_A=charge_current_total,
        discharge_current_total_A=discharge_current_total,
        charge_power_total_W=charge_power_total,
        discharge_power_total_W=discharge_power_total,
    )

"""Tests for inverter_bridge.bms.octopus_protocol — frames + CRC + decoders.

Fixtures sintéticos: construimos respuestas Modbus FC=0x04 con CRC válido
desde Python para validar el path completo encode → decode. Los valores raw
se eligen de modo que produzcan los reales del banco GADI (referencia: dump
del XZH-ElecTech BMS16S200A en logs/octopus_2026-05-15/seplos_register_map.txt).
"""
from __future__ import annotations

import struct

import pytest

from inverter_bridge.bms.octopus_protocol import (
    PIA_BYTE_COUNT,
    PIA_FRAMES_LITERAL,
    PIB_FRAMES_LITERAL,
    TEMP_K10_NA_SENTINEL,
    DecodeError,
    PiaData,
    PibData,
    build_request,
    crc16_modbus,
    parse_response,
    pia_request,
    pib_request,
    verify_crc,
)

# ─── CRC16-MODBUS ────────────────────────────────────────────────


def test_crc16_known_vectors():
    """Vectores conocidos de CRC-16/MODBUS."""
    # Empty
    assert crc16_modbus(b"") == 0xFFFF
    # Single byte
    assert crc16_modbus(b"\x00") == 0x40BF
    # PIA pack 1 body: 0x01 0x04 0x10 0x00 0x00 0x12 → CRC esperado 0xC774 (little-endian: 74 C7)
    assert crc16_modbus(bytes([0x01, 0x04, 0x10, 0x00, 0x00, 0x12])) == 0xC774


def test_crc16_matches_pia_literals():
    """Cada frame PIA tiene CRC válido por construcción."""
    for pack, frame in PIA_FRAMES_LITERAL.items():
        assert verify_crc(frame), f"PIA pack {pack} CRC inválido"


def test_crc16_matches_pib_literals():
    for pack, frame in PIB_FRAMES_LITERAL.items():
        assert verify_crc(frame), f"PIB pack {pack} CRC inválido"


def test_verify_crc_rejects_too_short():
    assert verify_crc(b"") is False
    assert verify_crc(b"\x00\x01\x02") is False


def test_verify_crc_rejects_bad_crc():
    bad = bytes([0x01, 0x04, 0x10, 0x00, 0x00, 0x12, 0x00, 0x00])
    assert verify_crc(bad) is False


# ─── Frame builders ──────────────────────────────────────────────


@pytest.mark.parametrize("pack", [1, 2, 3, 4])
def test_pia_request_matches_literal(pack):
    """El builder debe producir bit-perfecto los literales del firmware."""
    assert pia_request(pack) == PIA_FRAMES_LITERAL[pack]


@pytest.mark.parametrize("pack", [1, 2, 3, 4])
def test_pib_request_matches_literal(pack):
    assert pib_request(pack) == PIB_FRAMES_LITERAL[pack]


def test_build_request_arbitrary():
    """Verifica que build_request encaja con CRC válido para parámetros arbitrarios."""
    frame = build_request(slave=0x01, addr=0x1300, count=0x6A, fc=0x04)
    assert frame[:6] == bytes([0x01, 0x04, 0x13, 0x00, 0x00, 0x6A])
    assert verify_crc(frame)


# ─── Helpers para fixtures de respuesta ──────────────────────────


def _make_response(slave: int, regs: list[int]) -> bytes:
    """Construye un response Modbus FC=0x04 con CRC válido a partir de regs (u16, big-endian)."""
    payload = b"".join(struct.pack(">H", r & 0xFFFF) for r in regs)
    body = bytes([slave, 0x04, len(payload)]) + payload
    crc = crc16_modbus(body)
    return body + struct.pack("<H", crc)


# ─── PIA decoder ─────────────────────────────────────────────────


def test_pia_decode_realistic_pack1():
    """Decodifica un response PIA con valores realistas del banco GADI."""
    # Valores que esperaríamos ver post-decode:
    #   voltage = 52.45 V  → raw 5245
    #   current = -7.83 A  → raw signed -783 → unsigned 65535-783+1 = 64753 = 0xFCF1
    #   remaining_ah = 184.5 → raw 18450
    #   nominal_ah = 280.0   → raw 28000
    #   reg[4] = ignored
    #   soc = 65.9 %         → raw 659
    #   soh = 99.1 %         → raw 991
    #   cycles = 47          → raw 47
    #   reg[8..17] = padding (zeros)
    regs = [5245, 64753, 18450, 28000, 0, 659, 991, 47] + [0] * 10
    frame = _make_response(slave=1, regs=regs)

    result = parse_response(frame)

    assert isinstance(result, PiaData)
    assert result.pack == 1
    assert result.voltage_V == pytest.approx(52.45)
    assert result.current_A == pytest.approx(-7.83)
    assert result.remaining_Ah == pytest.approx(184.5)
    assert result.nominal_Ah == pytest.approx(280.0)
    assert result.soc_pct == pytest.approx(65.9)
    assert result.soh_pct == pytest.approx(99.1)
    assert result.cycles == 47


def test_pia_signed_current_positive():
    """Carga (current positivo) decodifica correctamente."""
    regs = [5300, 1250, 0, 28000, 0, 800, 1000, 0] + [0] * 10  # 12.50 A charging
    frame = _make_response(slave=2, regs=regs)
    result = parse_response(frame)
    assert isinstance(result, PiaData)
    assert result.current_A == pytest.approx(12.50)
    assert result.pack == 2


def test_pia_signed_current_zero():
    regs = [5300, 0, 0, 28000, 0, 800, 1000, 0] + [0] * 10
    frame = _make_response(slave=3, regs=regs)
    result = parse_response(frame)
    assert isinstance(result, PiaData)
    assert result.current_A == 0.0


# ─── PIB decoder ─────────────────────────────────────────────────


def _make_pib_response(
    slave: int,
    cells_mV: list[int],
    cell_temps_K10: list[int],
    env_K10: int,
    pcb_K10: int,
) -> bytes:
    """Construye un response PIB realista con 16 cells + 4 cell temps + env + pcb."""
    # 26 regs total: 16 cells + tail_start=32 byte offset (16 regs) = 26 regs total
    # Tail layout: [4 cell_temps (8 bytes)] [16 bytes spare] [env_temp (2)] [pcb_temp (2)] = 28 bytes
    # Wait — payload is 52 bytes = 26 regs. cells use 16 regs (32 bytes). Tail has 10 regs (20 bytes).
    # Within tail (offset 32 in payload):
    #   tail+0..7: 4 cell temps (8 bytes = 4 regs)
    #   tail+8..15: 4 regs spare
    #   tail+16..17: env temp (1 reg)
    #   tail+18..19: pcb temp (1 reg)
    assert len(cells_mV) == 16, "necesito 16 cell voltages"
    assert len(cell_temps_K10) == 4, "necesito 4 cell temps"

    regs = list(cells_mV)
    regs += list(cell_temps_K10)  # 4 regs
    regs += [0, 0, 0, 0]           # 4 spare regs
    regs += [env_K10]              # 1 reg
    regs += [pcb_K10]              # 1 reg

    assert len(regs) == 26, f"PIB necesita 26 regs, tengo {len(regs)}"
    return _make_response(slave=slave, regs=regs)


def test_pib_decode_realistic():
    """Decodifica PIB con 16 celdas + temps realistas."""
    cells = [3275] * 16  # 3.275 V cada celda, banco perfectamente balanceado
    cells[5] = 3270      # un outlier ligeramente bajo
    cells[10] = 3280     # un outlier ligeramente alto
    # Cell temps en K*10: 298.5K → 25.35°C
    cell_temps = [2985, 2987, 2986, 2984]
    env = 2980  # 24.85°C
    pcb = 3020  # 28.85°C

    frame = _make_pib_response(slave=1, cells_mV=cells, cell_temps_K10=cell_temps, env_K10=env, pcb_K10=pcb)
    result = parse_response(frame)

    assert isinstance(result, PibData)
    assert result.pack == 1
    assert result.cell_v_mV == tuple(cells)
    assert result.cell_v_min_mV == 3270
    assert result.cell_v_max_mV == 3280
    assert result.cell_v_avg_mV == 3275  # (3275*14 + 3270 + 3280) // 16 = 52400 // 16 = 3275
    assert result.cell_v_delta_mV == 10
    assert result.cell_temp_C == pytest.approx((25.35, 25.55, 25.45, 25.25))
    assert result.env_temp_C == pytest.approx(24.85)
    assert result.pcb_temp_C == pytest.approx(28.85)


def test_pib_sentinel_temperature_becomes_none():
    """Sensor missing (raw 0x0AAB) → None en lugar de 0°C."""
    cells = [3275] * 16
    # Cell temps: 3 reales + 1 sentinel
    cell_temps = [2985, TEMP_K10_NA_SENTINEL, 2986, 2984]
    env = TEMP_K10_NA_SENTINEL  # env también missing
    pcb = 3020

    frame = _make_pib_response(slave=2, cells_mV=cells, cell_temps_K10=cell_temps, env_K10=env, pcb_K10=pcb)
    result = parse_response(frame)

    assert isinstance(result, PibData)
    assert result.cell_temp_C[1] is None
    assert result.cell_temp_C[0] is not None
    assert result.env_temp_C is None
    assert result.pcb_temp_C is not None


# ─── Error paths ─────────────────────────────────────────────────


def test_parse_too_short_raises():
    with pytest.raises(DecodeError, match="too short"):
        parse_response(b"\x01\x04")


def test_parse_bad_crc_raises():
    regs = [5300, 0, 0, 28000, 0, 800, 1000, 0] + [0] * 10
    frame = _make_response(slave=1, regs=regs)
    # Corrompe el CRC: flip último byte
    bad = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(DecodeError, match="CRC"):
        parse_response(bad)


def test_parse_wrong_fc_raises():
    body = bytes([0x01, 0x03, 0x24]) + bytes(36)  # FC=0x03 instead of 0x04
    crc = crc16_modbus(body)
    frame = body + struct.pack("<H", crc)
    with pytest.raises(DecodeError, match="function code"):
        parse_response(frame)


def test_parse_invalid_slave_raises():
    regs = [5300, 0, 0, 28000, 0, 800, 1000, 0] + [0] * 10
    frame = _make_response(slave=99, regs=regs)
    with pytest.raises(DecodeError, match="slave address"):
        parse_response(frame)


def test_parse_unknown_byte_count_raises():
    """byte_count que no es PIA (0x24) ni PIB (0x34) debería fallar."""
    body = bytes([0x01, 0x04, 0x10]) + bytes(16)  # byte_count=0x10 unknown
    crc = crc16_modbus(body)
    frame = body + struct.pack("<H", crc)
    with pytest.raises(DecodeError, match="unknown byte_count"):
        parse_response(frame)


def test_parse_truncated_pia():
    """Frame con header válido pero payload corto."""
    # Anunciamos byte_count=PIA pero solo damos 10 bytes de payload
    body = bytes([0x01, 0x04, PIA_BYTE_COUNT]) + bytes(10)
    crc = crc16_modbus(body)
    frame = body + struct.pack("<H", crc)
    with pytest.raises(DecodeError, match="truncated"):
        parse_response(frame)

"""Octopus / Seplos Modbus protocol over BLE GATT — frame encoding + decoders.

Port directo del lambda C++ en `panel-s3-step5-ui.yaml` líneas 720-820. Las
constantes (CRC, byte counts, register offsets, sentinels, scaling factors)
se mantienen idénticas para preservar el comportamiento end-to-end.

Frame structure (Modbus RTU sobre BLE GATT):

    Request (8 bytes):
        [slave, fc=0x04, addr_hi, addr_lo, count_hi, count_lo, crc_lo, crc_hi]

    Response (variable):
        [slave, fc=0x04, byte_count, data..., crc_lo, crc_hi]
        byte_count = 2 * N donde N = número de registros

Comandos usados:
  - PIA (cmd 0x10 / block 0x1000): 18 registers — voltage, current, SoC, SoH,
    cycles, remaining_Ah, nominal_Ah, etc. Por pack.
  - PIB (cmd 0x11 / block 0x1100): 26 registers — 16 cell voltages + 4 cell
    temps + 2 env/PCB temps + spares. Por pack.

Service UUID: 0xFFF0
  - Char FFF1: notify (responses)
  - Char FFF2: write (requests)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# --- Constantes BLE ---
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

# --- Frames pre-computados (extraídos del YAML, validados con CRC16-MODBUS) ---
# Aunque podríamos calcularlos en runtime con `build_request()`, los embebidos
# del firmware viven acá literalmente como referencia y double-check.
PIA_FRAMES_LITERAL: dict[int, bytes] = {
    1: bytes([0x01, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0xC7]),
    2: bytes([0x02, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0xF4]),
    3: bytes([0x03, 0x04, 0x10, 0x00, 0x00, 0x12, 0x75, 0x25]),
    4: bytes([0x04, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0x92]),
}

PIB_FRAMES_LITERAL: dict[int, bytes] = {
    1: bytes([0x01, 0x04, 0x11, 0x00, 0x00, 0x1A, 0x74, 0xFD]),
    2: bytes([0x02, 0x04, 0x11, 0x00, 0x00, 0x1A, 0x74, 0xCE]),
    3: bytes([0x03, 0x04, 0x11, 0x00, 0x00, 0x1A, 0x75, 0x1F]),
    4: bytes([0x04, 0x04, 0x11, 0x00, 0x00, 0x1A, 0x74, 0xA8]),
}

# Modbus FC=0x04 byte_count fields esperados
PIA_BYTE_COUNT = 0x24  # 36 bytes = 18 registers * 2
PIB_BYTE_COUNT = 0x34  # 52 bytes = 26 registers * 2

# Sentinel "value not available" para temperaturas K*10
# (0x0AAB = 2731 → 273.1 K → ~0 °C, usado por el BMS para indicar sensor missing)
TEMP_K10_NA_SENTINEL = 0x0AAB


# --- CRC16-MODBUS ---
def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS — polinomio 0xA001, initial 0xFFFF, LSB-first.

    Bit-idéntica al lambda C++ del YAML.
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def verify_crc(frame: bytes) -> bool:
    """Verifica CRC del último word del frame."""
    if len(frame) < 4:
        return False
    payload = frame[:-2]
    received = struct.unpack("<H", frame[-2:])[0]  # little-endian per Modbus
    return crc16_modbus(payload) == received


def build_request(slave: int, addr: int, count: int, fc: int = 0x04) -> bytes:
    """Construye un frame Modbus RTU request con CRC.

    Args:
        slave: slave address (1..4 para los packs BlueSun)
        addr: register address (0x1000 = PIA, 0x1100 = PIB)
        count: register count (0x12 = 18 para PIA, 0x1A = 26 para PIB)
        fc: function code (0x04 = read input registers)

    Returns:
        bytes — 8 bytes con CRC al final.
    """
    body = struct.pack(">BBHH", slave, fc, addr, count)
    crc = crc16_modbus(body)
    return body + struct.pack("<H", crc)


def pia_request(pack: int) -> bytes:
    return build_request(slave=pack, addr=0x1000, count=0x0012)


def pib_request(pack: int) -> bytes:
    return build_request(slave=pack, addr=0x1100, count=0x001A)


# --- Resultado parsings tipados ---
@dataclass(frozen=True)
class PiaData:
    """Decoded PIA (cmd 0x10) response per pack."""
    pack: int                  # 1..4
    voltage_V: float           # raw[0] / 100
    current_A: float           # signed raw[1] / 100  (positive = charging convention)
    remaining_Ah: float        # raw[2] / 100
    nominal_Ah: float          # raw[3] / 100
    soc_pct: float             # raw[5] / 10
    soh_pct: float             # raw[6] / 10
    cycles: int                # raw[7]
    raw_response: bytes = b""  # debug/forensics


@dataclass(frozen=True)
class PibData:
    """Decoded PIB (cmd 0x11) response per pack."""
    pack: int                       # 1..4
    cell_v_mV: tuple[int, ...]      # 16 cells, mV
    cell_v_min_mV: int
    cell_v_max_mV: int
    cell_v_avg_mV: int
    cell_v_delta_mV: int
    cell_temp_C: tuple[float | None, ...]  # 4 temps, °C (None if sentinel)
    env_temp_C: float | None
    pcb_temp_C: float | None
    raw_response: bytes = b""


# --- Decoders ---
def _k10_to_c(raw: int) -> float | None:
    """K*10 (raw uint16) → °C, con sentinel 0x0AAB → None."""
    if raw == TEMP_K10_NA_SENTINEL:
        return None
    return raw / 10.0 - 273.15


class DecodeError(Exception):
    pass


def parse_response(frame: bytes) -> PiaData | PibData:
    """Parse a Modbus FC=0x04 response (PIA o PIB) → dataclass tipado.

    Espera frame completo: [slave, fc, byte_count, data..., crc_lo, crc_hi].
    Distingue PIA vs PIB por el `byte_count`.

    Raises:
        DecodeError: si CRC inválido, byte_count desconocido, o longitud insuficiente.
    """
    if len(frame) < 5:
        raise DecodeError(f"frame too short: {len(frame)} bytes (expected ≥5)")

    if not verify_crc(frame):
        raise DecodeError("CRC16 mismatch")

    slave = frame[0]
    fc = frame[1]
    byte_count = frame[2]

    if fc != 0x04:
        raise DecodeError(f"unexpected function code 0x{fc:02X} (expected 0x04)")

    if slave < 1 or slave > 4:
        raise DecodeError(f"unexpected slave address {slave} (expected 1..4)")

    expected_total = 3 + byte_count + 2  # header + payload + crc
    if len(frame) < expected_total:
        raise DecodeError(
            f"frame truncated: have {len(frame)} bytes, need {expected_total} "
            f"(byte_count={byte_count})"
        )

    payload = frame[3 : 3 + byte_count]

    if byte_count == PIA_BYTE_COUNT:
        return _parse_pia(slave, payload, frame)
    elif byte_count == PIB_BYTE_COUNT:
        return _parse_pib(slave, payload, frame)
    else:
        raise DecodeError(
            f"unknown byte_count 0x{byte_count:02X} ({byte_count}); "
            f"expected 0x24 (PIA) or 0x34 (PIB)"
        )


def _parse_pia(slave: int, payload: bytes, full_frame: bytes) -> PiaData:
    """Decode PIA payload (36 bytes = 18 registers).

    Layout (replica del lambda C++ del YAML):
      reg[0]: voltage_V (u16, /100)
      reg[1]: current_A (s16, /100, signed)
      reg[2]: remaining_Ah (u16, /100)
      reg[3]: nominal_Ah (u16, /100)
      reg[4]: (skipped — no se publica en el YAML actual)
      reg[5]: SoC% (u16, /10)
      reg[6]: SoH% (u16, /10)
      reg[7]: cycles (u16)
      reg[8..17]: (skipped — registros adicionales no publicados)
    """
    if len(payload) < 36:
        raise DecodeError(f"PIA payload too short: {len(payload)} bytes (need 36)")

    # Big-endian: Modbus registers son MSB first
    regs_u = struct.unpack(">18H", payload[:36])
    # Para current necesitamos signed
    current_signed = struct.unpack(">h", payload[2:4])[0]

    return PiaData(
        pack=slave,
        voltage_V=regs_u[0] / 100.0,
        current_A=current_signed / 100.0,
        remaining_Ah=regs_u[2] / 100.0,
        nominal_Ah=regs_u[3] / 100.0,
        soc_pct=regs_u[5] / 10.0,
        soh_pct=regs_u[6] / 10.0,
        cycles=regs_u[7],
        raw_response=full_frame,
    )


def _parse_pib(slave: int, payload: bytes, full_frame: bytes) -> PibData:
    """Decode PIB payload (52 bytes = 26 registers).

    Layout:
      regs[0..15]: 16 cell voltages (u16, mV)
      regs[16..]: temps (K*10) + spares. El YAML detecta el tail_start
        empíricamente según body_len (52 → tail_start=32, 54 → tail_start=34).
        Para PIB con 52 bytes payload (cuerpo 54 incluyendo header sin CRC),
        tail_start=32 según condición `body_len==54 ? 34 : 32` del firmware.
        Donde body_len = response_total - 3 - 2 = 52 + 3 + 2 - 3 - 2 = 52 →
        tail_start = 32 (offset en bytes desde inicio de payload).

      Tail (offsets dentro del payload):
        +0..+7: 4 cell temps (K*10, u16 cada uno = 8 bytes)
        +16..+17: env temp (K*10)
        +18..+19: pcb temp (K*10)
    """
    if len(payload) < 52:
        raise DecodeError(f"PIB payload too short: {len(payload)} bytes (need 52)")

    # 16 cell voltages
    cell_v = struct.unpack(">16H", payload[:32])
    cell_min = min(cell_v)
    cell_max = max(cell_v)
    cell_sum = sum(cell_v)
    cell_avg = cell_sum // 16
    cell_delta = cell_max - cell_min

    # Tail: temps. Body_len = len(payload) + 2 (CRC). Si == 54 → tail_start=34, else 32.
    # Para PIB con byte_count=52, body_len=54 → tail_start=34 wait, no.
    # El YAML define `body_len = x.size() - 3 - 2`. x es el frame completo;
    # x.size() para PIB = 3 (header) + 52 (payload) + 2 (CRC) = 57.
    # body_len = 57 - 3 - 2 = 52. Como 52 ≠ 54, tail_start = 32.
    body_len_yaml_equivalent = 3 + len(payload) - 3 - 2  # = len(payload) - 2
    tail_start = 34 if body_len_yaml_equivalent == 54 else 32

    # Validar que tenemos al menos hasta tail_start+18+2 bytes
    if len(payload) < tail_start + 20:
        raise DecodeError(
            f"PIB payload too short for temp tail: {len(payload)} bytes "
            f"(need {tail_start + 20} at tail_start={tail_start})"
        )

    cell_temps_raw = struct.unpack(">4H", payload[tail_start : tail_start + 8])
    env_temp_raw = struct.unpack(">H", payload[tail_start + 16 : tail_start + 18])[0]
    pcb_temp_raw = struct.unpack(">H", payload[tail_start + 18 : tail_start + 20])[0]

    cell_temps_C = tuple(_k10_to_c(t) for t in cell_temps_raw)

    return PibData(
        pack=slave,
        cell_v_mV=cell_v,
        cell_v_min_mV=cell_min,
        cell_v_max_mV=cell_max,
        cell_v_avg_mV=cell_avg,
        cell_v_delta_mV=cell_delta,
        cell_temp_C=cell_temps_C,
        env_temp_C=_k10_to_c(env_temp_raw),
        pcb_temp_C=_k10_to_c(pcb_temp_raw),
        raw_response=full_frame,
    )


# --- Frame builders sanity check ---
def _self_test() -> None:
    """Verifica que los frames calculados coinciden con los literales del YAML."""
    for pack, literal in PIA_FRAMES_LITERAL.items():
        calc = pia_request(pack)
        assert calc == literal, f"PIA pack {pack}: calc={calc.hex()} vs literal={literal.hex()}"
    for pack, literal in PIB_FRAMES_LITERAL.items():
        calc = pib_request(pack)
        assert calc == literal, f"PIB pack {pack}: calc={calc.hex()} vs literal={literal.hex()}"


if __name__ == "__main__":
    _self_test()
    print("octopus_protocol self-test OK")
    print("PIA pack 1:", pia_request(1).hex())
    print("PIB pack 1:", pib_request(1).hex())

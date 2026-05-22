"""Python + bleak BLE probe contra el Pack01 master del banco BlueSun.

Objetivo: validar que **Python sobre BlueZ** (vs el firmware ESPHome del Panel S3)
puede ejecutar el ciclo completo Octopus / Seplos:
  1. Conectar a Pack01 (MAC `C0:D6:3C:52:0F:0D`).
  2. Descubrir servicios + char FFF1 (notify) + FFF2 (write).
  3. Subscribe a FFF1.
  4. Para cada pack (1..4): escribir frame PIA (FC=0x04 read 18 input regs @0x1000)
     y leer notificaciones; decodificar V/I/SoC para sanity check.
  5. Imprimir resultados.
  6. Desconectar limpio.

Antes de correr este probe **debe liberarse el BLE del Panel S3** (el Pack01 acepta
una sola conexión BLE simultánea). El probe asume que el caller ya tiene el
kill-switch HA `switch.panel_cuartoelectrico_panel_bms_enabled` en off, o que el
Panel S3 está físicamente desconectado.

Riesgo a producción: cero. Es un script de lectura pasiva; no escribe nada al BMS
(FC=0x04 read-only). El bus interno del banco no se afecta.

Uso:
    sudo apt install python3-pip   # si no está
    /opt/inverter-bridge/src/.venv/bin/pip install bleak
    /opt/inverter-bridge/src/.venv/bin/python tools/_bms_ble_probe.py

Salida esperada (happy path):
    [Pack 1] V=53.42 A=8.21 SoC=66.5%  Cells=16x  T_max=27.5°C
    [Pack 2] V=53.39 A=8.18 SoC=66.3%  ...
    ...
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.stderr.write(
        "ERROR: bleak no está instalado. Instálalo con:\n"
        "  /opt/inverter-bridge/src/.venv/bin/pip install bleak\n"
    )
    sys.exit(2)

# --- Configuración ---
PACK01_MAC = "C0:D6:3C:52:0F:0D"
PACK01_NAME_HINT = "BN012502180020"
SERVICE_FFF0 = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_FFF1_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_FFF2_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"

# Frames cmd 0x10 (PIA = block 0x1000, 18 input regs) extraídos del firmware ESPHome
# panel-s3-step5-ui.yaml líneas 994-1002. Pack 1..4, slave addr en byte 0, CRC en bytes 6-7.
PIA_FRAMES = {
    1: bytes([0x01, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0xC7]),
    2: bytes([0x02, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0xF4]),
    3: bytes([0x03, 0x04, 0x10, 0x00, 0x00, 0x12, 0x75, 0x25]),
    4: bytes([0x04, 0x04, 0x10, 0x00, 0x00, 0x12, 0x74, 0x92]),
}

# Logging conciso
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bms-probe")


# --- CRC16-MODBUS ---
def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS — polinomio 0xA001, inicial 0xFFFF, LSB-first."""
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
    if len(frame) < 4:
        return False
    payload = frame[:-2]
    received_crc = struct.unpack("<H", frame[-2:])[0]
    return crc16_modbus(payload) == received_crc


# --- Decoder PIA (block 0x1000, 18 registers) ---
#
# El response Modbus FC=0x04 tiene la forma:
#   [slave, fc, byte_count, data..., crc_lo, crc_hi]
# donde byte_count = 2*N (N=18 registers => byte_count=36).
#
# Mapeo de registros para Seplos PIA (per el dump del XZH-ElecTech BMS16S200A):
#   reg[0] = pack voltage (raw / 100 = V)        e.g. 5236 -> 52.36V
#   reg[1] = pack current (raw, signed, /100 = A — convention TBD)
#   reg[2] = remaining capacity Ah*100? (raw 3800 -> 38.00 Ah)
#   reg[3] = nominal capacity ?
#   reg[4] = ? cycle count?
#   reg[5] = SoH%? raw 125 -> 12.5%? no, more like /10 = 12.5? Hmm.
#   reg[6] = SoC % (raw 1000 -> 100.0%)
#   ...
#
# El mapeo exacto está en project_bluesun_seplos_reframe_2026-05-15 memory + Panel S3 YAML.
# Para el probe sólo decodificamos los primeros 7 registros con interpretación
# heurística. Validamos la decodificación contra la realidad confirmada por el Panel S3
# en HA.
def decode_pia(payload: bytes) -> dict:
    """Decode PIA response payload (bytes after byte_count, before CRC)."""
    if len(payload) < 36:
        return {"error": f"payload too short ({len(payload)} bytes, expected 36+)"}
    regs = struct.unpack(">18H", payload[:36])  # big-endian, 18 unsigned 16-bit
    # Interpret current as signed 16-bit
    current_signed = struct.unpack(">18h", payload[:36])[1]
    return {
        "voltage_V": regs[0] / 100.0,
        "current_A": current_signed / 100.0,
        "remaining_Ah": regs[2] / 100.0,
        "nominal_Ah": regs[3] / 100.0,
        "raw_reg4": regs[4],
        "raw_reg5": regs[5],
        "soc_pct": regs[6] / 10.0,
        "raw_regs_7_to_17": regs[7:],
    }


# --- Probe ---
async def probe_pack(client: BleakClient, pack_num: int, timeout_s: float = 3.0) -> dict | None:
    """Send PIA request for pack_num, wait for response notification, decode."""
    response_q: asyncio.Queue = asyncio.Queue()

    def notify_handler(_sender, data):
        # Filter responses for this pack: byte 0 must match slave addr
        if data and data[0] == pack_num and data[1] == 0x04:
            response_q.put_nowait(bytes(data))

    await client.start_notify(CHAR_FFF1_NOTIFY, notify_handler)
    frame = PIA_FRAMES[pack_num]
    log.info("Pack %d: TX %s", pack_num, frame.hex())
    await client.write_gatt_char(CHAR_FFF2_WRITE, frame, response=False)

    try:
        response = await asyncio.wait_for(response_q.get(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await client.stop_notify(CHAR_FFF1_NOTIFY)
        return None

    await client.stop_notify(CHAR_FFF1_NOTIFY)

    log.info("Pack %d: RX %d bytes: %s", pack_num, len(response), response.hex())

    # Validate response structure: slave + fc + byte_count + data + crc(2)
    if len(response) < 5:
        return {"error": f"response too short ({len(response)} bytes)", "raw": response.hex()}
    if not verify_crc(response):
        return {"error": "CRC mismatch", "raw": response.hex()}
    byte_count = response[2]
    payload = response[3 : 3 + byte_count]
    decoded = decode_pia(payload)
    decoded["_raw_hex"] = response.hex()
    return decoded


async def main():
    log.info("Conectando a Pack01 master %s ...", PACK01_MAC)
    async with BleakClient(PACK01_MAC, timeout=15.0) as client:
        log.info("Conectado. MTU=%s", getattr(client, "mtu_size", "?"))

        # List services for confirmation
        services = client.services
        fff0 = services.get_service(SERVICE_FFF0)
        if not fff0:
            log.error("Service 0xFFF0 NO encontrado. Servicios vistos:")
            for s in services:
                log.error("  %s", s.uuid)
            return 1
        log.info("Service 0xFFF0 OK con chars: %s", [c.uuid for c in fff0.characteristics])

        # Probe each pack
        results = {}
        for pack in (1, 2, 3, 4):
            r = await probe_pack(client, pack)
            results[pack] = r
            await asyncio.sleep(0.5)  # rate-limit a la chain RS485 interna

    log.info("=" * 60)
    log.info("RESULTADOS")
    log.info("=" * 60)
    for pack, r in results.items():
        if r is None:
            log.warning("Pack %d: TIMEOUT (sin respuesta)", pack)
        elif "error" in r:
            log.warning("Pack %d: ERROR: %s", pack, r["error"])
        else:
            log.info(
                "Pack %d: V=%.2f A=%+.2f SoC=%.1f%% Rem=%.1fAh Nom=%.1fAh",
                pack, r["voltage_V"], r["current_A"], r["soc_pct"],
                r["remaining_Ah"], r["nominal_Ah"],
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

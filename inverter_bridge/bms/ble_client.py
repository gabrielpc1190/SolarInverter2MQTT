"""Cliente BLE async para el BMS BlueSun (Pack01 master) usando bleak.

Equivalente Python del `ble_client` + `octopus_notify_rx` + scheduler de polling
que vivía en el firmware Panel S3 (panel-s3-step5-ui.yaml). Mantiene la misma
interaccion wire-level (write FFF2 → notify FFF1) pero corre bajo BlueZ Linux
en la OPi gadi-inverter-bridge.

API:
    client = OctopusBleClient(mac="C0:D6:3C:52:0F:0D")
    await client.connect()
    pia = await client.poll_pia(pack=1)      # PiaData
    pib = await client.poll_pib(pack=1)      # PibData
    await client.disconnect()

Manejo de fallos:
    - Disconnect espontaneo durante poll → BleakError; el caller debe
      reconectar antes del siguiente poll.
    - Timeout esperando notify → asyncio.TimeoutError; el caller decide
      reintentar o aceptar pack-missing en este ciclo.
    - CRC mismatch / decode error → DecodeError (de octopus_protocol);
      el caller cuenta y sigue.
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.exc import BleakError

from .octopus_protocol import (
    CHAR_NOTIFY_UUID,
    CHAR_WRITE_UUID,
    PiaData,
    PibData,
    parse_response,
    pia_request,
    pib_request,
)

log = logging.getLogger(__name__)

# Tiempos por defecto (sobreescribibles via constructor)
_DEFAULT_CONNECT_TIMEOUT_S = 15.0
_DEFAULT_RESPONSE_TIMEOUT_S = 3.0


class OctopusBleClient:
    """Cliente BLE one-master para Pack01 BlueSun.

    Thread-safety: NO seguro entre threads. Tiene que correr dentro de un
    asyncio loop. Para uso desde codigo sincrono, envolver en
    asyncio.run_coroutine_threadsafe.

    Single-central: Pack01 acepta SOLO 1 conexión BLE. Si otro cliente
    (ej. la app Octopus móvil) intenta conectar, este cliente recibe
    desconexión.
    """

    def __init__(
        self,
        mac: str,
        *,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        response_timeout_s: float = _DEFAULT_RESPONSE_TIMEOUT_S,
    ) -> None:
        self.mac = mac
        self.connect_timeout_s = connect_timeout_s
        self.response_timeout_s = response_timeout_s
        self._client: BleakClient | None = None
        # Cola de respuestas notificadas. Cada response llega por separado
        # (Octopus envia 1 frame por request); usamos Queue para correlar.
        self._notify_q: asyncio.Queue[bytes] = asyncio.Queue()
        # Lock para serializar requests (Modbus es half-duplex en el bus
        # interno del banco).
        self._tx_lock = asyncio.Lock()

    # ───── Connection management ───────────────────────────────────

    async def connect(self) -> None:
        """Conecta al Pack01 master y subscribe a la char de notify."""
        if self._client is not None and self._client.is_connected:
            log.debug("connect(): ya conectado")
            return
        log.info("Conectando a Pack01 master %s (timeout=%.1fs)…", self.mac, self.connect_timeout_s)
        self._client = BleakClient(self.mac, timeout=self.connect_timeout_s)
        await self._client.connect()
        await self._client.start_notify(CHAR_NOTIFY_UUID, self._on_notify)
        log.info("Conectado a Pack01. MTU=%s", getattr(self._client, "mtu_size", "?"))

    async def disconnect(self) -> None:
        """Desconecta limpiamente."""
        if self._client is None:
            return
        try:
            if self._client.is_connected:
                try:
                    await self._client.stop_notify(CHAR_NOTIFY_UUID)
                except BleakError as e:
                    log.debug("stop_notify durante disconnect: %s (ignorado)", e)
                await self._client.disconnect()
                log.info("Desconectado de Pack01")
        finally:
            self._client = None
            # Limpia la cola por si quedaron responses huerfanos
            while not self._notify_q.empty():
                self._notify_q.get_nowait()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        """Callback de bleak cuando llega una notificación FFF1."""
        # bleak entrega bytearray; pasamos a bytes immutable para inspección segura
        frame = bytes(data)
        # Encola sin bloquear (la queue es ilimitada por defecto)
        try:
            self._notify_q.put_nowait(frame)
        except asyncio.QueueFull:  # solo si configuráramos maxsize
            log.warning("notify queue full, dropping frame %s", frame.hex())

    # ───── Request/response ────────────────────────────────────────

    async def _request(self, frame: bytes, slave: int, expected_byte_count: int) -> bytes:
        """Envia frame y espera el response que matchee (slave, byte_count).

        Filtra frames espurios (ej. retransmisión del LCD, eco de otro pack si
        algo raro pasa) hasta encontrar el correcto o timeout.
        """
        if self._client is None or not self._client.is_connected:
            raise BleakError("not connected")

        # Drenar respuestas viejas que puedan haber llegado entre polls
        while not self._notify_q.empty():
            stale = self._notify_q.get_nowait()
            log.debug("drained stale frame: %s", stale[:8].hex())

        async with self._tx_lock:
            log.debug("TX (slave=%d): %s", slave, frame.hex())
            await self._client.write_gatt_char(CHAR_WRITE_UUID, frame, response=False)

            # Espera response que matchee. Tolerancia: 3 retries para descartar frames
            # espurios antes de dar timeout total.
            deadline_s = self.response_timeout_s
            for _attempt in range(3):
                try:
                    response = await asyncio.wait_for(self._notify_q.get(), timeout=deadline_s)
                except TimeoutError:
                    log.warning("timeout esperando response slave=%d after %.1fs", slave, deadline_s)
                    raise
                # Validar header mínimo
                if len(response) < 3:
                    log.debug("response truncado (%d bytes), descarto", len(response))
                    continue
                if response[0] != slave:
                    log.debug(
                        "response slave mismatch (got=%d expected=%d), descarto y sigo",
                        response[0], slave,
                    )
                    continue
                if response[1] != 0x04:
                    log.debug("response fc=%d (expected 0x04), descarto", response[1])
                    continue
                if response[2] != expected_byte_count:
                    log.debug(
                        "response byte_count=%d (expected %d), descarto",
                        response[2], expected_byte_count,
                    )
                    continue
                log.debug("RX matched (slave=%d, %d bytes)", slave, len(response))
                return response

            raise TimeoutError(
                f"3 frames spurios consecutivos sin response valido slave={slave}"
            )

    async def poll_pia(self, pack: int) -> PiaData:
        """Pollea PIA (cmd 0x10, block 0x1000) del pack indicado."""
        if not (1 <= pack <= 4):
            raise ValueError(f"pack debe ser 1..4, got {pack}")
        frame = pia_request(pack)
        response = await self._request(frame, slave=pack, expected_byte_count=0x24)
        result = parse_response(response)
        if not isinstance(result, PiaData):
            raise RuntimeError(f"expected PiaData, got {type(result).__name__}")
        return result

    async def poll_pib(self, pack: int) -> PibData:
        """Pollea PIB (cmd 0x11, block 0x1100) del pack indicado."""
        if not (1 <= pack <= 4):
            raise ValueError(f"pack debe ser 1..4, got {pack}")
        frame = pib_request(pack)
        response = await self._request(frame, slave=pack, expected_byte_count=0x34)
        result = parse_response(response)
        if not isinstance(result, PibData):
            raise RuntimeError(f"expected PibData, got {type(result).__name__}")
        return result

    # ───── Context manager ─────────────────────────────────────────

    async def __aenter__(self) -> OctopusBleClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

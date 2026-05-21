# inverter-bridge

Custom Python daemon that polls SunGoldPower split-phase inverters (and other SRNE-based hybrid inverters) via Modbus RTU over USB-Serial, publishing sensor data to MQTT in a format compatible with Solar Assistant's HA topology.

Target hardware: any SBC with USB-A ports (developed against Orange Pi 3 LTS); supports 1..N inverters (tested with 2 in split-phase).

## Why

Solar Assistant is a closed-source commercial Pi image that polls supported inverters and publishes to MQTT. It's reliable but opaque — when its update cadence degrades or it loses connection, you have no visibility and no fix. This daemon implements the same wire-level Modbus RTU polling, publishes to the same MQTT topic shape SA uses, so existing Home Assistant entities and automations continue working without changes.

## Features

- Custom Modbus RTU implementation (CRC16, fc 0x03, exception parsing) — ~150 lines, no `pymodbus` dependency.
- Bus-noise tolerant stream parser (the inverter's own LCD chatters on the same bus; parser tolerates interleaved frames).
- Parallel polling via `ThreadPoolExecutor` (one thread per inverter — separate serial ports, no contention).
- Per-MPPT PV power computed from voltage × current registers (the inverter exposes current, not power, for these).
- Client-side energy integration (Wh accumulators with disk persistence to survive restarts).
- HA Discovery payloads with `force_update: true` for low-granularity sensors so they don't appear stale.
- MQTT Last-Will-Testament for `availability` topic — drives `binary_sensor.inverter_bridge_online`.
- `_meta/*` diagnostic sensors: `poll_duration_ms`, `crc_fails_total`, `uptime_s`.
- Tools: bus capture for fixtures, orphan-discovery cleanup, HA entity_registry zombie purge, entity rename.

## Quick start (dev)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest
```

200 tests, runs in < 3 s.

## Deploy

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full procedure (system user, venv, config, systemd unit).

## Status

Production-ready against SRNE split-phase inverters (model code `SR-XXXXXXXX` family). Bus reliability and topic compatibility with Solar Assistant have been verified empirically. Reverse-engineering notes for the register map are in the test fixtures and in module docstrings.

## License

MIT — see [`LICENSE`](LICENSE).

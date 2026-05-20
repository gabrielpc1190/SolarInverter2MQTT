# inverter-bridge

Custom Python daemon that polls SunGoldPower split-phase inverters via SRNE Modbus RTU and publishes sensor data to MQTT for Home Assistant.

Replaces Solar Assistant in the home installation. Target hardware: Orange Pi 3 LTS, 2 inverters on USB-Serial CH340.

## Status

Phase 0 — under development. Spec and plan live in the HomeAssistant repo:
- `docs/superpowers/specs/2026-05-19-inverter-bridge-design.md`
- `docs/superpowers/plans/2026-05-19-inverter-bridge.md`

## Quick start (dev)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
pytest
```

## License

MIT — see `LICENSE`.

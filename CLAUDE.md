# CLAUDE.md — inverter-bridge

Daemon Python que reemplaza Solar Assistant en el sitio GADI. Despliegue en producción → leer `README.md` para overview y `docs/DEPLOYMENT.md` antes de tocar el host.

## Identidad

- **Repo GitHub:** `git@github.com:gabrielpc1190/SolarInverter2MQTT.git`. Branch principal `master`. Push directo (Conv. Commits en inglés).
- **Host de producción:** `GADI-InverterBridge` (172.16.9.32, Orange Pi 3 LTS, Armbian/Debian 13). Acceso vía alias SSH `GADI-InverterBridge` (root) o `GADI-InverterBridge-gabriel`. Detalle de host en [INFRA.md](../INFRA.md) y memoria [[project-gadi-inverter-bridge]].
- **Estado:** production-ready para inversores SunGoldPower split-phase basados en SRNE. Las 200+ pruebas (`pytest`) corren en <3 s.

## Reglas críticas

- **Compatibilidad MQTT con Solar Assistant es contrato.** Los tópicos y el shape de payload (`gadi_inverters/*`) están consumidos por HA — entidades y automations ya dependen. Cualquier cambio en `mqtt_publisher.py` que altere topic, retain, o discovery payload necesita validación contra HA antes de pushear.
- **Mapa de registros canónico en `inverter_bridge/srne_map.py`.** Cambios al register map deben respaldarse con fixture en `tests/` (capturas reales del bus). No agregar registros "porque el datasheet dice X" sin captura.
- **Producción:** push a master = deploy candidato. Antes de pushear cambios funcionales, correr `pytest` local y revisar diff de tópicos MQTT.
- **Servicio en host remoto:** `inverter-bridge.service` (systemd, unit en `systemd/inverter-bridge.service`). Logs vía `journalctl -u inverter-bridge -f`. Reload con `systemctl restart inverter-bridge` tras actualizar venv.

## Mapa rápido del código

| Archivo | Rol |
|---|---|
| `inverter_bridge/daemon.py` | Loop principal, scheduler de polls |
| `inverter_bridge/modbus.py` | Implementación custom Modbus RTU (CRC16, fc 0x03, exceptions) — ~150 líneas, sin `pymodbus` |
| `inverter_bridge/serial_io.py` | I/O serial + parser stream tolerante a ruido del LCD del inversor |
| `inverter_bridge/srne_map.py` | Register map SRNE (fuente de verdad de qué se lee) |
| `inverter_bridge/parsers.py` | Decodificación de frames a valores tipados |
| `inverter_bridge/aggregator.py` | Agregaciones cross-inversor (totales banco, etc.) |
| `inverter_bridge/energy_integrator.py` | Acumuladores Wh con persistencia en disco |
| `inverter_bridge/mqtt_publisher.py` | Publicación MQTT + HA Discovery + LWT availability |
| `inverter_bridge/config.py` | Carga de `config.yaml` |
| `tools/` | Utilidades: bus capture (fixtures), orphan-discovery cleanup, HA entity_registry purge, rename |
| `docs/DEPLOYMENT.md` | Procedimiento de despliegue completo en el OPi |
| `docs/MQTT_CANONICAL.json` | Esquema canónico de tópicos/payloads (contrato con HA) |

## Workflow de cambio típico

1. Editar local en `/mnt/NAS/inverter-bridge/`.
2. `pytest` — debe pasar todo en <3 s.
3. Commit (Conv. Commits, inglés): `feat:` / `fix:` / `refactor:` / etc.
4. Push a `master`.
5. En el OPi: `cd /opt/inverter-bridge && git pull && systemctl restart inverter-bridge` (o lo que `docs/DEPLOYMENT.md` indique exactamente).
6. `journalctl -u inverter-bridge -n 50 -f` para confirmar arranque limpio.

## Gotchas

- **Identificación de puerto USB-Serial:** usar `/dev/serial/by-path/` (CH340 no tiene serial único, by-id es ambiguo). Cambio de cable a otro puerto USB = cambio de mapping. Si un poll falla todo, sospechar reordenamiento de puertos antes que bug.
- **SMP roto en el OPi:** solo 1 de 4 cores activo (Allwinner H6 + kernel sunxi64). Sabido y aceptable; el daemon usa ~1% CPU.
- **NFS no es paths de runtime:** este repo vive en NAS para edición desde DevClaude, pero el host de producción tiene su propio clone en `/opt/inverter-bridge` (o donde DEPLOYMENT.md lo ponga). No confundir.

## Referencias

- Memoria principal: [[project-gadi-inverter-bridge]] (⚠️ tiene info histórica del protocolo Voltronic ASCII — fue reemplazado por Modbus RTU/SRNE; pendiente refrescar memoria).
- Repo HA consumidor: `/mnt/NAS/HomeAssistant/` (entidades `sensor.gadi_inverters_*` dependen de los tópicos publicados aquí).
- Hardware del host: [INFRA.md](../INFRA.md).
- Convenciones globales: [/mnt/NAS/CLAUDE.md](../CLAUDE.md).

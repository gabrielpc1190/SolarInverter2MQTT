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
- **Servicio en host remoto:** `inverter-bridge.service` (systemd, unit en `systemd/inverter-bridge.service`, **`Type=notify` + `WatchdogSec=120`** desde 2026-07-04 — el daemon manda `READY=1`/`WATCHDOG=1` vía `inverter_bridge/sdnotify.py`; si el loop principal se cuelga, systemd lo reinicia). Logs vía `journalctl -u inverter-bridge -f`. Deploy/restart con `tools/deploy.sh`.

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
5. **Deploy al OPi con `tools/deploy.sh`** (rsync por checksum de TODO el package + unit
   systemd + chown + restart + verificación de journal en un solo paso):
   ```bash
   tools/deploy.sh          # usa el alias SSH GADI-InverterBridge
   ```
   Reemplaza (2026-07-04) el viejo flujo de scp manual de archivos sueltos, que ya causó
   deriva repo↔host una vez (auditoría H3). El venv en `/opt/inverter-bridge/src/.venv/`
   tiene el package instalado en modo **editable**, así que el rsync del código es
   suficiente — no hay que reinstalar.
6. El script ya muestra el journal; para seguirlo en vivo: `journalctl -u inverter-bridge -f`.

> **Nota:** el OPi de producción no tiene `git` instalado ni un `.git` en `/opt/inverter-bridge/src/` (verificado 2026-05-30). El doc `docs/DEPLOYMENT.md` describe el bootstrap inicial con `git clone`; los updates van por `tools/deploy.sh`.

## Host (estado 2026-07-04, post-auditoría)

- **Auditoría completa código+host:** [docs/2026-07-04_revision_codigo-y-host.html](docs/2026-07-04_revision_codigo-y-host.html). Fixes A1/A2/M3/M5/M7/M8/M1-min/B1/B8/B9 aplicados en commit `1c3ecef` (tests en `tests/test_audit_fixes.py`). Quedan como backlog: M2 (matching por count vs chatter LCD — requiere fixtures reales), M1-completo (guard de re-submit), M4 (marcador offline a medio cablear — decidir), M6 (packs BMS congelados siguen sumando), B2 (bloque `faults` se lee y no se publica — alerting gratis).
- **Parches:** `unattended-upgrades` instalado (security-only, diario) el 2026-07-04.
- **`/etc/inverter-bridge.yaml`:** perms `640 root:dialout`. ⚠️ Gotcha systemd: aunque el usuario `inverter-bridge` pertenece al grupo `inverter-bridge`, bajo la unit (con `Group=dialout`) el proceso NO recibió los grupos suplementarios — un config `root:inverter-bridge` dio `PermissionError` en producción (2026-07-04); el grupo del config debe ser el **primario** de la unit (`dialout`). La contraseña MQTT vive aparte en `/etc/inverter-bridge.secrets` (`600 inverter-bridge`).
- **Respaldo diario:** cron 03:17 CST en devclaude copia config + secrets + contadores de energía + unit a `/mnt/NAS/.secrets/backups/inverter-bridge/` (oculto del nas-explorer).
- **Timezone del host:** `America/Costa_Rica` (journal en CST desde 2026-07-04; antes UTC).
- **Load average +2.0 fantasma:** `armbian-hardware-monitor/optimize` se colgaban en D-state en cada boot leyendo el sysfs de cpufreq (secuela del SMP roto) — **deshabilitados 2026-07-04**; los 2 procesos colgados del boot del 20-may desaparecen en el próximo reboot.

## Gotchas

- **Identificación de puerto USB-Serial:** usar `/dev/serial/by-path/` (CH340 no tiene serial único, by-id es ambiguo). Cambio de cable a otro puerto USB = cambio de mapping. Si un poll falla todo, sospechar reordenamiento de puertos antes que bug.
- **SMP roto en el OPi:** solo 1 de 4 cores activo (Allwinner H6 + kernel sunxi64). Sabido y aceptable; el daemon usa ~1% CPU.
- **NFS no es paths de runtime:** este repo vive en NAS para edición desde DevClaude, pero el host de producción tiene su propio clone en `/opt/inverter-bridge` (o donde DEPLOYMENT.md lo ponga). No confundir.
- **Reconexión BLE del BMS es auto-sanadora (desde 2026-06-09):** si el link BLE al Pack01 master se cae, `bms/service.py::_poll_loop` sale para que `_run_async` reconecte con backoff. Dos disparadores: desconexión explícita (`is_connected=False` → sale inmediato) y "link zombie" (todo da timeout pero `is_connected` sigue True → sale tras `bms.max_failed_cycles` ciclos, default 3). Antes de este fix el loop se tragaba el `"not connected"` y giraba para siempre sin reconectar (requería restart manual del daemon — caso real que tumbó el `sensor.gadi_battery_soc`).

## Referencias

- Memoria principal: [[project-gadi-inverter-bridge]] (⚠️ tiene info histórica del protocolo Voltronic ASCII — fue reemplazado por Modbus RTU/SRNE; pendiente refrescar memoria).
- Repo HA consumidor: `/mnt/NAS/HomeAssistant/` (entidades `sensor.gadi_inverters_*` dependen de los tópicos publicados aquí).
- Hardware del host: [INFRA.md](../INFRA.md).
- Convenciones globales: [/mnt/NAS/CLAUDE.md](../CLAUDE.md).

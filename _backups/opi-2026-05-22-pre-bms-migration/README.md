# OPi `gadi-inverter-bridge` snapshot — pre BMS migration (2026-05-22)

Estado del host OPi (172.16.9.32) **antes** de extender el daemon `inverter-bridge` con el módulo BMS BLE Octopus que va a leer el banco BlueSun en lugar del Panel S3 cuartoeléctrico.

**Objetivo:** punto de rollback inmediato si la migración del BMS al OPi degrada o rompe la ingestión de los inversores (que ya funciona en producción desde 2026-05-21).

## Estado al snapshot

| Aspecto | Valor |
|---|---|
| Host | `gadi-inverter-bridge` / 172.16.9.32 (Orange Pi 3 LTS, Armbian 26 / Debian 13 / kernel `6.12.68-current-sunxi64`) |
| Daemon corriendo | `inverter-bridge.service` — `active (running)` desde 2026-05-21 16:11 UTC (~23 h al momento del snapshot) |
| User systemd | `inverter-bridge` (Group `dialout` para acceso `/dev/ttyUSB*`) |
| Memoria del daemon | ~15.4 MiB residente, 4 tasks, CPU acumulada ~9 min en 23 h |
| Código desplegado | `/opt/inverter-bridge/src/` (editable install) — `inverter_bridge==0.1.0` |
| Config | `/etc/inverter-bridge.yaml` (28 líneas, sin secrets inline) |
| Secret | `/etc/inverter-bridge.secrets` — archivo de **5 bytes** con el password MQTT raw (referenciado por `mqtt.password_file:` en el config) |
| State persistente | `/var/lib/inverter-bridge/energy.json` — 6 acumuladores Wh del energy integrator |
| Python | 3.x del venv en `/opt/inverter-bridge/src/.venv` (versión exacta en `host-fingerprint.txt`) |
| Dependencias pip | mínimas: `paho-mqtt==2.1.0`, `pyserial==3.5`, `PyYAML==6.0.3` (ver `pip-freeze.txt`) |
| Git en el host | **NO instalado.** Rastreo del commit desplegado por timestamps de archivos en `/opt/inverter-bridge/src/inverter_bridge/` (ver `host-fingerprint.txt`) y/o `git log` desde `/mnt/NAS/inverter-bridge` (NFS, mismo working tree). |

## Contenido del snapshot

| Archivo | Origen real | Notas |
|---|---|---|
| `inverter-bridge.yaml` | `/etc/inverter-bridge.yaml` | Config raw del daemon. NO contiene passwords (delegados a `password_file:`). Seguro para repo. |
| `inverter-bridge.secrets.template` | template del `/etc/inverter-bridge.secrets` real | Placeholder `<MQTT_PASSWORD_HERE>`. El valor real (5 bytes raw, el password del broker `core-mosquitto`) vive solo en la OPi y en el HA donde se configuró el broker. NO se commitea el real. |
| `energy.json` | `/var/lib/inverter-bridge/energy.json` | 6 acumuladores Wh: `battery_energy_in_wh`, `battery_energy_out_wh`, `pv_energy_wh`, `load_energy_wh`, `grid_energy_in_wh`, `grid_energy_out_wh`. Restaurar este archivo preserva el histórico del energy integrator client-side. |
| `inverter-bridge.service` | `/etc/systemd/system/inverter-bridge.service` | Unit con hardening (`ProtectSystem=strict`, `NoNewPrivileges`, `ReadWritePaths` mínimos). |
| `pip-freeze.txt` | `pip freeze` del venv desplegado | 3 deps + editable install. |
| `host-fingerprint.txt` | uname / ip / hciconfig / lsusb / lsblk / serial paths / systemctl status / Python version | Útil para diagnóstico y para reconstruir un host equivalente desde cero. |
| `journal-7d.txt` | `journalctl -u inverter-bridge --since "7 days ago"` | ~5 300 líneas. Trace del daemon corriendo limpiamente bajo carga real. |

## Cómo restaurar — escenarios

### A. Solo se corrompió `energy.json` (acumuladores Wh perdidos)

```bash
scp energy.json GADI-InverterBridge:/tmp/
ssh GADI-InverterBridge '
  systemctl stop inverter-bridge
  install -o inverter-bridge -g dialout -m 600 /tmp/energy.json /var/lib/inverter-bridge/energy.json
  systemctl start inverter-bridge
'
```

### B. Cambió config y querés rollback al pre-migración

```bash
scp inverter-bridge.yaml GADI-InverterBridge:/tmp/
ssh GADI-InverterBridge '
  systemctl stop inverter-bridge
  install -o root -g root -m 644 /tmp/inverter-bridge.yaml /etc/inverter-bridge.yaml
  systemctl start inverter-bridge
'
```

### C. Querés rollback completo del código del daemon

El código vive en git (`/mnt/NAS/inverter-bridge/`, remote `git@github.com:gabrielpc1190/SolarInverter2MQTT.git`). El commit desplegado al momento del snapshot se identifica por:

```bash
# desde DevClaude:
cd /mnt/NAS/inverter-bridge
git log -1 --oneline   # commit actual del working tree
```

Para rollback:
```bash
ssh GADI-InverterBridge '
  systemctl stop inverter-bridge
  cd /opt/inverter-bridge/src
  # como user gabriel (NOT git instalado en host — usar working copy desde NAS si NFS aplica,
  # o resync manual del repo)
  systemctl start inverter-bridge
'
```

NOTA: el OPi NO tiene `git` instalado. Workarounds:
1. **Recommended**: copiar el repo entero (`rsync` desde DevClaude) al `/opt/inverter-bridge/src/` en el OPi, sin venv (el venv puede preservarse — el editable install lo apunta al directorio).
2. **Alternative**: instalar git en el OPi (`apt install git`) y agregar a la documentación de deployment.

### D. Disaster recovery total — OPi limpia

Procedimiento completo en [`../../docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md). Resumen:

1. Armbian limpio + WiFi `GADI_IoT`.
2. `useradd -r -G dialout inverter-bridge`.
3. `mkdir -p /opt/inverter-bridge/src /var/lib/inverter-bridge /var/log/inverter-bridge`.
4. Clonar repo a `/opt/inverter-bridge/src` y `cd src; python3 -m venv .venv && .venv/bin/pip install -e .`.
5. Copiar `inverter-bridge.yaml` a `/etc/`, crear `/etc/inverter-bridge.secrets` con el password MQTT.
6. Copiar `energy.json` a `/var/lib/inverter-bridge/` (preserva historia) o omitir para empezar de cero.
7. Instalar `inverter-bridge.service` en `/etc/systemd/system/`, `systemctl daemon-reload && systemctl enable --now inverter-bridge`.

## Lo que este snapshot NO incluye

- **Imagen completa del eMMC** del OPi (~16 GB). No vale la pena por tamaño y porque el host es reinstalable desde docs + repo.
- **`/etc/inverter-bridge.secrets` real** (el password raw). Está solo en la OPi; el broker MQTT (HA `core-mosquitto`) tiene el otro lado del par.
- **Logs históricos >7 días.** Disponibles vía `journalctl -u inverter-bridge --since` en el host si hace falta.

## Plan de migración asociado

Este es el respaldo "lado OPi" del plan [`../../../HomeAssistant/docs/superpowers/plans/2026-05-22-opi-bms-migration.md`](../../../HomeAssistant/docs/superpowers/plans/2026-05-22-opi-bms-migration.md) (referencia cross-repo).

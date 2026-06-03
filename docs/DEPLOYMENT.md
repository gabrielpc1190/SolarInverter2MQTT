# Deployment

This guide targets a small Linux SBC (Orange Pi 3 LTS in our setup, but any Debian/Ubuntu-style host with USB-A ports and Python 3.11+ should work).

## 1. Install Python + pip + venv

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

`python3 --version` should report 3.11 or higher. (Armbian trixie ships Python 3.13.)

## 2. Create the service user

The daemon runs as a dedicated non-login system user that's a member of `dialout` so it can open `/dev/ttyUSB*`:

```bash
sudo useradd --system --no-create-home --groups dialout --shell /usr/sbin/nologin inverter-bridge
sudo mkdir -p /var/log/inverter-bridge /var/lib/inverter-bridge
sudo chown inverter-bridge:inverter-bridge /var/log/inverter-bridge /var/lib/inverter-bridge
```

`/var/lib/inverter-bridge/` is where the daemon persists Wh accumulators (`energy.json`) so a restart doesn't reset the counters.

## 3. Clone + install

```bash
sudo mkdir -p /opt/inverter-bridge
sudo chown inverter-bridge:inverter-bridge /opt/inverter-bridge
sudo -u inverter-bridge bash -c '
    cd /opt/inverter-bridge
    git clone https://github.com/gabrielpc1190/SolarInverter2MQTT.git src
    cd src
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install .
'
```

## 4. Config + MQTT password

```bash
sudo cp /opt/inverter-bridge/src/config.example.yaml /etc/inverter-bridge.yaml
sudo $EDITOR /etc/inverter-bridge.yaml
# - Update each inverter's `port:` to the real `by-path` symlink
#   (validate with `ls -la /dev/serial/by-path/`)
# - Update `mqtt.host` to your broker
# - Update `mqtt.username`
# - `topic_prefix` defaults to `gadi_inverters` (renamed from `solar_assistant` on
#   2026-06-03). HA entity_ids are unaffected by the prefix — discovery unique_ids
#   are prefix-independent — so HA re-binds existing entities by unique_id.

sudo install -o inverter-bridge -g inverter-bridge -m 0600 /dev/stdin \
    /etc/inverter-bridge.secrets <<< 'YOUR_MQTT_PASSWORD_HERE'
```

## 5. Install + enable the systemd unit

```bash
sudo cp /opt/inverter-bridge/src/systemd/inverter-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inverter-bridge
sudo journalctl -u inverter-bridge -f
```

Expected within ~10 s: `_meta/uptime_s`, `_meta/poll_duration_ms`, plus the hot-tier sensor publishes.

## 6. Verify

From any host with `mosquitto-clients` on the same network as your broker:

```bash
mosquitto_sub -h <BROKER_IP> -u <USER> -P "$(cat /path/to/secret)" \
    -t 'gadi_inverters/#' -v -W 30 | head -50
```

Within 30 s you should see:

- `gadi_inverters/availability` → `online`
- ~85 retained discovery payloads on `homeassistant/sensor/<unique_id>/config` and `homeassistant/binary_sensor/inverter_bridge_online/config`
- State updates every ~3 s on the per-inverter hot-tier sensors
- Energy accumulators (`total/battery_energy_in`, `total/pv_energy`, etc.) ticking up over time

In Home Assistant → Settings → Devices & Services → MQTT → "SRNE Split-phase x 2" device → all sensors visible. Or filter the entity list by the topic prefix used in your discovery payloads.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `journalctl` shows "no valid response" repeatedly on one bus | Cable picking up EMI from AC wiring; or the wrong slave addr; or the inverter is on a noisy internal LCD bus | Replace the USB cable with a shielded / ferrite-cored one. Verify `slave:` in config matches what the inverter responds to (try `0x01` and `0x02`). Daemon retries automatically — bump `retry_attempts` if needed |
| Daemon restarts every ~30 s | `WatchdogSec` in service unit but daemon doesn't `sd_notify` | Already disabled in the shipped unit. Re-check `/etc/systemd/system/inverter-bridge.service` doesn't have a `WatchdogSec=` line |
| All `exception 0x02` (illegal_data_address) on certain blocks | Block not implemented in this firmware variant | Normal for some SRNE firmwares — the daemon tolerates and continues. Remove block from `srne_map.BLOCKS` if you want to silence the debug logs |
| HA shows entities but values are stale (>10 s) | `availability_topic` LWT misconfigured, or HA recorder dedup | Restart daemon to re-publish discovery; verify `binary_sensor.inverter_bridge_online == on` in HA; SoC/mode use `force_update: true` so they're never stale beyond the polling cycle |
| `energy.json: Read-only file system` | systemd `ProtectSystem=strict` blocks writes outside `ReadWritePaths` | Verify `ReadWritePaths=/var/log/inverter-bridge /var/lib/inverter-bridge` is in the service file |

## 8. Optional: HA watchdog automation

If you want HA to notify you when the bridge goes offline, see the example in the project documentation. The relevant entities are:

- `binary_sensor.inverter_bridge_online` — LWT-driven, goes `off` if the broker doesn't hear from the daemon for > keepalive (60 s default).
- `sensor.<topic_prefix>_meta_poll_duration_ms` — alert if it climbs above ~10000 ms persistently (means inverter retries are getting expensive).
- `sensor.<topic_prefix>_meta_uptime_s` — alert if it resets unexpectedly often (daemon is crashing).

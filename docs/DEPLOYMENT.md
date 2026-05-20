# Deployment — inverter-bridge on Orange Pi 3 LTS

Assumes a freshly bootstrapped OPi 3 LTS as documented in the spec §8 Phase 0 (Armbian trixie, key auth, `gabriel` user in `dialout`).

## 1. Install Python 3.12+ + pip + venv

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

The OPi runs Debian 13 (Armbian trixie) which ships Python 3.13 by default.

## 2. Create service user

```bash
sudo useradd --system --no-create-home --groups dialout --shell /usr/sbin/nologin inverter-bridge
sudo mkdir -p /var/log/inverter-bridge
sudo chown inverter-bridge:inverter-bridge /var/log/inverter-bridge
```

## 3. Clone + install

```bash
sudo mkdir -p /opt/inverter-bridge
sudo chown inverter-bridge:inverter-bridge /opt/inverter-bridge
sudo -u inverter-bridge bash -c '
    cd /opt/inverter-bridge
    git clone https://github.com/gabrielpc1190/inverter-bridge.git src
    cd src
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install .
'
```

## 4. Config + secret

```bash
sudo cp /opt/inverter-bridge/src/config.example.yaml /etc/inverter-bridge.yaml
sudo $EDITOR /etc/inverter-bridge.yaml
# - Update by-path paths (validate with `ls /dev/serial/by-path/`)
# - Set MQTT host + username
# - Set password_file path

sudo install -o inverter-bridge -g inverter-bridge -m 0600 /dev/stdin /etc/inverter-bridge.secrets <<< 'YOUR_MQTT_PASSWORD_HERE'
```

## 5. Install + enable systemd unit

```bash
sudo cp /opt/inverter-bridge/src/systemd/inverter-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inverter-bridge
sudo journalctl -u inverter-bridge -f
```

Expected within 10 s: log lines for hot cycle + discovery payloads published.

## 6. Verify

From any host with `mosquitto_sub` (or via Home Assistant MQTT integration):

```bash
mosquitto_sub -h 192.0.2.10 -u inverter_bridge -P "$(cat ~/secret)" \
    -t 'gadi_inverters/#' -v -W 30 | head -50
```

Within 30 s you should see:
- `gadi_inverters/availability` -> `online`
- ~50 `homeassistant/sensor/gadi_inverters_*/config` discovery messages (retained)
- ~50 `gadi_inverters/<sensor>/state` messages updating every ~3 s

In Home Assistant -> Developer Tools -> States -> filter by `sensor.gadi_inverters_` -> all 50+ entities present.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `journalctl` shows "no valid response" repeatedly | Bus contention / wrong slave / wrong port | Re-verify `ls /dev/serial/by-path/` and slave assignments |
| Daemon restarts every 30 s | `WatchdogSec` not being met | Increase `WatchdogSec` in service file or lower `hot_interval_s` in config |
| Inv1 has high fail rate (>20%) | Known bus noise (see spec §13 #15) | Daemon retries automatically; if too persistent increase `retry_attempts` |
| All exception 0x02 errors logged at DEBUG | Block not implemented in this firmware | Normal; remove block from polling list if you don't need it |
| HA shows entities but values stale | `availability_topic` not set; verify LWT | Restart daemon to re-publish discovery |

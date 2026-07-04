#!/bin/bash
# Deploy inverter-bridge to the production OPi.
#
# Replaces the old "scp the files I remember touching" flow, which already
# caused repo↔host drift once (audit H3, 2026-07-04). rsync -c compares by
# checksum so EVERYTHING that differs gets synced, and nothing else.
#
# Usage: tools/deploy.sh [ssh-host]      (default: GADI-InverterBridge)
set -euo pipefail

HOST="${1:-GADI-InverterBridge}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Sync código (rsync por checksum) =="
rsync -rlc --itemize-changes \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$REPO/inverter_bridge/" "$HOST:/opt/inverter-bridge/src/inverter_bridge/"

echo "== Sync unit systemd =="
rsync -c --itemize-changes \
  "$REPO/systemd/inverter-bridge.service" \
  "$HOST:/etc/systemd/system/inverter-bridge.service"

echo "== Permisos + restart + verificación =="
ssh "$HOST" "
  chown -R inverter-bridge:inverter-bridge /opt/inverter-bridge/src/inverter_bridge &&
  systemctl daemon-reload &&
  systemctl restart inverter-bridge &&
  sleep 5 &&
  systemctl is-active inverter-bridge &&
  journalctl -u inverter-bridge -n 12 --no-pager
"
echo '== Deploy OK =='

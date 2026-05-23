"""One-shot: publica payload vacío retained a las discovery topics viejas para
que HA borre las entidades 'gadi_bms_bluesun_bluesun_*' que crearon naming feo.

Tambien limpia state topics viejos para que no queden retained orfanos.

Despues correr esto, hacer systemctl restart inverter-bridge para que publique
discovery con el nuevo naming `bluesun_*`.
"""
from __future__ import annotations
import sys
import time

import paho.mqtt.client as mqtt

# Old short slugs (las que publicabamos como object_id ANTES del rename)
OLD_OBJECT_IDS = []
for p in (1, 2, 3, 4):
    pp = f"pack{p:02d}"
    # PIA per pack
    for suffix in ("voltage", "current", "soc", "soh", "cycles", "remaining_ah", "nominal_ah"):
        OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_{suffix}")
    # PIB per pack
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_cell_v_min")
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_cell_v_max")
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_cell_v_avg")
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_cell_v_delta")
    for i in range(1, 5):
        OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_cell_temp_{i}")
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_env_temp")
    OLD_OBJECT_IDS.append(f"gadi_bms_{pp}_pcb_temp")

# Bank aggregates
for s in ("voltage_avg", "current_total", "soc_avg", "soc_spread", "power",
          "power_charging", "power_discharging", "remaining_ah", "nominal_ah",
          "min_soh", "max_cycles", "max_cell_temp"):
    OLD_OBJECT_IDS.append(f"gadi_bms_bank_{s}")

# Energy
OLD_OBJECT_IDS.append("gadi_bms_battery_energy_in")
OLD_OBJECT_IDS.append("gadi_bms_battery_energy_out")

# Serials
for p in (1, 2, 3, 4):
    OLD_OBJECT_IDS.append(f"gadi_bms_pack{p:02d}_serial")

print(f"Limpiando {len(OLD_OBJECT_IDS)} discovery topics viejos…")

# Read mqtt creds from /etc/inverter-bridge.secrets
PASSWORD = open("/etc/inverter-bridge.secrets").read().strip()

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bms_cleanup", clean_session=True)
c.username_pw_set("mqtt", PASSWORD)
c.connect("172.16.10.12", 1883, keepalive=10)
c.loop_start()
time.sleep(1)

for oid in OLD_OBJECT_IDS:
    # Clear discovery (HA removes entity)
    c.publish(f"homeassistant/sensor/{oid}/config", payload="", qos=1, retain=True)
    # Clear state (no orphan retained)
    c.publish(f"gadi_bms/{oid}/state", payload="", qos=0, retain=True)

# Also clear availability topic (it was retained)
c.publish("gadi_bms/availability", payload="", qos=1, retain=True)

# Wait for all messages to flush
time.sleep(2)
c.loop_stop()
c.disconnect()
print(f"Done. {len(OLD_OBJECT_IDS)} discovery + state topics limpiados.")
print("Ahora: systemctl restart inverter-bridge para publicar nuevo discovery bluesun_*")

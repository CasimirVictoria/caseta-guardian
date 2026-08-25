#!/usr/bin/env python3
"""
Caseta Zigbee - Dimoni Natiu de Telemetria i Domòtica Zigbee en Memòria RAM
Suport complet per a múltiples sensors (Termohigròmetre + Multisensor 4-en-1).

Filosofia Zero Desgast de Disc:
- Base de dades activa en RAM (tmpfs a /run/caseta-zigbee/zigbee.db).
- Seguiment independent per sensor de temperatura, humitat, lux, presència i bateria.
- Resum diari compacte (1 sola línia JSON a les 23:59h).
"""

import asyncio
import datetime
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import paho.mqtt.client as mqtt
import zigpy_znp.zigbee.application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [caseta-zigbee] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("caseta-zigbee")

FLASH_DB_BACKUP = "/data/caseta-guardian/zigbee_backup.db"
HISTORY_DIR = "/data/caseta-guardian/history"
HISTORY_CLIMA_FILE = "/data/caseta-guardian/history/historic_clima.jsonl"
RAM_RUN_DIR = "/run/caseta-zigbee"
RAM_DB_PATH = "/run/caseta-zigbee/zigbee.db"
SERIAL_PORT = os.environ.get("ZIGBEE_PORT", "/dev/ttyUSB0")
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

class ZigbeeManager:
    def __init__(self):
        self.app = None
        self.mqtt_client = None
        self.running = True
        self.last_saved_day = None
        self.sensors = {
            "sensor_1": {
                "nom": "Termohigròmetre TS0201",
                "ieee": "a4:c1:38:8c:5b:70:28:4f",
                "temperatura": None,
                "humitat": None,
                "bateria": None,
                "t_max": -100.0, "t_max_hora": None,
                "t_min": 100.0,  "t_min_hora": None,
                "mostres_temp": [],
                "h_max": 0.0,    "h_min": 100.0,
                "ultima_actualitzacio": None
            },
            "sensor_2": {
                "nom": "Multisensor 4-en-1 ZG-204ZV",
                "ieee": "a4:c1:38:a6:3a:59:a0:35",
                "temperatura": None,
                "humitat": None,
                "lux": None,
                "presencia": False,
                "bateria": None,
                "t_max": -100.0, "t_max_hora": None,
                "t_min": 100.0,  "t_min_hora": None,
                "mostres_temp": [],
                "h_max": 0.0,    "h_min": 100.0,
                "lux_max": 0,    "lux_max_hora": None,
                "ultima_actualitzacio": None
            }
        }

    def get_sensor_key(self, ieee_str):
        for k, v in self.sensors.items():
            if v["ieee"].lower() == ieee_str.lower():
                return k
        # Si és un dispositiu nou no registrat
        new_key = f"sensor_{len(self.sensors) + 1}"
        self.sensors[new_key] = {
            "nom": f"Sensor Zigbee {ieee_str[-5:]}",
            "ieee": ieee_str,
            "temperatura": None, "humitat": None, "lux": None, "presencia": False, "bateria": None,
            "t_max": -100.0, "t_max_hora": None, "t_min": 100.0, "t_min_hora": None,
            "mostres_temp": [], "h_max": 0.0, "h_min": 100.0, "lux_max": 0, "lux_max_hora": None,
            "ultima_actualitzacio": None
        }
        return new_key

    def setup_ram_database(self):
        os.makedirs(RAM_RUN_DIR, exist_ok=True)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        if os.path.exists(FLASH_DB_BACKUP) and not os.path.exists(RAM_DB_PATH):
            try:
                shutil.copy2(FLASH_DB_BACKUP, RAM_DB_PATH)
                log.info("Còpia de seguretat de la xarxa Zigbee carregada a la RAM (%s)", RAM_DB_PATH)
            except Exception as e:
                log.error("Error copiant BD a RAM: %s", e)
        elif os.path.exists("/data/caseta-guardian/zigbee.db") and not os.path.exists(RAM_DB_PATH):
            try:
                shutil.copy2("/data/caseta-guardian/zigbee.db", RAM_DB_PATH)
                log.info("Migrada BD inicial a la RAM (%s)", RAM_DB_PATH)
            except Exception as e:
                log.error("Error migrant BD inicial a RAM: %s", e)

        self.seed_from_database()

    def seed_from_database(self):
        if not os.path.exists(RAM_DB_PATH):
            return
        try:
            conn = sqlite3.connect(RAM_DB_PATH)
            c = conn.cursor()
            rows = c.execute("SELECT ieee, cluster_id, attr_id, value FROM attributes_cache_v15").fetchall()
            now_h = datetime.datetime.now().strftime("%H:%M")
            now_full = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for ieee, cid, aid, val in rows:
                if str(ieee) == "00:12:4b:00:30:db:ef:c5":
                    continue
                s_key = self.get_sensor_key(str(ieee))
                s = self.sensors[s_key]
                s["ultima_actualitzacio"] = now_full
                if cid == 1026 and aid == 0:  # Temp
                    t = val / 100.0
                    s["temperatura"] = t
                    s["t_max"] = t
                    s["t_min"] = t
                    s["t_max_hora"] = now_h
                    s["t_min_hora"] = now_h
                    s["mostres_temp"].append(t)
                elif cid == 1029 and aid == 0:  # Humitat
                    h = val / 100.0
                    s["humitat"] = h
                    s["h_max"] = h
                    s["h_min"] = h
                elif cid == 1024 and aid == 0:  # Lux
                    lux = round(math.pow(10, (val - 1) / 10000.0), 1) if val > 0 else 0
                    s["lux"] = lux
                    s["lux_max"] = lux
                    s["lux_max_hora"] = now_h
                elif cid == 1 and aid in (32, 33):  # Bateria
                    s["bateria"] = round(val / 2.0) if aid == 33 else val
            conn.close()
            log.info("Telemetria inicial de 2 sensors sembrada amb èxit des de la BD!")
        except Exception as e:
            log.debug("No s'han pogut llegir atributs inicials: %s", e)

    def backup_to_flash(self):
        if os.path.exists(RAM_DB_PATH):
            try:
                shutil.copy2(RAM_DB_PATH, FLASH_DB_BACKUP)
                log.info("💾 Còpia de seguretat de topologia Zigbee persistida a Flash (%s)", FLASH_DB_BACKUP)
            except Exception as e:
                log.error("Error persistint BD a Flash: %s", e)

    def setup_mqtt(self):
        try:
            self.mqtt_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else None,
                client_id="caseta-zigbee-daemon"
            )
            self.mqtt_client.on_connect = self.on_mqtt_connect
            self.mqtt_client.on_message = self.on_mqtt_message
            self.mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            log.info("Connectat al broker MQTT intern FlashMQ (%s:%s)", MQTT_HOST, MQTT_PORT)
            self.publish_clima_telemetry()
        except Exception as e:
            log.warning("No s'ha pogut connectar a FlashMQ: %s", e)

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None):
        client.subscribe("caseta/clima/cmd/#")
        self.publish_clima_telemetry()

    def on_mqtt_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode("utf-8"))
            if topic == "caseta/clima/cmd/permit_join":
                temps = payload.get("seconds", 180)
                if self.app:
                    asyncio.create_task(self.app.permit(min(250, temps)))
                    log.info("Mode d'emparellament activat per %s segons", temps)
        except Exception as e:
            log.error("Error processant missatge MQTT: %s", e)

    def publish_clima_telemetry(self):
        if not self.mqtt_client:
            return
        payload = {
            "sensors": self.sensors,
            # Resum global saló
            "temperatura": self.sensors["sensor_1"]["temperatura"] or self.sensors["sensor_2"]["temperatura"],
            "humitat": self.sensors["sensor_1"]["humitat"] or self.sensors["sensor_2"]["humitat"],
            "lux": self.sensors["sensor_2"]["lux"],
            "presencia": self.sensors["sensor_2"]["presencia"],
            "actualitzat": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.mqtt_client.publish("caseta/clima", json.dumps(payload), retain=True)

    def on_attribute_updated(self, cluster, attr_id, value):
        dev = getattr(getattr(cluster, "endpoint", None), "device", None)
        ieee_str = str(getattr(dev, "ieee", ""))
        s_key = self.get_sensor_key(ieee_str)
        s = self.sensors[s_key]
        
        ara = datetime.datetime.now()
        hora_str = ara.strftime("%H:%M")
        s["ultima_actualitzacio"] = ara.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Temperatura (0x0402)
        if cluster.cluster_id == 0x0402 and attr_id == 0:
            temp_c = round(value / 100.0, 2)
            s["temperatura"] = temp_c
            s["mostres_temp"].append(temp_c)
            if len(s["mostres_temp"]) > 1440:
                s["mostres_temp"].pop(0)

            if temp_c > s["t_max"]:
                s["t_max"] = temp_c
                s["t_max_hora"] = hora_str
            if temp_c < s["t_min"]:
                s["t_min"] = temp_c
                s["t_min_hora"] = hora_str
            self.publish_clima_telemetry()

        # 2. Humitat (0x0405)
        elif cluster.cluster_id == 0x0405 and attr_id == 0:
            hum_pct = round(value / 100.0, 1)
            s["humitat"] = hum_pct
            if hum_pct > s["h_max"]:
                s["h_max"] = hum_pct
            if hum_pct < s["h_min"]:
                s["h_min"] = hum_pct
            self.publish_clima_telemetry()

        # 3. Lux (0x0400)
        elif cluster.cluster_id == 0x0400 and attr_id == 0:
            lux = round(math.pow(10, (value - 1) / 10000.0), 1) if value > 0 else 0
            s["lux"] = lux
            if lux > s.get("lux_max", 0):
                s["lux_max"] = lux
                s["lux_max_hora"] = hora_str
            self.publish_clima_telemetry()

        # 4. Presència (0x0406 / 0x0500)
        elif cluster.cluster_id in (0x0406, 0x0500) and attr_id in (0, 2):
            s["presencia"] = bool(value & 1)
            self.publish_clima_telemetry()

        # 5. Pila (0x0001)
        elif cluster.cluster_id == 0x0001 and attr_id == 0x0021:
            s["bateria"] = round(value / 2.0)
            self.publish_clima_telemetry()

    def check_midnight_rollup(self):
        ara = datetime.datetime.now()
        dia_actual = ara.strftime("%Y-%m-%d")
        if ara.hour == 23 and ara.minute >= 58 and self.last_saved_day != dia_actual:
            rollup = {
                "data": dia_actual,
                "sensors": {}
            }
            for k, s in self.sensors.items():
                if s["temperatura"] is not None:
                    rollup["sensors"][k] = {
                        "nom": s["nom"],
                        "t_min": s["t_min"], "t_min_hora": s["t_min_hora"],
                        "t_max": s["t_max"], "t_max_hora": s["t_max_hora"],
                        "t_avg": round(sum(s["mostres_temp"]) / len(s["mostres_temp"]), 2) if s["mostres_temp"] else None,
                        "h_min": s["h_min"], "h_max": s["h_max"],
                        "lux_max": s.get("lux_max"), "lux_max_hora": s.get("lux_max_hora")
                    }
                    # Reset
                    s["t_max"] = s["temperatura"]
                    s["t_max_hora"] = ara.strftime("%H:%M")
                    s["t_min"] = s["temperatura"]
                    s["t_min_hora"] = ara.strftime("%H:%M")
                    s["mostres_temp"] = [s["temperatura"]]
            try:
                with open(HISTORY_CLIMA_FILE, "a") as f:
                    f.write(json.dumps(rollup) + "\n")
                log.info("💾 Resum diari de clima (2 sensors) persistit a Flash (%s)", dia_actual)
                self.last_saved_day = dia_actual
            except Exception as e:
                log.error("Error guardant resum diari de clima: %s", e)

    def on_device_joined(self, device):
        log.info(f"🎉 Nou dispositiu detectat: {device.ieee} (NWK: {hex(device.nwk)})")

    def on_device_initialized(self, device):
        log.info(f"✅ Dispositiu emparellat i inicialitzat: {device.ieee} ({device.manufacturer} - {device.model})")
        self.backup_to_flash()

class ListenerProxy:
    def __init__(self, manager):
        self.mgr = manager

    def device_joined(self, device):
        self.mgr.on_device_joined(device)

    def raw_device_initialized(self, device):
        self.mgr.on_device_initialized(device)

    def attribute_updated(self, cluster, attr_id, value):
        self.mgr.on_attribute_updated(cluster, attr_id, value)

async def main():
    manager = ZigbeeManager()
    manager.setup_ram_database()
    manager.setup_mqtt()

    config = {
        "device": {
            "path": SERIAL_PORT,
            "baudrate": 115200
        },
        "database_path": RAM_DB_PATH
    }

    log.info(f"Iniciant xarxa Zigbee en memòria RAM ({RAM_DB_PATH})...")
    app = await zigpy_znp.zigbee.application.ControllerApplication.new(
        config=config,
        auto_form=True,
        start_radio=True
    )
    manager.app = app
    listener = ListenerProxy(manager)
    app.add_listener(listener)

    for dev in app.devices.values():
        dev.add_listener(listener)
        for ep in dev.endpoints.values():
            if hasattr(ep, "in_clusters"):
                for cl in ep.in_clusters.values():
                    cl.add_listener(listener)

    log.info(f"🟢 Dimoni Zigbee Natiu 100% operatiu a la RAM! (Dispositius coneguts: {len(app.devices)})")

    while manager.running:
        manager.check_midnight_rollup()
        await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Aturant Dimoni Zigbee de forma neta...")

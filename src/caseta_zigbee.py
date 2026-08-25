#!/usr/bin/env python3
"""
Caseta Zigbee - Dimoni Natiu de Telemetria i Domòtica Zigbee en Memòria RAM
100% Python Pur (sense Node.js ni Node-RED) per a Cerbo GX (Venus OS).

Filosofia Zero Desgast de Disc:
- Base de dades activa en RAM (tmpfs a /run/caseta-zigbee/zigbee.db).
- Lectura inicial d'atributs històrics de la BD per sembrar la telemetria.
- Publicació instantània de telemetria i estadístiques a FlashMQ (caseta/clima).
- Resum diari compacte (1 sola línia JSON a les 23:59h a /data/caseta-guardian/history/historic_clima.jsonl).
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
        self.stats = {
            "salo": {
                "temperatura": None,
                "humitat": None,
                "lux": None,
                "presencia": False,
                "bateria_temp": None,
                "bateria_multi": None,
                "t_max": -100.0, "t_max_hora": None,
                "t_min": 100.0,  "t_min_hora": None,
                "mostres_temp": [],
                "h_max": 0.0,    "h_min": 100.0,
                "lux_max": 0,    "lux_max_hora": None,
                "minuts_presencia": 0,
                "ultima_actualitzacio": None
            }
        }

    def setup_ram_database(self):
        """Prepara la base de dades en memòria RAM (tmpfs) per protegir la Flash."""
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

        # Sembrem la telemetria amb les últimes dades conegudes
        self.seed_from_database()

    def seed_from_database(self):
        """Llegeix l'últim estat dels sensors a la base de dades per no començar a cegues."""
        if not os.path.exists(RAM_DB_PATH):
            return
        try:
            conn = sqlite3.connect(RAM_DB_PATH)
            c = conn.cursor()
            rows = c.execute("SELECT cluster_id, attr_id, value FROM attributes_cache_v15").fetchall()
            salo = self.stats["salo"]
            for cid, aid, val in rows:
                if cid == 1026 and aid == 0:  # Temp
                    t = val / 100.0
                    salo["temperatura"] = t
                    salo["t_max"] = t
                    salo["t_min"] = t
                    salo["t_max_hora"] = datetime.datetime.now().strftime("%H:%M")
                    salo["t_min_hora"] = datetime.datetime.now().strftime("%H:%M")
                    salo["mostres_temp"].append(t)
                elif cid == 1029 and aid == 0:  # Humitat
                    h = val / 100.0
                    salo["humitat"] = h
                    salo["h_max"] = h
                    salo["h_min"] = h
                elif cid == 1024 and aid == 0:  # Lux
                    lux = round(math.pow(10, (val - 1) / 10000.0), 1) if val > 0 else 0
                    salo["lux"] = lux
                    salo["lux_max"] = lux
                    salo["lux_max_hora"] = datetime.datetime.now().strftime("%H:%M")
            conn.close()
            salo["ultima_actualitzacio"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info(f"Telemetria inicial sembrada de la BD: {salo['temperatura']} ºC, {salo['humitat']} %, {salo['lux']} Lux")
        except Exception as e:
            log.debug("No s'han pogut llegir atributs inicials: %s", e)

    def backup_to_flash(self):
        """Guarda una còpia de la topologia de xarxa a Flash (només en emparellar)."""
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
        salo = self.stats["salo"]
        payload = {
            "temperatura": salo["temperatura"],
            "humitat": salo["humitat"],
            "lux": salo["lux"],
            "presencia": salo["presencia"],
            "bateria_temp": salo["bateria_temp"],
            "bateria_multi": salo["bateria_multi"],
            "t_max": salo["t_max"] if salo["t_max"] > -50 else None,
            "t_max_hora": salo["t_max_hora"],
            "t_min": salo["t_min"] if salo["t_min"] < 50 else None,
            "t_min_hora": salo["t_min_hora"],
            "t_avg": round(sum(salo["mostres_temp"]) / len(salo["mostres_temp"]), 2) if salo["mostres_temp"] else None,
            "h_max": salo["h_max"],
            "h_min": salo["h_min"],
            "lux_max": salo["lux_max"],
            "lux_max_hora": salo["lux_max_hora"],
            "actualitzat": salo["ultima_actualitzacio"]
        }
        self.mqtt_client.publish("caseta/clima", json.dumps(payload), retain=True)

    def on_attribute_updated(self, cluster, attr_id, value):
        salo = self.stats["salo"]
        ara = datetime.datetime.now()
        hora_str = ara.strftime("%H:%M")
        salo["ultima_actualitzacio"] = ara.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Mesura de Temperatura (0x0402)
        if cluster.cluster_id == 0x0402 and attr_id == 0:
            temp_c = round(value / 100.0, 2)
            salo["temperatura"] = temp_c
            salo["mostres_temp"].append(temp_c)
            if len(salo["mostres_temp"]) > 1440:
                salo["mostres_temp"].pop(0)

            if temp_c > salo["t_max"]:
                salo["t_max"] = temp_c
                salo["t_max_hora"] = hora_str
            if temp_c < salo["t_min"]:
                salo["t_min"] = temp_c
                salo["t_min_hora"] = hora_str
            self.publish_clima_telemetry()

        # 2. Mesura d'Humitat Relativa (0x0405)
        elif cluster.cluster_id == 0x0405 and attr_id == 0:
            hum_pct = round(value / 100.0, 1)
            salo["humitat"] = hum_pct
            if hum_pct > salo["h_max"]:
                salo["h_max"] = hum_pct
            if hum_pct < salo["h_min"]:
                salo["h_min"] = hum_pct
            self.publish_clima_telemetry()

        # 3. Mesura d'Il·luminació en Lux (0x0400)
        elif cluster.cluster_id == 0x0400 and attr_id == 0:
            lux = round(math.pow(10, (value - 1) / 10000.0), 1) if value > 0 else 0
            salo["lux"] = lux
            if lux > salo["lux_max"]:
                salo["lux_max"] = lux
                salo["lux_max_hora"] = hora_str
            self.publish_clima_telemetry()

        # 4. Ocupació / Presència (0x0406 / 0x0500)
        elif cluster.cluster_id in (0x0406, 0x0500) and attr_id in (0, 2):
            salo["presencia"] = bool(value & 1)
            self.publish_clima_telemetry()

        # 5. Nivell de Pila / Bateria (0x0001)
        elif cluster.cluster_id == 0x0001 and attr_id == 0x0021:
            batt_pct = round(value / 2.0)
            if getattr(getattr(getattr(cluster, "endpoint", None), "device", None), "model", "") == "TS0201":
                salo["bateria_temp"] = batt_pct
            else:
                salo["bateria_multi"] = batt_pct
            self.publish_clima_telemetry()

    def check_midnight_rollup(self):
        """A les 23:59h, escriu una sola línia JSON resum al disc eMMC."""
        ara = datetime.datetime.now()
        dia_actual = ara.strftime("%Y-%m-%d")
        if ara.hour == 23 and ara.minute >= 58 and self.last_saved_day != dia_actual:
            salo = self.stats["salo"]
            if salo["temperatura"] is not None:
                rollup = {
                    "data": dia_actual,
                    "t_min": salo["t_min"],
                    "t_min_hora": salo["t_min_hora"],
                    "t_max": salo["t_max"],
                    "t_max_hora": salo["t_max_hora"],
                    "t_avg": round(sum(salo["mostres_temp"]) / len(salo["mostres_temp"]), 2) if salo["mostres_temp"] else None,
                    "h_min": salo["h_min"],
                    "h_max": salo["h_max"],
                    "lux_max": salo["lux_max"],
                    "lux_max_hora": salo["lux_max_hora"]
                }
                try:
                    with open(HISTORY_CLIMA_FILE, "a") as f:
                        f.write(json.dumps(rollup) + "\n")
                    log.info("💾 Resum diari de clima persistit amb èxit a Flash (%s)", dia_actual)
                    self.last_saved_day = dia_actual
                    
                    # Reset per al nou dia
                    salo["t_max"] = salo["temperatura"]
                    salo["t_max_hora"] = ara.strftime("%H:%M")
                    salo["t_min"] = salo["temperatura"]
                    salo["t_min_hora"] = ara.strftime("%H:%M")
                    salo["mostres_temp"] = [salo["temperatura"]]
                except Exception as e:
                    log.error("Error guardant resum diari de clima: %s", e)

    def on_device_joined(self, device):
        log.info("🎉 Nou dispositiu detectat: %s (NWK: %s)", device.ieee, hex(device.nwk))

    def on_device_initialized(self, device):
        log.info("✅ Dispositiu emparellat i inicialitzat: %s (%s - %s)", device.ieee, device.manufacturer, device.model)
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

    log.info("Iniciant xarxa Zigbee en memòria RAM (%s)...", RAM_DB_PATH)
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

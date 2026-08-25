#!/usr/bin/env python3
"""
Caseta Zigbee - Dimoni Natiu de Telemetria i Domòtica Zigbee en Memòria RAM
Suport complet per a múltiples sensors (Termohigròmetre + Multisensor 4-en-1).

Filosofia Zero Desgast de Disc:
- Base de dades activa en RAM (tmpfs a /run/caseta-zigbee/zigbee.db).
- Captura completa d'atributs ZCL estàndard, ordres IAS Zone (presència) i Tuya DP mitjançant ClusterListener wrappers.
- Auto-vinculació de nous dispositius en calent.
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
import zoneinfo
import paho.mqtt.client as mqtt
import zigpy_znp.zigbee.application

TZ_MADRID = zoneinfo.ZoneInfo("Europe/Madrid")

def now_madrid():
    return datetime.datetime.now(TZ_MADRID)

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
        self.bound_clusters = set()
        self.sensors = {
            "sensor_1": {
                "nom": "Habitació xiquets (TS0201)",
                "ieee": "a4:c1:38:8c:5b:70:28:4f",
                "temperatura": None,
                "humitat": None,
                "bateria": None,
                "t_max": -100.0, "t_max_hora": None,
                "t_min": 100.0,  "t_min_hora": None,
                "temp_sum": 0.0, "temp_count": 0,
                "h_max": 0.0,    "h_min": 100.0,
                "ultima_actualitzacio": None
            },
            "sensor_2": {
                "nom": "Saló (ZG-204ZV)",
                "ieee": "a4:c1:38:a6:3a:59:a0:35",
                "temperatura": None,
                "humitat": None,
                "lux": None,
                "presencia": False,
                "bateria": None,
                "t_max": -100.0, "t_max_hora": None,
                "t_min": 100.0,  "t_min_hora": None,
                "temp_sum": 0.0, "temp_count": 0,
                "h_max": 0.0,    "h_min": 100.0,
                "lux_max": 0,    "lux_max_hora": None,
                "ultima_actualitzacio": None
            }
        }

    def get_sensor_key(self, ieee_str):
        for k, v in self.sensors.items():
            if v["ieee"].lower() == ieee_str.lower():
                return k
        new_key = f"sensor_{len(self.sensors) + 1}"
        self.sensors[new_key] = {
            "nom": f"Sensor {ieee_str[-5:]}",
            "ieee": ieee_str,
            "temperatura": None, "humitat": None, "lux": None, "presencia": False, "bateria": None,
            "t_max": -100.0, "t_max_hora": None, "t_min": 100.0, "t_min_hora": None,
            "temp_sum": 0.0, "temp_count": 0, "h_max": 0.0, "h_min": 100.0, "lux_max": 0, "lux_max_hora": None,
            "ultima_actualitzacio": None
        }
        return new_key

    def setup_ram_database(self):
        os.makedirs(RAM_RUN_DIR, exist_ok=True)
        os.makedirs(HISTORY_DIR, exist_ok=True)
        self.seed_from_database()

    def seed_from_database(self):
        if not os.path.exists(RAM_DB_PATH):
            return
        try:
            conn = sqlite3.connect(RAM_DB_PATH)
            c = conn.cursor()
            rows = c.execute("SELECT ieee, cluster_id, attr_id, value FROM attributes_cache_v15").fetchall()
            now_h = now_madrid().strftime("%H:%M")
            now_full = now_madrid().strftime("%Y-%m-%d %H:%M:%S")
            for ieee, cid, aid, val in rows:
                if str(ieee) == "00:12:4b:00:30:db:ef:c5":
                    continue
                s_key = self.get_sensor_key(str(ieee))
                s = self.sensors[s_key]
                s["ultima_actualitzacio"] = now_full
                if cid == 1026 and aid == 0:  # Temp
                    t = self.parse_temperature(val)
                    if t is not None:
                        s["temperatura"] = t
                        s["t_max"] = t
                        s["t_min"] = t
                        s["t_max_hora"] = now_h
                        s["t_min_hora"] = now_h
                        s["temp_sum"] = t
                        s["temp_count"] = 1
                elif cid == 1029 and aid == 0:  # Humitat
                    h = self.parse_humidity(val)
                    if h is not None:
                        s["humitat"] = h
                        s["h_max"] = h
                        s["h_min"] = h
                elif cid == 1024 and aid == 0:  # Lux
                    s["lux"] = self.parse_lux(val)
                elif cid == 1 and aid in (32, 33):  # Bateria
                    if aid == 33:
                        s["bateria"] = round(val / 2.0)
            conn.close()
            log.info("Telemetria inicial sembrada des de la BD SQLite de la RAM!")
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
                    log.info(f"Mode d'emparellament activat per {temps} segons")
        except Exception as e:
            log.error("Error processant missatge MQTT: %s", e)

    def publish_clima_telemetry(self):
        if not self.mqtt_client:
            return
        payload = {
            "sensors": self.sensors,
            "temperatura": self.sensors["sensor_2"]["temperatura"] or self.sensors["sensor_1"]["temperatura"],
            "humitat": self.sensors["sensor_2"]["humitat"] or self.sensors["sensor_1"]["humitat"],
            "lux": self.sensors["sensor_2"]["lux"],
            "presencia": self.sensors["sensor_2"]["presencia"],
            "actualitzat": now_madrid().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.mqtt_client.publish("caseta/clima", json.dumps(payload), retain=True)

    def parse_temperature(self, raw_val):
        try:
            val = float(raw_val)
            if val > 500:
                t = val / 100.0
            elif val > 60:
                t = val / 10.0
            elif 5 <= val <= 55:
                t = val
            elif val < 5:
                return None
            else:
                t = val / 100.0
            return round(t, 2)
        except Exception:
            return None

    def parse_humidity(self, raw_val):
        try:
            val = float(raw_val)
            if val > 100:
                h = val / 100.0
            else:
                h = val
            return round(h, 1) if 0 <= h <= 100 else None
        except Exception:
            return None

    def parse_lux(self, raw_val):
        try:
            val = float(raw_val)
            if val > 1:
                return round(math.pow(10, (val - 1) / 10000.0), 1)
            return 0.0
        except Exception:
            return 0.0

    def attach_device_listeners(self, dev):
        if not dev:
            return
        try:
            ieee_str = str(dev.ieee)
            for ep in dev.endpoints.values():
                for cl_dict in (getattr(ep, "in_clusters", {}), getattr(ep, "out_clusters", {})):
                    for cid, cl in cl_dict.items():
                        bind_key = (ieee_str, getattr(ep, "endpoint_id", 1), cid)
                        if bind_key not in self.bound_clusters:
                            cl.add_listener(ClusterListener(self, cl))
                            self.bound_clusters.add(bind_key)
            log.info(f"Listeners vinculats als clústers del dispositiu {dev.ieee} ({getattr(dev, 'model', 'Device')})")
        except Exception as e:
            log.error("Error vinculant listeners: %s", e)

    def parse_tuya_dp(self, s, raw_bytes):
        """Descodifica la trama de Datapoints privats de Tuya (0xEF00)."""
        try:
            if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) < 6:
                return
            idx = 2  # Salta seq (2 bytes)
            while idx + 4 <= len(raw_bytes):
                dp_id = raw_bytes[idx]
                dp_type = raw_bytes[idx + 1]
                dp_len = (raw_bytes[idx + 2] << 8) | raw_bytes[idx + 3]
                idx += 4
                if idx + dp_len > len(raw_bytes):
                    break
                val_bytes = raw_bytes[idx : idx + dp_len]
                idx += dp_len

                # DP 1: Presència (Bool/Enum)
                if dp_id == 1 and dp_len >= 1:
                    is_pres = bool(val_bytes[0])
                    s["presencia"] = is_pres
                    log.info(f"🚶 Tuya DP1 Presència ({s['nom']}): {'🚶 DETECTADA' if is_pres else '🟢 REPOS'}")

                # DP 106 / 12 / 101: Il·luminació (Lux)
                elif dp_id in (106, 12, 101) and dp_len >= 1:
                    lux_val = int.from_bytes(val_bytes, "big")
                    s["lux"] = float(lux_val)
                    if lux_val > s.get("lux_max", 0):
                        s["lux_max"] = lux_val
                        s["lux_max_hora"] = now_madrid().strftime("%H:%M")
                    log.info(f"☀️ Tuya DP{dp_id} Lux ({s['nom']}): {lux_val} Lux")

                # DP 2 / 102: Temperatura (0.1 ºC o 0.01 ºC)
                elif dp_id in (2, 102) and dp_len >= 1:
                    raw_t = int.from_bytes(val_bytes, "big")
                    t = self.parse_temperature(raw_t)
                    if t is not None:
                        s["temperatura"] = t
                        log.info(f"🌡️ Tuya DP{dp_id} Temp ({s['nom']}): {t} ºC")

                # DP 3 / 103: Humitat (%)
                elif dp_id in (3, 103) and dp_len >= 1:
                    raw_h = int.from_bytes(val_bytes, "big")
                    h = self.parse_humidity(raw_h)
                    if h is not None:
                        s["humitat"] = h
                        log.info(f"💧 Tuya DP{dp_id} Hum ({s['nom']}): {h} %")
            self.publish_clima_telemetry()
        except Exception as e:
            log.debug("Error descodificant Tuya DP: %s", e)

    def on_cluster_command(self, cluster, tsn, command_id, args):
        dev = getattr(getattr(cluster, "endpoint", None), "device", None)
        ieee_str = str(getattr(dev, "ieee", ""))
        s_key = self.get_sensor_key(ieee_str)
        s = self.sensors[s_key]
        
        ara = now_madrid()
        s["ultima_actualitzacio"] = ara.strftime("%Y-%m-%d %H:%M:%S")

        # IAS Zone (0x0500) - Zone Status Change Notification (command_id == 0)
        if cluster.cluster_id == 0x0500:
            try:
                status = args[0] if len(args) > 0 else 0
                is_presence = bool(status & 1)
                s["presencia"] = is_presence
                log.info(f"🚶 Canvi d'estat de presència ({s['nom']}): {'🚶 DETECTADA' if is_presence else '🟢 REPOS'}")
                self.publish_clima_telemetry()
            except Exception as e:
                log.debug("Error descodificant IAS Zone command: %s", e)

        # Tuya Private Cluster (0xEF00)
        elif cluster.cluster_id == 0xEF00:
            if args:
                raw_payload = args[0] if isinstance(args[0], (bytes, bytearray)) else (args if isinstance(args, (bytes, bytearray)) else None)
                if raw_payload:
                    self.parse_tuya_dp(s, raw_payload)

    def on_attribute_updated(self, cluster, attr_id, value):
        dev = getattr(getattr(cluster, "endpoint", None), "device", None)
        ieee_str = str(getattr(dev, "ieee", ""))
        s_key = self.get_sensor_key(ieee_str)
        s = self.sensors[s_key]
        
        ara = now_madrid()
        hora_str = ara.strftime("%H:%M")
        s["ultima_actualitzacio"] = ara.strftime("%Y-%m-%d %H:%M:%S")

        # 1. Mesura de Temperatura (0x0402)
        if cluster.cluster_id == 0x0402 and attr_id == 0:
            temp_c = self.parse_temperature(value)
            if temp_c is not None:
                s["temperatura"] = temp_c
                s["temp_sum"] += temp_c
                s["temp_count"] += 1

                if temp_c > s["t_max"]:
                    s["t_max"] = temp_c
                    s["t_max_hora"] = hora_str
                if temp_c < s["t_min"]:
                    s["t_min"] = temp_c
                    s["t_min_hora"] = hora_str
                log.info(f"🌡️ Nova temperatura ({s['nom']}): {temp_c} ºC")
                self.publish_clima_telemetry()

        # 2. Mesura d'Humitat Relativa (0x0405)
        elif cluster.cluster_id == 0x0405 and attr_id == 0:
            hum_pct = self.parse_humidity(value)
            if hum_pct is not None:
                s["humitat"] = hum_pct
                if hum_pct > s["h_max"]:
                    s["h_max"] = hum_pct
                if hum_pct < s["h_min"]:
                    s["h_min"] = hum_pct
                log.info(f"💧 Nova humitat ({s['nom']}): {hum_pct} %")
                self.publish_clima_telemetry()

        # 3. Mesura d'Il·luminació (0x0400)
        elif cluster.cluster_id == 0x0400 and attr_id == 0:
            lux = self.parse_lux(value)
            s["lux"] = lux
            if lux > s.get("lux_max", 0):
                s["lux_max"] = lux
                s["lux_max_hora"] = hora_str
            log.info(f"☀️ Nova il·luminació ({s['nom']}): {lux} Lux (cru: {value})")
            self.publish_clima_telemetry()

        # 4. Presència / Moviment (0x0406 / 0x0500)
        elif cluster.cluster_id in (0x0406, 0x0500) and attr_id in (0, 2):
            s["presencia"] = bool(value & 1)
            log.info(f"🚶 Presència per atribut ({s['nom']}): {s['presencia']}")
            self.publish_clima_telemetry()

        # 5. Nivell de Pila / Bateria (0x0001)
        elif cluster.cluster_id == 0x0001:
            if attr_id == 0x0021:  # Battery percentage
                s["bateria"] = round(value / 2.0)
                self.publish_clima_telemetry()
            elif attr_id == 0x0020: # Voltage in decivolts
                volt = value / 10.0
                pct = max(0, min(100, round((volt - 2.0) / 1.0 * 100)))
                if s["bateria"] is None:
                    s["bateria"] = pct
                self.publish_clima_telemetry()

    def check_midnight_rollup(self):
        ara = now_madrid()
        dia_actual = ara.strftime("%Y-%m-%d")
        if ara.hour == 23 and ara.minute >= 58 and self.last_saved_day != dia_actual:
            rollup = {
                "data": dia_actual,
                "sensors": {}
            }
            for k, s in self.sensors.items():
                if s["temperatura"] is not None:
                    avg_t = round(s["temp_sum"] / s["temp_count"], 2) if s["temp_count"] > 0 else s["temperatura"]
                    rollup["sensors"][k] = {
                        "nom": s["nom"],
                        "t_min": s["t_min"], "t_min_hora": s["t_min_hora"],
                        "t_max": s["t_max"], "t_max_hora": s["t_max_hora"],
                        "t_avg": avg_t,
                        "h_min": s["h_min"], "h_max": s["h_max"],
                        "lux_max": s.get("lux_max"), "lux_max_hora": s.get("lux_max_hora")
                    }
                    s["t_max"] = s["temperatura"]
                    s["t_max_hora"] = ara.strftime("%H:%M")
                    s["t_min"] = s["temperatura"]
                    s["t_min_hora"] = ara.strftime("%H:%M")
                    s["temp_sum"] = s["temperatura"]
                    s["temp_count"] = 1
            try:
                with open(HISTORY_CLIMA_FILE, "a") as f:
                    f.write(json.dumps(rollup) + "\n")
                log.info("💾 Resum diari de clima (2 sensors) persistit a Flash (%s)", dia_actual)
                self.last_saved_day = dia_actual
            except Exception as e:
                log.error("Error guardant resum diari de clima: %s", e)

    def attach_all_devices(self):
        if not self.app:
            return
        for dev in self.app.devices.values():
            self.attach_device_listeners(dev)

    def on_device_joined(self, device):
        log.info(f"🎉 Nou dispositiu detectat: {device.ieee} (NWK: {hex(device.nwk)})")
        self.attach_device_listeners(device)

    def on_device_initialized(self, device):
        log.info(f"✅ Dispositiu emparellat i inicialitzat: {device.ieee} ({getattr(device, 'manufacturer', '')} - {getattr(device, 'model', '')})")
        self.attach_device_listeners(device)
        self.backup_to_flash()

class ClusterListener:
    def __init__(self, manager, cluster):
        self.mgr = manager
        self.cluster = cluster

    def attribute_updated(self, attr_id, value, *args, **kwargs):
        self.mgr.on_attribute_updated(self.cluster, attr_id, value)

    def cluster_command(self, tsn, command_id, args, *extra_args, **kwargs):
        self.mgr.on_cluster_command(self.cluster, tsn, command_id, args)

    def zdo_command(self, *args, **kwargs):
        pass

class AppListener:
    def __init__(self, manager):
        self.mgr = manager

    def device_joined(self, device):
        self.mgr.on_device_joined(device)

    def raw_device_initialized(self, device):
        self.mgr.on_device_initialized(device)

    def device_initialized(self, device):
        self.mgr.on_device_initialized(device)

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
    app.add_listener(AppListener(manager))

    for dev in app.devices.values():
        manager.attach_device_listeners(dev)

    manager.publish_clima_telemetry()
    log.info(f"🟢 Dimoni Zigbee Natiu 100% operatiu a la RAM! (Dispositius coneguts: {len(app.devices)})")
    await app.permit(250)
    log.info("📡 Finestra d'emparellament (permit join) oberta automàticament per 250 segons!")

    while manager.running:
        manager.attach_all_devices()
        manager.check_midnight_rollup()
        await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Aturant Dimoni Zigbee de forma neta...")

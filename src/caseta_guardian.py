#!/usr/bin/env python3
"""
Caseta Guardian - Dimoni Natiu de Gestió Energètica i Protecció de Bateria
Instal·lació Victron ESS (Cerbo GX, MultiPlus-II, Pylontech US3000C, Huawei PV).

JERARQUIA DE PRIORITATS:
1. 🛡️ Salut de la Bateria (Prioritat 0 - Top Balancing nocturn, limitació corrent, escut 65%).
2. 🔌 Resiliència i SAI (No quedar-se sense llum, reserva tarda/vespre 85%, alerta calor 95-100%).
3. 🏝️ Zero Regal (Aïllament a Inverter Only si SoC > 84% & injecció > 100W per 30s).
4. ☀️ Màxim Aprofitament Solar (Ajust de sòl matinal al 70% de 07h a 16h, i pujada a 85% a partir de les 16h).
"""

import datetime
import json
import logging
import math
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Carregar configuració opcional des de config.json o variables d'entorn
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "config.json")
config = {}
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except Exception:
        pass

CERBO_IP = os.environ.get("CERBO_IP", config.get("cerbo_ip", "127.0.0.1" if os.path.exists("/opt/victronenergy") else "192.168.1.100"))
MQTT_PORT = int(os.environ.get("MQTT_PORT", config.get("mqtt_port", 1883)))
PORTAL_ID = os.environ.get("PORTAL_ID", config.get("portal_id", "c0619ab2xxxx"))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", config.get("ntfy_topic", "victron_solar_alerts"))
TUYA_S06_IP = os.environ.get("TUYA_S06_IP", config.get("tuya_s06_ip", "192.168.1.50"))
TUYA_DEV_ID = os.environ.get("TUYA_DEV_ID", config.get("tuya_dev_id", "your_device_id"))
TUYA_LOCAL_KEY = os.environ.get("TUYA_LOCAL_KEY", config.get("tuya_local_key", "your_local_key"))

LATITUDE = float(os.environ.get("LATITUDE", config.get("latitude", 39.0)))
LONGITUDE = float(os.environ.get("LONGITUDE", config.get("longitude", -0.3)))

FORECAST_CACHE_FILE = "/tmp/caseta_forecast_cache.json"
DAILY_STATS_FILE = "/tmp/caseta_daily_stats.json"

# Paràmetres de Bateria & Mode MultiPlus
SOC_ISLANDING_THRESHOLD = 84.0      # SoC mínim per plantejar Mode 2 (Inverter Only)
GRID_EXPORT_THRESHOLD_W = -100.0    # Potència d'injecció (negativa) per considerar abocament
EXPORT_TRIGGER_TIME_S = 30.0        # Temps sostingut d'abocament abans d'aïllar (30s)

BATTERY_MAX_REST_CURRENT_A = 2.0    # La bateria ha d'estar descansant (<2A descàrrega) per aïllar
RECONNECT_MAX_DISCHARGE_A = 15.0    # Si descàrrega > 15A en Mode 2 -> Reconnecta xarxa (5s)
RECONNECT_MAX_DISCHARGE_TIME_S = 5.0
RECONNECT_MIN_SOC = 80.0            # Si SoC <= 80% en Mode 2 -> Reconnecta xarxa

MIN_SWITCH_INTERVAL_S = 300.0       # Mínim 5 minuts entre commutacions de relé de xarxa

# Escuts de Climatització
AC_SHIELD_CUTOFF_SOC = 65.0         # Si SoC <= 65% -> Apaga l'aire automàticament
AC_WARN_SOC = 67.0                  # Si SoC <= 67% -> Envia avís push al mòbil

# Preus Contractats Imagina Energía (CUPS: ES0021000007582432JL - 1.150 kW)
COST_FIX_DIARI = 0.170              # Potència P1 (0.117€) + P3 (0.026€) + Comptador (0.027€)
PREU_P1_PUNTA = 0.177691            # 10-14h i 18-22h L-V
PREU_P2_PLA = 0.103870              # 08-10h, 14-18h, 22-24h L-V
PREU_P3_VALL = 0.069473             # 00-08h L-V, i 24h Caps de Setmana i Festius Nacionals/Autonòmics
FACTOR_IMPOSTOS = 1.1418            # Impost Elèctric 3.8% + IVA 10%

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("CasetaGuardian")


class CasetaGuardian:
    def __init__(self):
        self.portal_id = PORTAL_ID
        self.client = None
        
        # Estat en temps real
        self.soc = 95.0
        self.battery_v = 51.2
        self.battery_i = 0.0
        self.battery_p = 0.0
        self.battery_temp = 29.0
        self.cell_max_v = 3.45
        self.cell_min_v = 3.38
        
        self.grid_p = 0.0
        self.grid_v = 220.0
        self.pv_p = 0.0
        self.pv_energy_forward = None
        self.ac_loads = 200.0
        self.vebus_mode = 3 # 3 = ON, 2 = Inverter Only, 1 = Charger Only, 4 = OFF
        self.cerbo_min_soc = 70.0
        
        # Previsió Solar & Clima
        self.today_kwh_est = 7.0
        self.tomorrow_kwh_est = 7.0
        self.remaining_kwh_today = 5.0
        self.max_temp_today = 32.0
        self.sunset_temp = 28.0
        self.blackout_risk = 20
        self.target_reserve_soc = 85.0
        self.last_applied_min_soc = None
        
        # Comptadors Diaris & Acumulats
        self.current_day_str = time.strftime("%Y-%m-%d")
        self.solar_start_kwh = None
        self.solar_kwh_today = 0.0
        self.consumption_kwh_today = 0.0
        self.grid_import_kwh_today = 0.0
        self.grid_export_kwh_today = 0.0
        self.solar_peak_w = 0.0
        self.grid_cost_energy_today = 0.0
        self.cost_total_today = 0.0
        self.load_daily_stats()
        
        # Comptadors de Temps & Histèresi
        self.last_mode_switch_time = 0.0
        self.last_eval_time = time.time()
        self.export_start_time = None
        self.high_discharge_start_time = None
        self.warn_67_sent = False
        self.ac_cutoff_sent = False
        self.last_openmeteo_fetch = 0.0

    def is_holiday_or_weekend(self, t=None):
        """Determina si una data és cap de setmana o festiu oficial (P3 Vall 24h a Espanya / C. Valenciana)."""
        if t is None:
            t = time.localtime()
        # 1. Cap de setmana (Dissabte=5, Diumenge=6)
        if t.tm_wday >= 5:
            return True
        
        year, month, day = t.tm_year, t.tm_mon, t.tm_mday
        # 2. Festius nacionals i autonòmics oficials
        fixed_holidays = {
            (1, 1),   # Any Nou
            (1, 6),   # Reis
            (3, 19),  # Sant Josep (Comunitat Valenciana)
            (5, 1),   # Festa del Treball
            (6, 24),  # Sant Joan (Comunitat Valenciana)
            (8, 15),  # Assumpció de la Mare de Déu
            (10, 9),  # Dia de la Comunitat Valenciana
            (10, 12), # Festa Nacional d'Espanya
            (11, 1),  # Tots Sants
            (12, 6),  # Dia de la Constitució
            (12, 8),  # Immaculada Concepció
            (12, 25), # Nadal
        }
        if (month, day) in fixed_holidays:
            return True
            
        # 3. Festius mòbils de Pasqua (Divendres Sant i Dilluns de Pasqua)
        a = year % 19
        b = year // 100
        c = year % 100
        d_div = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d_div - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        easter_m = (h + l - 7 * m + 114) // 31
        easter_d = ((h + l - 7 * m + 114) % 31) + 1
        
        easter_date = datetime.date(year, easter_m, easter_d)
        good_friday = easter_date - datetime.timedelta(days=2)
        easter_monday = easter_date + datetime.timedelta(days=1)
        
        cur_date = datetime.date(year, month, day)
        if cur_date in (good_friday, easter_monday):
            return True
            
        return False

    def get_current_kwh_rate(self):
        """Retorna el preu contractat per kWh segons la franja horària, cap de setmana i festius."""
        t = time.localtime()
        if self.is_holiday_or_weekend(t):
            return PREU_P3_VALL
        
        h = t.tm_hour
        if 0 <= h < 8:
            return PREU_P3_VALL
        elif (10 <= h < 14) or (18 <= h < 22):
            return PREU_P1_PUNTA
        else:
            return PREU_P2_PLA

    def load_daily_stats(self):
        """Carrega o inicialitza les estadístiques diàries des de disc."""
        if os.path.exists(DAILY_STATS_FILE):
            try:
                with open(DAILY_STATS_FILE, "r") as f:
                    data = json.load(f)
                if data.get("date") == self.current_day_str:
                    self.solar_start_kwh = data.get("solar_start_kwh")
                    self.solar_kwh_today = data.get("solar_kwh_today", 0.0)
                    self.consumption_kwh_today = data.get("consumption_kwh_today", 0.0)
                    self.grid_import_kwh_today = data.get("grid_import_kwh_today", 0.0)
                    self.grid_export_kwh_today = data.get("grid_export_kwh_today", 0.0)
                    self.solar_peak_w = data.get("solar_peak_w", 0.0)
                    self.grid_cost_energy_today = data.get("grid_cost_energy_today", 0.0)
                    self.cost_total_today = data.get("cost_total_today", round(COST_FIX_DIARI * FACTOR_IMPOSTOS, 2))
            except Exception:
                pass

    def save_daily_stats(self):
        """Desa les estadístiques diàries a disc."""
        today = time.strftime("%Y-%m-%d")
        if today != self.current_day_str:
            # Canvi de dia (00:00h): reinicialitza comptadors per al nou dia
            self.current_day_str = today
            self.solar_start_kwh = self.pv_energy_forward
            self.solar_kwh_today = 0.0
            self.consumption_kwh_today = 0.0
            self.grid_import_kwh_today = 0.0
            self.grid_export_kwh_today = 0.0
            self.solar_peak_w = 0.0
            self.grid_cost_energy_today = 0.0
            self.cost_total_today = round(COST_FIX_DIARI * FACTOR_IMPOSTOS, 2)

        solar_cov = round((self.solar_kwh_today / self.consumption_kwh_today * 100.0), 1) if self.consumption_kwh_today > 0 else 0.0
        subtotal = COST_FIX_DIARI + self.grid_cost_energy_today
        self.cost_total_today = subtotal * FACTOR_IMPOSTOS

        payload = {
            "date": self.current_day_str,
            "solar_start_kwh": self.solar_start_kwh,
            "solar_kwh_today": round(self.solar_kwh_today, 2),
            "consumption_kwh_today": round(self.consumption_kwh_today, 2),
            "grid_import_kwh_today": round(self.grid_import_kwh_today, 2),
            "grid_export_kwh_today": round(self.grid_export_kwh_today, 2),
            "solar_peak_w": round(self.solar_peak_w, 1),
            "solar_coverage_percent": min(100.0, solar_cov),
            "grid_cost_energy_today": round(self.grid_cost_energy_today, 4),
            "cost_total_today": round(self.cost_total_today, 2),
            "is_weekend_or_holiday": self.is_holiday_or_weekend()
        }
        try:
            with open(DAILY_STATS_FILE, "w") as f:
                json.dump(payload, f)
        except Exception:
            pass

    def send_ntfy(self, title: str, message: str, priority: str = "default", tags: str = "battery,warning"):
        """Envia notificació push instantània al mòbil mitjançant ntfy.sh."""
        url = f"https://ntfy.sh/{NTFY_TOPIC}"
        try:
            req = urllib.request.Request(
                url,
                data=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8"),
                    "Priority": priority,
                    "Tags": tags
                },
                method="POST"
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                log.info(f"📱 Notificació enviada al mòbil: {title}")
        except Exception as e:
            log.warning(f"Error enviant notificació ntfy: {e}")

    def send_tuya_ir_power_off(self):
        """Envia ordre infraroja per apagar l'aire condicionat."""
        log.info("❄️ [ESCUT CRÍTIC] Apagant aire condicionat per protegir la reserva de bateria...")
        try:
            import tinytuya
            d = tinytuya.Device(TUYA_DEV_ID, TUYA_S06_IP, TUYA_LOCAL_KEY, version=3.3)
            d.set_status("0", 201)
            log.info("Comanda IR Tuya 'power_off' transmesa amb èxit!")
        except Exception as e:
            log.warning(f"Comanda Tuya (emissor IR): {e}")

    def sync_cerbo_min_soc(self):
        """Sincronitza el Minimum SOC de l'ESS del Cerbo GX segons l'hora, balanç energètic i clima."""
        if not self.client or self.portal_id in ("c0619ab2xxxx", "+", "#"):
            return
        
        current_hour = int(time.strftime("%H"))
        
        # 1. 🌙 Nit Supervall (00:01h a 06:59h): Top-Balancing i 100% de SAI a 7 cts/kWh
        if 0 <= current_hour < 7:
            target = 100.0
            phase_name = "🌙 Nit Supervall (100% Top-Balancing & SAI Màxim)"
            
        # 2. 🔥 Alerta de Calor Extrema / Risc Alt d'Apagada (Risc >= 60%)
        elif self.blackout_risk >= 60:
            target = 95.0
            phase_name = "🚨 Alerta Calor Extrema (95% SAI Blindat)"
            
        # 3. ⚠️ Alerta de Calor Moderada (Risc >= 30%)
        elif self.blackout_risk >= 30:
            target = 85.0
            phase_name = "⚠️ Calor Moderada (85% Reserva Preventiva)"
            
        # 4. ☀️ Franja Diürna (07:00h a 19:59h):
        elif 7 <= current_hour < 20:
            is_afternoon = (current_hour >= 16) or (self.remaining_kwh_today < 2.0)
            
            if is_afternoon:
                target = 85.0
                phase_name = "🌇 Tarda / Vespre Resilient (85% Màxima Seguretat & SAI)"
            elif self.today_kwh_est >= 5.0:
                target = 70.0
                phase_name = "☀️ Dia Radiant (70% per absorbir excedent solar)"
            else:
                target = 85.0
                phase_name = "☁️ Dia Variable / Tarda (85% Coixí Solar)"
                
        # 5. 🌆 Vespre (20:00h a 23:59h): Coixí de transició abans de la nit
        else:
            target = 85.0
            phase_name = "🌆 Vespre (85% Manteniment de SAI)"

        self.target_reserve_soc = target

        if self.last_applied_min_soc != target:
            topic = f"W/{self.portal_id}/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit"
            payload = json.dumps({"value": target})
            self.client.publish(topic, payload)
            log.info(f"⚙️ Sincronitzat Minimum SOC a Cerbo GX: {target:.0f}% [{phase_name}]")
            self.last_applied_min_soc = target

    def update_open_meteo_forecast(self):
        """Consulta Open-Meteo per a radiació i càlcul de risc d'apagada."""
        now = time.time()
        if now - self.last_openmeteo_fetch < 1800 and os.path.exists(FORECAST_CACHE_FILE):
            self.sync_cerbo_min_soc()
            return
        
        self.last_openmeteo_fetch = now
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&hourly=direct_radiation,diffuse_radiation,global_tilted_irradiance,temperature_2m&timezone=auto&forecast_days=2"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CasetaGuardian/1.0"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            rads = hourly.get("global_tilted_irradiance", hourly.get("direct_radiation", []))
            
            today_str = time.strftime("%Y-%m-%d")
            current_hour = int(time.strftime("%H"))
            today_kwh = 0.0
            tomorrow_kwh = 0.0
            remaining_kwh = 0.0
            max_temp = 25.0
            sunset_temp = 25.0
            
            for t, temp, rad in zip(times, temps, rads):
                est_w = min(1350, max(0, (rad / 1000.0) * 1350 * 0.82))
                if t.startswith(today_str):
                    today_kwh += est_w / 1000.0
                    h_int = int(t.split("T")[1][:2])
                    if h_int >= current_hour:
                        remaining_kwh += est_w / 1000.0
                    if temp > max_temp:
                        max_temp = temp
                    if "T21:" in t:
                        sunset_temp = temp
                else:
                    tomorrow_kwh += est_w / 1000.0
            
            self.today_kwh_est = today_kwh
            self.tomorrow_kwh_est = tomorrow_kwh
            self.remaining_kwh_today = remaining_kwh
            self.max_temp_today = max_temp
            self.sunset_temp = sunset_temp
            
            # Càlcul d'Índex de Risc de Tall / Col·lapse Zonal
            risk = 0
            if max_temp >= 38: risk += 40
            elif max_temp >= 35: risk += 25
            elif max_temp >= 32: risk += 10
            
            if sunset_temp >= 31: risk += 40
            elif sunset_temp >= 29: risk += 25
            elif sunset_temp >= 27: risk += 10
            
            if self.grid_v < 195: risk += 20
            elif self.grid_v < 205: risk += 10
            
            self.blackout_risk = min(100, max(0, risk))
            self.sync_cerbo_min_soc()
                
            cache_payload = {
                "timestamp": now,
                "today_kwh": round(today_kwh, 2),
                "tomorrow_kwh": round(tomorrow_kwh, 2),
                "remaining_kwh_today": round(remaining_kwh, 2),
                "max_temp_today": round(max_temp, 1),
                "sunset_temp": round(sunset_temp, 1),
                "blackout_risk": self.blackout_risk,
                "target_reserve_soc": self.target_reserve_soc
            }
            with open(FORECAST_CACHE_FILE, "w") as f:
                json.dump(cache_payload, f)

            log.info(f"📊 Open-Meteo: Sol total = {today_kwh:.1f} kWh (Queden {remaining_kwh:.1f} kWh) | Màx = {max_temp:.1f}ºC | Risc Tall = {self.blackout_risk}% -> Target SoC = {self.target_reserve_soc:.0f}%")
        except Exception as e:
            log.warning(f"Avís consultant Open-Meteo: {e}")

    def set_vebus_mode(self, new_mode: int, reason: str):
        """Canvia el mode del MultiPlus (2 = Inverter Only / Aïllat, 3 = ON / Connectat)."""
        now = time.time()
        if now - self.last_mode_switch_time < MIN_SWITCH_INTERVAL_S:
            remaining = int(MIN_SWITCH_INTERVAL_S - (now - self.last_mode_switch_time))
            log.debug(f"Histèresi de relé activa ({remaining}s restants). Ignorant canvi de mode a {new_mode}.")
            return
        
        if self.vebus_mode == new_mode:
            return
        
        log.info(f"🔄 CANVI DE MODE MULTIPLUS: Mode {self.vebus_mode} -> Mode {new_mode} ({reason})")
        self.last_mode_switch_time = now
        self.vebus_mode = new_mode
        
        if self.client:
            topic = f"W/{self.portal_id}/vebus/276/Mode"
            payload = json.dumps({"value": new_mode})
            self.client.publish(topic, payload)
            
            mode_name = "🏝️ Inverter Only (Mode Aïllat)" if new_mode == 2 else "🔌 ON (Connectat a Xarxa)"
            self.send_ntfy("Canvi de Mode MultiPlus", f"{mode_name}\nMotiu: {reason}", priority="low", tags="electric_plug")

    def evaluate_system(self):
        """Motor principal d'avaluació de prioritats i integració d'energia (Executat cada 2s)."""
        now = time.time()
        dt_s = max(0.5, min(10.0, now - self.last_eval_time))
        self.last_eval_time = now

        # Integració Numèrica d'Energia en kWh i Cost
        kwh_step = (dt_s / 3600.0) / 1000.0
        if self.ac_loads > 0:
            self.consumption_kwh_today += self.ac_loads * kwh_step
        if self.grid_p > 0:
            kwh_imported_step = self.grid_p * kwh_step
            self.grid_import_kwh_today += kwh_imported_step
            self.grid_cost_energy_today += kwh_imported_step * self.get_current_kwh_rate()
        elif self.grid_p < -50:
            self.grid_export_kwh_today += abs(self.grid_p) * kwh_step

        if self.pv_p > self.solar_peak_w:
            self.solar_peak_w = self.pv_p

        # Càlcul de Producció Solar des del comptador Carlo Gavazzi
        if self.pv_energy_forward is not None:
            if self.solar_start_kwh is None:
                self.solar_start_kwh = self.pv_energy_forward
            self.solar_kwh_today = max(0.0, self.pv_energy_forward - self.solar_start_kwh)
        else:
            if self.pv_p > 0:
                self.solar_kwh_today += self.pv_p * kwh_step

        self.save_daily_stats()
        
        # 1. Escut de Seguretat de Climatització (<65% SoC)
        if self.soc <= AC_SHIELD_CUTOFF_SOC:
            if not self.ac_cutoff_sent:
                self.send_tuya_ir_power_off()
                self.send_ntfy("🚨 Apagada d'Emergència Aire", f"Bateria ha baixat al {self.soc:.1f}%. Aire apagat per protegir la reserva intocable.", priority="high", tags="warning,snowflake")
                self.ac_cutoff_sent = True
        elif self.soc > 70.0:
            self.ac_cutoff_sent = False
            
        # 2. Avís Precoç al Mòbil (<=67% SoC)
        if self.soc <= AC_WARN_SOC and not self.warn_67_sent and not self.ac_cutoff_sent:
            self.send_ntfy("🔋 Atenció: Bateria al 67%", f"La bateria està al {self.soc:.1f}%. Si l'aire està encès, s'apagarà automàticament al {AC_SHIELD_CUTOFF_SOC}% per protegir la casa.", priority="default", tags="battery")
            self.warn_67_sent = True
        elif self.soc > 72.0:
            self.warn_67_sent = False
            
        # 3. Protecció Química en Mode Aïllat
        if self.vebus_mode == 2:
            if self.battery_i < -RECONNECT_MAX_DISCHARGE_A:
                if self.high_discharge_start_time is None:
                    self.high_discharge_start_time = now
                elif (now - self.high_discharge_start_time) >= RECONNECT_MAX_DISCHARGE_TIME_S:
                    self.set_vebus_mode(3, f"Descàrrega alta ({abs(self.battery_i):.1f}A > {RECONNECT_MAX_DISCHARGE_A}A per >5s)")
                    self.high_discharge_start_time = None
            else:
                self.high_discharge_start_time = None
                
            if self.soc <= RECONNECT_MIN_SOC:
                self.set_vebus_mode(3, f"Bateria ha arribat al sòl de reserva ({self.soc:.1f}% <= {RECONNECT_MIN_SOC}%)")

        # 4. Zero Regal (Aïllament a Inverter Only)
        if self.vebus_mode == 3:
            is_exporting = (self.grid_p <= GRID_EXPORT_THRESHOLD_W)
            is_battery_full_enough = (self.soc > SOC_ISLANDING_THRESHOLD)
            is_battery_resting = (self.battery_i >= -BATTERY_MAX_REST_CURRENT_A)
            has_solar = (self.pv_p >= 400.0)
            
            if is_exporting and is_battery_full_enough and is_battery_resting and has_solar:
                if self.export_start_time is None:
                    self.export_start_time = now
                    log.info(f"⚠️ Detectat abocament de {abs(self.grid_p):.0f}W amb SoC {self.soc:.1f}%. Iniciant compte enrere de 30s...")
                elif (now - self.export_start_time) >= EXPORT_TRIGGER_TIME_S:
                    self.set_vebus_mode(2, f"Abocament sostingut de {abs(self.grid_p):.0f}W durant >30s amb SoC {self.soc:.1f}%")
                    self.export_start_time = None
            else:
                self.export_start_time = None

    def on_mqtt_message(self, client, userdata, msg):
        """Processa missatges MQTT de telemetria en temps real."""
        topic = msg.topic
        
        # Auto-descobriment del Portal ID
        if self.portal_id in ("c0619ab2xxxx", "+") and topic.startswith("N/"):
            parts = topic.split("/")
            if len(parts) > 1 and parts[1] not in ("+", "#"):
                self.portal_id = parts[1]
                log.info(f"🔍 Auto-descobert Portal ID: {self.portal_id}")

        try:
            payload = json.loads(msg.payload.decode())
            val = payload.get("value")
        except Exception:
            return

        if val is None:
            return

        if topic.endswith("/battery/512/Soc"):
            self.soc = float(val)
        elif topic.endswith("/battery/512/Dc/0/Voltage"):
            self.battery_v = float(val)
        elif topic.endswith("/battery/512/Dc/0/Current"):
            self.battery_i = float(val)
        elif topic.endswith("/battery/512/Dc/0/Power"):
            self.battery_p = float(val)
        elif topic.endswith("/battery/512/Dc/0/Temperature"):
            self.battery_temp = float(val)
        elif topic.endswith("/system/0/Ac/Grid/L1/Power"):
            self.grid_p = float(val)
        elif topic.endswith("/vebus/276/Ac/ActiveIn/L1/V"):
            self.grid_v = float(val)
        elif topic.endswith("/pvinverter/31/Ac/Power"):
            self.pv_p = float(val)
        elif topic.endswith("/pvinverter/31/Ac/Energy/Forward"):
            self.pv_energy_forward = float(val)
        elif topic.endswith("/system/0/Ac/Consumption/L1/Power"):
            self.ac_loads = float(val)
        elif topic.endswith("/vebus/276/Mode"):
            self.vebus_mode = int(val)
        elif topic.endswith("/Settings/CGwacs/BatteryLife/MinimumSocLimit"):
            self.cerbo_min_soc = float(val)

    def run(self):
        """Inicia el dimoni guardià."""
        log.info(f"🚀 Iniciant Caseta Guardian (Cerbo IP: {CERBO_IP})...")
        
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else None)
        self.client.on_message = self.on_mqtt_message
        
        try:
            self.client.connect(CERBO_IP, MQTT_PORT, 10)
        except Exception as e:
            log.error(f"No s'ha pogut connectar al broker MQTT de {CERBO_IP}: {e}")
            return
            
        topics = [
            f"N/+/battery/512/Soc",
            f"N/+/battery/512/Dc/0/Voltage",
            f"N/+/battery/512/Dc/0/Current",
            f"N/+/battery/512/Dc/0/Power",
            f"N/+/battery/512/Dc/0/Temperature",
            f"N/+/system/0/Ac/Grid/L1/Power",
            f"N/+/vebus/276/Ac/ActiveIn/L1/V",
            f"N/+/pvinverter/31/Ac/Power",
            f"N/+/pvinverter/31/Ac/Energy/Forward",
            f"N/+/system/0/Ac/Consumption/L1/Power",
            f"N/+/vebus/276/Mode",
            f"N/+/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit"
        ]
        
        for t in topics:
            self.client.subscribe(t)
            
        self.client.loop_start()
        self.client.publish(f"R/{self.portal_id}/keepalive", "")
        
        self.update_open_meteo_forecast()
        log.info("🛡️ Guardià en línia i vigilant telemetria en directe!")
        
        try:
            while True:
                time.sleep(2.0)
                self.client.publish(f"R/{self.portal_id}/keepalive", "")
                self.update_open_meteo_forecast()
                self.evaluate_system()
        except KeyboardInterrupt:
            log.info("Aturant guardià...")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    guardian = CasetaGuardian()
    guardian.run()

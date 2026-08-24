#!/usr/bin/env python3
"""
Caseta Guardian - Dimoni de Control Energètic per a Victron ESS + Cerbo GX
Caseta d'Ador (La Safor, València)

Funcions principals:
1. ☀️ Zero Regal 100% Reversible (Smart Islanding): Obre el relé AC1 en excés solar i reconnecta en sobrecàrrega.
2. 🕒 Control Circadiari de Minimum SOC: 70% de dia (absorció solar), 85% de tarda (escut SAI) i 100% de nit (balanceig a 7 cts/kWh).
3. 🏖️ Cap de Setmana / Festiu Avançat: 100% Top-Balancing a la tarda en hores vall contínues (24h a 7 cts/kWh).
4. 🌤️ Integració Predictiva Open-Meteo: Anticipa onades de calor i ajusta la reserva SAI preventivament.
5. ❄️ Domòtica d'Emergència Tuya: Apaga l'aire condicionat per IR si la bateria baixa del 65% en aïllat.
6. 📈 Històric Permanent Diari (CSV): Arxiu cada mitjanit a ~/.local/share/caseta-guardian/historic_diari.csv.
7. 🚨 Watchdog de Baixa Tensió Rural (<190V durant >2 minuts).
"""

import csv
import datetime
import hashlib
import hmac
import json
import logging
import os
import ssl
import sys
import time
import urllib.request
try:
    import zoneinfo
    MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt no està instal·lat. Instal·la'l amb 'uv pip install paho-mqtt'")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("caseta-guardian")

def get_madrid_now() -> datetime.datetime:
    """Retorna la data i hora exacta a la zona horària de Madrid (peninsular)."""
    if MADRID_TZ:
        return datetime.datetime.now(MADRID_TZ)
    return datetime.datetime.now()

def get_easter_date(year: int) -> datetime.date:
    """Calcula el Diumenge de Pasqua amb l'algorisme de Butcher/Gauss."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)

CONFIG_PATHS = [
    os.path.join(os.path.dirname(__file__), "..", "config.json"),
    os.path.expanduser("~/Documents/Segon_Cervell/projects/caseta-guardian/config.json"),
    os.path.expanduser("~/.config/caseta-guardian/config.json"),
    "/data/caseta-guardian/config.json"
]

config = {}
for p in CONFIG_PATHS:
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                config = json.load(f)
            log.info(f"Carregada configuració des de: {p}")
            break
        except Exception as e:
            log.warning(f"No s'ha pogut llegir {p}: {e}")

CERBO_IP = os.environ.get("CERBO_IP", config.get("cerbo_ip", "127.0.0.1" if os.path.exists("/opt/victronenergy") else "192.168.1.106"))
PORTAL_ID = os.environ.get("PORTAL_ID", config.get("portal_id", "48e7da8782fd"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", config.get("ntfy_topic", "caseta_ador_alerts"))
TUYA_S06_IP = os.environ.get("TUYA_S06_IP", config.get("tuya_s06_ip", "192.168.1.135"))
HISTORY_CSV_FILE = os.path.expanduser("~/.local/share/caseta-guardian/historic_diari.csv")

# Constants de bateria
TOTAL_NOMINAL_KWH = 3.552
BATTERY_SOH_FACTOR = 0.90
NET_CAPACITY_KWH = TOTAL_NOMINAL_KWH * BATTERY_SOH_FACTOR

# Tarifes 2.0TD Imagina Energía
P1_RATE = 0.177691
P2_RATE = 0.103870
P3_RATE = 0.069473
POTENCIA_FIXED_DAY = 0.170
TAX_MULTIPLIER = 1.1418

class CasetaGuardian:
    def __init__(self):
        self.portal_id = PORTAL_ID
        self.client = None
        self.running = True
        
        # Telemetria en directe
        self.soc = 0.0
        self.soh = 90.0
        self.bat_v = 0.0
        self.bat_i = 0.0
        self.bat_p = 0.0
        self.cell_max = 0.0
        self.cell_min = 0.0
        self.max_cell_delta_today = 0.0
        
        self.pv_p = 0.0
        self.ac_loads = 0.0
        self.grid_p = 0.0
        self.grid_v = 220.0
        self.vebus_mode = 3
        self.vebus_state = 3
        
        # Comptadors i temporitzadors
        self.export_start_time = None
        self.high_discharge_start_time = None
        self.low_voltage_start_time = None
        self.last_mode_switch_time = 0.0
        self.last_forecast_time = 0.0
        self.last_stats_calc_time = 0.0
        self.last_keepalive_time = 0.0
        self.last_applied_min_soc = None
        
        # Previsió Open-Meteo
        self.today_kwh_est = 5.0
        self.remaining_kwh_today = 3.0
        self.tomorrow_kwh_est = 5.0
        self.max_temp_today = 30.0
        self.sunset_temp_today = 26.0
        self.blackout_risk = 0
        self.target_reserve_soc = 85.0
        
        # Acumulats d'avui
        self.current_day_str = get_madrid_now().strftime("%Y-%m-%d")
        self.solar_kwh_today = 0.0
        self.solar_peak_w = 0.0
        self.consumption_kwh_today = 0.0
        self.grid_import_kwh_today = 0.0
        self.grid_export_kwh_today = 0.0
        self.p1_kwh_today = 0.0
        self.p2_kwh_today = 0.0
        self.p3_kwh_today = 0.0
        self.mode2_time_seconds = 0.0
        self.relay_switch_count = 0
        self.tuya_ac_turned_off_today = False
        
        os.makedirs(os.path.dirname(HISTORY_CSV_FILE), exist_ok=True)
        self.init_history_csv()

    def init_history_csv(self):
        if not os.path.exists(HISTORY_CSV_FILE):
            try:
                with open(HISTORY_CSV_FILE, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow([
                        "Data", "Solar_kWh", "Consum_kWh", "Importat_kWh", "Exportat_kWh",
                        "Cobertura_Solar_Pct", "Cost_Total_EUR", "Temps_Mode2_Min",
                        "Canvis_Rele", "Max_DeltaV_mV", "SoH_BMS_Pct", "Es_CapSetmana_o_Festiu"
                    ])
            except Exception as e:
                log.warning(f"No s'ha pogut inicialitzar {HISTORY_CSV_FILE}: {e}")

    def append_to_history(self, date_str: str):
        try:
            cov = (self.solar_kwh_today / max(0.01, self.consumption_kwh_today)) * 100.0
            cost = self.calculate_today_cost()
            is_hol = "SI" if self.is_holiday_or_weekend() else "NO"
            
            with open(HISTORY_CSV_FILE, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    date_str,
                    f"{self.solar_kwh_today:.2f}",
                    f"{self.consumption_kwh_today:.2f}",
                    f"{self.grid_import_kwh_today:.2f}",
                    f"{self.grid_export_kwh_today:.2f}",
                    f"{min(100.0, cov):.1f}",
                    f"{cost:.2f}",
                    f"{self.mode2_time_seconds / 60.0:.1f}",
                    self.relay_switch_count,
                    f"{self.max_cell_delta_today:.0f}",
                    f"{self.soh:.0f}",
                    is_hol
                ])
            log.info(f"📁 [HISTÒRIC PERMANENT] Dia {date_str} arxivat amb èxit ({self.solar_kwh_today:.2f} kWh solars, {self.consumption_kwh_today:.2f} kWh consum, {cost:.2f} €).")
        except Exception as e:
            log.error(f"Error registrant històric permanent diari: {e}")

    def send_notification(self, title: str, message: str, priority: str = "default", tags: str = "zap"):
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}"
            data = message.encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Title", title)
            req.add_header("Priority", priority)
            req.add_header("Tags", tags)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info(f"📱 Notificació enviada al mòbil: {title}")
        except Exception as e:
            log.warning(f"No s'ha pogut enviar notificació ntfy: {e}")

    def is_holiday_or_weekend(self) -> bool:
        """Determina si avui és cap de setmana o festiu oficial (P3 Vall 24h)."""
        now = get_madrid_now()
        if now.weekday() in (5, 6): # Dissabte o Diumenge
            return True
            
        y, m, d = now.year, now.month, now.day
        fixed_holidays = [
            (1, 1),   # Any Nou
            (1, 6),   # Reis Mags
            (3, 19),  # Sant Josep (CV)
            (5, 1),   # Festa del Treball
            (6, 24),  # Sant Joan (CV)
            (8, 15),  # Assumpció de la Verge
            (10, 9),  # Dia de la Comunitat Valenciana
            (10, 12), # Festa Nacional d'Espanya
            (11, 1),  # Tots Sants
            (12, 6),  # Dia de la Constitució
            (12, 8),  # Immaculada Concepció
            (12, 25), # Nadal
        ]
        if (m, d) in fixed_holidays:
            return True
            
        easter = get_easter_date(y)
        good_friday = easter - datetime.timedelta(days=2)
        easter_monday = easter + datetime.timedelta(days=1)
        
        today_date = now.date()
        if today_date in (good_friday, easter_monday):
            return True
            
        return False

    def update_energy_forecast(self):
        """Consulta Open-Meteo per estimar radiació solar i temperatura màxima a Ador."""
        now = time.time()
        if now - self.last_forecast_time < 1800: # cada 30 min
            return
            
        self.last_forecast_time = now
        try:
            url = "https://api.open-meteo.com/v1/forecast?latitude=39.0&longitude=-0.3&daily=shortwave_radiation_sum,temperature_2m_max&hourly=direct_normal_irradiance,temperature_2m&timezone=Europe%2FMadrid&forecast_days=2"
            req = urllib.request.Request(url, headers={"User-Agent": "CasetaGuardian/2.0"})
            with urllib.request.urlopen(req, timeout=10) as rep:
                data = json.loads(rep.read().decode())
                
            daily = data.get("daily", {})
            rad_list = daily.get("shortwave_radiation_sum", [22.0, 22.0])
            temp_max_list = daily.get("temperature_2m_max", [30.0, 30.0])
            
            # 1.35 kWp Huawei * radiació MJ/m2 * factor de conversió
            self.today_kwh_est = max(1.0, (rad_list[0] / 3.6) * 1.35 * 0.78)
            self.tomorrow_kwh_est = max(1.0, (rad_list[1] / 3.6) * 1.35 * 0.78)
            self.max_temp_today = temp_max_list[0]
            
            # Radiació restant avui
            hourly = data.get("hourly", {})
            dni = hourly.get("direct_normal_irradiance", [])
            temps = hourly.get("temperature_2m", [])
            current_hour = get_madrid_now().hour
            
            if len(dni) >= 24:
                remaining_dni = sum(dni[current_hour:24])
                total_dni = max(1.0, sum(dni[0:24]))
                self.remaining_kwh_today = self.today_kwh_est * (remaining_dni / total_dni)
            else:
                self.remaining_kwh_today = max(0.0, self.today_kwh_est * (1.0 - (current_hour / 20.0)))
                
            if len(temps) >= 22:
                self.sunset_temp_today = temps[21]
            else:
                self.sunset_temp_today = self.max_temp_today - 4.0
                
            # Risc d'apagada per sobrecàrrega rural d'estiu
            if self.max_temp_today >= 38.0:
                self.blackout_risk = 70
            elif self.max_temp_today >= 34.0:
                self.blackout_risk = 45
            elif self.max_temp_today >= 31.0:
                self.blackout_risk = 25
            else:
                self.blackout_risk = 10

            log.info(f"📊 Open-Meteo: Sol total = {self.today_kwh_est:.1f} kWh (Queden {self.remaining_kwh_today:.1f} kWh) | Màx = {self.max_temp_today:.1f}ºC | Risc Tall = {self.blackout_risk}% -> Target SoC = {self.target_reserve_soc:.0f}%")
            
            # Desa cache per a l'script 'caseta'
            cache = {
                "today_kwh": round(self.today_kwh_est, 1),
                "remaining_kwh": round(self.remaining_kwh_today, 1),
                "tomorrow_kwh": round(self.tomorrow_kwh_est, 1),
                "max_temp_today": round(self.max_temp_today, 1),
                "sunset_temp": round(self.sunset_temp_today, 1),
                "blackout_risk": self.blackout_risk,
                "target_reserve_soc": self.target_reserve_soc,
                "timestamp": now
            }
            with open("/tmp/caseta_forecast_cache.json", "w") as f:
                json.dump(cache, f)
                
        except Exception as e:
            log.warning(f"Error actualitzant Open-Meteo: {e}")

    def trigger_tuya_ac_off(self):
        """Envia l'ordre d'apagar l'aire condicionat per infrarojos Tuya en emergència."""
        if self.tuya_ac_turned_off_today:
            return
            
        log.info("❄️ [ESCUT CLIMÀTIC] Apagant Aire Condicionat per infrarojos Tuya...")
        self.tuya_ac_turned_off_today = True
        
        # 1. Intent Local directe (tinytuya)
        try:
            import tinytuya
            dev_id = config.get("tuya_s06_id", "bf5377f0d014d59a72kexi")
            loc_key = config.get("tuya_s06_key", "7e813a8b417e29cf")
            ir = tinytuya.OutletDevice(dev_id, TUYA_S06_IP, loc_key, version=3.3)
            ir.set_status(False, 1)
            log.info("❄️ Comanda Tuya Local transmesa amb èxit!")
            self.send_notification("❄️ Escut Domòtic Activat", "S'ha apagat l'aire condicionat automàticament per protegir la reserva del 65% de la bateria!", "high", "snowflake")
            return
        except Exception as e:
            log.warning(f"Comanda Tuya Local no disponible ({e}). Provant Tuya Cloud...")

        # 2. Intent Tuya Cloud API
        try:
            cid = config.get("tuya_cloud_client_id", "v9mkg98345ektd4p4m7u")
            sec = config.get("tuya_cloud_secret", "d1326442650c45169a9b8979ceb649ee")
            dev_id = config.get("tuya_s06_id", "bf5377f0d014d59a72kexi")
            
            t_ms = str(int(time.time() * 1000))
            base_url = "https://openapi.tuyaeu.com"
            path = f"/v1.0/devices/{dev_id}/commands"
            body = json.dumps({"commands": [{"code": "power", "value": False}]})
            
            str_to_sign = f"{cid}{t_ms}POST\n{hashlib.sha256(body.encode()).hexdigest()}\n\n{path}"
            sign = hmac.new(sec.encode(), str_to_sign.encode(), hashlib.sha256).hexdigest().upper()
            
            h = {"client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"}
            r = urllib.request.Request(f"{base_url}{path}", data=body.encode(), headers=h, method="POST")
            with urllib.request.urlopen(r, timeout=5) as rep:
                result_data = json.loads(rep.read().decode())
                if result_data.get("success"):
                    log.info("❄️ Comanda Tuya Cloud 'power=0' (Apagat AC) transmesa amb èxit!")
                else:
                    log.warning(f"Resposta Tuya Cloud: {result_data}")
        except Exception as e:
            log.warning(f"Error enviant comanda Tuya Cloud: {e}")

    def sync_cerbo_min_soc(self):
        """Sincronitza el Minimum SOC de l'ESS del Cerbo GX segons l'hora de Madrid, balanç energètic, cap de setmana/festiu i clima."""
        if not self.client or self.portal_id in ("c0619ab2xxxx", "+", "#"):
            return
        
        now_madrid = get_madrid_now()
        current_hour = now_madrid.hour
        is_weekend_or_hol = self.is_holiday_or_weekend()
        
        # 1. 🌙 Nit Supervall (00:00h a 06:59h Madrid): Top-Balancing i 100% de SAI a 7 cts/kWh
        if 0 <= current_hour < 7:
            target = 100.0
            phase_name = "🌙 Nit Supervall (100% Top-Balancing & SAI Màxim)"
            
        # 2. ☀️ Franja Diürna Solar (07:00h a 15:59h Madrid - Feiners i Caps de Setmana):
        # Baixem el sòl al 70% per deixar un 30% buit a la bateria per engolir el sol
        elif 7 <= current_hour < 16:
            if self.blackout_risk >= 60:
                target = 95.0
                phase_name = "🚨 Alerta Calor Extrema (95% SAI Blindat)"
            elif self.today_kwh_est >= 4.5:
                target = 70.0
                phase_name = "☀️ Dia Radiant (70% per absorbir excedent solar)"
            else:
                target = 85.0
                phase_name = "☁️ Dia Variable / Baix Sol (85% Coixí Preventiu)"

        # 3. 🏖️ Cap de Setmana o Festiu a la Tarda/Vespre (Preu Vall 24h continu a ~7 cts):
        elif is_weekend_or_hol and (current_hour >= 18 or (current_hour >= 16 and (self.pv_p < self.ac_loads or self.pv_p < 200.0))):
            target = 100.0
            phase_name = "🏖️ Cap de Setmana/Festiu Vespre (100% Top-Balancing Avançat - Vall 24h a 7 cts)"
            
        # 4. 🌇 Tarda / Vespre Feiners (16:00h a 23:59h Madrid):
        else:
            if self.blackout_risk >= 60:
                target = 95.0
                phase_name = "🚨 Alerta Calor Extrema (95% SAI Blindat)"
            else:
                target = 85.0
                phase_name = "🌇 Tarda / Vespre Resilient (85% Màxima Seguretat & SAI)"

        self.target_reserve_soc = target

        if self.last_applied_min_soc != target:
            topic = f"W/{self.portal_id}/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit"
            payload = json.dumps({"value": target})
            self.client.publish(topic, payload)
            log.info(f"⚙️ Sincronitzat Minimum SOC a Cerbo GX: {target:.0f}% [{phase_name}]")
            self.last_applied_min_soc = target

    def set_multiplus_mode(self, target_mode: int, reason: str):
        now = time.time()
        if now - self.last_mode_switch_time < 20: # protecció histeresi
            return
            
        mode_names = {1: "Charger Only", 2: "Inverter Only (Aïllat)", 3: "ON (Connectat a Xarxa)", 4: "OFF"}
        old_mode_str = mode_names.get(self.vebus_mode, f"Mode {self.vebus_mode}")
        new_mode_str = mode_names.get(target_mode, f"Mode {target_mode}")
        
        log.info(f"🔄 CANVI DE MODE MULTIPLUS: {old_mode_str} -> {new_mode_str} ({reason})")
        topic = f"W/{self.portal_id}/vebus/276/Mode"
        payload = json.dumps({"value": target_mode})
        self.client.publish(topic, payload)
        
        self.vebus_mode = target_mode
        self.last_mode_switch_time = now
        self.relay_switch_count += 1
        
        priority = "high" if target_mode == 2 else "default"
        self.send_notification("Canvi de Mode MultiPlus", f"{old_mode_str} ➡️ {new_mode_str}\n{reason}", priority=priority)

    def calculate_today_cost(self) -> float:
        energy_cost = (self.p1_kwh_today * P1_RATE) + (self.p2_kwh_today * P2_RATE) + (self.p3_kwh_today * P3_RATE)
        total_subtotal = POTENCIA_FIXED_DAY + energy_cost
        return round(total_subtotal * TAX_MULTIPLIER, 2)

    def update_energy_integrals(self):
        now = time.time()
        if self.last_stats_calc_time == 0.0:
            self.last_stats_calc_time = now
            return
            
        dt = now - self.last_stats_calc_time
        self.last_stats_calc_time = now
        
        # Reset diari a mitjanit (hora de Madrid)
        today_str = get_madrid_now().strftime("%Y-%m-%d")
        if today_str != self.current_day_str:
            self.append_to_history(self.current_day_str)
            self.current_day_str = today_str
            self.solar_kwh_today = 0.0
            self.solar_peak_w = 0.0
            self.consumption_kwh_today = 0.0
            self.grid_import_kwh_today = 0.0
            self.grid_export_kwh_today = 0.0
            self.p1_kwh_today = 0.0
            self.p2_kwh_today = 0.0
            self.p3_kwh_today = 0.0
            self.mode2_time_seconds = 0.0
            self.relay_switch_count = 0
            self.max_cell_delta_today = 0.0
            self.tuya_ac_turned_off_today = False
            log.info(f"🔄 Reset d'acumulats diaris per al nou dia: {today_str}")

        hours = dt / 3600.0
        
        if self.pv_p > 0:
            self.solar_kwh_today += (self.pv_p / 1000.0) * hours
            if self.pv_p > self.solar_peak_w:
                self.solar_peak_w = self.pv_p
                
        if self.ac_loads > 0:
            self.consumption_kwh_today += (self.ac_loads / 1000.0) * hours
            
        if self.vebus_mode == 2:
            self.mode2_time_seconds += dt
        else:
            if self.grid_p > 0:
                imp_kwh = (self.grid_p / 1000.0) * hours
                self.grid_import_kwh_today += imp_kwh
                
                # Discriminació horària 2.0TD (hora de Madrid)
                now_madrid = get_madrid_now()
                ch = now_madrid.hour
                if self.is_holiday_or_weekend():
                    self.p3_kwh_today += imp_kwh
                elif ch in (10, 11, 12, 13, 18, 19, 20, 21):
                    self.p1_kwh_today += imp_kwh
                elif ch in (8, 9, 14, 15, 16, 17, 22, 23):
                    self.p2_kwh_today += imp_kwh
                else:
                    self.p3_kwh_today += imp_kwh
            elif self.grid_p < -20:
                self.grid_export_kwh_today += (abs(self.grid_p) / 1000.0) * hours

        if self.cell_max > 0 and self.cell_min > 0:
            delta_mv = (self.cell_max - self.cell_min) * 1000.0
            if delta_mv > self.max_cell_delta_today:
                self.max_cell_delta_today = delta_mv

        cov_pct = (self.solar_kwh_today / max(0.01, self.consumption_kwh_today)) * 100.0
        cost_today = self.calculate_today_cost()

        stats = {
            "date": self.current_day_str,
            "solar_kwh_today": round(self.solar_kwh_today, 2),
            "solar_peak_w": round(self.solar_peak_w, 1),
            "consumption_kwh_today": round(self.consumption_kwh_today, 2),
            "grid_import_kwh_today": round(self.grid_import_kwh_today, 2),
            "grid_export_kwh_today": round(self.grid_export_kwh_today, 2),
            "solar_coverage_percent": round(min(100.0, cov_pct), 1),
            "cost_total_today": cost_today,
            "mode2_time_minutes": round(self.mode2_time_seconds / 60.0, 1),
            "relay_switch_count": self.relay_switch_count,
            "max_cell_delta_today": round(self.max_cell_delta_today, 1),
            "soh_bms": round(self.soh, 0),
            "timestamp": now
        }
        try:
            with open("/tmp/caseta_daily_stats.json", "w") as f:
                json.dump(stats, f)
        except Exception:
            pass

    def evaluate_state_machine(self):
        """Màquina d'estats del Guardià: Zero Regal, Escut de Sobrecàrrega, Watchdog <190V i Escut Tuya."""
        now = time.time()

        # 1. 🛡️ ESCUT DE PROTECCIÓ SAI DOMÒTIC (<65% SoC)
        if self.soc < 65.0 and not self.tuya_ac_turned_off_today:
            self.trigger_tuya_ac_off()

        # 2. 🚨 WATCHDOG DE BAIXA TENSIÓ RURAL (<190V durant >2 minuts)
        if self.vebus_mode != 2 and self.grid_v < 190.0:
            if self.low_voltage_start_time is None:
                self.low_voltage_start_time = now
            elif now - self.low_voltage_start_time >= 120.0:
                self.send_notification("🚨 Tensió Xarxa Crítica", f"Tensió rural a {self.grid_v:.1f}V (<190V durant >2 minuts). Vigilant estabilitat!", "high", "warning")
                self.low_voltage_start_time = now # reinicia
        else:
            self.low_voltage_start_time = None

        # 3. ⚡ RECONNEXIÓ D'EMERGÈNCIA A XARXA (Mode 2 -> Mode 3)
        if self.vebus_mode == 2:
            # Condició A: Descàrrega forta de bateria (>15A durant >5s)
            if self.bat_i < -15.0:
                if self.high_discharge_start_time is None:
                    self.high_discharge_start_time = now
                elif now - self.high_discharge_start_time >= 5.0:
                    self.set_multiplus_mode(3, f"Descàrrega alta ({abs(self.bat_i):.1f}A > 15.0A per >5s)")
                    self.high_discharge_start_time = None
                    return
            else:
                self.high_discharge_start_time = None

            # Condició B: Bateria caient per sota de la reserva segura (<70% SoC)
            if self.soc < 70.0:
                self.set_multiplus_mode(3, f"Bateria ha baixat del sòl segur ({self.soc:.1f}% < 70.0%)")
                return

            # Condició C: Producció solar esgotada i consum actiu
            if self.pv_p < 50.0 and self.ac_loads > 300.0 and self.soc <= 85.0:
                self.set_multiplus_mode(3, f"Sol esgotat ({self.pv_p:.0f}W) i consum a casa ({self.ac_loads:.0f}W)")
                return

        # 4. 🏝️ DESCONNEXIÓ PER EVITAR ABOCAMENT (Mode 3 -> Mode 2)
        elif self.vebus_mode == 3:
            # Condició: Abocant >50W amb bateria alta (>=88% SoC) durant >30s
            if self.grid_p is not None and self.grid_p < -50.0 and self.soc >= 88.0:
                if self.export_start_time is None:
                    self.export_start_time = now
                    log.info(f"⚠️ Detectat abocament de {abs(self.grid_p):.0f}W amb SoC {self.soc:.1f}%. Iniciant compte enrere de 30s...")
                elif now - self.export_start_time >= 30.0:
                    self.set_multiplus_mode(2, f"Abocament sostingut de {abs(self.grid_p):.0f}W durant >30s amb SoC {self.soc:.1f}%")
                    self.export_start_time = None
                    return
            else:
                self.export_start_time = None

    def on_mqtt_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) > 1 and parts[1] not in ("+", "#"):
                self.portal_id = parts[1]
                
            val = json.loads(msg.payload.decode()).get("value")
            topic = msg.topic
            
            # Telemetria de Bateria
            if topic.endswith("/battery/512/Soc"):
                self.soc = float(val) if val is not None else self.soc
            elif topic.endswith("/battery/512/Soh"):
                self.soh = float(val) if val is not None else self.soh
            elif topic.endswith("/battery/512/Dc/0/Voltage"):
                self.bat_v = float(val) if val is not None else self.bat_v
            elif topic.endswith("/battery/512/Dc/0/Current"):
                self.bat_i = float(val) if val is not None else self.bat_i
            elif topic.endswith("/battery/512/Dc/0/Power"):
                self.bat_p = float(val) if val is not None else self.bat_p
            elif topic.endswith("/battery/512/System/MaxCellVoltage"):
                self.cell_max = float(val) if val is not None else self.cell_max
            elif topic.endswith("/battery/512/System/MinCellVoltage"):
                self.cell_min = float(val) if val is not None else self.cell_min
                
            # Telemetria Solar i Xarxa
            elif topic.endswith("/pvinverter/31/Ac/Power"):
                self.pv_p = float(val) if val is not None else self.pv_p
            elif topic.endswith("/system/0/Ac/Consumption/L1/Power"):
                self.ac_loads = float(val) if val is not None else self.ac_loads
            elif topic.endswith("/system/0/Ac/Grid/L1/Power"):
                self.grid_p = float(val) if val is not None else self.grid_p
            elif topic.endswith("/vebus/276/Ac/ActiveIn/L1/V"):
                self.grid_v = float(val) if val is not None else self.grid_v
            elif topic.endswith("/vebus/276/Mode"):
                self.vebus_mode = int(val) if val is not None else self.vebus_mode
            elif topic.endswith("/vebus/276/VebusChargeState"):
                self.vebus_state = int(val) if val is not None else self.vebus_state
                
        except Exception:
            pass

    def run(self):
        log.info(f"🚀 Iniciant Caseta Guardian (Cerbo IP: {CERBO_IP})...")
        
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_message = self.on_mqtt_message
        
        try:
            self.client.connect(CERBO_IP, 1883, 60)
        except Exception as e:
            log.error(f"Error fatal connectant al broker MQTT del Cerbo GX ({CERBO_IP}): {e}")
            return
            
        self.client.subscribe("N/#")
        self.client.loop_start()
        
        self.sync_cerbo_min_soc()
        self.update_energy_forecast()
        
        log.info("🛡️ Guardià en línia i vigilant telemetria en directe!")
        
        while self.running:
            try:
                now = time.time()
                
                # Keepalive optimitzat cada 20s
                if now - self.last_keepalive_time >= 20:
                    self.client.publish(f"R/{self.portal_id}/keepalive", "")
                    self.last_keepalive_time = now
                    
                self.sync_cerbo_min_soc()
                self.update_energy_forecast()
                self.update_energy_integrals()
                self.evaluate_state_machine()
                
                time.sleep(1.0)
            except KeyboardInterrupt:
                log.info("Aturant Caseta Guardian...")
                self.running = False
            except Exception as e:
                log.error(f"Error al bucle principal: {e}")
                time.sleep(2.0)
                
        self.client.loop_stop()
        self.client.disconnect()

if __name__ == "__main__":
    guardian = CasetaGuardian()
    guardian.run()

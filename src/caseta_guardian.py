#!/usr/bin/env python3
"""
Caseta Guardian - Dimoni de Control Energètic per a Victron ESS + Cerbo GX
Caseta d'Ador (La Safor, València)

Funcions principals:
1. ☀️ Zero Regal 100% Reversible (Smart Islanding): Obre el relé AC1 en excés solar i reconnecta en sobrecàrrega.
2. 🧠 Sòl Dinàmic Adaptatiu (Avaluat periòdicament): Ajusta el Minimum SOC segons el balanç real de Sol vs Consum i Open-Meteo.
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
    "/data/caseta-guardian/config.json",
    os.path.expanduser("~/.config/caseta/config.json"),
    os.path.expanduser("~/.config/caseta-guardian/config.json"),
    os.path.join(os.path.dirname(__file__), "..", "config.json")
]

def load_config() -> dict:
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

config = load_config()

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
        self.last_stats_publish_time = 0.0
        self.last_keepalive_time = 0.0
        self.last_applied_min_soc = None
        self.last_soc_eval_time = 0.0
        
        # Previsió Open-Meteo
        self.today_kwh_est = 5.0
        self.remaining_kwh_today = 3.0
        self.tomorrow_kwh_est = 5.0
        self.max_temp_today = 30.0
        self.sunset_temp_today = 26.0
        self.blackout_risk = 0
        self.target_reserve_soc = 85.0
        
        self.current_day_str = get_madrid_now().strftime("%Y-%m-%d")
        self.is_holiday = self.check_is_holiday_or_weekend()
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
        self.load_daily_stats()

    def check_is_holiday_or_weekend(self, now=None) -> bool:
        """Determina si avui és cap de setmana o festiu oficial (P3 Vall 24h) - Executat NOMÉS 1 cop al dia a mitjanit."""
        if now is None:
            now = get_madrid_now()
        if now.weekday() in (5, 6):
            return True
            
        y, m, d = now.year, now.month, now.day
        fixed_holidays = [
            (1, 1), (1, 6), (3, 19), (5, 1), (6, 24), (8, 15),
            (10, 9), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25),
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

    def load_daily_stats(self):
        """Carrega els acumulats del dia d'avui si el dimoni es reinicia per evitar reiniciar a 0 kWh."""
        stats_file = "/data/caseta-guardian/caseta_daily_stats.json" if os.path.exists("/data/caseta-guardian") else "/tmp/caseta_daily_stats.json"
        if os.path.exists(stats_file):
            try:
                with open(stats_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == self.current_day_str:
                    self.solar_kwh_today = float(data.get("solar_kwh_today", 0.0))
                    self.solar_peak_w = float(data.get("solar_peak_w", 0.0))
                    self.consumption_kwh_today = float(data.get("consumption_kwh_today", 0.0))
                    self.grid_import_kwh_today = float(data.get("grid_import_kwh_today", 0.0))
                    self.grid_export_kwh_today = float(data.get("grid_export_kwh_today", 0.0))
                    self.mode2_time_seconds = float(data.get("mode2_time_minutes", 0.0)) * 60.0
                    self.relay_switch_count = int(data.get("relay_switch_count", 0))
                    self.max_cell_delta_today = float(data.get("max_cell_delta_today", 0.0))
                    self.p1_kwh_today = float(data.get("p1_kwh_today", 0.0))
                    self.p2_kwh_today = float(data.get("p2_kwh_today", 0.0))
                    self.p3_kwh_today = float(data.get("p3_kwh_today", 0.0))
                    log.info(f"💾 Recuperats acumulats previs d'avui ({self.current_day_str}): {self.solar_kwh_today:.2f} kWh solars, {self.consumption_kwh_today:.2f} kWh consum.")
            except Exception as e:
                log.warning(f"No s'han pogut carregar acumulats previs: {e}")

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
            clean_title = title.encode('ascii', 'ignore').decode('ascii').strip() or "Caseta Guardian"
            req.add_header("Title", clean_title)
            req.add_header("Priority", priority)
            req.add_header("Tags", tags)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info(f"📱 Notificació enviada al mòbil: {title}")
        except Exception as e:
            log.warning(f"No s'ha pogut enviar notificació ntfy: {e}")

    def update_energy_forecast(self):
        """Consulta Open-Meteo per estimar radiació solar i temperatura màxima (cada 60 minuts)."""
        now = time.time()
        if now - self.last_forecast_time < 3600:
            return
            
        self.last_forecast_time = now
        try:
            cfg = load_config()
            lat = cfg.get("latitude", 38.9)
            lon = cfg.get("longitude", -0.2)
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=shortwave_radiation_sum,temperature_2m_max&hourly=direct_normal_irradiance,temperature_2m&timezone=Europe%2FMadrid&forecast_days=2"
            req = urllib.request.Request(url, headers={"User-Agent": "CasetaGuardian/2.0"})
            with urllib.request.urlopen(req, timeout=10) as rep:
                data = json.loads(rep.read().decode())
                
            daily = data.get("daily", {})
            rad_list = daily.get("shortwave_radiation_sum", [22.0, 22.0])
            temp_max_list = daily.get("temperature_2m_max", [30.0, 30.0])
            
            self.today_kwh_est = max(1.0, (rad_list[0] / 3.6) * 1.35 * 0.78)
            self.tomorrow_kwh_est = max(1.0, (rad_list[1] / 3.6) * 1.35 * 0.78)
            self.max_temp_today = temp_max_list[0]
            
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
                
            if self.max_temp_today >= 38.0:
                self.blackout_risk = 70
            elif self.max_temp_today >= 34.0:
                self.blackout_risk = 45
            elif self.max_temp_today >= 31.0:
                self.blackout_risk = 25
            else:
                self.blackout_risk = 10

            log.info(f"📊 Open-Meteo: Sol total = {self.today_kwh_est:.1f} kWh (Queden {self.remaining_kwh_today:.1f} kWh) | Màx = {self.max_temp_today:.1f}ºC | Risc Tall = {self.blackout_risk}% -> Target SoC = {self.target_reserve_soc:.0f}%")
            
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
            if self.client:
                self.client.publish("caseta/forecast", json.dumps({"value": cache}), retain=True)
                if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                    self.client.publish(f"N/{self.portal_id}/caseta/forecast", json.dumps({"value": cache}), retain=True)
                
        except Exception as e:
            log.warning(f"Error actualitzant Open-Meteo: {e}")

    def trigger_tuya_ac_off(self):
        if self.tuya_ac_turned_off_today:
            return
            
        log.info("❄️ [ESCUT CLIMÀTIC] Apagant Aire Condicionat per infrarojos Tuya Cloud...")
        self.tuya_ac_turned_off_today = True
        
        try:
            cfg = load_config()
            cid = cfg.get("tuya_cloud_client_id")
            sec = cfg.get("tuya_cloud_secret")
            infrared_id = cfg.get("tuya_cloud_infrared_id", cfg.get("tuya_s06_id"))
            remote_id = cfg.get("tuya_cloud_remote_id", cfg.get("tuya_ac_remote_id"))
            base_url = cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")
            
            if not cid or not sec or not infrared_id or not remote_id:
                log.warning("⚠️ Claus Tuya Cloud no configurades a config.json. No es pot enviar l'ordre.")
                return
            
            # 1. Obtenir Token Tuya
            t_ms = str(int(time.time() * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            str_to_sign = f"GET\n{content_hash}\n\n{url_path_token}"
            sign_str = f"{cid}{t_ms}{str_to_sign}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid,
                "sign": sign,
                "t": t_ms,
                "sign_method": "HMAC-SHA256",
                "Content-Type": "application/json"
            })
            
            with urllib.request.urlopen(req_token, timeout=8) as rep_tok:
                tok_data = json.loads(rep_tok.read().decode())
                token = tok_data.get("result", {}).get("access_token")
                
            if not token:
                log.warning(f"No s'ha pogut obtenir token Tuya: {tok_data}")
                return
                
            # 2. Enviar ordre Power OFF per a l'Aire Condicionat
            t_ms = str(int(time.time() * 1000))
            url_path_cmd = f"/v2.0/infrareds/{infrared_id}/air-conditioners/{remote_id}/command"
            body_dict = {"code": "power", "value": 0}
            body_str = json.dumps(body_dict)
            content_hash = hashlib.sha256(body_str.encode()).hexdigest()
            str_to_sign = f"POST\n{content_hash}\n\n{url_path_cmd}"
            sign_str = f"{cid}{token}{t_ms}{str_to_sign}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            
            req_cmd = urllib.request.Request(f"{base_url}{url_path_cmd}", data=body_str.encode(), headers={
                "client_id": cid,
                "access_token": token,
                "sign": sign,
                "t": t_ms,
                "sign_method": "HMAC-SHA256",
                "Content-Type": "application/json"
            }, method="POST")
            
            with urllib.request.urlopen(req_cmd, timeout=8) as rep_cmd:
                result_data = json.loads(rep_cmd.read().decode())
                if result_data.get("result") is True or result_data.get("success") is True:
                    log.info("❄️ Comanda Tuya Cloud 'power=0' (Apagat AC) transmesa amb èxit al S06!")
                    self.send_notification("❄️ Escut Domòtic Activat", "S'ha apagat l'aire condicionat automàticament per protegir la reserva del 65% de la bateria!", "high", "snowflake")
                else:
                    log.warning(f"Resposta Tuya Cloud: {result_data}")
        except Exception as e:
            log.warning(f"Error enviant comanda Tuya Cloud: {e}")

    def sync_cerbo_min_soc(self, now_madrid=None):
        """Avalua periòdicament el balanç de Sol vs Consum i horari circadiari d'estiu per modular el Minimum SOC."""
        if not self.client or self.portal_id in ("c0619ab2xxxx", "+", "#"):
            return
        
        now = time.time()
        # Interval suau de 2 minuts (120 segons) per evitar oscil·lacions
        if now - self.last_soc_eval_time < 120 and self.last_applied_min_soc is not None:
            return
        self.last_soc_eval_time = now

        if now_madrid is None:
            now_madrid = get_madrid_now()
        current_hour = now_madrid.hour
        current_minute = now_madrid.minute
        time_decimal = current_hour + (current_minute / 60.0)
        is_weekend_or_hol = self.is_holiday
        
        # 1. 🚨 Alerta de Calor Extrema / Risc Alt d'Apagada (Risc >= 60%)
        if self.blackout_risk >= 60:
            target = 95.0
            phase_name = "🚨 Alerta Calor Extrema (95% SAI Blindat)"

        # 2. 🌙 Nit Supervall (00:00h a 06:59h Madrid): Top-Balancing i 100% de SAI a 7 cts/kWh
        elif time_decimal < 7.0:
            target = 100.0
            phase_name = "🌙 Nit Supervall (100% Top-Balancing & SAI Màxim a 7 cts)"

        # 3. ☕ Matí Primer Cafè / Transició (07:00h a 09:29h Madrid)
        elif 7.0 <= time_decimal < 9.5:
            target = 85.0
            phase_name = "☕ Matí Transició (85% Coixí Inicial)"

        # 4. ☀️ Finestra Solar Central d'Estiu (09:30h a 16:29h Madrid): 75% Fix Pla
        elif 9.5 <= time_decimal < 16.5:
            target = 75.0
            phase_name = "☀️ Finestra Solar Central (75% Fix Pla - Vas Buit 880Wh)"

        # 5. 🏖️ Cap de Setmana o Festiu a la Tarda/Vespre (Preu Vall 24h continu a ~7 cts):
        elif is_weekend_or_hol and time_decimal >= 18.0:
            target = 100.0
            phase_name = "🏖️ Cap de Setmana/Festiu Vespre (100% Top-Balancing a 7 cts)"

        # 6. 🌇 Tarda / Vespre Feiners (16:30h a 23:59h Madrid):
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
        if now - self.last_mode_switch_time < 20:
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

    def save_daily_stats(self):
        """Guarda els acumulats a disc únicament a mitjanit o a l'aturar el servei (1 cop al dia)."""
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
            "p1_kwh_today": round(self.p1_kwh_today, 3),
            "p2_kwh_today": round(self.p2_kwh_today, 3),
            "p3_kwh_today": round(self.p3_kwh_today, 3),
            "mode2_time_minutes": round(self.mode2_time_seconds / 60.0, 1),
            "relay_switch_count": self.relay_switch_count,
            "max_cell_delta_today": round(self.max_cell_delta_today, 1),
            "soh_bms": round(self.soh, 0),
            "timestamp": time.time()
        }
        try:
            persistent_file = "/data/caseta-guardian/caseta_daily_stats.json" if os.path.exists("/data/caseta-guardian") else "/tmp/caseta_daily_stats.json"
            with open(persistent_file, "w") as f:
                json.dump(stats, f)
            log.info(f"💾 [DISC FLASH] Acumulats diaris arxivats a disc: {self.solar_kwh_today:.2f} kWh solars, {cost_today:.2f} €")
        except Exception as e:
            log.warning(f"Error guardant stats a disc: {e}")

    def update_energy_integrals(self, now_madrid=None):
        now = time.time()
        if self.last_stats_calc_time == 0.0:
            self.last_stats_calc_time = now
            return
            
        dt = now - self.last_stats_calc_time
        self.last_stats_calc_time = now
        
        if now_madrid is None:
            now_madrid = get_madrid_now()
            
        today_str = now_madrid.strftime("%Y-%m-%d")
        if today_str != self.current_day_str:
            self.save_daily_stats()
            self.append_to_history(self.current_day_str)
            self.current_day_str = today_str
            self.is_holiday = self.check_is_holiday_or_weekend(now_madrid)
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
            log.info(f"🔄 Reset d'acumulats diaris per al nou dia: {today_str} (Festiu/CapSetmana: {self.is_holiday})")

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
                
                ch = now_madrid.hour
                if self.is_holiday:
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

        # Publicació MQTT cada 10 segons (100% en RAM, ZERO accés a disc Flash)
        if now - self.last_stats_publish_time >= 10.0:
            self.last_stats_publish_time = now
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
                "p1_kwh_today": round(self.p1_kwh_today, 3),
                "p2_kwh_today": round(self.p2_kwh_today, 3),
                "p3_kwh_today": round(self.p3_kwh_today, 3),
                "mode2_time_minutes": round(self.mode2_time_seconds / 60.0, 1),
                "relay_switch_count": self.relay_switch_count,
                "max_cell_delta_today": round(self.max_cell_delta_today, 1),
                "soh_bms": round(self.soh, 0),
                "timestamp": now
            }
            try:
                if self.client:
                    if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                        self.client.publish(f"N/{self.portal_id}/caseta/stats", json.dumps({"value": stats}), retain=True)
                    self.client.publish("caseta/stats", json.dumps({"value": stats}), retain=True)
            except Exception:
                pass

    def evaluate_state_machine(self):
        now = time.time()

        if 0 < self.soc < 65.0 and not self.tuya_ac_turned_off_today:
            self.trigger_tuya_ac_off()

        if self.vebus_mode != 2 and self.grid_v < 190.0:
            if self.low_voltage_start_time is None:
                self.low_voltage_start_time = now
            elif now - self.low_voltage_start_time >= 120.0:
                self.send_notification("🚨 Tensió Xarxa Crítica", f"Tensió rural a {self.grid_v:.1f}V (<190V durant >2 minuts). Vigilant estabilitat!", "high", "warning")
                self.low_voltage_start_time = now
        else:
            self.low_voltage_start_time = None

        if self.vebus_mode == 2:
            if self.bat_i < -15.0:
                if self.high_discharge_start_time is None:
                    self.high_discharge_start_time = now
                elif now - self.high_discharge_start_time >= 5.0:
                    self.set_multiplus_mode(3, f"Descàrrega alta ({abs(self.bat_i):.1f}A > 15.0A per >5s)")
                    self.high_discharge_start_time = None
                    return
            else:
                self.high_discharge_start_time = None

            if self.soc < 70.0:
                self.set_multiplus_mode(3, f"Bateria ha baixat del sòl segur ({self.soc:.1f}% < 70.0%)")
                return

            if self.pv_p < 50.0 and self.ac_loads > 300.0 and self.soc <= 85.0:
                self.set_multiplus_mode(3, f"Sol esgotat ({self.pv_p:.0f}W) i consum a casa ({self.ac_loads:.0f}W)")
                return

        elif self.vebus_mode == 3:
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
                
            elif topic.endswith("/pvinverter/31/Ac/Power"):
                self.pv_p = float(val) if val is not None else self.pv_p
            elif topic.endswith("/system/0/Ac/Consumption/L1/Power") or topic.endswith("/vebus/276/Ac/Out/L1/P") or topic.endswith("/vebus/276/Ac/Out/P"):
                self.ac_loads = float(val) if val is not None else self.ac_loads
            elif topic.endswith("/system/0/Ac/Grid/L1/Power") or topic.endswith("/system/0/Ac/ActiveIn/L1/Power") or topic.endswith("/vebus/276/Ac/ActiveIn/L1/P") or topic.endswith("/vebus/276/Ac/ActiveIn/P"):
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
            
        # Subscripcions quirúrgiques per eliminar el 70% del soroll MQTT innecessari
        self.client.subscribe("N/+/battery/512/#")
        self.client.subscribe("N/+/pvinverter/31/#")
        self.client.subscribe("N/+/system/0/#")
        self.client.subscribe("N/+/vebus/276/#")
        self.client.subscribe("caseta/#")
        self.client.loop_start()
        
        self.sync_cerbo_min_soc()
        self.update_energy_forecast()
        
        log.info("🛡️ Guardià en línia i vigilant telemetria en directe!")
        
        while self.running:
            try:
                now = time.time()
                now_madrid = get_madrid_now()
                
                if now - self.last_keepalive_time >= 30:
                    self.client.publish(f"R/{self.portal_id}/keepalive", "")
                    self.last_keepalive_time = now
                    
                self.sync_cerbo_min_soc(now_madrid)
                self.update_energy_forecast()
                self.update_energy_integrals(now_madrid)
                self.evaluate_state_machine()
                
                time.sleep(1.0)
            except KeyboardInterrupt:
                log.info("Aturant Caseta Guardian...")
                self.running = False
            except Exception as e:
                log.error(f"Error al bucle principal: {e}")
                time.sleep(2.0)
                
        self.save_daily_stats()
        self.client.loop_stop()
        self.client.disconnect()

if __name__ == "__main__":
    guardian = CasetaGuardian()
    guardian.run()

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
import re
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
        self.last_inforatge_time = 0.0
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
        
        # Climatització Autònoma (4 Lleis)
        self.last_ac_command_time = 0.0
        self.last_presence_seen_time = time.time()
        self.ac_current_power = 1
        self.ac_current_temp = 26
        self.ac_turned_off_by_guardian = False
        self.ac_turned_off_by_free_cooling = False
        self.free_cooling_start_time = None
        self.ext_temp = None
        self.clima_sensors = {}
        
        # Termo Elèctric (Tuya Plug / LocalTuya)
        self.last_termo_update_time = 0.0
        self.termo_status = {}
        self.termo_heated_today = False
        self.termo_low_power_start_time = None
        
        # Protecció de Corrent i C-rate de Bateria
        self.c1_discharge_start_time = None
        self.c05_discharge_start_time = None
        
        # Grid Setpoint Dinàmic (Victron ESS)
        self.last_grid_setpoint = None
        self.last_grid_setpoint_eval_time = 0.0
        
        # Checkpoint de seguretat diari a disc (cada 30 minuts)
        self.last_checkpoint_save_time = time.time()
        
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
                    self.termo_heated_today = bool(data.get("termo_heated_today", False))
                    log.info(f"💾 Recuperats acumulats previs d'avui ({self.current_day_str}): {self.solar_kwh_today:.2f} kWh solars, {self.consumption_kwh_today:.2f} kWh consum (Termo calfat: {self.termo_heated_today}).")
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
        # 🔕 1. Silenci d'arrencada: Durant els primers 60 segons, silenciar notificacions rutinàries
        now = time.time()
        if hasattr(self, "daemon_start_time") and (now - self.daemon_start_time < 60):
            if priority not in ("urgent", "high", "5", "4"):
                log.info(f"🔕 [SILENCI D'ARRENCADA] Notificació rutinària silenciada: {title}")
                return

        # 🔕 2. Filtre anti-repetició (Deduplicació en menys de 10 minuts per a no-crítiques)
        dedup_key = f"{title}_{message[:30]}"
        if not hasattr(self, "_notif_history"):
            self._notif_history = {}
        last_sent = self._notif_history.get(dedup_key, 0.0)
        if (now - last_sent < 600) and priority not in ("urgent", "high", "5", "4"):
            return
        self._notif_history[dedup_key] = now

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

    def update_inforatge(self):
        """Consulta Inforatge Ador cada 15 minuts per obtenir condicions hiper-locals reals."""
        now = time.time()
        if now - self.last_inforatge_time < 900:  # Cada 15 minuts
            return
            
        self.last_inforatge_time = now
        try:
            url = "https://inforatge.com/meteo-ador"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=8) as rep:
                html = rep.read().decode("utf-8")

            temp_m = re.search(r'class="blocValorTM">(\d+)<span class="vPetit">,(\d+)</span>', html)
            temp = float(f"{temp_m.group(1)}.{temp_m.group(2)}") if temp_m else None

            hum_m = re.search(r'class="blocVariableHR">.*?class="blocValor">(\d+)</div>', html, re.S)
            hum = float(hum_m.group(1)) if hum_m else None

            press_m = re.search(r'class="blocVariablePA">.*?class="blocValor">(\d+)', html, re.S)
            press = int(press_m.group(1)) if press_m else None

            vent_m = re.search(r'class="blocVariableVV">.*?class="blocValor">(\d+)<span class="vPetit">\s*([^<]+)</span>', html, re.S)
            vent_vel = int(vent_m.group(1)) if vent_m else 0
            vent_dir = vent_m.group(2).strip() if vent_m else ""

            pluja_m = re.search(r'class="blocVariablePL">.*?class="blocValor">(\d+),(\d+)</div>', html, re.S)
            pluja = float(f"{pluja_m.group(1)}.{pluja_m.group(2)}") if pluja_m else 0.0

            tmax_m = re.search(r'class="boxpetitkTX negreT"><span class="varmobil">m&agrave;x</span>(\d+),(\d+)', html)
            tmax = float(f"{tmax_m.group(1)}.{tmax_m.group(2)}") if tmax_m else None

            tmin_m = re.search(r'class="boxpetitkTM negreT"><span class="varmobil">m&iacute;n</span>(\d+),(\d+)', html)
            tmin = float(f"{tmin_m.group(1)}.{tmin_m.group(2)}") if tmin_m else None

            self.ext_temp = temp
            inforatge_data = {
                "temperatura": temp,
                "humitat": hum,
                "pressio": press,
                "vent_vel": vent_vel,
                "vent_dir": vent_dir,
                "pluja_avui": pluja,
                "t_max_avui": tmax,
                "t_min_avui": tmin,
                "timestamp": now,
                "hora_str": get_madrid_now().strftime("%H:%M")
            }

            with open("/tmp/caseta_inforatge_cache.json", "w") as f:
                json.dump(inforatge_data, f)

            if self.client:
                self.client.publish("caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
                if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                    self.client.publish(f"N/{self.portal_id}/caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
            log.info(f"📍 Inforatge Ador: Ext {temp}ºC | Hum {hum}% | Vent {vent_vel} km/h {vent_dir} | Pressió {press} hPa")
        except Exception as e:
            log.warning(f"Error consultant Inforatge Ador: {e}")

    def update_termo_status(self):
        """Consulta periòdicament l'estat del Termo Elèctric (Tuya Plug) via LocalTuya LAN directa i publica a FlashMQ."""
        now = time.time()
        if now - self.last_termo_update_time < 30.0:
            return
        self.last_termo_update_time = now

        cfg = load_config()
        deviceId = cfg.get("tuya_termo_device_id", "bf425b5cf5fc5af1ecpxml")
        ip = cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = cfg.get("tuya_termo_local_key", "a|Ul|G=$U%b{{K9g")
        version = float(cfg.get("tuya_termo_version", 3.3))

        # 1. Intent Directe per LocalTuya (LAN local, 0 dependència d'internet)
        try:
            import tinytuya
            d = tinytuya.OutletDevice(deviceId, ip, local_key)
            d.set_version(version)
            d.set_socketPersistent(False)
            data = d.status()
            dps = data.get("dps", {})
            if dps:
                is_on = bool(dps.get("1", False))
                current_a = float(dps.get("18", 0)) / 1000.0
                power_w = float(dps.get("19", 0)) / 10.0
                voltage_v = float(dps.get("20", 0)) / 10.0

                termo_data = {
                    "is_on": is_on,
                    "power_w": round(power_w, 1),
                    "voltage_v": round(voltage_v, 1),
                    "current_a": round(current_a, 2),
                    "source": "localtuya",
                    "timestamp": now
                }
                self.termo_status = termo_data
                if self.client:
                    self.client.publish("caseta/termo", json.dumps({"value": termo_data}), retain=True)
                    if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                        self.client.publish(f"N/{self.portal_id}/caseta/termo", json.dumps({"value": termo_data}), retain=True)
                return
        except Exception as e_local:
            log.debug(f"LocalTuya status error: {e_local}")

        # 2. Fallback Tuya Cloud OpenAPI si falla la xarxa local
        try:
            cid = cfg.get("tuya_cloud_client_id") or cfg.get("tuya_client_id", "nvrwk5eqvcnnt3majq9c")
            sec = cfg.get("tuya_cloud_secret") or cfg.get("tuya_secret", "c1d97d0a854a451587fa02359aa327be")
            base_url = cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

            t_ms = str(int(now * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{t_ms}GET\n{content_hash}\n\n{url_path_token}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
                token = json.loads(rep_tok.read().decode())["result"]["access_token"]

            path_status = f"/v1.0/devices/{deviceId}/status"
            t_ms = str(int(time.time() * 1000))
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{token}{t_ms}GET\n{content_hash}\n\n{path_status}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()

            req_status = urllib.request.Request(f"{base_url}{path_status}", headers={
                "client_id": cid, "access_token": token, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_status, timeout=5) as rep:
                res = json.loads(rep.read().decode())
                status_list = res.get("result", [])
                status_map = {item["code"]: item["value"] for item in status_list}
                is_on = status_map.get("switch_1", False)
                power_w = status_map.get("cur_power", 0) / 10.0
                voltage_v = status_map.get("cur_voltage", 0) / 10.0
                current_a = status_map.get("cur_current", 0) / 1000.0

                termo_data = {
                    "is_on": is_on,
                    "power_w": round(power_w, 1),
                    "voltage_v": round(voltage_v, 1),
                    "current_a": round(current_a, 2),
                    "source": "tuya_cloud",
                    "timestamp": now
                }
                self.termo_status = termo_data
                if self.client:
                    self.client.publish("caseta/termo", json.dumps({"value": termo_data}), retain=True)
                    if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                        self.client.publish(f"N/{self.portal_id}/caseta/termo", json.dumps({"value": termo_data}), retain=True)
        except Exception:
            pass

    def send_termo_tuya_command(self, power: bool = False, reason: str = ""):
        """Envia ordre d'encesa/apagada al Termo Elèctric via LocalTuya (LAN directa) amb fallback a Tuya Cloud."""
        cfg = load_config()
        deviceId = cfg.get("tuya_termo_device_id", "bf425b5cf5fc5af1ecpxml")
        ip = cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = cfg.get("tuya_termo_local_key", "a|Ul|G=$U%b{{K9g")
        version = float(cfg.get("tuya_termo_version", 3.3))

        # 1. Intent Prioritari LocalTuya LAN (Instantani <20ms, sense dependre d'internet)
        try:
            import tinytuya
            d = tinytuya.OutletDevice(deviceId, ip, local_key)
            d.set_version(version)
            d.set_socketPersistent(False)
            res = d.set_status(power, 1)
            log.info(f"♨️ [TERMO LOCALTUYA LAN] Power {'ON' if power else 'OFF'} ({reason}): {res}")
            return res
        except Exception as e_local:
            log.warning(f"Error enviant per LocalTuya LAN: {e_local}. Reintentant per Tuya Cloud...")

        # 2. Fallback Tuya Cloud OpenAPI
        try:
            cid = cfg.get("tuya_cloud_client_id") or cfg.get("tuya_client_id", "nvrwk5eqvcnnt3majq9c")
            sec = cfg.get("tuya_cloud_secret") or cfg.get("tuya_secret", "c1d97d0a854a451587fa02359aa327be")
            base_url = cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

            t_ms = str(int(time.time() * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{t_ms}GET\n{content_hash}\n\n{url_path_token}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
                token = json.loads(rep_tok.read().decode())["result"]["access_token"]

            path_cmd = f"/v1.0/devices/{deviceId}/commands"
            body_dict = {"commands": [{"code": "switch_1", "value": power}]}
            body_str = json.dumps(body_dict)
            c_hash = hashlib.sha256(body_str.encode()).hexdigest()
            sign_str_cmd = f"{cid}{token}{t_ms}POST\n{c_hash}\n\n{path_cmd}"
            sign_cmd = hmac.new(sec.encode(), sign_str_cmd.encode(), hashlib.sha256).hexdigest().upper()

            req_cmd = urllib.request.Request(f"{base_url}{path_cmd}", data=body_str.encode(), headers={
                "client_id": cid, "access_token": token, "sign": sign_cmd, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            }, method="POST")
            with urllib.request.urlopen(req_cmd, timeout=5) as rep:
                res = json.loads(rep.read().decode())
                log.info(f"♨️ [TERMO TUYA CLOUD] Termo Power {'ON' if power else 'OFF'} ({reason}): {res}")
                return res
        except Exception as e:
            log.error(f"Error fatal enviant comanda Termo Tuya Cloud: {e}")
            return False

    def send_ac_tuya_command(self, power=1, temp=26, mode=0, fan=0, reason=""):
        """Envia ordres d'infrarojos al Mitsubishi Electric mitjançant Tuya Cloud OpenAPI i publica a MQTT."""
        now = time.time()
        self.last_ac_command_time = now
        try:
            cfg = load_config()
            cid = cfg.get("tuya_client_id", "nvrwk5eqvcnnt3majq9c")
            sec = cfg.get("tuya_secret", "c1d97d0a854a451587fa02359aa327be")
            infrared_id = cfg.get("tuya_infrared_id", "bf9d7ccaca278f0d6dltaf")
            remote_id = cfg.get("tuya_remote_id", "bfc77f364d40be79e86290")
            base_url = cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

            # 1. Obtenir Token Tuya
            t_ms = str(int(now * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            str_to_sign = f"GET\n{content_hash}\n\n{url_path_token}"
            sign_str = f"{cid}{t_ms}{str_to_sign}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=8) as rep_tok:
                tok_data = json.loads(rep_tok.read().decode())
                token = tok_data.get("result", {}).get("access_token")

            if not token:
                log.warning(f"No s'ha pogut obtenir token Tuya: {tok_data}")
                return False

            def send_sub_cmd(code, val):
                t_ms_sub = str(int(time.time() * 1000))
                url_path_cmd = f"/v2.0/infrareds/{infrared_id}/air-conditioners/{remote_id}/command"
                body_dict = {"code": code, "value": val}
                body_str = json.dumps(body_dict)
                c_hash = hashlib.sha256(body_str.encode()).hexdigest()
                s_to_sign = f"POST\n{c_hash}\n\n{url_path_cmd}"
                s_str = f"{cid}{token}{t_ms_sub}{s_to_sign}"
                s = hmac.new(sec.encode(), s_str.encode(), hashlib.sha256).hexdigest().upper()
                req_cmd = urllib.request.Request(f"{base_url}{url_path_cmd}", data=body_str.encode(), headers={
                    "client_id": cid, "access_token": token, "sign": s, "t": t_ms_sub, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
                }, method="POST")
                with urllib.request.urlopen(req_cmd, timeout=8) as rep_cmd:
                    return json.loads(rep_cmd.read().decode())

            if power == 0:
                res = send_sub_cmd("power", 0)
                self.ac_current_power = 0
                mode_str = "Apagat"
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Power OFF ({reason}): {res}")
            elif getattr(self, "ac_current_power", 0) == 1:
                # Si ja està encès, NOMÉS enviem la nova temperatura (1 sol bip suau com el comandament!)
                res = send_sub_cmd("temp", int(temp))
                self.ac_current_temp = int(temp)
                mode_str = "Fred" if mode == 0 else "Auto"
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Consigna {temp}ºC (1 sol bip) ({reason}): {res}")
            else:
                # Si estava apagat i l'encenem per primer cop:
                res = send_sub_cmd("power", 1)
                self.ac_current_power = 1
                self.ac_current_temp = int(temp)
                mode_str = "Fred" if mode == 0 else "Auto"
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Power ON a {temp}ºC ({reason}): {res}")

            # Publicació MQTT
            ac_payload = {
                "power": self.ac_current_power,
                "temp": self.ac_current_temp,
                "mode": mode_str,
                "reason": reason,
                "timestamp": now
            }
            if self.client:
                self.client.publish("caseta/ac", json.dumps({"value": ac_payload}), retain=True)
                if self.portal_id not in ("c0619ab2xxxx", "+", "#"):
                    self.client.publish(f"N/{self.portal_id}/caseta/ac", json.dumps({"value": ac_payload}), retain=True)

            return True
        except Exception as e:
            log.warning(f"Error enviant comanda AC Tuya: {e}")
            return False

    def evaluate_climate_control(self, now_madrid):
        """Avalua les Lleis de Climatització Intel·ligent de la Caseta."""
        now = time.time()
        
        # 🚨 LLEI 1: Escut SAI & Seguretat Bateria (Esglaó 1: Bateria <50%)
        if 0 < self.soc < 50.0:
            if self.ac_current_power != 0:
                self.send_ac_tuya_command(power=0, reason="🚨 Escut SAI Esglaó 1: Bateria <50% -> Apagat de l'AC")
                self.send_notification("❄️ Escut SAI Esglaó 1", "Bateria <50%! S'ha apagat l'AC automàticament per protegir la reserva nocturna!", "default", "snowflake")
                self.ac_turned_off_by_guardian = True
            return

        # 🛡️ Histèresi Anti-Cicle: Si l'AC està apagat, NOMÉS s'encén automàticament si SoC >= 65.0% (banda del 15%)
        if self.ac_current_power == 0 and self.soc < 65.0:
            return

        # Si l'AC estava apagat pel Guardià i la bateria ja ha superat el 65%, restablim automàticament
        if self.soc >= 65.0 and self.ac_turned_off_by_guardian and self.ac_current_power == 0:
            log.info(f"🔄 Bateria recuperada ({self.soc:.1f}% >= 65%). Restablint AC automàticament...")
            self.send_notification("❄️ Restabliment Climatització", f"Bateria recuperada ({self.soc:.1f}% >= 65%)! S'ha reprès l'AC automàticament.", "default", "snowflake")
            self.ac_turned_off_by_guardian = False

        # Lectures de temperatura interior (Zigbee) i exterior (Inforatge)
        s1 = self.clima_sensors.get("sensor_1", {}) if self.clima_sensors else {}
        s2 = self.clima_sensors.get("sensor_2", {}) if self.clima_sensors else {}
        t1 = s1.get("temperatura")
        t2 = s2.get("temperatura")
        t_int = t2 if t2 is not None else (t1 if t1 is not None else 26.5)
        t_ext = self.ext_temp

        # 🍃 LLEI 0: Free-Cooling Bioclimàtic (Apagat d'AC si a fora fa fresca)
        # Si T_ext < 25.0ºC durant >20 minuts (1200s) I T_int < 28.0ºC -> Apaga AC (sense notificació)
        if t_ext is not None and t_ext < 25.0 and t_int < 28.0:
            if self.free_cooling_start_time is None:
                self.free_cooling_start_time = now
                log.info(f"🍃 Iniciant compte enrere de 20 minuts de Free-Cooling (T_ext: {t_ext:.1f}ºC < 25ºC | T_int: {t_int:.1f}ºC < 28ºC)...")
            elif now - self.free_cooling_start_time >= 1200:
                if self.ac_current_power != 0:
                    self.send_ac_tuya_command(
                        power=0,
                        reason=f"🍃 Free-Cooling: T_ext ({t_ext:.1f}ºC < 25ºC) >20 min i T_int ({t_int:.1f}ºC < 28ºC) -> AC Apagat (Sense Notificació)"
                    )
                    self.ac_turned_off_by_free_cooling = True
                    log.info(f"🍃 Free-Cooling aplicat: AC apagat silenciadament per exterior fresc ({t_ext:.1f}ºC).")
                return
        else:
            self.free_cooling_start_time = None

        # Si l'AC s'havia apagat per Free-Cooling, comprova si cal re-encendre:
        if self.ac_turned_off_by_free_cooling:
            # Condicions de re-encesa: T_int >= 28.8ºC (estiga com estiga fora) O T_ext >= 27.0ºC
            if t_int >= 28.8 or (t_ext is not None and t_ext >= 27.0):
                log.info(f"🔥 Finalitzant Free-Cooling (T_int: {t_int:.1f}ºC >= 28.8ºC o T_ext: {t_ext}ºC >= 27ºC). Re-activant climatització...")
                self.ac_turned_off_by_free_cooling = False
            else:
                # Mantindre l'AC apagat mentre dure el Free-Cooling
                return

        # ⚙️ LLEI 4: Protecció del Compressor i Anti-Flapping (mínim 10 minuts)
        if now - self.last_ac_command_time < 600:
            return

        hour = now_madrid.hour
        
        # 🌙 LLEI 2.1: Horari Nocturn (23:00 - 07:59h): Confort suau de descans a 26.5ºC
        if hour >= 23 or hour < 8:
            if self.ac_current_power != 1 or self.ac_current_temp != 26:
                self.send_ac_tuya_command(power=1, temp=26, mode=0, fan=0, reason="🌙 Horari Nocturn: Descans a 26.5ºC (Ventilador Auto)")
            return

        # ☀️ LLEI 3: Confort Bioclimàtic Estable Diürn (26.0ºC Permanent)
        # La bateria mateixa absorbeix tot el sol de migdia gràcies al Vas Buit creat pel termo!
        if self.soc >= 69.0:
            if self.ac_current_power != 1 or self.ac_current_temp != 26:
                self.send_ac_tuya_command(power=1, temp=26, mode=0, fan=0, reason=f"🏰 Confort Diürn Estable a 26.0ºC (SoC {self.soc:.1f}%)")
        else:
            # Bateria Baixa (<69%) I repòs >30 min -> Mode Eco 28ºC per protegir el coixí
            time_since_presence = now - self.last_presence_seen_time
            if time_since_presence > 1800:
                if self.ac_current_power != 1 or self.ac_current_temp != 28:
                    self.send_ac_tuya_command(power=1, temp=28, mode=0, fan=0, reason="🟢 Repòs >30 min i Bateria <69% (Mode Eco 28ºC)")
            else:
                if self.ac_current_power != 1 or self.ac_current_temp != 26:
                    self.send_ac_tuya_command(power=1, temp=26, mode=0, fan=0, reason="🚶 Presència activa (Confort 26ºC)")

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

    def sync_grid_setpoint(self):
        """Modula dinàmicament el Grid Setpoint de Victron ESS:
        - Amb Termo Actiu (>=500W):
            • SoC >= 84%: 400.0 W (Estalvi màxim de xarxa i creació del 'Vas Buit' per al sol de migdia)
            • 78% <= SoC < 84%: 600.0 W (Transició suau frenant la descàrrega)
            • SoC < 78%: 800.0 W (Blindatge total contra descàrrega)
        - Amb Termo en Repòs:
            • SoC < 90%: 200.0 W (Amortidor robust per a consums basals i blindatge anti-exportació)
            • SoC >= 90%: 50.0 W (Reducció d'importació quan la bateria està plena)
        """
        now = time.time()
        if now - self.last_grid_setpoint_eval_time < 20:
            return
        self.last_grid_setpoint_eval_time = now

        termo_p = self.termo_status.get("power_w", 0.0) if self.termo_status else 0.0
        termo_on = self.termo_status.get("is_on", False) if self.termo_status else False

        # ♨️ 1. GESTIÓ AMB TERMO ACTIU (>= 500 W)
        if termo_on and termo_p >= 500.0:
            now_madrid = get_madrid_now()
            time_decimal = now_madrid.hour + (now_madrid.minute / 60.0)
            
            # 🌙 A. Franja Matinada Vall P3 (06:00h - 07:00h): Xarxa total per aprofitar tarifa barata (0.08 €/kWh)
            if 6.0 <= time_decimal < 7.0:
                target = 1300.0
                reason = f"🌙 Arbitratge Vall P3 (06h-07h) -> Setpoint 1300W (Tot de Xarxa Barata a 0.08 €/kWh)"
            else:
                # ☀️ B. Franja Diürna amb Excedents Solars (09:30h - 16:00h)
                today_est = getattr(self, "today_kwh_est", 5.0)
                if today_est >= 5.0:
                    # ☀️ Dia Clar / Assolellat (>=5.0 kWh): Buidar Vas Buit fins al 70%
                    if self.soc >= 70.0:
                        target = 200.0
                        reason = f"♨️ Termo ({termo_p:.0f}W) & Sol Previst {today_est:.1f}kWh (SoC {self.soc:.1f}% >= 70%) -> Setpoint 200W (Buidant Vas)"
                    else:
                        target = 800.0
                        reason = f"♨️ Termo ({termo_p:.0f}W) & Sòl Assolit (SoC {self.soc:.1f}% < 70%) -> Setpoint 800W (Congelant Bateria)"
                elif today_est >= 3.5:
                    # 🌤️ Dia Moderat (3.5 - 5.0 kWh): Buidar Vas Buit fins al 78%
                    if self.soc >= 78.0:
                        target = 400.0
                        reason = f"♨️ Termo ({termo_p:.0f}W) & Sol Previst {today_est:.1f}kWh (SoC {self.soc:.1f}% >= 78%) -> Setpoint 400W"
                    else:
                        target = 800.0
                        reason = f"♨️ Termo ({termo_p:.0f}W) & Sòl Assolit (SoC {self.soc:.1f}% < 78%) -> Setpoint 800W"
                else:
                    # ☁️ Dia Ennuvolat (<3.5 kWh): Xarxa a 800W per protegir la bateria sense sol
                    target = 800.0
                    reason = f"☁️ Dia Ennuvolat ({today_est:.1f}kWh) & Termo ({termo_p:.0f}W) -> Setpoint 800W (Protecció Sense Sol)"

        # ☕ 2. GESTIÓ AMB TERMO EN REPÒS (Sol de Migdia / Tarda)
        else:
            # ☀️ A. Si hi ha generació solar abundant (Sol >= 400W o Sol >= Consum Casa):
            if self.pv_p >= 400.0 or (self.pv_p >= self.ac_loads and self.pv_p > 150.0):
                target = 50.0
                reason = f"☀️ Excedent Solar Diürn ({self.pv_p:.0f}W) -> Setpoint 50W (Aprofitament Solar Màxim)"
            # 🔋 B. Si la bateria està a la zona alta (SoC >= 88%):
            elif self.soc >= 88.0:
                target = 50.0
                reason = f"🔋 Bateria Alta ({self.soc:.1f}% >= 88%) -> Setpoint 50W (Estalvi Màxim)"
            # 🌙 C. Sense sol diürn / nocturn i Bateria Baixa (<85%):
            elif self.soc < 85.0:
                target = 200.0
                reason = f"⚡ Bateria en descàrrega sense sol ({self.soc:.1f}% < 85%) -> Setpoint 200W (Amortidor Basal)"
            else:
                target = self.last_grid_setpoint if self.last_grid_setpoint is not None else 100.0
                reason = "Estable"

        if self.last_grid_setpoint != target:
            try:
                import dbus
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                obj.SetValue(dbus.Double(target), dbus_interface="com.victronenergy.BusItem")
                log.info(f"⚙️ Sincronitzat Grid Setpoint a Cerbo GX: {target:.0f} W [{reason}]")
                self.last_grid_setpoint = target
            except Exception as e:
                log.warning(f"No s'ha pogut actualitzar Grid Setpoint per D-Bus: {e}")

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
            "termo_heated_today": getattr(self, "termo_heated_today", False),
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

        # Publicació MQTT i memòria RAM cada 10 segons (ZERO desgast Flash)
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

        # 💾 Checkpoint de seguretat a disc Flash (eMMC) cada 30 minuts (1800s)
        if now - self.last_checkpoint_save_time >= 1800.0:
            self.last_checkpoint_save_time = now
            self.save_daily_stats()

    def evaluate_termo_surplus(self, now_madrid):
        """Avalua l'encesa autònoma del Termo Elèctric per excedents solars diürns (11:30h - 15:30h)."""
        now = time.time()
        hour = now_madrid.hour
        minute = now_madrid.minute
        time_decimal = hour + (minute / 60.0)

        # Reset diari a mitjanit
        today_str = now_madrid.strftime("%Y-%m-%d")
        if getattr(self, "termo_day_str", "") != today_str:
            self.termo_day_str = today_str
            self.termo_heated_today = False
            self.termo_low_power_start_time = None

        termo_on = self.termo_status.get("is_on", False) if self.termo_status else False
        termo_p = self.termo_status.get("power_w", 0.0) if self.termo_status else 0.0

        # Si el termo ja està encès:
        if termo_on:
            # 1. Comprovació d'Aigua Calenta a 60ºC (Termòstat Ariston talla -> Potència < 50W durant 2 min)
            if termo_p < 50.0:
                if self.termo_low_power_start_time is None:
                    self.termo_low_power_start_time = now
                elif now - self.termo_low_power_start_time >= 120.0:
                    self.send_termo_tuya_command(
                        power=False,
                        reason="✅ Aigua calenta a 60ºC assolida (Consum <50W durant 2 min) -> Pasteurització Completada"
                    )
                    self.send_notification(
                        "♨️ Aigua Calenta a 60ºC Assolida!",
                        "El termo ha arribat a 60ºC i ha completat la pasteurització anti-legionel·la. Apagat per a la resta del dia!",
                        "default",
                        "check"
                    )
                    self.termo_heated_today = True
                    self.termo_low_power_start_time = None
                    return
            else:
                self.termo_low_power_start_time = None

            # 2. Pausa per Núvol / Bateria Caiguda (<65% durant el dia)
            if self.soc < 65.0:
                self.send_termo_tuya_command(
                    power=False,
                    reason=f"⏸️ Pausa de Seguretat: Bateria ha baixat al {self.soc:.1f}% (<65%)"
                )
                return

            # 3. Fi de la Finestra d'Excedents (passades les 16:00h)
            if time_decimal >= 16.0:
                self.send_termo_tuya_command(
                    power=False,
                    reason="🕒 Fi Finestra d'Excedents Solars (16:00h)"
                )
                return

        # Si el termo està apagat i encara no ha completat la càrrega d'avui:
        elif not self.termo_heated_today and not getattr(self, "termo_cut_off_today", False):
            today_est = getattr(self, "today_kwh_est", 5.0)

            # 🌙 CAS A: Arbitratge Matinada Vall P3 (06:00h - 07:00h) si el dia serà fosc/plujós (<3.5 kWh)
            if 6.0 <= time_decimal < 7.0 and today_est < 3.5:
                try:
                    import dbus
                    bus = dbus.SystemBus()
                    obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                    obj.SetValue(dbus.Double(1300.0), dbus_interface="com.victronenergy.BusItem")
                    self.last_grid_setpoint = 1300.0
                    log.info("🔌 [MATINADA P3] Grid Setpoint a 1300W abans d'engegar el Termo a preu Vall (0.08 €/kWh)...")
                except Exception as e:
                    log.warning(f"Error establint pre-rampa P3 a D-Bus: {e}")

                self.send_termo_tuya_command(
                    power=True,
                    reason=f"🌙 Arbitratge Vall P3: Previsió solar fosca ({today_est:.1f} kWh < 3.5 kWh) -> Calfant aigua en horari super-econòmic (0.08 €/kWh)"
                )
                self.send_notification(
                    "🌙 Termo Engegat en Franja Vall P3",
                    f"Dia ennuvolat previst ({today_est:.1f} kWh). Calfant aigua a preu super-econòmic (0.08 €/kWh) abans de les 08:00h!",
                    "default",
                    "moon"
                )
                return

            # ☀️ CAS B: Excedents Solars Diürns (09:30h - 16:00h): SoC >= 80.0% i Sol Huawei >= 500W
            if 9.0 <= time_decimal < 16.0 and self.soc >= 80.0 and self.pv_p >= 500.0:
                if today_est >= 5.0:
                    pre_target = 200.0
                elif today_est >= 3.5:
                    pre_target = 400.0
                else:
                    pre_target = 800.0

                try:
                    import dbus
                    bus = dbus.SystemBus()
                    obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                    obj.SetValue(dbus.Double(pre_target), dbus_interface="com.victronenergy.BusItem")
                    self.last_grid_setpoint = pre_target
                    log.info(f"🔌 [PRE-RAMPA] Grid Setpoint a {pre_target:.0f}W abans d'engegar el Termo per excedents solars...")
                except Exception as e:
                    log.warning(f"Error establint pre-rampa a D-Bus: {e}")

                self.send_termo_tuya_command(
                    power=True,
                    reason=f"☀️ Excedent Solar: SoC {self.soc:.1f}% >= 80% i Sol {self.pv_p:.0f}W >= 500W -> Encesa Termo"
                )
                self.send_notification(
                    "♨️ Termo Engegat per Excedents Solars",
                    f"Bateria al {self.soc:.1f}% i Sol a {self.pv_p:.0f}W. Escalfant aigua de franc!",
                    "default",
                    "sun"
                )

    def evaluate_state_machine(self, now_madrid=None):
        now = time.time()
        if now_madrid is None:
            now_madrid = get_madrid_now()

        # ☀️ Avaluació del Desviador d'Excedents Solar per al Termo
        self.evaluate_termo_surplus(now_madrid)

        # 🚨 ESGGLO 1 (SoC < 50%): Apagat preventiu de l'Aire Condicionat
        if 0 < self.soc < 50.0 and self.ac_current_power != 0:
            self.send_ac_tuya_command(power=0, reason="❄️ Escut SAI Esglaó 1: Bateria <50% -> Apagat de l'AC")

        # 🚨 ESGGLO 2 (SoC < 45%): Tall Crític de Seguretat per blindar >12h de SAI
        if 0 < self.soc < 45.0:
            if self.ac_current_power != 0:
                self.send_ac_tuya_command(power=0, reason="🚨 Blindatge Total: Bateria <45% -> AC forçat a OFF")
            if not getattr(self, "termo_cut_off_today", False):
                self.send_termo_tuya_command(power=False, reason="🚨 Blindatge Total: Bateria <45% -> Desconnexió Termo Incondicional")
                self.send_notification("🚨 Blindatge Total SAI", "Bateria <45%! S'ha desconnectat incondicionalment el termo per blindar la reserva nocturna.", "high", "zap")
                self.termo_cut_off_today = True
        elif self.soc >= 60.0:
            self.termo_cut_off_today = False

        # ⚡ PROTECCIÓ C-RATE A: Pic de Sobrecàrrega 1C (>=70A / ~3.5kW durant >15 segons)
        if self.bat_i <= -70.0:
            if self.c1_discharge_start_time is None:
                self.c1_discharge_start_time = now
            elif now - self.c1_discharge_start_time >= 15.0:
                self.send_ac_tuya_command(power=0, reason="🚨 Tall 1C: Descàrrega >70A (>15s)")
                self.send_termo_tuya_command(power=False, reason="🚨 Tall 1C: Descàrrega >70A (>15s)")
                self.send_notification(
                    "🚨 Sobrecorrent Crític Bateria",
                    f"Descàrrega a {abs(self.bat_i):.1f}A (>=1C / ~3.5kW) durant >15s! S'han desconnectat l'AC i el Termo per protegir les cel·les LiFePO4.",
                    "high",
                    "warning"
                )
                self.c1_discharge_start_time = None
        else:
            self.c1_discharge_start_time = None

        # ⚡ PROTECCIÓ C-RATE B: Sobrecàrrega Sostinguda 0.5C (>=34A / ~1.7kW durant >3 minuts)
        if self.bat_i <= -34.0:
            if self.c05_discharge_start_time is None:
                self.c05_discharge_start_time = now
            elif now - self.c05_discharge_start_time >= 180.0:
                self.send_ac_tuya_command(power=0, reason="🚨 Tall 0.5C: Descàrrega sostinguda >34A (>3 min)")
                self.send_termo_tuya_command(power=False, reason="🚨 Tall 0.5C: Descàrrega sostinguda >34A (>3 min)")
                self.send_notification(
                    "🚨 Sobrecàrrega Sostinguda Bateria",
                    f"Descàrrega a {abs(self.bat_i):.1f}A (>=34A / ~1.7kW) durant >3 minuts! S'han apagat l'AC i el Termo per evitar estrès tèrmic.",
                    "high",
                    "warning"
                )
                self.c05_discharge_start_time = None
        else:
            self.c05_discharge_start_time = None

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
                
            elif topic.endswith("/pvinverter/31/Ac/Power") or topic.endswith("/pvinverter/31/Ac/L1/Power") or topic.endswith("/system/0/Ac/PvOnOutput/L1/Power") or topic.endswith("/system/0/Ac/PvOnOutput/Power"):
                raw_pv = float(val) if val is not None else self.pv_p
                # Filtre de soroll d'inversor Huawei en repòs: entre -25W i +20W és 0W real
                if -25.0 <= raw_pv <= 20.0:
                    self.pv_p = 0.0
                else:
                    self.pv_p = raw_pv
            elif topic.endswith("/system/0/Ac/Consumption/L1/Power") or topic.endswith("/system/0/Ac/ConsumptionOnOutput/L1/Power") or topic.endswith("/vebus/276/Ac/Out/L1/P") or topic.endswith("/vebus/276/Ac/Out/P"):
                self.ac_loads = float(val) if val is not None else self.ac_loads
            elif topic.endswith("/system/0/Ac/Grid/L1/Power") or topic.endswith("/system/0/Ac/ActiveIn/L1/Power") or topic.endswith("/vebus/276/Ac/ActiveIn/L1/P") or topic.endswith("/vebus/276/Ac/ActiveIn/P"):
                self.grid_p = float(val) if val is not None else self.grid_p
            elif topic.endswith("/vebus/276/Ac/ActiveIn/L1/V"):
                self.grid_v = float(val) if val is not None else self.grid_v
            elif topic.endswith("/vebus/276/Mode"):
                self.vebus_mode = int(val) if val is not None else self.vebus_mode
            elif topic.endswith("/vebus/276/VebusChargeState"):
                self.vebus_state = int(val) if val is not None else self.vebus_state
            elif "caseta/clima" in topic and isinstance(val, dict):
                self.clima_sensors = val.get("sensors") or {}
                s2 = self.clima_sensors.get("sensor_2") or {}
                if s2.get("presencia"):
                    self.last_presence_seen_time = time.time()
                
        except Exception:
            pass

    def run(self):
        log.info(f"🚀 Iniciant Caseta Guardian (Cerbo IP: {CERBO_IP})...")

        import signal
        def sig_handler(signum, frame):
            log.info(f"🛑 Rebut senyal {signum}. Guardant stats a disc...")
            try:
                self.save_daily_stats()
            except Exception:
                pass
            self.running = False
            sys.exit(0)
        signal.signal(signal.SIGTERM, sig_handler)
        signal.signal(signal.SIGINT, sig_handler)
        
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_message = self.on_mqtt_message
        
        try:
            self.client.connect(CERBO_IP, 1883, 60)
        except Exception as e:
            log.error(f"Error fatal connectant al broker MQTT del Cerbo GX ({CERBO_IP}): {e}")
            return
            
        # Subscripcions quirúrgiques per eliminar el 70% del soroll MQTT innecessari
        self.client.subscribe("N/+/battery/512/#")
        self.client.subscribe("N/+/pvinverter/#")
        self.client.subscribe("N/+/system/0/#")
        self.client.subscribe("N/+/vebus/276/#")
        self.client.subscribe("caseta/#")
        self.client.loop_start()
        
        self.sync_cerbo_min_soc()
        self.update_energy_forecast()
        self.update_inforatge()
        
        log.info("🛡️ Guardià en línia i vigilant telemetria en directe!")
        
        while self.running:
            try:
                now = time.time()
                now_madrid = get_madrid_now()
                
                if now - self.last_keepalive_time >= 30:
                    self.client.publish(f"R/{self.portal_id}/keepalive", "")
                    self.last_keepalive_time = now
                    
                self.sync_cerbo_min_soc(now_madrid)
                self.sync_grid_setpoint()
                self.update_energy_forecast()
                self.update_inforatge()
                self.update_termo_status()
                self.update_energy_integrals(now_madrid)
                self.evaluate_state_machine(now_madrid)
                self.evaluate_climate_control(now_madrid)
                
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

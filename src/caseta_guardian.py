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
import json
import logging
import os
import re
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

# Imports dels mòduls
from caseta_guardian_modules.config import (
    load_config, require_config,
    TOTAL_NOMINAL_KWH, BATTERY_SOH_FACTOR, NET_CAPACITY_KWH,
    P1_RATE, P2_RATE, P3_RATE, POTENCIA_FIXED_DAY, TAX_MULTIPLIER
)
from caseta_guardian_modules.notifications import NotificationManager
from caseta_guardian_modules.tuya_manager import TuyaManager
from caseta_guardian_modules.energy_forecast import EnergyForecast
from caseta_guardian_modules.state_machine import StateMachine
from caseta_guardian_modules.mqtt_client import MQTTClient
from caseta_guardian_modules.dbus_telemetry import DBusTelemetry


def get_madrid_now() -> datetime.datetime:
    """Retorna la data i hora exacta a la zona horària oficial de València/Madrid (peninsular)."""
    if MADRID_TZ:
        return datetime.datetime.now(MADRID_TZ)
    return datetime.datetime.now()


def madrid_log_timetuple(*args):
    return get_madrid_now().timetuple()


logging.Formatter.converter = madrid_log_timetuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("caseta-guardian")


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


config = load_config()

CERBO_IP = os.environ.get("CERBO_IP", config.get("cerbo_ip", "127.0.0.1" if os.path.exists("/opt/victronenergy") else "192.168.1.106"))
PORTAL_ID = os.environ.get("PORTAL_ID", config.get("portal_id", "48e7da8782fd"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", config.get("ntfy_topic", "caseta_ador_alerts"))
HISTORY_CSV_FILE = os.path.expanduser("~/.local/share/caseta-guardian/historic_diari.csv")


class CasetaGuardian:
    def __init__(self):
        self.portal_id = PORTAL_ID
        self.running = True

        # Inicialització dels mòduls
        self.notifications = NotificationManager(NTFY_TOPIC)
        self.tuya = TuyaManager(config)
        self.forecast = EnergyForecast(config)
        self.state_machine = StateMachine(self.tuya, self.notifications)
        self.mqtt_client = MQTTClient(CERBO_IP, PORTAL_ID)
        self.dbus_telemetry = DBusTelemetry()

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
        self.last_inforatge_time = 0.0
        self.last_stats_calc_time = 0.0
        self.last_stats_publish_time = 0.0
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
        self.ac_current_power = None
        self.ac_current_temp = 26
        self.ac_turned_off_by_guardian = False
        self.ac_turned_off_by_free_cooling = False
        self.ac_manual_off_time = None
        self.ac_manual_on_time = None
        self.last_guardian_ac_power_off_time = 0.0
        self.free_cooling_start_time = None
        self.ext_temp = None
        self.ext_humidity = 50.0
        self.rain_today = 0.0
        self.clima_sensors = {}

        # Termo Elèctric (Tuya Plug / LocalTuya)
        self.last_termo_update_time = 0.0
        self.termo_status = {}
        self.termo_heated_today = False
        self.termo_low_power_start_time = None
        self.termo_kwh_today = 0.0
        self.termo_start_time_str = ""
        self.termo_end_time_str = ""
        self.termo_active_seconds_today = 0.0
        self.termo_currently_heating = False
        self.termo_last_heated_date = "2026-08-28"
        self.termo_last_60_ts = None
        self.termo_est_temp = 60.0
        self.last_termo_calc_time = time.time()

        # Recuperació d'estat persistent a disc
        try:
            p_file = "/data/caseta-guardian/caseta_daily_stats.json" if os.path.exists("/data/caseta-guardian") else "/tmp/caseta_daily_stats.json"
            if os.path.exists(p_file):
                with open(p_file, "r") as _f:
                    _d = json.load(_f)
                    self.termo_last_heated_date = _d.get("termo_last_heated_date", "2026-08-28")
                    self.termo_last_60_ts = _d.get("termo_last_60_ts")
                    self.termo_est_temp = float(_d.get("termo_est_temp", 60.0))
                    if self.termo_last_60_ts is None and _d.get("termo_heated_today") and _d.get("termo_end_time_str"):
                        try:
                            _d_str = _d.get("date", self.current_day_str)
                            _e_str = _d.get("termo_end_time_str")
                            _dt = datetime.datetime.strptime(f"{_d_str} {_e_str}", "%Y-%m-%d %H:%M")
                            if MADRID_TZ:
                                self.termo_last_60_ts = _dt.replace(tzinfo=MADRID_TZ).timestamp()
                            else:
                                self.termo_last_60_ts = _dt.timestamp()
                        except Exception:
                            pass
        except Exception:
            pass

        # Endoll Doble Cuina (LocalTuya: Microones/Torradora + Cafetera)
        self.doble_status = {}
        self.doble_kwh_today = 0.0
        self.last_doble_update_time = 0.0
        self.last_doble_calc_time = time.time()

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
        try:
            if os.path.exists("/tmp/caseta_inforatge_cache.json"):
                with open("/tmp/caseta_inforatge_cache.json", "r") as f:
                    _inf = json.load(f)
                    self.ext_temp = _inf.get("temperatura")
                    self.ext_humidity = float(_inf.get("humitat", 50.0))
                    self.rain_today = float(_inf.get("pluja_avui", 0.0))
        except Exception:
            pass

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
                    self.termo_last_heated_date = str(data.get("termo_last_heated_date", getattr(self, "termo_last_heated_date", "2026-09-28")))
                    self.termo_last_60_ts = data.get("termo_last_60_ts", getattr(self, "termo_last_60_ts", None))
                    self.termo_est_temp = float(data.get("termo_est_temp", getattr(self, "termo_est_temp", 60.0)))
                    self.termo_kwh_today = float(data.get("termo_kwh_today", 0.0))
                    self.termo_start_time_str = str(data.get("termo_start_time_str", ""))
                    self.termo_end_time_str = str(data.get("termo_end_time_str", ""))
                    self.termo_active_seconds_today = float(data.get("termo_active_seconds_today", 0.0))
                    if self.termo_last_60_ts is None and self.termo_heated_today and self.termo_end_time_str:
                        try:
                            end_dt = datetime.datetime.strptime(f"{self.current_day_str} {self.termo_end_time_str}", "%Y-%m-%d %H:%M")
                            if MADRID_TZ:
                                self.termo_last_60_ts = end_dt.replace(tzinfo=MADRID_TZ).timestamp()
                            else:
                                self.termo_last_60_ts = end_dt.timestamp()
                        except Exception:
                            pass
                    self.doble_kwh_today = float(data.get("doble_kwh_today", 0.0))
                    saved_off = data.get("ac_manual_off_time")
                    if saved_off and (time.time() - float(saved_off) < 3600.0):
                        self.ac_manual_off_time = float(saved_off)
                    saved_on = data.get("ac_manual_on_time")
                    if saved_on and (time.time() - float(saved_on) < 7200.0):
                        self.ac_manual_on_time = float(saved_on)
                    log.info(f"💾 Recuperats acumulats previs d'avui ({self.current_day_str}): {self.solar_kwh_today:.2f} kWh solars, {self.consumption_kwh_today:.2f} kWh consum (Termo: {self.termo_kwh_today:.2f} kWh, Cuina: {self.doble_kwh_today:.2f} kWh).")
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
            is_hol = "SI" if self.is_holiday else "NO"

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
            self.ext_humidity = float(hum) if hum is not None else 50.0
            self.rain_today = float(pluja) if pluja is not None else 0.0
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

            if self.mqtt_client.client:
                self.mqtt_client.publish("caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
                self.mqtt_client.publish_to_portal("caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
            log.info(f"📍 Inforatge Ador: Ext {temp}ºC | Hum {hum}% | Vent {vent_vel} km/h {vent_dir} | Pressió {press} hPa")
        except Exception as e:
            log.warning(f"Error consultant Inforatge Ador: {e}")

    def update_ac_status(self):
        """Consulta l'estat real del comandament virtual de l'AC a Tuya Cloud cada 2 minuts (120s) amb token en memòria cau."""
        now = time.time()
        if now - getattr(self, "last_ac_status_query_time", 0.0) < 45.0:
            return
        self.last_ac_status_query_time = now

        cid = require_config(config, "tuya_client_id", "Client ID de Tuya Cloud")
        sec = require_config(config, "tuya_secret", "Secret de Tuya Cloud")
        base_url = config.get("tuya_base_url", "https://openapi.tuyaeu.com")
        remote_id = require_config(config, "tuya_remote_id", "ID del comandament virtual de l'AC")

        try:
            token = self.tuya.get_tuya_access_token(cid, sec, base_url)
            if not token:
                return

            path_status = f"/v1.0/devices/{remote_id}/status"
            t_ms = str(int(time.time() * 1000))
            sign_str = f"{cid}{token}{t_ms}GET\n{content_hash if 'content_hash' in locals() else hashlib.sha256(b'').hexdigest()}\n\n{path_status}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()

            req_status = urllib.request.Request(f"{base_url}{path_status}", headers={
                "client_id": cid, "access_token": token, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_status, timeout=5) as rep:
                res = json.loads(rep.read().decode())
                if res.get("success", False):
                    status_list = res.get("result", [])
                    status_map = {item["code"]: item["value"] for item in status_list}
                    pwr_val = status_map.get("power", "0")
                    pwr = 1 if str(pwr_val) in ("1", "true", "True") else 0
                    temp = int(status_map.get("temp", self.ac_current_temp))
                    mode_val = str(status_map.get("mode", "0"))
                    mode_str = "Fred" if mode_val in ("0", "cool") else "Auto"

                    prev_pwr = getattr(self, "ac_current_power", None)

                    if prev_pwr is not None:
                        # ✋ Detecció d'apagat manual per l'usuari
                        if prev_pwr == 1 and pwr == 0:
                            dt_guardian_off = now - getattr(self, "last_guardian_ac_power_off_time", 0.0)
                            if dt_guardian_off > 45.0:
                                self.ac_manual_off_time = now
                                self.ac_manual_on_time = None
                                fin_dt = get_madrid_now() + datetime.timedelta(seconds=3600)
                                fin_str = fin_dt.strftime("%H:%M")
                                log.info(f"✋ [CLIMA] Detectat apagat manual de l'AC per l'usuari (Tuya/App). Bloqueig d'encesa automàtica durant 60 minuts (fins a les {fin_str}h).")
                                self.notifications.send_notification(
                                    "✋ AC Apagat Manualment",
                                    f"S'ha detectat l'apagat manual de l'aire condicionat. No es tornarà a encendre automàticament fins a les {fin_str}h (pausa d'1 hora).",
                                    "default",
                                    "hand"
                                )
                        elif prev_pwr == 0 and pwr == 1:
                            dt_guardian_cmd = now - getattr(self, "last_ac_command_time", 0.0)
                            if dt_guardian_cmd > 45.0:
                                self.ac_manual_on_time = now
                                log.info("▶️ [CLIMA] Detectada encesa manual de l'AC per l'usuari a Tuya/comandament. Prioritat manual activa (es mantindrà encès 2 hores).")
                            if getattr(self, "ac_manual_off_time", None) is not None:
                                log.info("▶️ [CLIMA] Cancel·lant la pausa d'apagat per encesa manual.")
                                self.ac_manual_off_time = None
                            self.ac_turned_off_by_free_cooling = False
                            self.free_cooling_start_time = None
                        elif prev_pwr == 1 and pwr == 1 and temp != self.ac_current_temp:
                            dt_guardian_cmd = now - getattr(self, "last_ac_command_time", 0.0)
                            if dt_guardian_cmd > 45.0:
                                self.ac_manual_on_time = now
                                log.info(f"🌡️ [CLIMA] Canvi manual de consigna a {temp}ºC per l'usuari. Prioritat manual estesa 2 hores.")

                    self.ac_current_power = pwr
                    self.ac_current_temp = temp

                    # Càlcul del motiu per a telemetria MQTT
                    manual_on = bool(getattr(self, "ac_manual_on_time", None) and (now - self.ac_manual_on_time < 7200.0))
                    manual_off = bool(getattr(self, "ac_manual_off_time", None) and (now - self.ac_manual_off_time < 3600.0))

                    if pwr == 1:
                        if manual_on:
                            rem_on = int(round((7200.0 - (now - self.ac_manual_on_time)) / 60.0))
                            reason_txt = f"Manual Usuari ({rem_on} min prioritat)"
                        else:
                            reason_txt = "Automàtic / Guardià"
                    elif manual_off:
                        rem_m = int(round((3600.0 - (now - self.ac_manual_off_time)) / 60.0))
                        reason_txt = f"Pausa Manual Usuari ({rem_m} min restants)"
                    else:
                        reason_txt = "En Repòs"

                    ac_payload = {
                        "power": pwr,
                        "temp": temp,
                        "mode": mode_str if pwr == 1 else "Apagat",
                        "reason": reason_txt,
                        "manual_on": manual_on,
                        "manual_off": manual_off,
                        "timestamp": now
                    }
                    if self.mqtt_client.client:
                        self.mqtt_client.publish("caseta/ac", json.dumps({"value": ac_payload}), retain=True)
                        self.mqtt_client.publish_to_portal("caseta/ac", json.dumps({"value": ac_payload}), retain=True)
        except Exception as e:
            log.debug(f"Error consultant estat AC Tuya: {e}")

    def evaluate_climate_control(self, now_madrid):
        """Avalua les Lleis de Climatització Intel·ligent de la Caseta."""
        now = time.time()

        # 🍂 Climatització Automàtica Desactivada (Temporada suau de tardor/hivern)
        if 0 < self.soc < 60.0 and getattr(self, "ac_current_power", 0) != 0:
            self.tuya.send_ac_command(power=0, reason="🚨 Escut SAI: Bateria <60% -> Apagat de l'AC")
            self.notifications.send_notification("❄️ Escut SAI Clima", "Bateria <60%! S'ha apagat l'AC automàticament per protegir la reserva de bateria!", "default", "snowflake")
            self.ac_turned_off_by_guardian = True
        return

    def sync_cerbo_min_soc(self, now_madrid=None):
        """Avalua periòdicament el balanç de Sol vs Consum i horari circadiari d'estiu per modular el Minimum SOC."""
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

        # 2. 🌙 Nit i Matinada Vall P3 (00:00h a 07:59h Madrid): 100% Minimum SOC & Càrrega Plena a 0.08 €/kWh
        elif time_decimal < 8.0:
            target = 100.0
            phase_name = "🌙 Nit i Matinada Vall P3 (100% Minimum SOC & Zero Descàrrega a 0.08 €/kWh)"

        # 3. ☀️ Finestra Diürna Adaptativa (08:00h a 16:29h Madrid): Modulació Dinàmica per Sol Real
        elif 8.0 <= time_decimal < 16.5:
            today_est = getattr(self, "today_kwh_est", 4.5)
            rem_sun = max(0.0, today_est - self.solar_kwh_today)
            cur_pv = getattr(self, "pv_p", 0.0)

            # ☀️ Cas 1: Sol Abundant (Sol Real >= 500W O Sol Restant >= 4.5 kWh amb Sol Actual >= 250W)
            if (cur_pv >= 500.0) or (rem_sun >= 4.5 and cur_pv >= 250.0):
                target = 68.0
                phase_name = f"☀️ Sol Radiant ({cur_pv:.0f}W, {rem_sun:.1f}kWh restants) -> 68% Vas Buit Gran"
            # 🌤️ Cas 2: Sol Moderat (Sol Real >= 200W O Sol Restant >= 3.0 kWh amb Sol Actual >= 100W)
            elif (cur_pv >= 200.0) or (rem_sun >= 3.0 and cur_pv >= 100.0):
                target = 78.0
                phase_name = f"🌤️ Sol Moderat ({cur_pv:.0f}W, {rem_sun:.1f}kWh restants) -> 78% Vas Equilibrat"
            # ⛅ Cas 3: Sol Feble / Núvols / Cel Tapat (Sol Real < 100W)
            else:
                target = 88.0
                phase_name = f"⛅ Sol Feble/Núvols ({cur_pv:.0f}W, {rem_sun:.1f}kWh restants) -> 88% Blindatge Bateria"

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
            # 1. Intent D-Bus Natiu si estem a Cerbo GX (Instantani <0.1ms i atòmic)
            try:
                import dbus
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/BatteryLife/MinimumSocLimit")
                obj.SetValue(dbus.Double(target), dbus_interface="com.victronenergy.BusItem")
                log.info(f"⚙️ [DBUS NATIU] Sincronitzat Minimum SOC a Cerbo GX: {target:.0f}% [{phase_name}]")
            except Exception as e:
                log.debug(f"DBus direct no disponible, usant MQTT: {e}")

            # 2. Publicació MQTT per a clients externs
            topic = f"W/{self.portal_id}/settings/0/Settings/CGwacs/BatteryLife/MinimumSocLimit"
            payload = json.dumps({"value": target})
            self.mqtt_client.publish(topic, payload)
            self.last_applied_min_soc = target

    def sync_grid_setpoint(self):
        """Modula dinàmicament el Grid Setpoint de Victron ESS."""
        now = time.time()

        termo_p = self.termo_status.get("power_w", 0.0) if self.termo_status else 0.0
        termo_on = self.termo_status.get("is_on", False) if self.termo_status else False
        is_termo_active = termo_on and termo_p >= 500.0

        # Resposta immediata (0s d'espera): Bypassem el throttle de 20s si el termo s'encén o s'apaga
        termo_state_changed = (is_termo_active and (self.last_grid_setpoint or 0) < 500.0) or \
                              (not is_termo_active and (self.last_grid_setpoint or 0) >= 500.0)

        if not termo_state_changed and (now - self.last_grid_setpoint_eval_time < 20):
            return
        self.last_grid_setpoint_eval_time = now

        # ♨️ 1. GESTIÓ AMB TERMO ACTIU (>= 500 W) -> Importació a 4.5A (Límit segur contractat 5A)
        if is_termo_active:
            # Reconnexió immediata a xarxa si el MultiPlus estava en Inverter Only (0s d'espera)
            if self.vebus_mode == 2:
                self.set_multiplus_mode(3, f"♨️ Termo Actiu ({termo_p:.0f}W) -> Reconnexió Immediata a Xarxa (Suport 4.5A)")

            grid_v_safe = self.grid_v if getattr(self, "grid_v", 0.0) >= 190.0 else 230.0
            # 4.5A exactes ajustats a la tensió real de la xarxa (ex: 222V * 4.5A = 1000W; 230V * 4.5A = 1035W)
            target = round(min(1050.0, max(900.0, 4.5 * grid_v_safe)))
            reason = f"♨️ Termo Actiu ({termo_p:.0f}W) -> Setpoint {target:.0f}W (4.5A a {grid_v_safe:.1f}V - Blindatge Bateria)"

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

        # 1. Intent D-Bus Natiu directe sobre VE.Bus (Instantani <0.1ms)
        try:
            import dbus
            bus = dbus.SystemBus()
            vebus_service = None
            for name in bus.list_names():
                if name.startswith("com.victronenergy.vebus"):
                    vebus_service = name
                    break
            if vebus_service:
                obj = bus.get_object(vebus_service, "/Mode")
                obj.SetValue(dbus.Int32(target_mode), dbus_interface="com.victronenergy.BusItem")
                log.info(f"⚙️ [DBUS NATIU] MultiPlus-II Mode canviat a {new_mode_str} via {vebus_service}")
        except Exception as e:
            log.debug(f"DBus direct no disponible per a VE.Bus Mode, usant MQTT: {e}")

        # 2. Publicació MQTT
        topic = f"W/{self.portal_id}/vebus/276/Mode"
        payload = json.dumps({"value": target_mode})
        self.mqtt_client.publish(topic, payload)

        self.vebus_mode = target_mode
        self.last_mode_switch_time = now
        self.relay_switch_count += 1

        priority = "high" if target_mode == 2 else "default"
        self.notifications.send_notification("Canvi de Mode MultiPlus", f"{old_mode_str} ➡️ {new_mode_str}\n{reason}", priority=priority)

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
            "termo_last_heated_date": getattr(self, "termo_last_heated_date", "2026-08-27"),
            "termo_last_60_ts": getattr(self, "termo_last_60_ts", None),
            "termo_est_temp": round(getattr(self, "termo_est_temp", 60.0), 1),
            "termo_kwh_today": round(getattr(self, "termo_kwh_today", 0.0), 2),
            "termo_start_time_str": getattr(self, "termo_start_time_str", ""),
            "termo_end_time_str": getattr(self, "termo_end_time_str", ""),
            "termo_active_seconds_today": round(getattr(self, "termo_active_seconds_today", 0.0), 0),
            "doble_kwh_today": round(getattr(self, "doble_kwh_today", 0.0), 2),
            "ac_manual_off_time": getattr(self, "ac_manual_off_time", None),
            "ac_manual_on_time": getattr(self, "ac_manual_on_time", None),
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
            self.termo_heated_today = False
            self.termo_kwh_today = 0.0
            self.termo_start_time_str = ""
            self.termo_end_time_str = ""
            self.termo_active_seconds_today = 0.0
            self.termo_currently_heating = False
            self.doble_kwh_today = 0.0
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

        # Integració de consum elèctric dels endolls Tuya (Termo i Cuina) en memòria RAM (ZERO desgast Flash)
        if self.termo_status:
            termo_p = float(self.termo_status.get("power_w", 0.0))
            if termo_p >= 5.0:
                kwh_inc = (termo_p / 1000.0) * hours
                self.termo_kwh_today += kwh_inc
                self.termo_active_seconds_today += dt
                self.termo_currently_heating = True
                if not self.termo_start_time_str:
                    self.termo_start_time_str = now_madrid.strftime("%H:%M")
                # Model Físic Calorimètric (100L): +8.605 ºC per kWh injectat
                # Límit 59.5 ºC per càlcul d'energia (els 60.0 ºC només es fixen si el termòstat Ariston talla a <50W)
                self.termo_est_temp = min(59.5, self.termo_est_temp + (kwh_inc * 8.605))
            else:
                self.termo_currently_heating = False
                if self.termo_start_time_str and not self.termo_end_time_str and self.termo_kwh_today > 0.1:
                    self.termo_end_time_str = now_madrid.strftime("%H:%M")
                # Dissipació tèrmica per temps transcorregut:
                # El termo perd ~0.35 ºC / hora d'aïllament mentre està apagat/repòs (fins a temp ambient 20 ºC)
                self.termo_est_temp = max(20.0, self.termo_est_temp - (hours * 0.35))

        if self.doble_status:
            doble_p = float(self.doble_status.get("power_w", 0.0))
            if doble_p >= 2.0:
                self.doble_kwh_today += (doble_p / 1000.0) * hours

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
                "pv_power_w": round(self.pv_p, 1),
                "ac_loads_w": round(self.ac_loads, 1),
                "grid_power_w": round(self.grid_p, 1),
                "grid_voltage_v": round(self.grid_v, 1),
                "vebus_mode": self.vebus_mode,
                "timestamp": now
            }
            try:
                if self.mqtt_client.client:
                    self.mqtt_client.publish_to_portal("caseta/stats", json.dumps({"value": stats}), retain=True)
                    self.mqtt_client.publish("caseta/stats", json.dumps({"value": stats}), retain=True)

                    # Publicació d'estat i energia dels endolls intel·ligents Tuya
                    if self.termo_status:
                        days_since_60 = None
                        hours_since_60 = None
                        if getattr(self, "termo_last_60_ts", None):
                            try:
                                hours_since_60 = round((now - self.termo_last_60_ts) / 3600.0, 1)
                            except Exception:
                                hours_since_60 = None
                        if getattr(self, "termo_last_heated_date", ""):
                            try:
                                d_last = datetime.datetime.strptime(self.termo_last_heated_date, "%Y-%m-%d").date()
                                days_since_60 = (now_madrid.date() - d_last).days
                            except Exception:
                                days_since_60 = None
                        if getattr(self, "termo_heated_today", False):
                            days_since_60 = 0
                            if hours_since_60 is None and getattr(self, "termo_end_time_str", ""):
                                try:
                                    end_dt = datetime.datetime.strptime(f"{self.current_day_str} {self.termo_end_time_str}", "%Y-%m-%d %H:%M")
                                    if MADRID_TZ:
                                        ts = end_dt.replace(tzinfo=MADRID_TZ).timestamp()
                                    else:
                                        ts = end_dt.timestamp()
                                    hours_since_60 = round((now - ts) / 3600.0, 1)
                                except Exception:
                                    pass

                        termo_payload = dict(self.termo_status)
                        termo_payload.update({
                            "temp_c": round(getattr(self, "termo_est_temp", 60.0), 1),
                            "kwh_today": round(self.termo_kwh_today, 2),
                            "start_time": getattr(self, "termo_start_time_str", ""),
                            "end_time": getattr(self, "termo_end_time_str", ""),
                            "active_mins": int(round(getattr(self, "termo_active_seconds_today", 0.0) / 60.0)),
                            "is_heating": getattr(self, "termo_currently_heating", False),
                            "last_heated_date": getattr(self, "termo_last_heated_date", ""),
                            "last_60_ts": getattr(self, "termo_last_60_ts", None),
                            "days_since_60": days_since_60,
                            "hours_since_60": hours_since_60,
                            "timestamp": now
                        })
                        self.mqtt_client.publish("caseta/termo", json.dumps({"value": termo_payload}), retain=True)
                        self.mqtt_client.publish_to_portal("caseta/termo", json.dumps({"value": termo_payload}), retain=True)

                    if self.doble_status:
                        doble_payload = dict(self.doble_status)
                        doble_payload.update({
                            "kwh_today": round(self.doble_kwh_today, 2),
                            "timestamp": now
                        })
                        self.mqtt_client.publish("caseta/endoll_doble", json.dumps({"value": doble_payload}), retain=True)
                        self.mqtt_client.publish_to_portal("caseta/endoll_doble", json.dumps({"value": doble_payload}), retain=True)
            except Exception:
                pass

        # 💾 Checkpoint de seguretat a disc Flash (eMMC) cada 30 minuts (1800s)
        if now - self.last_checkpoint_save_time >= 1800.0:
            self.last_checkpoint_save_time = now
            self.save_daily_stats()

    def on_mqtt_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            # Blindatge del Portal ID: només actualitzar si és un missatge de telemetria N/ de 12 caràcters hexadecimals
            if parts[0] == "N" and len(parts) > 1 and len(parts[1]) == 12:
                self.portal_id = parts[1]

            raw_payload = json.loads(msg.payload.decode())
            val = raw_payload.get("value") if isinstance(raw_payload, dict) else raw_payload
            topic = msg.topic

            if topic.endswith("/battery/512/Soc") or topic.endswith("/system/0/Dc/Battery/Soc"):
                self.soc = float(val) if val is not None else self.soc
            elif topic.endswith("/battery/512/Soh") or topic.endswith("/system/0/Dc/Battery/Soh"):
                self.soh = float(val) if val is not None else self.soh
            elif topic.endswith("/battery/512/Dc/0/Voltage") or topic.endswith("/system/0/Dc/Battery/Voltage"):
                self.bat_v = float(val) if val is not None else self.bat_v
            elif topic.endswith("/battery/512/Dc/0/Current") or topic.endswith("/system/0/Dc/Battery/Current"):
                self.bat_i = float(val) if val is not None else self.bat_i
            elif topic.endswith("/battery/512/Dc/0/Power") or topic.endswith("/system/0/Dc/Battery/Power"):
                self.bat_p = float(val) if val is not None else self.bat_p
            elif topic.endswith("/battery/512/System/MaxCellVoltage"):
                self.cell_max = float(val) if val is not None else self.cell_max
            elif topic.endswith("/battery/512/System/MinCellVoltage"):
                self.cell_min = float(val) if val is not None else self.cell_min

            elif ("/pvinverter/" in topic and topic.endswith("/Ac/Power")) or topic.endswith("/pvinverter/31/Ac/Power") or topic.endswith("/system/0/Ac/PvOnOutput/L1/Power") or topic.endswith("/system/0/Ac/PvOnOutput/Power") or topic.endswith("/system/0/Dc/Pv/Power"):
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
            elif "caseta/clima" in topic:
                clima_dict = val if (isinstance(val, dict) and "sensors" in val) else (raw_payload if isinstance(raw_payload, dict) else {})
                self.clima_sensors = clima_dict.get("sensors") or {}
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

        if not self.mqtt_client.connect(self.on_mqtt_message):
            return

        self.sync_cerbo_min_soc()
        self.forecast.update_forecast()
        self.update_inforatge()

        log.info("🛡️ Guardià en línia i vigilant telemetria en directe!")

        while self.running:
            try:
                now = time.time()
                now_madrid = get_madrid_now()

                self.mqtt_client.send_keepalive()
                self.sync_cerbo_min_soc(now_madrid)
                self.sync_grid_setpoint()
                self.forecast.update_forecast()
                self.update_inforatge()
                self.update_ac_status()
                self.termo_status, self.last_termo_update_time, self.last_termo_calc_time = self.tuya.update_termo_status(
                    self.termo_status, self.last_termo_update_time, self.last_termo_calc_time
                )
                self.doble_status, self.last_doble_update_time, self.last_doble_calc_time = self.tuya.update_doble_status(
                    self.doble_status, self.last_doble_update_time, self.last_doble_calc_time
                )
                self.update_energy_integrals(now_madrid)
                self.dbus_telemetry.poll_telemetry(self)
                self.state_machine.evaluate_state_machine(self, now_madrid)
                self.evaluate_climate_control(now_madrid)

                time.sleep(1.0)
            except KeyboardInterrupt:
                log.info("Aturant Caseta Guardian...")
                self.running = False
            except Exception as e:
                log.error(f"Error al bucle principal: {e}")
                time.sleep(2.0)

        self.save_daily_stats()
        self.mqtt_client.disconnect()


if __name__ == "__main__":
    guardian = CasetaGuardian()
    guardian.run()

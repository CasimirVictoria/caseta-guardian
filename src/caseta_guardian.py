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
from caseta_guardian_modules.weather_worker import WeatherWorker
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
        self.weather = WeatherWorker(config, on_update_callback=self._on_weather_update)
        self.weather.start()
        self.forecast = self.weather
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
        self.grid_outage_notified = False
        self.grid_recovery_start_time = None
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
        self.termo_surplus_done = False
        self.termo_morning_done = False
        self.termo_notified_knob_60 = False
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
                    self.termo_currently_heating = bool(data.get("termo_currently_heating", False))
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

    def _on_weather_update(self, snapshot):
        """Callback quan el WeatherWorker obté noves dades en segon pla."""
        self.ext_temp = snapshot.ext_temp
        self.ext_humidity = snapshot.ext_humidity
        self.rain_today = snapshot.rain_today
        self.today_kwh_est = snapshot.today_kwh_est
        self.remaining_kwh_today = snapshot.remaining_kwh_today
        self.tomorrow_kwh_est = snapshot.tomorrow_kwh_est
        self.max_temp_today = snapshot.max_temp_today
        self.sunset_temp_today = snapshot.sunset_temp_today
        self.blackout_risk = snapshot.blackout_risk
        self.target_reserve_soc = snapshot.target_reserve_soc

        if hasattr(self, "mqtt_client") and self.mqtt_client and self.mqtt_client.client:
            try:
                inforatge_data = {
                    "temperatura": snapshot.ext_temp,
                    "humitat": snapshot.ext_humidity,
                    "pluja_avui": snapshot.rain_today,
                    "timestamp": snapshot.timestamp,
                    "hora_str": get_madrid_now().strftime("%H:%M")
                }
                self.mqtt_client.publish("caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
                self.mqtt_client.publish_to_portal("caseta/inforatge", json.dumps({"value": inforatge_data}), retain=True)
            except Exception:
                pass

    def update_inforatge(self):
        """Actualitza atributs de clima a partir de la instantània de WeatherWorker (0 ms)."""
        snap = self.weather.get_snapshot()
        if snap.timestamp > 0.0:
            self.ext_temp = snap.ext_temp
            self.ext_humidity = snap.ext_humidity
            self.rain_today = snap.rain_today
            self.today_kwh_est = snap.today_kwh_est
            self.remaining_kwh_today = snap.remaining_kwh_today
            self.tomorrow_kwh_est = snap.tomorrow_kwh_est
            self.max_temp_today = snap.max_temp_today
            self.sunset_temp_today = snap.sunset_temp_today
            self.blackout_risk = snap.blackout_risk
            self.target_reserve_soc = snap.target_reserve_soc

    def update_ac_status(self):
        """Actualitza l'estat de l'AC a partir de la instantània de TuyaManager (0 ms)."""
        now = time.time()
        snap = self.tuya.get_ac_snapshot()
        if snap.timestamp == 0.0:
            return

        pwr = snap.power
        temp = snap.temp
        mode_str = snap.mode

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
        if hasattr(self, "mqtt_client") and self.mqtt_client and self.mqtt_client.client:
            self.mqtt_client.publish("caseta/ac", json.dumps({"value": ac_payload}), retain=True)
            self.mqtt_client.publish_to_portal("caseta/ac", json.dumps({"value": ac_payload}), retain=True)

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
        termo_p = self.termo_status.get("power_w", 0.0) if self.termo_status else 0.0
        termo_on = self.termo_status.get("is_on", False) if self.termo_status else False
        is_termo_active = termo_on and termo_p >= 500.0

        # Resposta immediata (sense throttle de 120s) si el termo està actiu i el mínim soc és > 68%
        if not (is_termo_active and (self.last_applied_min_soc or 100) > 68.0):
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

            # Si el termo està actiu diürn, fixem 68% per evitar que ESS entre en mode "Recharge"
            if is_termo_active:
                target = 68.0
                phase_name = f"♨️ Termo Actiu Diürn ({cur_pv:.0f}W sol) -> 68% Vas Buit Termo"
            # ☀️ Cas 1: Sol Abundant (Sol Real >= 500W O Sol Restant >= 4.5 kWh amb Sol Actual >= 250W)
            elif (cur_pv >= 500.0) or (rem_sun >= 4.5 and cur_pv >= 250.0):
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

        # 4. ⛈️ Temps Advers / Pluja / Tronades a la Tarda-Vespre (>= 16:30h sense sol i amb pluja o risc):
        elif time_decimal >= 16.5 and (getattr(self, "rain_today", 0.0) >= 0.5 or getattr(self, "blackout_risk", 0) >= 30 or getattr(self, "grid_outage_notified", False)):
            target = 100.0
            phase_name = f"⛈️ Temps Advers / Pluja ({getattr(self, 'rain_today', 0.0):.1f} mm) -> 100% Blindatge SAI"

        # 5. 🏖️ Cap de Setmana o Festiu a la Tarda/Vespre (Preu Vall 24h continu a ~7 cts):
        elif is_weekend_or_hol and time_decimal >= 18.0:
            target = 100.0
            phase_name = "🏖️ Cap de Setmana/Festiu Vespre (100% Top-Balancing a 7 cts)"

        # 6. 🌇 Tarda / Vespre Feiners (16:30h a 23:59h Madrid):
        else:
            target = 85.0
            phase_name = "🌇 Tarda / Vespre Resilient (85% Màxima Seguretat & SAI)"

        # 👤 Comprovació de consigna manual de l'usuari a Cerbo GX a la tarda/vespre/nit:
        # Si l'usuari ha fixat manualment un límit superior (ex: 100% per seguretat/tronades), no el rebaixem.
        if time_decimal >= 16.5 or time_decimal < 8.0:
            try:
                import dbus
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/BatteryLife/MinimumSocLimit")
                current_cerbo_soc = float(obj.GetValue())
                if current_cerbo_soc > target:
                    target = current_cerbo_soc
                    phase_name = f"👤 Consigna Manual Prioritària de l'Usuari ({target:.0f}%)"
            except Exception:
                pass

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

    def sync_grid_setpoint(self, now_madrid=None):
        """Modula dinàmicament el Grid Setpoint de Victron ESS."""
        now = time.time()
        now_madrid = now_madrid or get_madrid_now()

        termo_p = self.termo_status.get("power_w", 0.0) if self.termo_status else 0.0
        termo_on = self.termo_status.get("is_on", False) if self.termo_status else False
        bat_i_discharge = abs(self.bat_i) if getattr(self, "bat_i", 0.0) < 0.0 else 0.0

        # Termo actiu si Tuya reporta >=500W, o si telemetria nativa Victron veu càrrega >=1400W o descàrrega >=10A
        is_termo_active = termo_on and (
            termo_p >= 500.0 or
            self.ac_loads >= 1400.0 or
            bat_i_discharge >= 10.0
        )

        # Resposta immediata (0s d'espera): Sense throttle cada segon si termo_on o estat canviat; en repòs pur, throttle de 20s
        termo_state_changed = (is_termo_active and (self.last_grid_setpoint or 0) < 500.0) or \
                              (not is_termo_active and (self.last_grid_setpoint or 0) >= 500.0)

        if not termo_on and not is_termo_active:
            if not termo_state_changed and (now - self.last_grid_setpoint_eval_time < 20):
                return
        self.last_grid_setpoint_eval_time = now

        # ♨️ 1. GESTIÓ AMB TERMO ACTIU (>= 500 W) -> Blindatge bateria (màx 800W descàrrega)
        if termo_on and self.vebus_mode == 2:
            self.set_multiplus_mode(3, f"🔌 Endoll Termo Encès -> Reconnexió Immediata a Xarxa Preventiva")

        if is_termo_active:
            grid_v_safe = self.grid_v if getattr(self, "grid_v", 0.0) >= 190.0 else 230.0
            max_grid_w = round(min(1050.0, max(900.0, 4.5 * grid_v_safe)))
            time_decimal = now_madrid.hour + (now_madrid.minute / 60.0)

            # A. Matinada Vall P3 (04:00h - 07:00h sense sol): suport de xarxa econòmica 4.5A
            if 4.0 <= time_decimal < 7.0:
                target = max_grid_w
                reason = f"🌙 Termo P3 Matinada ({termo_p:.0f}W) -> Setpoint {target:.0f}W (Suport Vall 4.5A)"
            else:
                # B. Diürn: Sol prioritari, i la bateria aporta com a MÀXIM ~650 W AC (~14A DC a 49V)
                # Tenint en compte l'eficiència del MultiPlus (~88%), 650W AC = ~740W DC (~14.8A)
                net_deficit = self.ac_loads - self.pv_p

                # Mesures directes de bateria pel BMS:
                bat_discharge_w = abs(self.bat_p) if getattr(self, "bat_p", 0.0) < 0.0 else 0.0
                bat_i_discharge = abs(self.bat_i) if getattr(self, "bat_i", 0.0) < 0.0 else 0.0

                if net_deficit <= 0.0:
                    target = 200.0
                    reason = f"☀️ Termo 100% Solar (Sol {self.pv_p:.0f}W >= Casa {self.ac_loads:.0f}W) -> Setpoint 200W (Mínim Coixí)"
                elif net_deficit <= 650.0 and bat_i_discharge <= 14.5:
                    target = 200.0
                    reason = f"🔋 Termo Suport Bateria Suau (Dèficit {net_deficit:.0f}W, Bat {bat_i_discharge:.1f}A <= 14.5A) -> Setpoint 200W (Mínim Coixí)"
                else:
                    # Necessitem suport dinàmic de xarxa per blindar la bateria a <=14A
                    deficit_grid = max(0.0, net_deficit - 650.0)
                    current_excess_grid = max(0.0, (bat_i_discharge - 14.0) * 50.0) if bat_i_discharge > 14.0 else 0.0
                    grid_needed = max(deficit_grid, current_excess_grid)
                    target = round(min(max_grid_w, max(200.0, grid_needed)))
                    reason = f"⚡ Suport Xarxa Dinàmic ({grid_needed:.0f}W) per limitar bateria a <=14A -> Setpoint {target:.0f}W"

        # ☕ 2. GESTIÓ AMB TERMO EN REPÒS (Sol de Migdia / Tarda)
        else:
            if termo_on:
                target = 200.0
                reason = "♨️ Termo Encès en Repòs -> Setpoint 200W (Mínim Coixí Preventiu)"
            # ☀️ A. Si hi ha generació solar abundant (Sol >= 400W o Sol >= Consum Casa):
            elif self.pv_p >= 400.0 or (self.pv_p >= self.ac_loads and self.pv_p > 150.0):
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

        cur_setpoint = self.last_grid_setpoint if self.last_grid_setpoint is not None else 50.0
        deadband = 50.0 if is_termo_active else 20.0
        if self.last_grid_setpoint is None or abs(target - cur_setpoint) >= deadband or termo_state_changed:
            self.last_grid_setpoint = target
            try:
                import dbus
                bus = dbus.SystemBus()
                obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                obj.SetValue(dbus.Double(target), dbus_interface="com.victronenergy.BusItem")
                log.info(f"⚙️ Sincronitzat Grid Setpoint a Cerbo GX: {target:.0f} W [{reason}]")
            except Exception as e:
                log.warning(f"No s'ha pogut actualitzar Grid Setpoint per D-Bus: {e}")

    def set_grid_setpoint_direct(self, target_w: float, reason: str = "Pre-setpoint"):
        """Aplica un setpoint de xarxa directament a Cerbo GX sense esperar al cicle periòdic."""
        self.last_grid_setpoint = float(target_w)
        self.last_grid_setpoint_eval_time = time.time()
        try:
            import dbus
            bus = dbus.SystemBus()
            obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
            obj.SetValue(dbus.Double(float(target_w)), dbus_interface="com.victronenergy.BusItem")
            log.info(f"⚙️ [DIRECT] Sincronitzat Grid Setpoint a Cerbo GX: {target_w:.0f} W [{reason}]")
        except Exception as e:
            log.warning(f"No s'ha pogut actualitzar Grid Setpoint directe per D-Bus: {e}")
        try:
            topic = f"W/{self.portal_id}/settings/0/Settings/CGwacs/AcPowerSetPoint"
            self.mqtt_client.publish(topic, json.dumps({"value": float(target_w)}))
        except Exception:
            pass

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
            "termo_surplus_done": getattr(self, "termo_surplus_done", False),
            "termo_morning_done": getattr(self, "termo_morning_done", False),
            "termo_last_heated_date": getattr(self, "termo_last_heated_date", "2026-08-27"),
            "termo_last_60_ts": getattr(self, "termo_last_60_ts", None),
            "termo_est_temp": round(getattr(self, "termo_est_temp", 60.0), 1),
            "termo_kwh_today": round(getattr(self, "termo_kwh_today", 0.0), 2),
            "termo_start_time_str": getattr(self, "termo_start_time_str", ""),
            "termo_end_time_str": getattr(self, "termo_end_time_str", ""),
            "termo_active_seconds_today": round(getattr(self, "termo_active_seconds_today", 0.0), 0),
            "termo_currently_heating": getattr(self, "termo_currently_heating", False),
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
            self.termo_surplus_done = False
            self.termo_morning_done = False
            self.termo_notified_knob_60 = False
            self.termo_notified_60_reached = False
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
            is_termo_on = bool(self.termo_status.get("is_on", False))
            is_heating_now = (termo_p >= 50.0) and is_termo_on

            if is_heating_now:
                kwh_inc = (termo_p / 1000.0) * hours
                self.termo_kwh_today += kwh_inc
                self.termo_active_seconds_today += dt

                # Transició a calfant: inici de nou cicle o sessió de calfament
                if not self.termo_currently_heating:
                    self.termo_currently_heating = True
                    self.termo_start_time_str = now_madrid.strftime("%H:%M")
                    self.termo_end_time_str = ""
                    log.info(f"♨️ [TERMO] Inici de cicle de calfament a les {self.termo_start_time_str} ({termo_p:.0f} W)")

                # Model Físic Calorimètric (100L): +8.605 ºC per kWh injectat
                # Límit 60.0 ºC per càlcul d'energia (rodeta Ariston a 60ºC)
                self.termo_est_temp = min(60.0, self.termo_est_temp + (kwh_inc * 8.605))
            else:
                # Transició de calfant a repòs
                if self.termo_currently_heating:
                    self.termo_currently_heating = False
                    self.termo_end_time_str = now_madrid.strftime("%H:%M")
                    log.info(f"⚪ [TERMO] Fi del cicle de calfament a les {self.termo_end_time_str}")

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

            payload_str = msg.payload.decode().strip()
            try:
                raw_payload = json.loads(payload_str)
            except Exception:
                raw_payload = payload_str
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
            elif topic.endswith("/system/0/Ac/Consumption/L1/Power") or topic.endswith("/system/0/Ac/ConsumptionOnOutput/L1/Power") or topic.endswith("/system/0/Ac/Consumption/Power") or topic.endswith("/system/0/Ac/ConsumptionOnOutput/Power"):
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
            elif topic in ("caseta/termo/set", "caseta/termo/cmd") or topic.endswith("/termo/set") or topic.endswith("/termo/cmd"):
                cmd_raw = str(val).lower() if not isinstance(val, dict) else str(val.get("power", val.get("state", ""))).lower()
                is_turn_on = cmd_raw in ("on", "true", "1", "start", "engegar", "encen")
                is_turn_off = cmd_raw in ("off", "false", "0", "stop", "aturar", "apagar")
                if is_turn_on:
                    log.info("📩 [MQTT CMD] Rebut comandament manual per engegar el termo amb la seqüència segura!")
                    self.termo_surplus_done = False
                    self.termo_low_power_start_time = None
                    self.termo_cooldown_until = 0.0
                    self.high_discharge_start_time = None
                    self.state_machine.start_termo_safely(
                        self,
                        reason="Ordre manual d'encesa segura (MQTT)",
                        notif_title="♨️ Termo Engegat Manualment",
                        notif_msg="Encesa segura en 3 passos sol·licitada per l'usuari.",
                        notif_icon="sun"
                    )
                elif is_turn_off:
                    log.info("📩 [MQTT CMD] Rebut comandament manual per apagar el termo!")
                    self.termo_pending_turn_on = None
                    self.tuya.send_termo_command(power=False, reason="Ordre manual d'apagat (MQTT)")

        except Exception:
            pass

    def stop(self):
        """Atura de forma ordenada tots els serveis i treballadors en segon pla."""
        if not self.running:
            return
        self.running = False
        log.info("🛑 Aturant Caseta Guardian...")
        try:
            self.save_daily_stats()
        except Exception as e:
            log.warning(f"Error guardant stats diàries en aturar: {e}")

        if hasattr(self, "weather") and self.weather:
            try:
                self.weather.stop()
            except Exception:
                pass

        if hasattr(self, "tuya") and self.tuya:
            try:
                self.tuya.stop()
            except Exception:
                pass

        if hasattr(self, "notifications") and self.notifications:
            try:
                self.notifications.stop()
            except Exception:
                pass

        if hasattr(self, "mqtt_client") and self.mqtt_client:
            try:
                self.mqtt_client.disconnect()
            except Exception:
                pass
        log.info("👋 Caseta Guardian aturat correctament.")

    def run(self):
        log.info(f"🚀 Iniciant Caseta Guardian (Cerbo IP: {CERBO_IP})...")

        import signal
        def sig_handler(signum, frame):
            log.info(f"🛑 Rebut senyal {signum}. Aturant el servei...")
            self.stop()
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
            t_loop_start = time.perf_counter()
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

            except KeyboardInterrupt:
                log.info("Aturant Caseta Guardian...")
                break
            except Exception as e:
                log.error(f"Error al bucle principal: {e}")
                time.sleep(1.0)
                continue

            t_work = time.perf_counter() - t_loop_start
            if t_work > 0.250:  # > 250ms (avís de bloqueig o latència anormal)
                log.warning(f"⚠️ [JITTER] Iteració del bucle ha tardat {t_work*1000:.1f}ms (>250ms)!")

            sleep_time = max(0.0, 1.0 - t_work)
            time.sleep(sleep_time)

        self.stop()


if __name__ == "__main__":
    guardian = CasetaGuardian()
    guardian.run()

"""
WeatherWorker: Gestor asíncron en segon pla per a Inforatge Ador i Open-Meteo.
Actualitza WeatherSnapshot de manera atòmica i thread-safe sense bloquejar Cerbo GX.
"""

import datetime
import json
import logging
import re
import threading
import time
import urllib.request
from typing import Callable, Optional

try:
    import zoneinfo
    MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None

from .state_models import WeatherSnapshot

log = logging.getLogger("caseta-guardian")

INFORATGE_CACHE_FILE = "/tmp/caseta_inforatge_cache.json"
FORECAST_CACHE_FILE = "/tmp/caseta_forecast_cache.json"


class WeatherWorker:
    """Worker en segon pla que descarrega i processa dades meteorològiques de forma asíncrona."""

    def __init__(self, cfg: dict, on_update_callback: Optional[Callable[[WeatherSnapshot], None]] = None):
        self.cfg = cfg
        self.on_update_callback = on_update_callback

        self._lock = threading.Lock()
        self._running = True

        self.last_inforatge_time = 0.0
        self.last_forecast_time = 0.0

        # Estat en memòria (Snapshot inicial per defecte o de cau)
        self._snapshot = self._load_initial_cache()

        # Fil autònom en segon pla
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="WeatherWorker",
            daemon=True
        )

    def start(self):
        """Inicia el fil secundari."""
        self._thread.start()

    def get_snapshot(self) -> WeatherSnapshot:
        """Retorna la instantània meteorològica actual (crida no bloquejant, <0.01 ms)."""
        with self._lock:
            return self._snapshot

    def update_forecast(self):
        """Mètode no bloquejant de compatibilitat retroactiva."""
        pass

    def get_forecast_cache(self) -> dict:
        snap = self.get_snapshot()
        return {
            "today_kwh": round(snap.today_kwh_est, 1),
            "remaining_kwh": round(snap.remaining_kwh_today, 1),
            "tomorrow_kwh": round(snap.tomorrow_kwh_est, 1),
            "max_temp_today": round(snap.max_temp_today, 1),
            "sunset_temp": round(snap.sunset_temp_today, 1),
            "blackout_risk": snap.blackout_risk,
            "target_reserve_soc": snap.target_reserve_soc,
            "timestamp": snap.timestamp
        }

    def _get_madrid_now(self) -> datetime.datetime:
        if MADRID_TZ:
            return datetime.datetime.now(MADRID_TZ)
        return datetime.datetime.now()

    def _load_initial_cache(self) -> WeatherSnapshot:
        """Carrega dades persistents de disc a l'arrencada per tenir estat vàlid immediat."""
        ext_t = None
        ext_h = 50.0
        rain = 0.0
        today_kwh = 5.0
        rem_kwh = 3.0
        tom_kwh = 5.0
        max_t = 30.0
        sunset_t = 26.0
        risk = 0
        target_soc = 85.0

        try:
            with open(INFORATGE_CACHE_FILE, "r") as f:
                d = json.load(f)
                ext_t = d.get("temperatura")
                ext_h = float(d.get("humitat", 50.0))
                rain = float(d.get("pluja_avui", 0.0))
        except Exception:
            pass

        try:
            with open(FORECAST_CACHE_FILE, "r") as f:
                d = json.load(f)
                today_kwh = float(d.get("today_kwh", 5.0))
                rem_kwh = float(d.get("remaining_kwh", 3.0))
                tom_kwh = float(d.get("tomorrow_kwh", 5.0))
                max_t = float(d.get("max_temp_today", 30.0))
                sunset_t = float(d.get("sunset_temp", 26.0))
                risk = int(d.get("blackout_risk", 0))
                target_soc = float(d.get("target_reserve_soc", 85.0))
        except Exception:
            pass

        return WeatherSnapshot(
            ext_temp=ext_t,
            ext_humidity=ext_h,
            rain_today=rain,
            today_kwh_est=today_kwh,
            remaining_kwh_today=rem_kwh,
            tomorrow_kwh_est=tom_kwh,
            max_temp_today=max_t,
            sunset_temp_today=sunset_t,
            blackout_risk=risk,
            target_reserve_soc=target_soc,
            timestamp=time.time()
        )

    def _worker_loop(self):
        """Bucle periòdic que s'executa en segon pla."""
        # Forcem una primera descàrrega inicial pocs segons després d'arrencar
        time.sleep(2.0)
        self._fetch_inforatge()
        self._fetch_open_meteo()

        while self._running:
            now = time.time()

            # Inforatge cada 15 minuts (900 segons)
            if now - self.last_inforatge_time >= 900:
                self._fetch_inforatge()

            # Open-Meteo cada 60 minuts (3600 segons)
            if now - self.last_forecast_time >= 3600:
                self._fetch_open_meteo()

            time.sleep(5.0)

    def _fetch_inforatge(self):
        """Consulta Inforatge Ador (amb timeout de 8s en segon pla)."""
        now = time.time()
        self.last_inforatge_time = now
        try:
            url = "https://inforatge.com/meteo-ador"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=8) as rep:
                html = rep.read().decode("utf-8")

            temp_m = re.search(r'class="blocValorTM">(-?\d+)<span class="vPetit">,(\d+)</span>', html)
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

            tmax_m = re.search(r'class="boxpetitkTX negreT"><span class="varmobil">m&agrave;x</span>(-?\d+),(\d+)', html)
            tmax = float(f"{tmax_m.group(1)}.{tmax_m.group(2)}") if tmax_m else None

            tmin_m = re.search(r'class="boxpetitkTM negreT"><span class="varmobil">m&iacute;n</span>(-?\d+),(\d+)', html)
            tmin = float(f"{tmin_m.group(1)}.{tmin_m.group(2)}") if tmin_m else None

            with self._lock:
                cur = self._snapshot
                self._snapshot = WeatherSnapshot(
                    ext_temp=temp if temp is not None else cur.ext_temp,
                    ext_humidity=float(hum) if hum is not None else cur.ext_humidity,
                    rain_today=float(pluja),
                    today_kwh_est=cur.today_kwh_est,
                    remaining_kwh_today=cur.remaining_kwh_today,
                    tomorrow_kwh_est=cur.tomorrow_kwh_est,
                    max_temp_today=cur.max_temp_today,
                    sunset_temp_today=cur.sunset_temp_today,
                    blackout_risk=cur.blackout_risk,
                    target_reserve_soc=cur.target_reserve_soc,
                    timestamp=now
                )
                updated_snap = self._snapshot

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
                "hora_str": self._get_madrid_now().strftime("%H:%M")
            }
            try:
                with open(INFORATGE_CACHE_FILE, "w") as f:
                    json.dump(inforatge_data, f)
            except Exception:
                pass

            log.info(f"📍 Inforatge Ador: Ext {temp}ºC | Hum {hum}% | Vent {vent_vel} km/h {vent_dir} | Pressió {press} hPa")

            if self.on_update_callback:
                self.on_update_callback(updated_snap)

        except Exception as e:
            log.warning(f"Error consultant Inforatge Ador en segon pla: {e}")

    def _fetch_open_meteo(self):
        """Consulta Open-Meteo (amb timeout de 10s en segon pla)."""
        now = time.time()
        self.last_forecast_time = now
        try:
            lat = self.cfg.get("latitude", 38.92)
            lon = self.cfg.get("longitude", -0.22)
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=shortwave_radiation_sum,temperature_2m_max&hourly=direct_normal_irradiance,temperature_2m&timezone=Europe%2FMadrid&forecast_days=2"
            req = urllib.request.Request(url, headers={"User-Agent": "CasetaGuardian/2.0"})
            with urllib.request.urlopen(req, timeout=10) as rep:
                data = json.loads(rep.read().decode())

            daily = data.get("daily", {})
            rad_list = daily.get("shortwave_radiation_sum", [22.0, 22.0])
            temp_max_list = daily.get("temperature_2m_max", [30.0, 30.0])

            today_kwh = max(1.0, (rad_list[0] / 3.6) * 1.35 * 0.78)
            tomorrow_kwh = max(1.0, (rad_list[1] / 3.6) * 1.35 * 0.78)
            max_temp = temp_max_list[0]

            hourly = data.get("hourly", {})
            dni = hourly.get("direct_normal_irradiance", [])
            temps = hourly.get("temperature_2m", [])

            current_hour = self._get_madrid_now().hour

            if len(dni) >= 24:
                remaining_dni = sum(dni[current_hour:24])
                total_dni = max(1.0, sum(dni[0:24]))
                rem_kwh = today_kwh * (remaining_dni / total_dni)
            else:
                rem_kwh = max(0.0, today_kwh * (1.0 - (current_hour / 20.0)))

            if len(temps) >= 22:
                sunset_temp = temps[21]
            else:
                sunset_temp = max_temp - 4.0

            if max_temp >= 38.0:
                risk = 70
            elif max_temp >= 34.0:
                risk = 45
            elif max_temp >= 31.0:
                risk = 25
            else:
                risk = 10

            target_soc = 85.0

            with self._lock:
                cur = self._snapshot
                self._snapshot = WeatherSnapshot(
                    ext_temp=cur.ext_temp,
                    ext_humidity=cur.ext_humidity,
                    rain_today=cur.rain_today,
                    today_kwh_est=today_kwh,
                    remaining_kwh_today=rem_kwh,
                    tomorrow_kwh_est=tomorrow_kwh,
                    max_temp_today=max_temp,
                    sunset_temp_today=sunset_temp,
                    blackout_risk=risk,
                    target_reserve_soc=target_soc,
                    timestamp=now
                )
                updated_snap = self._snapshot

            cache = {
                "today_kwh": round(today_kwh, 1),
                "remaining_kwh": round(rem_kwh, 1),
                "tomorrow_kwh": round(tomorrow_kwh, 1),
                "max_temp_today": round(max_temp, 1),
                "sunset_temp": round(sunset_temp, 1),
                "blackout_risk": risk,
                "target_reserve_soc": target_soc,
                "timestamp": now
            }
            try:
                with open(FORECAST_CACHE_FILE, "w") as f:
                    json.dump(cache, f)
            except Exception:
                pass

            log.info(f"📊 Open-Meteo: Sol total = {today_kwh:.1f} kWh (Queden {rem_kwh:.1f} kWh) | Màx = {max_temp:.1f}ºC | Risc Tall = {risk}% -> Target SoC = {target_soc:.0f}%")

            if self.on_update_callback:
                self.on_update_callback(updated_snap)

        except Exception as e:
            log.warning(f"Error consultant Open-Meteo en segon pla: {e}")

    def stop(self):
        """Atura el fil de fons."""
        self._running = False

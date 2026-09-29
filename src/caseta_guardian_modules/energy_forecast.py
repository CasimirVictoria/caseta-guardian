"""
Previsió energètica amb Open-Meteo i càlculs d'energia.
"""

import json
import logging
import time
import urllib.request

log = logging.getLogger("caseta-guardian")


class EnergyForecast:
    """Gestiona la previsió solar i els càlculs d'energia."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.last_forecast_time = 0.0
        self.today_kwh_est = 5.0
        self.remaining_kwh_today = 3.0
        self.tomorrow_kwh_est = 5.0
        self.max_temp_today = 30.0
        self.sunset_temp_today = 26.0
        self.blackout_risk = 0
        self.target_reserve_soc = 85.0

    def update_forecast(self):
        """Consulta Open-Meteo per estimar radiació solar i temperatura màxima (cada 60 minuts)."""
        now = time.time()
        if now - self.last_forecast_time < 3600:
            return

        self.last_forecast_time = now
        try:
            lat = self.cfg.get("latitude", 38.9)
            lon = self.cfg.get("longitude", -0.2)
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

            import datetime
            try:
                import zoneinfo
                MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
                current_hour = datetime.datetime.now(MADRID_TZ).hour
            except Exception:
                current_hour = datetime.datetime.now().hour

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

        except Exception as e:
            log.warning(f"Error actualitzant Open-Meteo: {e}")

    def get_forecast_cache(self):
        """Retorna la caché de previsió per publicar a MQTT."""
        return {
            "today_kwh": round(self.today_kwh_est, 1),
            "remaining_kwh": round(self.remaining_kwh_today, 1),
            "tomorrow_kwh": round(self.tomorrow_kwh_est, 1),
            "max_temp_today": round(self.max_temp_today, 1),
            "sunset_temp": round(self.sunset_temp_today, 1),
            "blackout_risk": self.blackout_risk,
            "target_reserve_soc": self.target_reserve_soc,
            "timestamp": time.time()
        }

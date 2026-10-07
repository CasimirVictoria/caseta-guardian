"""
Wrapper de compatibilitat per a EnergyForecast delegant en WeatherWorker.
"""

from .weather_worker import WeatherWorker, FORECAST_CACHE_FILE, INFORATGE_CACHE_FILE
from .state_models import WeatherSnapshot


class EnergyForecast(WeatherWorker):
    """Classe de compatibilitat retroactiva amb l'antic EnergyForecast."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        # Sincronitzem atributs clàssics
        snap = self.get_snapshot()
        self.today_kwh_est = snap.today_kwh_est
        self.remaining_kwh_today = snap.remaining_kwh_today
        self.tomorrow_kwh_est = snap.tomorrow_kwh_est
        self.max_temp_today = snap.max_temp_today
        self.sunset_temp_today = snap.sunset_temp_today
        self.blackout_risk = snap.blackout_risk
        self.target_reserve_soc = snap.target_reserve_soc

    def update_forecast(self):
        """Mètode clàssic per compatibilitat."""
        snap = self.get_snapshot()
        self.today_kwh_est = snap.today_kwh_est
        self.remaining_kwh_today = snap.remaining_kwh_today
        self.tomorrow_kwh_est = snap.tomorrow_kwh_est
        self.max_temp_today = snap.max_temp_today
        self.sunset_temp_today = snap.sunset_temp_today
        self.blackout_risk = snap.blackout_risk
        self.target_reserve_soc = snap.target_reserve_soc

    def get_forecast_cache(self):
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

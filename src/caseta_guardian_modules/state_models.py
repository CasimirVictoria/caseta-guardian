"""
Models de dades immutables (Dataclasses congelades) per a l'estat de Caseta Guardian.
Garanteixen seguretat en concurrència multi-fil (Zero Race Conditions).
"""

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(frozen=True)
class TermoSnapshot:
    """Instantània d'estat del Termo Elèctric (Tuya Plug)."""
    is_on: bool = False
    power_w: float = 0.0
    voltage_v: float = 230.0
    current_a: float = 0.0
    source: str = "offline"       # 'localtuya', 'cloud', 'offline'
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DoblePlugSnapshot:
    """Instantània d'estat de l'Endoll Doble de Cuina (Tuya Dual)."""
    is_on_1: bool = False         # Microones / Torradora
    is_on_2: bool = False         # Cafetera
    power_w: float = 0.0
    voltage_v: float = 230.0
    current_a: float = 0.0
    source: str = "offline"
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class WeatherSnapshot:
    """Instantània de meteorologia i previsió solar (Inforatge + Open-Meteo)."""
    ext_temp: Optional[float] = None
    ext_humidity: float = 50.0
    rain_today: float = 0.0
    today_kwh_est: float = 5.0
    remaining_kwh_today: float = 3.0
    tomorrow_kwh_est: float = 5.0
    max_temp_today: float = 30.0
    sunset_temp_today: float = 26.0
    blackout_risk: int = 0
    target_reserve_soc: float = 85.0
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ACSnapshot:
    """Instantània de l'estat de l'Aire Condicionat (Tuya IR)."""
    power: int = 0                # 0: apagat, 1: encès
    temp: int = 26
    mode: str = "Fred"
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NotificationItem:
    """Element en cua per a l'enviament asíncron a ntfy.sh."""
    title: str
    message: str
    priority: str = "default"
    tags: str = "zap"

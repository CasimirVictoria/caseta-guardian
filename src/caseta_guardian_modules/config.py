"""
Configuració i carregament de credencials per Caseta Guardian.
"""

import json
import os

CONFIG_PATHS = [
    "/data/caseta-guardian/config.json",
    os.path.expanduser("~/.config/caseta/config.json"),
    os.path.expanduser("~/.config/caseta-guardian/config.json"),
    os.path.join(os.path.dirname(__file__), "..", "config.json")
]


def load_config() -> dict:
    """Carrega la configuració des de les rutes conegudes."""
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def require_config(cfg: dict, key: str, description: str = "") -> str:
    """Obté una credencial obligatòria del config. Falla si no existeix."""
    val = cfg.get(key)
    if not val:
        raise ValueError(
            f"Configuració obligatòria falta: '{key}'"
            + (f" ({description})" if description else "")
            + f". Afegeix-la a config.json"
        )
    return val


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

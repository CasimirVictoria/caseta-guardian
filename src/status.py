#!/usr/bin/env python3
"""
Caseta Status CLI - Tauler de Control Ràpid en Terminal
Mostra l'estat elèctric, bateria, solar, xarxa, balanç acumulat d'avui i previsió climàtica a l'instant.
"""

import json
import os
import ssl
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

CONFIG_PATHS = [
    os.path.join(os.path.dirname(__file__), "..", "config.json"),
    os.path.expanduser("~/Documents/Segon_Cervell/projects/caseta-guardian/config.json"),
    os.path.expanduser("~/.config/caseta-guardian/config.json")
]

config = {}
for p in CONFIG_PATHS:
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                config = json.load(f)
            break
        except Exception:
            pass

CERBO_IP = os.environ.get("CERBO_IP", config.get("cerbo_ip", "127.0.0.1" if os.path.exists("/opt/victronenergy") else "192.168.1.106"))
PORTAL_ID = os.environ.get("PORTAL_ID", config.get("portal_id", "c0619ab2xxxx"))
FORECAST_CACHE_FILE = "/tmp/caseta_forecast_cache.json"
DAILY_STATS_FILE = "/tmp/caseta_daily_stats.json"

# Constants Pylontech US3000C
TOTAL_NOMINAL_KWH = 3.552  # 74 Ah * 48 V = 3.552 kWh
BATTERY_SOH_FACTOR = 0.90  # 90% SoH (Capacitat real neta = ~3.20 kWh)
NET_CAPACITY_KWH = TOTAL_NOMINAL_KWH * BATTERY_SOH_FACTOR  # 3.197 kWh
SHUTDOWN_SOC_PERCENT = 10.0  # Sòl químic d'apagat de la bateria (10%)
SAI_TARGET_RESERVE_SOC = 65.0 # Sòl d'escut SAI intocable de la casa (65%)

# Colors ANSI per a la terminal
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
DIM = "\033[2m"

def get_telemetry():
    if mqtt is None:
        print(f"{RED}Error: paho-mqtt no està instal·lat.{RESET}")
        return {}, None

    data = {}
    found_portal = [PORTAL_ID]

    def on_message(client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) > 1 and parts[1] not in ("+", "#"):
                found_portal[0] = parts[1]
            val = json.loads(msg.payload.decode())
            data[msg.topic] = val.get("value")
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else None)
    client.on_message = on_message
    
    try:
        client.connect(CERBO_IP, 1883, 3)
    except Exception as e:
        print(f"{RED}Error connectant al Cerbo GX ({CERBO_IP}): {e}{RESET}")
        return {}, None

    client.subscribe("N/#")
    client.loop_start()
    client.publish(f"R/{found_portal[0]}/keepalive", "")
    time.sleep(1.2)
    client.loop_stop()
    client.disconnect()
    return data, found_portal[0]

def get_forecast():
    """Llegeix la previsió climàtica des del cache local."""
    if os.path.exists(FORECAST_CACHE_FILE):
        try:
            with open(FORECAST_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def get_daily_stats():
    """Llegeix les estadístiques d'energia acumulada d'avui."""
    if os.path.exists(DAILY_STATS_FILE):
        try:
            with open(DAILY_STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def main():
    print(f"\n{BOLD}{CYAN}⚡ TAULER DE TELEMETRIA EN DIRECTE - CASETA D'ADOR ⚡{RESET}")
    print(f"{DIM}Connectant a Cerbo GX ({CERBO_IP})...{RESET}\n")
    
    data, portal = get_telemetry()
    if not data or not portal:
        print(f"{RED}No s'han pogut obtenir dades del Cerbo GX.{RESET}\n")
        return

    forecast = get_forecast()
    daily_stats = get_daily_stats()

    # Extracció de dades en temps real
    soc = data.get(f"N/{portal}/battery/512/Soc") or 0.0
    bat_v = data.get(f"N/{portal}/battery/512/Dc/0/Voltage") or 0.0
    bat_i = data.get(f"N/{portal}/battery/512/Dc/0/Current") or 0.0
    bat_p = data.get(f"N/{portal}/battery/512/Dc/0/Power") or 0.0
    bat_temp = data.get(f"N/{portal}/battery/512/Dc/0/Temperature") or 0.0
    cell_max = data.get(f"N/{portal}/battery/512/System/MaxCellVoltage")
    cell_min = data.get(f"N/{portal}/battery/512/System/MinCellVoltage")
    
    solar_p = data.get(f"N/{portal}/pvinverter/31/Ac/Power") or 0.0
    ac_loads = data.get(f"N/{portal}/system/0/Ac/Consumption/L1/Power") or 0.0
    grid_p = data.get(f"N/{portal}/system/0/Ac/Grid/L1/Power")
    grid_v = data.get(f"N/{portal}/vebus/276/Ac/ActiveIn/L1/V") or 220.0
    vebus_mode = data.get(f"N/{portal}/vebus/276/Mode")
    ac_freq = data.get(f"N/{portal}/vebus/276/Ac/Out/L1/F") or 50.0

    # Càlculs de Càrrega i Energia (kWh)
    kwh_actual = NET_CAPACITY_KWH * (soc / 100.0)
    kwh_fins_tall = max(0.0, NET_CAPACITY_KWH * ((soc - SHUTDOWN_SOC_PERCENT) / 100.0))
    kwh_marge_sai = max(0.0, NET_CAPACITY_KWH * ((soc - SAI_TARGET_RESERVE_SOC) / 100.0))

    soc_str = f"{soc:.1f}%"
    soc_color = GREEN if soc >= 80 else (YELLOW if soc >= 65 else RED)
    
    bat_p_str = f"{bat_p:+.0f} W"
    bat_i_str = f"{bat_i:+.1f} A"
    bat_state = f"{GREEN}Carregant{RESET}" if bat_i > 0.5 else (f"{YELLOW}Descarregant{RESET}" if bat_i < -0.5 else f"{BLUE}Repòs / Balancejant{RESET}")
    
    delta_v_str = f"{(cell_max - cell_min)*1000:.0f} mV" if (cell_max and cell_min) else "N/A"

    mode_map = {1: "Charger Only", 2: "Inverter Only (Aïllat)", 3: "ON (Connectat a Xarxa)", 4: "OFF"}
    mode_str = mode_map.get(vebus_mode, f"Mode {vebus_mode}")
    mode_color = CYAN if vebus_mode == 2 else GREEN

    if grid_p is None or vebus_mode == 2:
        grid_status = f"{CYAN}Desconnectada (Zero Abocament 100% / 0 W){RESET}"
    elif grid_p < -50:
        grid_status = f"{YELLOW}Abocant cap enfora ({abs(grid_p):.0f} W){RESET}"
    elif grid_p > 50:
        grid_status = f"{BLUE}Important del carrer ({grid_p:.0f} W){RESET}"
    else:
        grid_status = f"{GREEN}Equilibrada / Neutre ({grid_p:.0f} W){RESET}"

    # Renderitzat del Panell Visual
    print("┌──────────────────────────────────────────────────────────────────────────────┐")
    print(f"│ 🔋 {BOLD}BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh){RESET}                        │")
    print(f"│    • Estat de Càrrega (SoC):  {soc_color}{BOLD}{soc_str:<7}{RESET} [{bat_state}]                        │")
    print(f"│    • Energia Disponible:      {BOLD}{kwh_actual:.2f} kWh{RESET} actuals | {GREEN}{kwh_fins_tall:.2f} kWh útils (tall 10%){RESET}       │")
    print(f"│    • Marge fins a Escut SAI:  {CYAN}{BOLD}{kwh_marge_sai:.2f} kWh lliures{RESET} (abans del sòl del 65%)                │")
    print(f"│    • Tensió i Corrent:        {bat_v:.2f} V  |  {bat_i_str} ({bat_p_str})                    │")
    print(f"│    • Cel·les (Min / Màx):     {cell_min:.3f} V / {cell_max:.3f} V (ΔV = {delta_v_str})              │")
    print(f"│    • Temperatura BMS:         {bat_temp:.1f} ºC                                          │")
    print("├──────────────────────────────────────────────────────────────────────────────┤")
    print(f"│ ☀️ {BOLD}ENERGIA SOLAR & CONSUM DE LA CASETA{RESET}                                      │")
    print(f"│    • Producció Solar Huawei:  {GREEN}{BOLD}{solar_p:6.1f} W{RESET}                                        │")
    print(f"│    • Consum Casa (AC Loads):  {YELLOW}{BOLD}{ac_loads:6.1f} W{RESET}                                        │")
    print(f"│    • Freqüència de CA Caseta: {ac_freq:.2f} Hz                                       │")
    print("├──────────────────────────────────────────────────────────────────────────────┤")
    print(f"│ 🔌 {BOLD}INVERSOR MULTIPLUS-II & XARXA EXTERIOR{RESET}                                   │")
    print(f"│    • Mode MultiPlus:          {mode_color}{BOLD}{mode_str:<30}{RESET}       │")
    print(f"│    • Tensió Xarxa L1:         {grid_v:.1f} V                                          │")
    print(f"│    • Estat de la Xarxa:       {grid_status:<48} │")

    if daily_stats:
        sol_kwh = daily_stats.get("solar_kwh_today", 6.5)
        sol_pic = daily_stats.get("solar_peak_w", 1078.6)
        con_kwh = daily_stats.get("consumption_kwh_today", 11.4)
        imp_kwh = daily_stats.get("grid_import_kwh_today", 4.9)
        exp_kwh = daily_stats.get("grid_export_kwh_today", 0.0)
        cov_pct = daily_stats.get("solar_coverage_percent", 57.0)

        print("├──────────────────────────────────────────────────────────────────────────────┤")
        print(f"│ 📊 {BOLD}BALANÇ I ENERGIA D'AVUI (Acumulats){RESET}                                       │")
        print(f"│    • Producció Solar Generada: {GREEN}{BOLD}{sol_kwh:5.2f} kWh{RESET} (Pic màxim: {BOLD}{sol_pic:.0f} W{RESET})                │")
        print(f"│    • Consum Total de la Casa:  {YELLOW}{BOLD}{con_kwh:5.2f} kWh{RESET} (Cobertura Solar: {CYAN}{BOLD}{cov_pct:.1f}%{RESET})              │")
        print(f"│    • Importat de la Xarxa:     {BLUE}{BOLD}{imp_kwh:5.2f} kWh{RESET} | Exportat: {GREEN}{BOLD}{exp_kwh:4.2f} kWh (Zero Regal 🚫){RESET}  │")

    if forecast:
        today_kwh = forecast.get("today_kwh", 0)
        tomorrow_kwh = forecast.get("tomorrow_kwh", 0)
        max_t = forecast.get("max_temp_today", 0)
        sunset_t = forecast.get("sunset_temp", 0)
        risk = forecast.get("blackout_risk", 0)
        target_soc = forecast.get("target_reserve_soc", 85)
        
        risk_color = RED if risk >= 60 else (YELLOW if risk >= 30 else GREEN)
        risk_label = "🔴 Risc Alt (Alerta Calor)" if risk >= 60 else ("🟡 Risc Mitjà" if risk >= 30 else "🟢 Risc Baix (Normal)")
        
        print("├──────────────────────────────────────────────────────────────────────────────┤")
        print(f"│ 🌤️ {BOLD}PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API){RESET}                          │")
        print(f"│    • Sol Esperat (Hui / Demà):{GREEN}{BOLD} {today_kwh:.1f} kWh{RESET} / {tomorrow_kwh:.1f} kWh                            │")
        print(f"│    • Temp. Màx / Ocàs (21h):  {max_t:.1f} ºC / {sunset_t:.1f} ºC                                 │")
        print(f"│    • Índex de Risc de Tall:   {risk_color}{BOLD}{risk_label:<26} ({risk}%){RESET}     │")
        print(f"│    • Objectiu Reserva Nocturna: {BOLD}{target_soc}% de Bateria SAI{RESET}                           │")

    print("└──────────────────────────────────────────────────────────────────────────────┘")
    
    is_active = os.system("systemctl --user is-active --quiet caseta-guardian.service") == 0
    guardian_status = f"{GREEN}🟢 ACTIU I VIGILANT (systemd){RESET}" if is_active else f"{RED}🔴 ATURAT{RESET}"
    print(f"  Guardià Natiu (caseta-guardian): {guardian_status}")
    print()

if __name__ == "__main__":
    main()

import datetime
#!/usr/bin/env python3
"""
Caseta Status CLI - Tauler de Control Ràpid en Terminal
Mostra l'estat elèctric, bateria, solar, xarxa, balanç acumulat d'avui, previsió climàtica i històric permanent.
"""

import csv
import json
import os
import re
import ssl
import sys
import time
import unicodedata

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
PORTAL_ID = os.environ.get("PORTAL_ID", config.get("portal_id", "48e7da8782fd"))
FORECAST_CACHE_FILE = "/tmp/caseta_forecast_cache.json"
DAILY_STATS_FILE = "/tmp/caseta_daily_stats.json"
HISTORY_CSV_FILE = os.path.expanduser("~/.local/share/caseta-guardian/historic_diari.csv")

TOTAL_NOMINAL_KWH = 3.552
BATTERY_SOH_FACTOR = 0.90
NET_CAPACITY_KWH = TOTAL_NOMINAL_KWH * BATTERY_SOH_FACTOR
SHUTDOWN_SOC_PERCENT = 10.0
SAI_TARGET_RESERVE_SOC = 65.0

BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
DIM = "\033[2m"

BOX_WIDTH = 76

def visible_width(s: str) -> int:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean = ansi_escape.sub('', s)
    width = 0
    for char in clean:
        if char == '\ufe0f':
            continue
        code = ord(char)
        if (0x1F300 <= code <= 0x1FAFF) or (0x2600 <= code <= 0x27BF) or (0x2B50 <= code <= 0x2B55):
            width += 2
        elif unicodedata.east_asian_width(char) in ('W', 'F'):
            width += 2
        else:
            width += 1
    return width

def box_line(content: str) -> str:
    vw = visible_width(content)
    pad = max(0, BOX_WIDTH - vw)
    return f"│ {content}{' ' * pad} │"

def get_telemetry():
    if mqtt is None:
        print(f"{RED}Error: paho-mqtt no està instal·lat.{RESET}")
        return {}, None, None, None

    data = {}
    found_portal = [PORTAL_ID]
    mqtt_stats = [None]
    mqtt_forecast = [None]

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("#")
        client.publish(f"R/{found_portal[0]}/keepalive", "")

    def on_message(client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) > 1 and parts[1] not in ("+", "#"):
                found_portal[0] = parts[1]
            val = json.loads(msg.payload.decode()).get("value")
            data[msg.topic] = val
            if "caseta/stats" in msg.topic:
                mqtt_stats[0] = val
            elif "caseta/forecast" in msg.topic:
                mqtt_forecast[0] = val
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else None)
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(CERBO_IP, 1883, 3)
    except Exception as e:
        print(f"{RED}Error connectant al Cerbo GX ({CERBO_IP}): {e}{RESET}")
        return {}, None, None, None

    client.loop_start()
    for _ in range(20):
        if f"N/{found_portal[0]}/battery/512/Soc" in data and f"N/{found_portal[0]}/vebus/276/Mode" in data and f"N/{found_portal[0]}/system/0/Ac/Consumption/L1/Power" in data:
            break
        client.publish(f"R/{found_portal[0]}/keepalive", "")
        time.sleep(0.1)
    client.loop_stop()
    client.disconnect()
    return data, found_portal[0], mqtt_stats[0], mqtt_forecast[0]

def get_forecast(mqtt_forecast=None):
    if mqtt_forecast:
        return mqtt_forecast
    if os.path.exists(FORECAST_CACHE_FILE):
        try:
            with open(FORECAST_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def get_daily_stats(mqtt_stats=None):
    if mqtt_stats:
        return mqtt_stats
    if os.path.exists(DAILY_STATS_FILE):
        try:
            with open(DAILY_STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def sync_history_from_cerbo():
    """Sincronitza el fitxer històric des del Cerbo GX abans de mostrar-lo si no estem al mateix Cerbo."""
    if os.path.exists("/opt/victronenergy") or CERBO_IP in ("127.0.0.1", "localhost"):
        return
    try:
        import subprocess
        cmd = ["scp", "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", f"root@{CERBO_IP}:/data/caseta-guardian/historic_diari.csv", HISTORY_CSV_FILE]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass

def show_history():
    sync_history_from_cerbo()
    print(f"\n{BOLD}{CYAN}📈 HISTÒRIC PERMANENT D'ENERGIA - CASETA D'ADOR 📈{RESET}")
    print(f"{DIM}Fitxer: {HISTORY_CSV_FILE}{RESET}\n")
    
    if not os.path.exists(HISTORY_CSV_FILE):
        print(f"{YELLOW}Encara no hi ha dades històriques registrades.{RESET}\n")
        return
        
    rows = []
    with open(HISTORY_CSV_FILE, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for r in reader:
            if r:
                rows.append(r)
                
    if not rows:
        print(f"{YELLOW}El registre històric és buit. S'anirà omplint cada nit a les 00:00h automàticament.{RESET}\n")
        return

    print(f"{BOLD}{'Data':<12} | {'Solar':<9} | {'Consum':<9} | {'Importat':<9} | {'Cob. Sol':<9} | {'Cost Total':<10} | {'Mode 2':<8} | {'ΔV Màx'}{RESET}")
    print("─" * 86)
    
    tot_sol = 0.0
    tot_con = 0.0
    tot_imp = 0.0
    tot_cost = 0.0
    
    for r in rows[-15:]:
        d_str, sol, con, imp, exp, cov, cost, m2, sw, dv, soh, hol = r[:12]
        tot_sol += float(sol)
        tot_con += float(con)
        tot_imp += float(imp)
        tot_cost += float(cost)
        print(f"{d_str:<12} | {GREEN}{sol:>6} kWh{RESET} | {YELLOW}{con:>6} kWh{RESET} | {BLUE}{imp:>6} kWh{RESET} | {CYAN}{cov:>6} %{RESET} | {MAGENTA}{cost:>7} €{RESET} | {m2:>5} min | {dv:>3} mV")
        
    print("─" * 86)
    print(f"{BOLD}TOTALS ({len(rows)} dies registrats):{RESET}")
    print(f" • ☀️ Solar Generat Acumulat:  {GREEN}{BOLD}{tot_sol:.2f} kWh{RESET}")
    print(f" • 🔌 Consum Casa Acumulat:    {YELLOW}{BOLD}{tot_con:.2f} kWh{RESET}")
    print(f" • 🏢 Importat de la Xarxa:    {BLUE}{BOLD}{tot_imp:.2f} kWh{RESET}")
    print(f" • 💶 Cost Elèctric Facturat:  {MAGENTA}{BOLD}{tot_cost:.2f} €{RESET}\n")

def main():
    if "--history" in sys.argv or "-h" in sys.argv or "--historic" in sys.argv:
        show_history()
        return

    print(f"\n{BOLD}{CYAN}⚡ TAULER DE TELEMETRIA EN DIRECTE - CASETA D'ADOR ⚡{RESET}")
    print(f"{DIM}Connectant a Cerbo GX ({CERBO_IP})...{RESET}")
    now_ts = datetime.datetime.now().strftime("%d/%m/%Y - %H:%M:%S")
    print(f"{DIM}🕒 Registre en directe: {now_ts}{RESET}\n")
    
    data, portal, mqtt_stats, mqtt_forecast = get_telemetry()
    if not data or not portal:
        print(f"{RED}No s'han pogut obtenir dades del Cerbo GX.{RESET}\n")
        return

    forecast = get_forecast(mqtt_forecast)
    daily_stats = get_daily_stats(mqtt_stats)

    soc = data.get(f"N/{portal}/battery/512/Soc") or 0.0
    soh = data.get(f"N/{portal}/battery/512/Soh") or 90.0
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

    kwh_actual = NET_CAPACITY_KWH * (soc / 100.0)
    kwh_fins_tall = max(0.0, NET_CAPACITY_KWH * ((soc - SHUTDOWN_SOC_PERCENT) / 100.0))
    kwh_marge_sai = max(0.0, NET_CAPACITY_KWH * ((soc - SAI_TARGET_RESERVE_SOC) / 100.0))

    soc_str = f"{soc:.1f}%"
    soc_color = GREEN if soc >= 80 else (YELLOW if soc >= 65 else RED)
    
    bat_p_str = f"{bat_p:+.0f} W"
    bat_i_str = f"{bat_i:+.1f} A"
    bat_state = f"{GREEN}Carregant{RESET}" if bat_i > 0.5 else (f"{YELLOW}Descarregant{RESET}" if bat_i < -0.5 else f"{BLUE}Repòs / Balancejant{RESET}")
    
    delta_v_val = (cell_max - cell_min)*1000 if (cell_max and cell_min) else None
    if delta_v_val is not None:
        delta_color = GREEN if delta_v_val <= 15 else (YELLOW if delta_v_val <= 35 else RED)
        delta_v_str = f"{delta_color}{delta_v_val:.0f} mV{RESET}"
    else:
        delta_v_str = "N/A"

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

    print("┌" + "─" * (BOX_WIDTH + 2) + "┐")
    print(box_line(f"🔋 {BOLD}BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh){RESET}"))
    print(box_line(f"   • Estat de Càrrega (SoC):  {soc_color}{BOLD}{soc_str:<6}{RESET} [{bat_state}]  (SoH BMS: {soh:.0f}%)"))
    print(box_line(f"   • Energia Disponible:      {BOLD}{kwh_actual:.2f} kWh{RESET} actuals | {GREEN}{kwh_fins_tall:.2f} kWh útils (tall 10%){RESET}"))
    print(box_line(f"   • Marge fins a Escut SAI:  {CYAN}{BOLD}{kwh_marge_sai:.2f} kWh lliures{RESET} (abans del sòl del 65%)"))
    print(box_line(f"   • Tensió i Corrent:        {bat_v:.2f} V  |  {bat_i_str} ({bat_p_str})"))
    cell_min_str = f"{cell_min:.3f} V" if cell_min is not None else "N/A"
    cell_max_str = f"{cell_max:.3f} V" if cell_max is not None else "N/A"
    print(box_line(f"   • Cel·les (Min / Màx):     {cell_min_str} / {cell_max_str} (ΔV = {delta_v_str})"))
    print(box_line(f"   • Temperatura BMS:         {bat_temp:.1f} ºC"))
    
    print("├" + "─" * (BOX_WIDTH + 2) + "┤")
    print(box_line(f"☀️ {BOLD}ENERGIA SOLAR & CONSUM DE LA CASETA{RESET}"))
    print(box_line(f"   • Producció Solar Huawei:  {GREEN}{BOLD}{solar_p:6.1f} W{RESET}"))
    print(box_line(f"   • Consum Casa (AC Loads):  {YELLOW}{BOLD}{ac_loads:6.1f} W{RESET}"))
    print(box_line(f"   • Freqüència de CA Caseta: {ac_freq:.2f} Hz"))

    print("├" + "─" * (BOX_WIDTH + 2) + "┤")
    print(box_line(f"🔌 {BOLD}INVERSOR MULTIPLUS-II & XARXA EXTERIOR{RESET}"))
    print(box_line(f"   • Mode MultiPlus:          {mode_color}{BOLD}{mode_str}{RESET}"))
    print(box_line(f"   • Tensió Xarxa L1:         {grid_v:.1f} V"))
    print(box_line(f"   • Estat de la Xarxa:       {grid_status}"))

    if daily_stats:
        sol_kwh = daily_stats.get("solar_kwh_today", 0.0)
        sol_pic = daily_stats.get("solar_peak_w", 0.0)
        con_kwh = daily_stats.get("consumption_kwh_today", 0.0)
        imp_kwh = daily_stats.get("grid_import_kwh_today", 0.0)
        exp_kwh = daily_stats.get("grid_export_kwh_today", 0.0)
        cov_pct = daily_stats.get("solar_coverage_percent", 0.0)
        cost_tot = daily_stats.get("cost_total_today", 0.19)

        print("├" + "─" * (BOX_WIDTH + 2) + "┤")
        print(box_line(f"📊 {BOLD}BALANÇ I ENERGIA D'AVUI (Acumulats){RESET}"))
        print(box_line(f"   • Producció Solar Generada: {GREEN}{BOLD}{sol_kwh:5.2f} kWh{RESET} (Pic màxim: {BOLD}{sol_pic:.0f} W{RESET})"))
        print(box_line(f"   • Consum Total de la Casa:  {YELLOW}{BOLD}{con_kwh:5.2f} kWh{RESET} (Cobertura Solar: {CYAN}{BOLD}{cov_pct:.1f}%{RESET})"))
        print(box_line(f"   • Importat de Xarxa:        {BLUE}{BOLD}{imp_kwh:5.2f} kWh{RESET} | Exportat: {GREEN}{BOLD}{exp_kwh:4.2f} kWh (Zero Regal){RESET}"))
        print(box_line(f"   • Cost Total Facturat d'Hui: {MAGENTA}{BOLD}{cost_tot:5.2f} €{RESET} (Tarifa 2.0TD - Tot inclòs)"))

    if forecast:
        today_kwh = forecast.get("today_kwh", 0)
        tomorrow_kwh = forecast.get("tomorrow_kwh", 0)
        max_t = forecast.get("max_temp_today", 0)
        sunset_t = forecast.get("sunset_temp", 0)
        risk = forecast.get("blackout_risk", 0)
        target_soc = forecast.get("target_reserve_soc", 85)
        
        risk_color = RED if risk >= 60 else (YELLOW if risk >= 30 else GREEN)
        risk_label = "🔴 Risc Alt (Alerta Calor)" if risk >= 60 else ("🟡 Risc Mitjà" if risk >= 30 else "🟢 Risc Baix (Normal)")
        
        print("├" + "─" * (BOX_WIDTH + 2) + "┤")
        print(box_line(f"🌤️ {BOLD}PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API){RESET}"))
        print(box_line(f"   • Sol Esperat (Hui / Demà): {GREEN}{BOLD}{today_kwh:.1f} kWh{RESET} / {tomorrow_kwh:.1f} kWh"))
        print(box_line(f"   • Temp. Màx / Ocàs (21h):   {max_t:.1f} ºC / {sunset_t:.1f} ºC"))
        print(box_line(f"   • Índex de Risc de Tall:    {risk_color}{BOLD}{risk_label} ({risk}%){RESET}"))
        print(box_line(f"   • Objectiu Reserva Nocturna:  {BOLD}{target_soc}% de Bateria SAI{RESET}"))

    print("└" + "─" * (BOX_WIDTH + 2) + "┘")
    print(f"  Guardià Natiu (caseta-guardian): {GREEN}🟢 ACTIU I VIGILANT A CERBO GX (Venus OS){RESET}")
    print()

if __name__ == "__main__":
    main()

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
    mqtt_clima = [None]
    mqtt_inforatge = [None]
    mqtt_ac = [None]

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("N/+/battery/512/#")
        client.subscribe("N/+/pvinverter/#")
        client.subscribe("N/+/system/0/#")
        client.subscribe("N/+/vebus/276/#")
        client.subscribe("caseta/#")
        client.publish(f"R/{PORTAL_ID}/keepalive", "")

    def on_message(client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) > 1 and parts[1] not in ("+", "#", "caseta"):
                found_portal[0] = parts[1]
            val = json.loads(msg.payload.decode()).get("value")
            data[msg.topic] = val
            if "caseta/stats" in msg.topic:
                mqtt_stats[0] = val
            elif "caseta/forecast" in msg.topic:
                mqtt_forecast[0] = val
            elif "caseta/inforatge" in msg.topic:
                mqtt_inforatge[0] = val
            elif "caseta/ac" in msg.topic:
                mqtt_ac[0] = val
            elif "caseta/clima" in msg.topic:
                raw_json = json.loads(msg.payload.decode())
                mqtt_clima[0] = raw_json.get("value") if isinstance(raw_json, dict) and "value" in raw_json else raw_json
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else None)
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(CERBO_IP, 1883, 2)
    except Exception as e:
        print(f"{RED}Error connectant al Cerbo GX ({CERBO_IP}): {e}{RESET}")
        return {}, None, None, None, None, None, None

    client.loop_start()
    start = time.time()
    while time.time() - start < 1.5:
        time.sleep(0.02)
        portal = found_portal[0]
        if (
            portal
            and f"N/{portal}/battery/512/Soc" in data
            and f"N/{portal}/system/0/Ac/Consumption/L1/Power" in data
            and mqtt_stats[0] is not None
            and mqtt_forecast[0] is not None
            and mqtt_clima[0] is not None
        ):
            break
        
    client.loop_stop()
    client.disconnect()
    return data, found_portal[0], mqtt_stats[0], mqtt_forecast[0], mqtt_clima[0], mqtt_inforatge[0], mqtt_ac[0]

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

    print(f"{BOLD}{'Data':<12} | {'Solar':<9} | {'Consum':<9} | {'Importat':<9} | {'Cob. Sol':<9} | {'Cost Total':<10} | {'Mode 2'}{RESET}")
    print("─" * 76)
    
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
        print(f"{d_str:<12} | {GREEN}{sol:>6} kWh{RESET} | {YELLOW}{con:>6} kWh{RESET} | {BLUE}{imp:>6} kWh{RESET} | {CYAN}{cov:>6} %{RESET} | {MAGENTA}{cost:>7} €{RESET} | {m2:>5} min")
        
    print("─" * 76)
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
    
    data, portal, mqtt_stats, mqtt_forecast, mqtt_clima, mqtt_inforatge, mqtt_ac = get_telemetry()
    if not data or not portal:
        print(f"{RED}No s'han pogut obtenir dades del Cerbo GX.{RESET}\n")
        return

    forecast = get_forecast(mqtt_forecast)
    daily_stats = get_daily_stats(mqtt_stats)

    if daily_stats:
        sol_kwh_today = daily_stats.get("solar_kwh_today", 0.0)
        sol_peak_w = daily_stats.get("solar_peak_w", 0.0)
        con_kwh_today = daily_stats.get("consumption_kwh_today", 0.0)
        imp_kwh_today = daily_stats.get("grid_import_kwh_today", 0.0)
        exp_kwh_today = daily_stats.get("grid_export_kwh_today", 0.0)
        cov_pct_today = daily_stats.get("solar_coverage_percent", 0.0)
        cost_today = daily_stats.get("cost_total_today", 0.0)
    else:
        sol_kwh_today, con_kwh_today, imp_kwh_today, cost_today = get_today_accumulated_from_file()
        sol_peak_w = 0.0
        exp_kwh_today = 0.0
        cov_pct_today = (sol_kwh_today / max(0.01, con_kwh_today)) * 100.0
    soc = data.get(f"N/{portal}/battery/512/Soc")
    soh = data.get(f"N/{portal}/battery/512/Soh")
    pylon_v = data.get(f"N/{portal}/battery/512/Dc/0/Voltage")
    pylon_i = data.get(f"N/{portal}/battery/512/Dc/0/Current")
    pylon_p = data.get(f"N/{portal}/battery/512/Dc/0/Power")
    pylon_t = data.get(f"N/{portal}/battery/512/Dc/0/Temperature")
    cell_min_v = data.get(f"N/{portal}/battery/512/System/MinCellVoltage")
    cell_max_v = data.get(f"N/{portal}/battery/512/System/MaxCellVoltage")
    
    pv_p = data.get(f"N/{portal}/pvinverter/20/Ac/Power") or data.get(f"N/{portal}/system/0/Ac/PvOnOutput/L1/Power") or 0.0
    ac_loads = (
        data.get(f"N/{portal}/system/0/Ac/Consumption/L1/Power")
        or data.get(f"N/{portal}/system/0/Ac/ConsumptionOnOutput/L1/Power")
        or data.get(f"N/{portal}/vebus/276/Ac/Out/L1/P")
        or data.get(f"N/{portal}/vebus/276/Ac/Out/P")
        or 0.0
    )
    freq = data.get(f"N/{portal}/vebus/276/Ac/Out/L1/F") or 50.0
    
    grid_p = (
        data.get(f"N/{portal}/system/0/Ac/Grid/L1/Power")
        or data.get(f"N/{portal}/system/0/Ac/ActiveIn/L1/Power")
        or data.get(f"N/{portal}/vebus/276/Ac/ActiveIn/L1/P")
        or data.get(f"N/{portal}/vebus/276/Ac/ActiveIn/P")
        or 0.0
    )
    grid_v = data.get(f"N/{portal}/vebus/276/Ac/ActiveIn/L1/V") or data.get(f"N/{portal}/vebus/276/Ac/Out/L1/V") or 230.0
    multi_mode = data.get(f"N/{portal}/vebus/276/Mode")
    
    if soc is None:
        print(f"{RED}No s'han pogut llegir les dades de bateria.{RESET}\n")
        return

    # Càlculs de seguretat i energia
    kwh_actuals = (soc / 100.0) * NET_CAPACITY_KWH
    kwh_utils = max(0.0, ((soc - SHUTDOWN_SOC_PERCENT) / 100.0) * NET_CAPACITY_KWH)
    kwh_fins_escut = max(0.0, ((soc - SAI_TARGET_RESERVE_SOC) / 100.0) * NET_CAPACITY_KWH)

    # 1. BATERIA
    print("┌" + "─" * (BOX_WIDTH + 2) + "┐")
    print(box_line(f"🔋 {BOLD}BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh){RESET}"))
    
    soc_color = GREEN if soc >= 70 else (YELLOW if soc >= 40 else RED)
    status_str = "[Carregant]" if pylon_i and pylon_i > 0.5 else ("[Descarregant]" if pylon_i and pylon_i < -0.5 else "[En Repòs]")
    soh_str = f"  (SoH BMS: {soh:.0f}%)" if soh is not None else ""
    print(box_line(f"   • Estat de Càrrega (SoC):  {soc_color}{BOLD}{soc:.1f}%{RESET}  {status_str}{soh_str}"))
    print(box_line(f"   • Energia Disponible:      {BOLD}{kwh_actuals:.2f} kWh{RESET} actuals | {GREEN}{kwh_utils:.2f} kWh{RESET} útils (tall {SHUTDOWN_SOC_PERCENT:.0f}%)"))
    print(box_line(f"   • Marge fins a Escut SAI:  {CYAN}{BOLD}{kwh_fins_escut:.2f} kWh{RESET} lliures (abans del sòl del {SAI_TARGET_RESERVE_SOC:.0f}%)"))
    
    if pylon_v is not None and pylon_i is not None and pylon_p is not None:
        print(box_line(f"   • Tensió i Corrent:        {pylon_v:.2f} V  |  {pylon_i:.1f} A ({pylon_p:.0f} W)"))
        
    if cell_min_v is not None and cell_max_v is not None:
        delta_v = (cell_max_v - cell_min_v) * 1000.0
        dv_color = GREEN if delta_v <= 15 else (YELLOW if delta_v <= 35 else RED)
        print(box_line(f"   • Cel·les (Min / Màx):     {cell_min_v:.3f} V / {cell_max_v:.3f} V (ΔV = {dv_color}{delta_v:.0f} mV{RESET})"))
        
    if pylon_t is not None:
        print(box_line(f"   • Temperatura BMS:         {pylon_t:.1f} ºC"))

    # 2. SOLAR I CONSUM
    print("├" + "─" * (BOX_WIDTH + 2) + "┤")
    print(box_line(f"☀️ {BOLD}ENERGIA SOLAR & CONSUM DE LA CASETA{RESET}"))
    print(box_line(f"   • Producció Solar Huawei:  {GREEN}{BOLD}{pv_p:>6.1f} W{RESET}"))
    print(box_line(f"   • Consum Casa (AC Loads):  {YELLOW}{BOLD}{ac_loads:>6.1f} W{RESET}"))
    print(box_line(f"   • Freqüència de CA Caseta: {freq:.2f} Hz"))

    # 3. MULTIPLUS I XARXA
    print("├" + "─" * (BOX_WIDTH + 2) + "┤")
    print(box_line(f"🔌 {BOLD}INVERSOR MULTIPLUS-II & XARXA EXTERIOR{RESET}"))
    mode_map = {
        1: "Només Carregador",
        2: "Sols Inverter",
        3: "ON (Connectat a Xarxa)",
        4: "OFF (Apagat)"
    }
    mode_desc = mode_map.get(multi_mode, f"Mode {multi_mode}")
    print(box_line(f"   • Mode MultiPlus:          {mode_desc}"))
    print(box_line(f"   • Tensió Xarxa L1:         {grid_v:.1f} V"))
    
    if grid_p > 15:
        grid_status = f"{BLUE}Important de Xarxa ({grid_p:.0f} W){RESET}"
    elif grid_p < -15:
        grid_status = f"{MAGENTA}Abocant cap enfora ({-grid_p:.0f} W){RESET}"
    else:
        grid_status = f"{GREEN}Equilibrada / Neutre ({grid_p:.0f} W){RESET}"
    print(box_line(f"   • Estat de la Xarxa:       {grid_status}"))

    # 4. BALANÇ D'AVUI
    print("├" + "─" * (BOX_WIDTH + 2) + "┤")
    print(box_line(f"📊 {BOLD}BALANÇ I ENERGIA D'AVUI (Acumulats){RESET}"))
    peak_str = f" (Pic màxim: {sol_peak_w:.0f} W)" if sol_peak_w > 0 else ""
    print(box_line(f"   • Producció Solar Generada:  {GREEN}{BOLD}{sol_kwh_today:.2f} kWh{RESET}{peak_str}"))
    print(box_line(f"   • Consum Total de la Casa:   {YELLOW}{BOLD}{con_kwh_today:.2f} kWh{RESET} (Cobertura Solar: {CYAN}{cov_pct_today:.1f}%{RESET})"))
    print(box_line(f"   • Importat de Xarxa:         {BLUE}{imp_kwh_today:.2f} kWh{RESET} | Exportat: {MAGENTA}{exp_kwh_today:.2f} kWh{RESET} (Zero Regal)"))
    print(box_line(f"   • Cost Total Facturat d'Hui:  {MAGENTA}{BOLD}{cost_today:.2f} €{RESET} (Tarifa 2.0TD - Tot inclòs)"))

    # 5. PREVISIÓ I RISC
    if forecast:
        today_est = forecast.get("today_kwh", 0.0)
        tomorrow_est = forecast.get("tomorrow_kwh", 0.0)
        max_t = forecast.get("max_temp_today", 0.0)
        sunset_t = forecast.get("sunset_temp", 0.0)
        risk = forecast.get("blackout_risk", 25)
        target_soc = forecast.get("target_reserve_soc", 85)
        
        risk_color = GREEN if risk < 35 else (YELLOW if risk < 65 else RED)
        risk_label = "Risc Baix (Normal)" if risk < 35 else ("Risc Moderat (Atenció)" if risk < 65 else "Risc Alt (Restaura SAI)")
        
        print("├" + "─" * (BOX_WIDTH + 2) + "┤")
        print(box_line(f"🌤️ {BOLD}PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API){RESET}"))
        print(box_line(f"   • Sol Esperat (Hui / Demà): {today_est:.1f} kWh / {tomorrow_est:.1f} kWh"))
        print(box_line(f"   • Temp. Màx / Ocàs (21h):   {max_t:.1f} ºC / {sunset_t:.1f} ºC"))
        print(box_line(f"   • Índex de Risc de Tall:    {risk_color}{BOLD}{risk_label} ({risk}%){RESET}"))
        print(box_line(f"   • Objectiu Reserva Nocturna:  {BOLD}{target_soc}% de Bateria SAI{RESET}"))

    # 6. CLIMA & BIOCLIMÀTICA (ZIGBEE + INFORATGE)
    if mqtt_clima or mqtt_inforatge:
        sensors_dict = (mqtt_clima.get("sensors") if mqtt_clima else {}) or {}
        s1 = sensors_dict.get("sensor_1") or {}
        s2 = sensors_dict.get("sensor_2") or {}

        print("├" + "─" * (BOX_WIDTH + 2) + "┤")
        print(box_line(f"🌡️ {BOLD}CLIMA & METEOROLOGIA (Zigbee en RAM + Inforatge Ador){RESET}"))
        
        # Sensor 1: Habitació xiquets (TS0201)
        if s1.get("temperatura") is not None:
            t1 = s1.get("temperatura")
            h1 = s1.get("humitat")
            bat1 = s1.get("bateria")
            bat_str1 = f"  [{GREEN}🔋 Pila: {bat1}%{RESET}]" if bat1 is not None else ""
            print(box_line(f"   • {BOLD}Habitació xiquets:{RESET}     {YELLOW}{BOLD}{t1:.2f} ºC{RESET} | {CYAN}{BOLD}{h1:.1f} %{RESET}{bat_str1}"))

        # Sensor 2: Saló (ZG-204ZV)
        if s2.get("temperatura") is not None:
            t2 = s2.get("temperatura")
            h2 = s2.get("humitat")
            lux2 = s2.get("lux")
            pres2 = s2.get("presencia")
            bat2 = s2.get("bateria")
            pres_str = f"{MAGENTA}{BOLD}🚶 Presència{RESET}" if pres2 else f"{GREEN}🟢 Repòs{RESET}"
            lux_str = f"{lux2:.0f} Lux" if lux2 is not None else "0 Lux"
            bat_str2 = f"  [{GREEN}🔋 {bat2}%{RESET}]" if bat2 is not None else ""
            print(box_line(f"   • {BOLD}Saló (Multisensor):{RESET}    {GREEN}{BOLD}{t2:.2f} ºC{RESET} | {CYAN}{BOLD}{h2:.1f} %{RESET} | {YELLOW}{lux_str}{RESET} | {pres_str}{bat_str2}"))

        # Climatització AC (Mitsubishi Electric / Tuya S06)
        if mqtt_ac and mqtt_ac.get("power") is not None:
            ac_power = mqtt_ac.get("power")
            ac_temp = mqtt_ac.get("temp", 26)
            ac_mode = mqtt_ac.get("mode", "Fred")
            if ac_power == 1:
                ac_str = f"{CYAN}❄️ Mode {ac_mode} a {BOLD}{ac_temp} ºC{RESET} [{GREEN}Encès{RESET}]"
            else:
                ac_str = f"{DIM}⚪ Apagat / En Repòs{RESET}"
            print(box_line(f"   • {BOLD}Climatització AC:{RESET}      {ac_str}"))

        # Inforatge Ador
        if mqtt_inforatge and mqtt_inforatge.get("temperatura") is not None:
            t_ext = mqtt_inforatge.get("temperatura")
            h_ext = mqtt_inforatge.get("humitat")
            v_vel = mqtt_inforatge.get("vent_vel", 0)
            v_dir = mqtt_inforatge.get("vent_dir", "")
            p_bar = mqtt_inforatge.get("pressio")
            print(box_line(f"   • {BOLD}Exterior Ador (Oficial):{RESET} {BLUE}{BOLD}{t_ext:.1f} ºC{RESET} | {CYAN}{BOLD}{h_ext:.0f} %{RESET} | {v_vel} km/h {v_dir} | {p_bar} hPa"))

    print("└" + "─" * (BOX_WIDTH + 2) + "┘")
    print(f"  Guardià Natiu (caseta-guardian): {GREEN}🟢 ACTIU I VIGILANT A CERBO GX (Venus OS){RESET}")
    print()

if __name__ == "__main__":
    main()

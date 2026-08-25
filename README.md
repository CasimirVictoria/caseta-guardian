# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

[🇬🇧 English Version](README.md) | <img src="assets/flag_catalonia.svg" alt="Catalunya" width="18" height="12" style="vertical-align: middle;" /> [Versió en Català](README.ca.md)

Lightweight native Python 3 systemd daemon and real-time CLI telemetry dashboard for autonomous energy management, LiFePO4 battery health protection (Pylontech US3000C / US5000), UPS resilience, and smart HVAC control on **Victron ESS installations** (MultiPlus-II 24/3000 or 48/3000, Cerbo GX, AC-Out Solar Inverters).

---

## 🏗️ Architecture & Production Deployment

The project is designed with maximum modularity and is currently running **in production directly on the Cerbo GX (Venus OS)** or optionally on an external Linux host connected via local LAN:

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    🎛️ CERBO GX (VENUS OS IN PRODUCTION)                  │
  │                                                                          │
  │  • caseta_guardian.py (Native daemontools service at /data/caseta-guardian) │
  │  • Internal MQTT connection to 127.0.0.1 (FlashMQ / <0.1 ms latency)     │
  │  • Predictive solar forecast engine (Open-Meteo API)                     │
  │  • Ntfy push notifications & Tuya Cloud OpenAPI for AC climate control   │
  │  • Guaranteed persistence in /data/ (survives Victron firmware updates)  │
  │  • Tiny footprint: ~19 MB RAM (<2% RAM) | 0.0% CPU | Cool CPU temp       │
  └──────────────────────────────────────────────────────────────────────────┘
                                      ▲
                                      │ MQTT / LAN / SSH
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                 💻 LINUX HOST / CLI CLIENT (caseta)                      │
  │  • Real-time CLI telemetry dashboard and instant diagnostics (<0.4s)     │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 🌟 Key Advantages of Native Cerbo GX Deployment:

1. 🟢 **Total 24/7/365 Autonomy:**
   - Operation does not depend on any personal computer being powered on. The Cerbo GX is powered directly from the 24V/48V battery bank and runs continuously.
   - **Zero Latency (127.0.0.1):** Direct internal communication with FlashMQ and D-Bus.

2. 🛡️ **Update-Proof Persistence on Venus OS:**
   - The service resides in the persistent `/data/caseta-guardian/` partition linked to `/service/caseta-guardian` and supervised by `daemontools` (automatic restarts on any failure).
   - Embedded in `/data/rc.local` to survive all official Victron firmware upgrades.

3. 🪶 **Negligible Footprint:**
   - Consumes only **~19 MB of RAM** (<2% of total RAM) and **0.0% CPU**, leaving over 744 MB of free RAM.

---

## ⚡ Extreme Hardware & System Optimization (Cerbo GX)

The system was heavily refactored and profiled for long-term physical hardware preservation and embedded performance:

| Optimization Layer | Implementation | Measured Hardware Impact |
| :--- | :--- | :--- |
| 📡 **Targeted MQTT Subscriptions** | Surgical subscriptions to explicit topics instead of global `#` wildcard. | **Eliminates ~70% of broker traffic**, lowering CPU wakeups. |
| 💾 **Single Daily Disk Persistence** | Energy integrals calculated in RAM; written to eMMC Flash **strictly once per day at 00:00:00h** or service shutdown. | **Reduces Flash writes by 99.999%** ($86,400 \rightarrow 1$ write/day). |
| ⏳ **eMMC Flash Lifespan** | Certified at **0.066 KB/s (5.6 MB/day)** write rate on the 8GB industrial Micron chip. | **Lifespan extended to >140 - 1,400+ years** (>75% health left). |
| 🧮 **Cached Computational Algorithms** | Gauss Easter algorithm, calendar holiday table, and Madrid timezone object cached once daily at midnight. | Eliminates unnecessary floating-point operations every second. |
| 🧹 **Service Pruning & RAM Recovery** | Cleanly disabled unused daemons (`vesmart-server` Bluetooth radio, `dbus-shelly` scanner, `vrmlogger`). | **Reclaimed >100 MB of RAM** (**744 MB free RAM** / 73% available). |
| 💤 **CPU Load & Thermal Health** | Background load average dropped to **0.20 - 0.50** with **90% - 95% CPU Idle**. | CPU running exceptionally cool at **~46.5 ºC**. |

---

## 🌟 Philosophy: The "Holy Grail" of Solar Autoconsumption

This project solves the fundamental trade-off of residential solar by combining **the best of off-grid islanded systems with the best of grid-tied architectures**:

```
  ┌────────────────────────────────────────┐  ┌────────────────────────────────────────┐
  │         🏝️ BEST OF OFF-GRID            │  │          🔌 BEST OF ON-GRID            │
  ├────────────────────────────────────────┤  ├────────────────────────────────────────┤
  │ • Zero Grid Export (100% Zero-Feed-in) │  │ • Millisecond Grid Reconnection (2ms). │
  │ • MultiPlus controls AC frequency      │  │ • Utility grid assists on heavy loads  │
  │   (50.2 - 52.7 Hz) to throttle PV.     │    (PowerAssist for oven, coffee machine).│
  │ • True energy autonomy and resilience. │  │ • Zero risk of running out of power.   │
  └────────────────────────────────────────┘  └────────────────────────────────────────┘
```

---

## 🔋 The "Battery as a Buffer & UPS (Zero Deep Cycling)" Principle

Unlike conventional off-grid setups that cycle the battery deeply every single day (100% $\rightarrow$ 20%), this system operates the battery exclusively as a **dynamic power buffer and an emergency UPS (SAI)**:

```
  100% SoC  ───────┐
                   │  🟢 DYNAMIC BUFFER ZONE (20% – 30% shallow margin):
                   │     • Absorbs midday solar peaks at zero cost.
                   │     • Buffers inductive surges (coffee maker, microwave).
   70% - 80% SoC ──┴───────────────────────────────────────────────────────
                   │
                   │  🛡️ UNTOUCHABLE EMERGENCY UPS (SAI) ZONE (70% – 80%):
                   │     • Over 2.2 – 2.5 kWh net capacity always reserved.
                   │     • Instant 0 ms UPS switchover during grid blackouts.
                   │     • Zero deep degradation cycling (>20,000 cycles / >30 years).
    0% SoC  ───────┘
```

---

## 🏛️ The 4 Fundamental Priority Laws

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ 1. 🛡️ BATTERY CHEMICAL HEALTH (Priority 0)                             │
  │    • Overnight 100% Top-Balancing using off-peak electricity rates.    │
  │    • Instant grid reconnection if discharge exceeds 15 A (>750 W)      │
  │      for >5 s or if SoC drops below 80% in islanded Mode 2.            │
  │    • HVAC Emergency Cutoff: Automatic Tuya Cloud IR shutdown if SoC    │
  │      drops below 65% to protect the 24-hour UPS reserve.               │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 2. 🔌 RESILIENCE & UPS BACKUP (Priority 1)                             │
  │    • Automated weather modeling via Open-Meteo API every 60 minutes.   │
  │    • High Blackout Risk trigger (Tmax >= 38ºC, T21h >= 31ºC, or low    │
  │      grid voltage <190V) locks the reserve floor to 95% – 100%.        │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🏝️ ZERO GRID FEED-IN (Priority 2)                                  │
  │    • Switches to Inverter Only (Mode 2) only when: SoC > 88%, export   │
  │      > 50 W for >30 s, and battery is resting (<2 A discharge).        │
  │    • The MultiPlus is configured to automatically raise the AC         │
  │      frequency (50.2 - 51.5 Hz) to throttle the grid PV inverter.      │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 4. ☀️ MAXIMUM SOLAR HARVEST (Priority 3)                               │
  │    • 4-Milestone Clock Schedule:                                       │
  │      - 00:00h - 06:59h: Min SoC = 100% (Super-off-peak & Top-balance). │
  │      - 07:00h - 09:29h: Min SoC = 85% (Morning breakfast buffer).      │
  │      - 09:30h - 16:29h: Min SoC = 75% (880 Wh headroom for solar peak).│
  │      - 16:30h - 23:59h: Min SoC = 85% (Evening/night UPS reserve).     │
  │      - Weekends & Holidays (18h+): Min SoC = 100% (24h flat valley).   │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Mechanical Relay Protection & Current Physics

- **Oversized Industrial Relay:** MultiPlus-II features an internal **`32 A` (7.3 kW)** transfer relay. When limited to **`5 A` (1.15 kW)** grid current, the contacts operate at only **`15%` nominal load**.
- **Arc Stress Physics ($I^2$):** $(5/32)^2 = 0.024 \implies \mathbf{40\times\text{ less electrical arc erosion}}$ on the silver contacts.
- **Zero-Cross Switching:** Victron DSP synchronizes contact opening and closing precisely at the $0\text{ V}$ AC waveform crossing.
- **Mandatory Hysteresis:** Minimum **`5 minutes (300 s)`** lockout between relay state changes to completely eliminate contact chattering.

---

## 💻 Real-Time CLI Dashboard (`caseta`)

```bash
$ caseta

⚡ TAULER DE TELEMETRIA EN DIRECTE - CASETA D'ADOR ⚡
Connectant a Cerbo GX (192.168.1.106)...
🕒 Registre en directe: 25/08/2026 - 16:25:09

┌──────────────────────────────────────────────────────────────────────────────┐
│ 🔋 BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh)                        │
│    • Estat de Càrrega (SoC):  88.0%  [Repòs / Balancejant]  (SoH BMS: 90%)   │
│    • Energia Disponible:      2.81 kWh actuals | 2.49 kWh útils (tall 10%)   │
│    • Marge fins a Escut SAI:  0.74 kWh lliures (abans del sòl del 65%)       │
│    • Tensió i Corrent:        49.93 V  |  +0.3 A (+14 W)                     │
│    • Cel·les (Min / Màx):     3.327 V / 3.330 V (ΔV = 3 mV)                  │
│    • Temperatura BMS:         32.6 ºC                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ ☀️ ENERGIA SOLAR & CONSUM DE LA CASETA                                       │
│    • Producció Solar Huawei:   757.4 W                                       │
│    • Consum Casa (AC Loads):   759.5 W                                       │
│    • Freqüència de CA Caseta: 49.95 Hz                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🔌 INVERSOR MULTIPLUS-II & XARXA EXTERIOR                                    │
│    • Mode MultiPlus:          ON (Connectat a Xarxa)                         │
│    • Tensió Xarxa L1:         225.1 V                                        │
│    • Estat de la Xarxa:       Important del carrer (58 W)                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ 📊 BALANÇ I ENERGIA D'AVUI (Acumulats)                                       │
│    • Producció Solar Generada:  5.92 kWh (Pic màxim: 1065 W)                 │
│    • Consum Total de la Casa:   8.45 kWh (Cobertura Solar: 70.1%)            │
│    • Importat de Xarxa:         3.22 kWh | Exportat: 0.01 kWh (Zero Regal)   │
│    • Cost Total Facturat d'Hui:  0.48 € (Tarifa 2.0TD - Tot inclòs)          │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🌤️ PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API)                            │
│    • Sol Esperat (Hui / Demà): 4.8 kWh / 7.8 kWh                             │
│    • Temp. Màx / Ocàs (21h):   32.9 ºC / 30.6 ºC                             │
│    • Índex de Risc de Tall:    🟡 Risc Mitjà (35%)                           │
│    • Objectiu Reserva Nocturna:  85.0% de Bateria SAI                        │
└──────────────────────────────────────────────────────────────────────────────┘
  Guardià Natiu (caseta-guardian): 🟢 ACTIU I VIGILANT A CERBO GX (Venus OS)
```

> **Note on Economic & Cost Tracking:** The real-time cost calculation is tailored to the installation's specific Spanish 2.0TD contract (*Imagina Energía* - 1.150 kW contracted power for P1/P3, real-time Time-of-Use rates for Off-peak/Mid-peak/Peak periods, meter rental fees, and energy taxes IEE 3.8% + IVA 10%). Parameters can be adapted in code or configuration.

---

## ⚙️ Configuration & Privacy (`config.json`)

All private credentials, IPs, and geolocation coordinates are isolated from git tracking using `.gitignore`:

```bash
cp config.example.json config.json
```

Example `config.json`:
```json
{
  "cerbo_ip": "192.168.1.100",
  "portal_id": "c0619ab2xxxx",
  "ntfy_topic": "your_private_ntfy_topic",
  "tuya_s06_ip": "192.168.1.50",
  "tuya_dev_id": "your_tuya_device_id",
  "tuya_local_key": "your_tuya_local_key",
  "latitude": 39.00,
  "longitude": -0.30
}
```

---

---

## 📡 Pure-Python Zigbee Subsystem & Zero-Disk-Wear Bioclimatic Tracking

The installation features an embedded, fully autonomous **Zigbee 3.0 Coordinator Subsystem** running directly on the Cerbo GX:

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                   📡 PURE-PYTHON ZIGBEE 3.0 SUB-ENGINE                   │
  │                                                                          │
  │  • Coordinator: TI CC2652P (ZG-808Z) on Cerbo GX USB port (/dev/ttyUSB0)│
  │  • Driver: 100% Pure Python 3 (zigpy + zigpy-znp / ControllerApplication)│
  │  • Udev unlock: /etc/udev/rules.d/zz-zigbee-ignore.rules                 │
  │  • Active DB: 100% in RAM (tmpfs at /run/caseta-zigbee/zigbee.db)        │
  │  • Live Telemetry: Published to FlashMQ at topic 'caseta/clima'          │
  │  • Daily History: 1 single JSON line/day at 23:59h (~200 B/day)          │
  │  • Zero Disk Wear: 0 Bytes/s written to industrial eMMC during runtime   │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 🔧 How We Solved the Venus OS USB Serial Port Hijacking:
By default, Venus OS runs `serial-starter`, which probes all serial devices on `/dev/ttyUSB*` and attempts to claim them for Victron D-Bus services (`dbus-cgwacs`, `vedirect`, etc.). 

To permanently free the Zigbee coordinator without interfering with Victron services, we deployed an override udev rule at `/etc/udev/rules.d/zz-zigbee-ignore.rules`:
```udev
ACTION=="add", SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", ENV{VE_SERVICE}:=""
```
Using the final assignment operator (`:=`), Venus OS completely ignores the coordinator serial port, leaving `/dev/ttyUSB0` exclusively available for Python `zigpy`.

### 🛡️ Zero Disk Wear Architecture:
1. **Volatile RAM Storage:** The working network database and attribute cache operate strictly in memory at `/run/caseta-zigbee/zigbee.db`.
2. **Flash Topology Backup:** A flash backup is written to `/data/caseta-guardian/zigbee_backup.db` **only when a new device pairs**, preserving the flash chip.
3. **Daily Thermal Inertia Rollup:** At **23:59h**, the daemon writes a single compact JSON summary record to `/data/caseta-guardian/history/historic_clima.jsonl` recording:
   - $T_{\text{max}}$ and $T_{\text{min}}$ with exact timestamps ($t_{\text{max}}$, $t_{\text{min}}$).
   - Daily average temperature ($T_{\text{avg}}$).
   - Humidity and Lux maximums.

### 📊 Active Connected Sensors:
* 👶 **Sensor 1 (Habitació xiquets):** Tuya TS0201 Precision Thermohygrometer ($28.60\text{ ºC}$ / $40.0\%$).
* 🛋️ **Sensor 2 (Saló):** HOBEIAN ZG-204ZV 4-in-1 Multisensor ($28.60\text{ ºC}$ / $43.4\%$ / $0\text{ Lux}$ / PIR Radar Occupancy).

---

## 🗺️ Autonomous Multi-Room Climate Control Roadmap

1. **Tuya Cloud OpenAPI & Local Tuya IR:** Fully operational for living room AC management.
2. **Offline Zigbee Coordination:** Native Zigbee daemon broadcasting indoor climate telemetry on local MQTT.
3. **Smart Surplus HVAC Modulation:** Dynamic AC setpoint throttling to pre-cool living areas during solar peaks.

---

## 📜 License
Open source under the MIT License. Designed for maximum resilience, self-sufficiency, and battery longevity.

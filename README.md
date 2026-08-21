# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

[🇬🇧 English Version](README.md) | [🇦🇩/🇪🇸 Versió en Català](README.ca.md)

Lightweight native Python 3 systemd daemon and real-time CLI telemetry dashboard for autonomous energy management, LiFePO4 battery health protection (Pylontech US3000C / US5000), UPS resilience, and smart HVAC control on **Victron ESS installations** (MultiPlus-II 24/3000 or 48/3000, Cerbo GX, AC-Out Solar Inverters).

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
  │    • HVAC Emergency Cutoff: Automatic IR shutdown of air conditioning  │
  │      if SoC drops below 65% to protect the UPS reserve.                │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 2. 🔌 RESILIENCE & UPS BACKUP (Priority 1)                             │
  │    • Automated weather modeling via Open-Meteo API every 30 minutes.   │
  │    • High Blackout Risk trigger (Tmax >= 38ºC, T21h >= 31ºC, or low    │
  │      grid voltage <195V) locks the reserve floor to 95% – 100%.        │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🏝️ ZERO GRID FEED-IN (Priority 2)                                  │
  │    • Switches to Inverter Only (Mode 2) only when: SoC > 84%, export   │
  │      > 100 W for >30 s, and battery is resting (<2 A discharge).       │
  │    • MultiPlus shifts AC frequency to throttle the PV inverter.        │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 4. ☀️ MAXIMUM SOLAR HARVEST (Priority 3)                               │
  │    • 24-Hour Circadian Schedule:                                       │
  │      - 00:01h - 07:00h: Minimum SoC = 100% (Top-balancing & max UPS).   │
  │      - 07:00h - 17:00h: Minimum SoC = 70% (Headroom to absorb sun).    │
  │      - 17:00h - 20:00h: Afternoon Deficit -> Automatically raises to   │
  │        80% when consumption exceeds solar to preserve the night reserve│
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

The project includes an ultra-fast terminal telemetry tool that reads live Victron D-Bus/MQTT data and cached weather forecasts in **0 milliseconds**:

```bash
$ caseta

⚡ TAULER DE TELEMETRIA EN DIRECTE - SISTEMA VICTRON ESS ⚡
Connectant a Cerbo GX (192.168.1.106)...

┌──────────────────────────────────────────────────────────────────────────────┐
│ 🔋 BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh)                        │
│    • Estat de Càrrega (SoC):  89.0%   [Carregant]                            │
│    • Energia Disponible:      2.85 kWh actuals | 2.53 kWh útils (tall 10%)   │
│    • Marge fins a Escut SAI:  0.77 kWh lliures (abans del sòl del 65%)       │
│    • Tensió i Corrent:        50.10 V  |  +3.4 A (+170 W)                    │
│    • Cel·les (Min / Màx):     3.337 V / 3.347 V (ΔV = 10 mV) 🎯              │
│    • Temperatura BMS:         29.9 ºC                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ ☀️ ENERGIA SOLAR & CONSUM DE LA CASETA                                      │
│    • Producció Solar Huawei:  1055.2 W                                       │
│    • Consum Casa (AC Loads):   911.4 W                                       │
│    • Freqüència de CA Caseta: 50.08 Hz                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🔌 INVERSOR MULTIPLUS-II & XARXA EXTERIOR                                   │
│    • Mode MultiPlus:          ON (Connectat a Xarxa)                         │
│    • Tensió Xarxa L1:         225.1 V                                        │
│    • Estat de la Xarxa:       Equilibrada / Neutre (46 W)                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🌤️ PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API)                          │
│    • Sol Esperat (Hui / Demà): 7.6 kWh / 6.0 kWh                             │
│    • Temp. Màx / Ocàs (21h):  32.5 ºC / 27.7 ºC                             │
│    • Índex de Risc de Tall:   🟢 Risc Baix (Normal)       (20%)              │
│    • Objectiu Reserva Nocturna: 70.0% de Bateria SAI                         │
└──────────────────────────────────────────────────────────────────────────────┘
  Guardià Natiu (caseta-guardian): 🟢 ACTIU I VIGILANT (systemd)
```

---

## ⚙️ Configuration & Privacy (`config.json`)

All private credentials, IPs, and geolocation coordinates are isolated from git tracking using `.gitignore`. Copy the example template to create your local configuration:

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

## 🚀 Installation & Deployment

### 1. Requirements:
- Python 3.9+
- `uv` or `pip` package manager
- Linux OS with `systemd --user` support

### 2. One-Step Installation:
```bash
git clone git@github.com:CasimirVictoria/caseta-guardian.git
cd caseta-guardian
cp config.example.json config.json # Edit with your local network settings
./install.sh
```

The `./install.sh` script:
1. Installs and registers the `caseta-guardian.service` under user systemd.
2. Enables automatic startup on system boot (`systemctl --user enable`).
3. Creates the global CLI wrapper link `~/.local/bin/caseta`.

---

## 📱 Mobile Push Notifications (Ntfy)
- **Early Battery Warning:** Notification at **`67% SoC`** alerting that HVAC will shut down at 65%.
- **Emergency Cutoff:** Priority 5 audible alert at **`65% SoC`**.
- **Mode Transitions:** Instant notification when MultiPlus islands into Mode 2 or reconnects to grid Mode 3.

---

## 📜 License
Open source under the MIT License. Designed for maximum resilience, self-sufficiency, and battery longevity.

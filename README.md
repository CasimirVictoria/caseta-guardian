# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

Dimoni natiu en Python 3 i tauler de telemetria en consola CLI per a la gestió energètica autònoma, protecció química de la bateria LiFePO4 (Pylontech US3000C / US5000), resiliència SAI i climatització intel·ligent d'una instal·lació **Victron ESS** (MultiPlus-II 24/3000 o 48/3000, Cerbo GX, Inversor Solar en AC-Out).

---

## 🌟 La Filosofia del Projecte: El "Sant Grial" de l'Autoconsum

El projecte resol el gran dilema de l'energia solar combinant **el millor dels sistemes aïllats amb el millor dels sistemes connectats a xarxa**:

```
  ┌────────────────────────────────────────┐  ┌────────────────────────────────────────┐
  │      🏝️ EL MILLOR DE L'AÏLLADA         │  │       🔌 EL MILLOR DE LA XARXA         │
  ├────────────────────────────────────────┤  ├────────────────────────────────────────┤
  │ • Zero Abocament (Zero Regal 100%).    │  │ • Reconnexió en 2 mil·lisegons.        │
  │ • El MultiPlus controla la freqüència  │  │ • Suport de xarxa per a consums grans  │
  │   (50.2 - 52.7 Hz) i frena el solar.   │    (PowerAssist per a cafetera, forn).    │
  │ • Autarquia i independència total.     │  │ • Zero risc de quedar-se a zero.       │
  └────────────────────────────────────────┘  └────────────────────────────────────────┘
```

---

## 🔋 El Principi de la "Bateria com a Coixí i SAI (Zero Cicles Profunds)"

A diferència dels sistemes aïllats convencionals que buiden la bateria cada dia (100% $\rightarrow$ 20%), el sistema utilitza la bateria exclusivament com a **coixí dinàmic i SAI d'emergència**:

```
  100% SoC  ───────┐
                   │  🟢 ZONA DE COIXÍ DINÀMIC (20% – 30% de marge):
                   │     • Absorbeix els pics solars de migdia a cost zero.
                   │     • Amortitza l'arrencada de la cafetera o el microones.
   70% - 80% SoC ──┴───────────────────────────────────────────────────────
                   │
                   │  🛡️ ZONA INTOCABLE DE SAI D'EMERGÈNCIA (70% – 80%):
                   │     • Més de 2,2 a 2,5 kWh nets sempre guardats.
                   │     • SAI instantani (0 ms) per si cau la línia del carrer.
                   │     • Zero cicles de descàrrega profunda (>20.000 cicles / >30 anys).
    0% SoC  ───────┘
```

---

## 🏛️ Les 4 Lleis Fonamentals de Prioritat

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ 1. 🛡️ SALUT DE LA BATERIA (Prioritat 0)                               │
  │    • Top-Balancing nocturn al 100% aprofitant tarifa supervall.        │
  │    • Reconnexió immediata a xarxa si la descàrrega supera 15 A         │
  │      (>750 W) durant >5 s o si el SoC baixa del 80% en mode aïllat.    │
  │    • Escut de Seguretat Climatització: Apagada automàtica de l'aire     │
  │      condicionat per IR si el SoC baixa del 65%.                       │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 2. 🔌 RESILIÈNCIA I SAI (Prioritat 1)                                  │
  │    • Consulta meteorològica Open-Meteo cada 30 minuts.                 │
  │    • Si detecta onada de calor extrema (Tmax >= 38ºC o T21h >= 31ºC)   │
  │      o tensió baixa de xarxa (<195V), blinda el sòl al 95% – 100%.     │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🏝️ ZERO REGAL (Prioritat 2)                                         │
  │    • Commuta a Inverter Only (Mode 2) només si: SoC > 84%, injecció    │
  │      > 100 W durant més de 30 segons, i bateria en repòs (<2 A).       │
  │    • El MultiPlus puja la freqüència de CA per frenar el solar.        │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 4. ☀️ MÀXIM APROFITAMENT SOLAR (Prioritat 3)                            │
  │    • Cicle Circadiari 24h:                                             │
  │      - 00:01h a 07:00h: Sòl al 100% (Top-balancing i SAI nocturn).     │
  │      - 07:00h a 17:00h: Sòl al 70% (Espai buit per absorbir sol).      │
  │      - 17:00h a 20:00h: Dèficit de tarda -> Puja automàticament a 80%  │
  │        per preservar la reserva abans de la nit.                       │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Protecció Mecànica del Relé i Física del Corrent

- **Relé Sobredimensionat:** El relé intern del MultiPlus-II és de **`32 A` (7.3 kW)**. Com que la línia només demana **`5 A` (1.15 kW)**, treballa al **`15%` de càrrega**.
- **Física d'Arc ($I^2$):** $(5/32)^2 = 0.024 \implies \mathbf{40\text{ vegades menys estrès d'arc}}$ als contactes de plata.
- **Commutació per Pas per Zero (*Zero-Cross*):** El xip de Victron sincronitza l'obertura i tancament quan la tensió creua els $0\text{ V}$.
- **Histèresi Obligatòria:** Mínim **`5 minuts (300 s)`** entre canvis d'estat de relé.

---

## 💻 Tauler de Control Ràpid en Terminal (`caseta`)

El projecte inclou una comanda de terminal d'alta velocitat que llegeix la telemetria en directe des del Cerbo GX i la memòria cau d'Open-Meteo en **0 mil·lisegons**:

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

## ⚙️ Configuració i Privacitat (`config.json`)

El projecte està completament separat de dades privades mitjançant `.gitignore`. Pots copiar la plantilla d'exemple i personalitzar les teues dades locals:

```bash
cp config.example.json config.json
```

Contingut de `config.json`:
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

## 🚀 Instal·lació i Desplegament

### 1. Requisits:
- Python 3.9 o superior
- Gestor de paquets `uv` o `pip`
- Sistema operatiu Linux (amb suport `systemd --user`)

### 2. Instal·lació en 1 sol pas:
```bash
git clone <url-del-repositori>
cd caseta-guardian
./install.sh
```

El script `./install.sh`:
1. Copia i activa el servei d'usuari `caseta-guardian.service`.
2. Habilita l'inici automàtic en arrencar el sistema (`systemctl --user enable`).
3. Crea l'enllaç executable per a la comanda `caseta` a `~/.local/bin/caseta`.

---

## 📱 Notificacions Push al Mòbil (Ntfy)
- **Alerta Precoç de Bateria:** Notificació al **`67 % SoC`** avisant que s'apagarà l'aire al 65%.
- **Apagada d'Emergència:** Alerta Prioritat 5 al **`65 % SoC`**.
- **Canvi de Mode:** Notificació quan el MultiPlus entra en mode aïllat o reconnecta xarxa.

---

## 📜 Llicència
Projecte lliure sota llicència MIT. Dissenyat per a màxima resiliència, autarquia i sostenibilitat domèstica.

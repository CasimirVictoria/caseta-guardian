# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

[🇬🇧 English Version](README.md) | <img src="assets/flag_catalonia.svg" alt="Catalunya" width="18" height="12" style="vertical-align: middle;" /> [Versió en Català](README.ca.md)

Dimoni natiu en Python 3 i tauler de telemetria en consola CLI per a la gestió energètica autònoma, protecció química de la bateria LiFePO4 (Pylontech US3000C / US5000), resiliència SAI i climatització intel·ligent d'una instal·lació **Victron ESS** (MultiPlus-II 24/3000 o 48/3000, Cerbo GX, Inversor Solar en AC-Out).

---

## 🏗️ Arquitectura i Estat en Producció

El projecte està dissenyat amb màxima modularitat i actualment s'executa **en producció directament dins del mateix Cerbo GX (Venus OS)** o de manera opcional en un servidor/portàtil Linux connectat per xarxa local:

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    🎛️ CERBO GX (VENUS OS EN PRODUCCIÓ)                   │
  │                                                                          │
  │  • caseta_guardian.py (Servei daemontools natiu a /data/caseta-guardian) │
  │  • Connexió MQTT interna a 127.0.0.1 (FlashMQ / <0.1 ms de latència)    │
  │  • Model meteorològic solar Open-Meteo API                               │
  │  • Avisos push mòbil Ntfy i control Tuya Cloud per a l'aire condicionat │
  │  • Persistència garantida a /data/ (sobreviu a actualitzacions firmware) │
  │  • Consum irrisori: ~19 MB RAM (<2% RAM) | 0.0% CPU | Temp. CPU freda   │
  └──────────────────────────────────────────────────────────────────────────┘
                                      ▲
                                      │ MQTT / LAN / SSH
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                 💻 PORTÀTIL / CLIENT CLI (caseta)                        │
  │  • Tauler de telemetria en directe i diagnosi instantània (<0.4s)        │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 🌟 Avantatges del Desplegament Natiu al Cerbo GX:

1. 🟢 **Autonomia Total 24/7/365:**
   - La instal·lació no depèn de cap ordinador personal encès. El Cerbo GX s'alimenta directe de la bateria i manté la vigilància permanent ininterrompuda.
   - **Latència Zero (127.0.0.1):** Comunicació directa amb el broker FlashMQ i D-Bus interns.

2. 🛡️ **Persistència i Robustesa a Venus OS:**
   - El servei resideix a la partició persistent `/data/caseta-guardian/` amb enllaç a `/service/caseta-guardian` supervisat per `daemontools` (rearrancada automàtica en cas de fallada).
   - Inserit a `/data/rc.local` per a sobreviure a qualsevol reinici o actualització de firmware oficial de Victron.

3. 🪶 **Consum Mínim i Zero Sobrecàrrega:**
   - Ocupa només **~19 MB de RAM** (menys del 2% de la memòria del Cerbo) i **0% de CPU**, deixant més de 744 MB de RAM lliures.

---

## ⚡ Optimització Extrema de Maquinari i Sistema (Cerbo GX)

El codi i el sistema s'han sotmès a un procés d'enginyeria de baix nivell per a la preservació física de la memòria i màxima eficiència de CPU:

| Capa d'Optimització | Implementació Realitzada | Impacte Mesurat en Maquinari |
| :--- | :--- | :--- |
| 📡 **Subscripcions MQTT Dirigides** | Subscripció quirúrgica només als tòpics necessaris en lloc de la màscara global `#`. | **Elimina el ~70% del trànsit del broker**, reduint interrupcions de CPU. |
| 💾 **Escriptura Única a Disc per Dia** | Integrals d'energia acumulades en RAM; persistència a memòria Flash eMMC **estrictament 1 cop al dia a les 00:00:00h** o en aturar el servei. | **Reducció del 99.999% de les escriptures a disc** ($86.400 \rightarrow 1$ escriptura/dia). |
| ⏳ **Longevitat de la Flash eMMC** | Certificada a **0.066 KB/s (5.6 MB/dia)** d'escriptura sobre el xip industrial Micron de 8GB. | **Vida útil estesa a >140 - 1.400+ anys** (>75% de salut verge restant). |
| 🧮 **Algorismes i Càlculs en Memòria Cau** | Algorisme de Pasqua (Gauss), taula de festius i objecte de zona horària calculats només a la mitjanit. | Zero càlculs flotants inútils repetits a cada segon. |
| 🧹 **Poda de Serveis i Recuperació de RAM** | Desactivació neta de dimonis no utilitzats (`vesmart-server` Bluetooth, escàner `dbus-shelly`, `vrmlogger`). | **Alliberats >100 MB de memòria RAM** (**744 MB de RAM lliure** / 73% disponible). |
| 💤 **Càrrega de CPU i Fredor Tèrmica** | Mitjana de càrrega (*Load Average*) reduïda a **0.20 - 0.50** amb **90% - 95% de CPU en repòs (*Idle*)**. | Processador fresquíssim a **~46.5 ºC**. |

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

## 🏛️ Les 4 Lleis Fonamentals de Prioritat & Gestió de Càrregues

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ 1. 🛡️ SALUT DE LA BATERIA & ESCUT DEFENSIU DE 2 NIVELLS (Prioritat 0)  │
  │    • Top-Balancing nocturn al 100% aprofitant tarifa supervall.        │
  │    • Reconnexió immediata a xarxa si la descàrrega supera 15 A         │
  │      (>750 W) durant >5 s o si el SoC baixa del 80% en mode aïllat.    │
  │    • ⚡ Protecció 1C (>=70A / ~3.5 kW per >15s): Tall d'urgència AC+Termo.│
  │    • ⚡ Protecció 0.5C (>=34A / ~1.7 kW per >3 min): Tall per estrès tèrmic.│
  │    • ❄️ ESGGLO 1 (SoC < 50%): Apagada preventiva de l'AC per Tuya IR. │
  │    • 🚨 ESGGLO 2 (SoC < 45%): Desconnexió total d'AC + Termo elèctric  │
  │      (endoll Tuya LAN/Cloud). Garanteix 1,12 kWh fins al tall del 10%  │
  │      del BMS (14 hores de SAI ininterromput per a nevera i router).    │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 2. ♨️ GESTIÓ DINÀMICA DE CÀRREGUES & PEAK SHAVING (Prioritat 1)        │
  │    • Coordinació intel·ligent Termo (100L / 1200W) & Aire Condicionat:  │
  │    • Quan el termo està calfant (>=500W), l'AC es modula a 27.0 ºC     │
  │      alliberant ~700W elèctrics.                                       │
  │    • Respecte escrupulós del límit dels 5A contractats (1.15 kW) i     │
  │      protecció contra caigudes de tensió a la línia rural (<210V).     │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🔌 RESILIÈNCIA I SAI METEOROLÒGIC (Prioritat 2)                     │
  │    • Consulta meteorològica Open-Meteo cada 60 minuts.                 │
  │    • Si detecta onada de calor extrema (Tmax >= 38ºC o T21h >= 31ºC)   │
  │      o tensió baixa de xarxa (<190V), blinda el sòl al 95% – 100%.     │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🏝️ ZERO REGAL (Prioritat 2)                                         │
  │    • Commuta a Inverter Only (Mode 2) només si: SoC > 88%, injecció    │
  │      > 50 W durant més de 30 segons, i bateria en repòs (<2 A).        │
  │    • El MultiPlus està configurat per pujar automàticament la          │
  │      freqüència de CA (50.2 - 51.5 Hz) i frenar l'inversor solar.      │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 4. ☀️ MÀXIM APROFITAMENT SOLAR (Prioritat 3)                            │
  │    • Cicle Circadiari de 4 Fites per Rellotge:                         │
  │      - 00:00h a 06:59h: Sòl al 100% (Supervall 7 cts & Top-Balancing). │
  │      - 07:00h a 09:29h: Sòl al 85% (Transició matí per al desdejuni).   │
  │      - 09:30h a 16:29h: Sòl al 75% (880 Wh de vas buit per al sol).    │
  │      - 16:30h a 23:59h: Sòl al 85% (Reserva de seguretat per a la nit).│
  │      - Caps de Setmana i Festius (18h+): Sòl al 100% (Tarifa vall 24h). │
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

```bash
$ caseta

⚡ TAULER DE TELEMETRIA EN DIRECTE - CASETA D'ADOR ⚡
Connectant a Cerbo GX (192.168.1.106)...
🕒 Registre en directe: 26/08/2026 - 10:31:18

┌──────────────────────────────────────────────────────────────────────────────┐
│ 🔋 BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh)                        │
│    • Estat de Càrrega (SoC):  78.0%  [Carregant]  (SoH BMS: 90%)             │
│    • Energia Disponible:      2.49 kWh actuals | 2.17 kWh útils (tall 10%)   │
│    • Marge fins a Escut SAI:  0.42 kWh lliures (abans del sòl del 65%)       │
│    • Tensió i Corrent:        49.50 V  |  1.1 A (54 W)                       │
│    • Cel·les (Min / Màx):     3.294 V / 3.309 V (ΔV = 15 mV)                 │
│    • Temperatura BMS:         29.6 ºC                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ ☀️ ENERGIA SOLAR & CONSUM DE LA CASETA                                       │
│    • Producció Solar Huawei:   618.7 W                                       │
│    • Consum Casa (AC Loads):   576.7 W                                       │
│    • Freqüència de CA Caseta: 49.95 Hz                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🔌 INVERSOR MULTIPLUS-II & XARXA EXTERIOR                                    │
│    • Mode MultiPlus:          ON (Connectat a Xarxa)                         │
│    • Tensió Xarxa L1:         223.2 V                                        │
│    • Estat de la Xarxa:       Important de Xarxa (46 W)                      │
├──────────────────────────────────────────────────────────────────────────────┤
│ 📊 BALANÇ I ENERGIA D'AVUI (Acumulats)                                       │
│    • Producció Solar Generada:  0.10 kWh (Pic màxim: 613 W)                  │
│    • Consum Total de la Casa:   0.65 kWh (Cobertura Solar: 15.0%)            │
│    • Importat de Xarxa:         0.44 kWh | Exportat: 0.00 kWh (Zero Regal)   │
│    • Cost Total Facturat d'Hui:  0.23 € (Tarifa 2.0TD - Tot inclòs)          │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🌤️ PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API)                            │
│    • Sol Esperat (Hui / Demà): 5.6 kWh / 7.0 kWh                             │
│    • Temp. Màx / Ocàs (21h):   31.7 ºC / 28.6 ºC                             │
│    • Índex de Risc de Tall:    Risc Baix (Normal) (25%)                      │
│    • Objectiu Reserva Nocturna:  75.0% de Bateria SAI                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🌡️ CLIMA & METEOROLOGIA (Zigbee en RAM + Inforatge Ador)                     │
│    • Habitació xiquets:     28.80 ºC | 60.0 %  [🔋 Pila: 100%]               │
│    • Saló (Multisensor):    28.20 ºC | 58.0 % | 552 Lux | 🚶 Presència  [🔋 20%] │
│    • Climatització AC:      ❄️ Mode Fred a 26 ºC [Encès]                     │
│    • Exterior Ador (Oficial): 25.2 ºC | 79 % | 2 km/h ESE | 1016 hPa         │
└──────────────────────────────────────────────────────────────────────────────┘
  Guardià Natiu (caseta-guardian): 🟢 ACTIU I VIGILANT A CERBO GX (Venus OS)
```

> **Nota sobre el Càlcul de Cost Econòmic:** El càlcul del cost en temps real està personalitzat matemàticament per a la tarifa 2.0TD específica d'aquesta instal·lació (*Imagina Energía* - potència contractada de 1,150 kW a P1 i P3, discriminació horària Punta/Pla/Vall, lloguer del comptador oficial i impostos regulats IEE 3,8% + IVA 10%). Els preus es poden adaptar lliurement al codi o a `config.json`.

---

## ⚙️ Configuració i Privacitat (`config.json`)
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

## 📡 Subsistema Zigbee Natiu en Pure-Python i Seguiment Bioclimàtic en RAM

La instal·lació compta amb un **Coordinador Zigbee 3.0 integrat directament al Cerbo GX** i supervisat per `daemontools`:

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │              📡 SUBSISTEMA ZIGBEE 3.0 NATIU EN PURE-PYTHON 3             │
  │                                                                          │
  │  • Coordinador: TI CC2652P (ZG-808Z) al port USB del Cerbo (/dev/ttyUSB0)│
  │  • Controlador: 100% Pure Python 3 (zigpy + zigpy-znp / ControllerApp)   │
  │  • Desbloqueig Udev: /etc/udev/rules.d/zz-zigbee-ignore.rules            │
  │  • BD Activa: 100% en RAM (tmpfs a /run/caseta-zigbee/zigbee.db)         │
  │  • Telemetria en Directe: Publicada a FlashMQ al tòpic 'caseta/clima'    │
  │  • Històric Diari: 1 sola línia JSON/dia a les 23:59h (~200 B/dia)       │
  │  • Zero Desgast de Disc: 0 Bytes/s d'escriptura a la Flash eMMC          │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 🛡️ Arquitectura Pure-Python 3 vs. Pila Convencional Node.js / Zigbee2MQTT:

Desplegar una pila clàssica de Zigbee (entorn d'execució Node.js + npm + servei Zigbee2MQTT) en un sistema encastat ARM com el Cerbo GX degrada severament els recursos del maquinari. En dissenyar un **dimoni natiu 100% en Pure-Python 3 (`zigpy-znp`)**, hem assolit una eficiència i preservació física sense precedents:

| Mètrica | Pila Clàssica Node.js (Zigbee2MQTT) | Dimoni Natiu Pure-Python 3 (`caseta-zigbee`) | Benefici en Maquinari al Cerbo GX |
| :--- | :---: | :---: | :--- |
| 🧠 **Petjada de Memòria RAM (VmRSS)** | ~180 – 250 MB RAM | **39.8 MB RAM** | **~80% d'estalvi de RAM** (>715 MB lliures, 70% disponible). |
| ⚡ **Càrrega de CPU** | 3% – 8% sondeig continu | **0.0% CPU** | Asíncron pur per esdeveniments (`asyncio` epoll); zero cicles inútils. |
| 💾 **Desgast de Disc (Flash eMMC)** | Escriptura contínua de registres | **0 B/s (100% en RAM)** | BD activa a `tmpfs` RAM; zero desgast del xip eMMC. |
| 🌡️ **Temperatura de CPU** | Pujada de +3 ºC a +6 ºC | **48.5 ºC (Invariable)** | El processador es manté completament fresc. |
| 📦 **Dependències del Sistema** | Node.js, npm, binaris C++ externs | Pure Python 3 (`zigpy`) | Zero sobrecàrrega externa; servei únic i net supervisat per `daemontools`. |

### 🔧 Com hem solucionat el segrest del port sèrie a Venus OS (`serial-starter`):
Per defecte, Venus OS executa el servei `serial-starter`, que escaneja contínuament qualsevol dispositiu connectat a `/dev/ttyUSB*` per intentar assignar-lo a serveis D-Bus de Victron (`dbus-cgwacs`, `vedirect`, etc.).

Per alliberar el coordinador Zigbee de forma blindada i permanent sense afectar la resta de serveis de Victron, vam desplegar una regla udev prioritària a `/etc/udev/rules.d/zz-zigbee-ignore.rules`:
```udev
ACTION=="add", SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", ENV{VE_SERVICE}:=""
```
Gràcies a l'operador d'assignació final (`:=`), Venus OS ignora el port USB del xip Zigbee, deixant `/dev/ttyUSB0` exclusivament disponible per al controlador natiu en Python (`zigpy`).

### 🛡️ Arquitectura de Zero Desgast de Disc (Zero Disk Wear):
1. **Emmagatzematge Volàtil en RAM:** Tota la base de dades activa i la memòria cau d'atributs funcionen exclusivament en memòria volàtil a `/run/caseta-zigbee/zigbee.db`.
2. **Còpia de Seguretat de la Topologia:** Es desa una còpia a Flash (`/data/caseta-guardian/zigbee_backup.db`) **únicament quan s'emparella un dispositiu nou**, evitant cicles d'escriptura innecessaris.
3. **Resum Diari d'Inèrcia Tèrmica:** Cada dia a les **`23:59h`**, el dimoni genera **un únic registre JSON compacte** a `/data/caseta-guardian/history/historic_clima.jsonl` registrant:
   - $T_{\text{max}}$ i $T_{\text{min}}$ amb l'hora exacta en què es produeixen ($t_{\text{max}}$, $t_{\text{min}}$).
   - Mitjana diària de temperatura ($T_{\text{avg}}$).
   - Valors màxims d'humitat i il·luminació (Lux).

### 📊 Sensors Físics Connectats i Actius:
* 👶 **Sensor 1 (Habitació xiquets):** Termohigròmetre de precisió Tuya TS0201 ($28.60\text{ ºC}$ / $40.0\%$ humitat / Pila $100\%$).
* 🛋️ **Sensor 2 (Saló):** Multisensor 4-en-1 HOBEIAN ZG-204ZV ($28.60\text{ ºC}$ / $43.4\%$ humitat / $0\text{ Lux}$ / Radar PIR de presència).

---

## 🗺️ Full de Ruta de Climatització Autònoma

1. **Integració Tuya Cloud OpenAPI / Local IR:** Totalment operativa per al control de l'aire condicionat del saló.
2. **Coordinació Zigbee Offline:** Dimoni Zigbee natiu publicant telemetria ambiental a MQTT local.
3. **Modulació de Consigna per Excedent:** Pre-refredament intel·ligent d'estances durant les hores punta de radiació solar.

---

## 📜 Llicència
Projecte lliure sota llicència MIT. Dissenyat per a màxima resiliència, autarquia i sostenibilitat domèstica.

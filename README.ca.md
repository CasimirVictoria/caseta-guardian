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

## 🏛️ Les 4 Lleis Fonamentals de Prioritat

```
  ┌────────────────────────────────────────────────────────────────────────┐
  │ 1. 🛡️ SALUT DE LA BATERIA (Prioritat 0)                               │
  │    • Top-Balancing nocturn al 100% aprofitant tarifa supervall.        │
  │    • Reconnexió immediata a xarxa si la descàrrega supera 15 A         │
  │      (>750 W) durant >5 s o si el SoC baixa del 80% en mode aïllat.    │
  │    • Escut de Seguretat Climatització: Apagada automàtica de l'aire     │
  │      condicionat per Tuya Cloud IR si el SoC baixa del 65%.            │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 2. 🔌 RESILIÈNCIA I SAI (Prioritat 1)                                  │
  │    • Consulta meteorològica Open-Meteo cada 60 minuts.                 │
  │    • Si detecta onada de calor extrema (Tmax >= 38ºC o T21h >= 31ºC)   │
  │      o tensió baixa de xarxa (<190V), blinda el sòl al 95% – 100%.     │
  ├────────────────────────────────────────────────────────────────────────┤
  │ 3. 🏝️ ZERO REGAL (Prioritat 2)                                         │
  │    • Commuta a Inverter Only (Mode 2) només si: SoC > 88%, injecció    │
  │      > 50 W durant més de 30 segons, i bateria en repòs (<2 A).        │
  │    • El MultiPlus puja la freqüència de CA (50.2-51.5 Hz) per frenar.  │
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

┌──────────────────────────────────────────────────────────────────────────────┐
│ 🔋 BATERIA PYLONTECH US3000C (48V LiFePO4 / 3.55 kWh)                        │
│    • Estat de Càrrega (SoC):  80.0%  [Descarregant]                          │
│    • Energia Disponible:      2.56 kWh actuals | 2.24 kWh útils (tall 10%)   │
│    • Marge fins a Escut SAI:  0.48 kWh lliures (abans del sòl del 65%)       │
│    • Tensió i Corrent:        49.40 V  |  -6.7 A (-330 W)                    │
│    • Cel·les (Min / Màx):     3.259 V / 3.304 V (ΔV = 45 mV)                 │
│    • Temperatura BMS:         32.6 ºC                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│ ☀️ ENERGIA SOLAR & CONSUM DE LA CASETA                                       │
│    • Producció Solar Huawei:     7.7 W                                       │
│    • Consum Casa (AC Loads):  1249.0 W                                       │
│    • Freqüència de CA Caseta: 50.08 Hz                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🔌 INVERSOR MULTIPLUS-II & XARXA EXTERIOR                                    │
│    • Mode MultiPlus:          ON (Connectat a Xarxa)                         │
│    • Tensió Xarxa L1:         210.4 V                                        │
│    • Estat de la Xarxa:       Important del carrer (924 W)                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ 📊 BALANÇ I ENERGIA D'AVUI (Acumulats)                                       │
│    • Producció Solar Generada:  6.90 kWh (Pic màxim: 1079 W)                 │
│    • Consum Total de la Casa:  13.77 kWh (Cobertura Solar: 50.1%)            │
│    • Importat de Xarxa:         6.78 kWh | Exportat: 0.00 kWh (Zero Regal)   │
│    • Cost Total Facturat d'Hui:  0.73 € (Tarifa 2.0TD - Tot inclòs)          │
├──────────────────────────────────────────────────────────────────────────────┤
│ 🌤️ PREVISIÓ SOLAR & RISC DE TALL (Open-Meteo API)                            │
│    • Sol Esperat (Hui / Demà): 7.5 kWh / 5.7 kWh                             │
│    • Temp. Màx / Ocàs (21h):   32.3 ºC / 27.5 ºC                             │
│    • Índex de Risc de Tall:    🟢 Risc Baix (Normal) (20%)                   │
│    • Objectiu Reserva Nocturna:  85.0% de Bateria SAI                        │
└──────────────────────────────────────────────────────────────────────────────┘
  Guardià Natiu (caseta-guardian): 🟢 ACTIU I VIGILANT (systemd)
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

## 🗺️ Full de Ruta i Desenvolupaments Futurs: Autoregulació Climàtica Multihabitació

Implementar l'autoregulació intel·ligent de la temperatura a les diferents estances de la casa (saló, habitacions, etc.) en funció dels excedents solars és arquitectònicament molt senzill i està previst en el full de ruta del projecte:

1. **Integració Tuya Cloud OpenAPI / Local IR:** Ja implementada i validada amb èxit en producció per a l'aire condicionat del saló ([`temperatura_salo`](file:///home/casimir/temperatura_salo.sh) i protecció per bateria $<65\%$).
2. **Control 100% Autònom i Resilient per Zigbee:**
   - El maquinari físic ja està disponible i llest per al seu desplegament: Coordinador USB Zigbee (`Sonoff Zigbee 3.0 Plus`), emissors d'infrarojos Zigbee i sensors de precisió de temperatura i humitat Zigbee per a cada habitació.
   - La integració de `zigbee2mqtt` en un equip amfitrió local permetrà publicar les lectures de temperatura de cada estança i enviar comandes IR directament al broker MQTT del Cerbo GX, amb $100\%$ d'autonomia local i zero dependència d'Internet o del núvol.
3. **Modulació de Climatització per Excedent Solar:**
   - Ajust dinàmic de consigna: pre-refredament automàtic d'habitacions en moments de màxim pic solar i suavització de consums quan convinga blindar la reserva de la bateria.

*Aquesta ampliació s'implementarà en futures versions tan bon punt disposem de temps per al muntatge i configuració física dels sensors.*

---

## 📜 Llicència
Projecte lliure sota llicència MIT. Dissenyat per a màxima resiliència, autarquia i sostenibilitat domèstica.

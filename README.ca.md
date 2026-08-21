# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

[🇬🇧 English Version](README.md) | [🇦🇩/🇪🇸 Versió en Català](README.ca.md)

Dimoni natiu en Python 3 i tauler de telemetria en consola CLI per a la gestió energètica autònoma, protecció química de la bateria LiFePO4 (Pylontech US3000C / US5000), resiliència SAI i climatització intel·ligent d'una instal·lació **Victron ESS** (MultiPlus-II 24/3000 o 48/3000, Cerbo GX, Inversor Solar en AC-Out).

---

## 🏗️ Arquitectura: Migració de Node-RED al Cerbo GX cap a un Portàtil Sempre Encès

Aquest projecte neix d'una evolució arquitectònica clau: **migrar la lògica pesada de Node-RED que corria al maquinari integrat del Cerbo GX cap a un script natiu i lleuger en Python 3 que s'executa en un portàtil / servidor Linux sempre encès a la Caseta**.

```
  ┌────────────────────────────────────────┐       MQTT (LAN / <1 ms)       ┌────────────────────────────────────────┐
  │     💻 PORTÀTIL LINUX (SEMPRE ENCÈS)   │ ─────────────────────────────> │            🎛️ CERBO GX (VENUS OS)      │
  │ • caseta_guardian.py (dimoni systemd)  │ <───────────────────────────── │ • Broker FlashMQ (C++)                 │
  │ • Model meteorològic Open-Meteo        │                                │ • Comunicació CANbus BMS (Bateria)     │
  │ • Avisos Ntfy i IR Tuya per a l'aire   │                                │ • Comptador Solar Modbus (Huawei)      │
  │ • 15 MB RAM | <0.1% CPU                │                                │ • Firmware Pur de Fàbrica (46.8 ºC)    │
  └────────────────────────────────────────┘                                └────────────────────────────────────────┘
```

### 🌟 Per què aquesta Arquitectura Distribuïda és Molt Superior:

1. 🪶 **Cerbo GX Pur i Intocable de Fàbrica:**
   - S'allibera el Cerbo de l'entorn pesat de Node.js, paletes npm fràgils i càrrega de servidors web.
   - Es dedica exclusivament a la seua feina de maquinari: **mesura d'alta precisió, gestió de busos industrials (CANbus / RS485) i seguretat de l'inversor**.
   - **Immunitat total a actualitzacions:** Pots actualitzar Venus OS a qualsevol nova versió oficial sense por a trencar cap flux ni perdre configuracions.

2. ❄️ **Alliberament Brutal de Memòria i Temperatura:**
   - **Memòria RAM Lliure al Cerbo:** El consum cau de ~450 MB amb Node-RED a només **`270 MB`** (deixant **més de 720 MB de RAM lliures**!).
   - **Temperatura de la CPU:** La CPU del Cerbo GX ha baixat de 55 ºC – 58 ºC a només **`46.8 ºC`** *(entre 8 i 11 ºC més fresc a ple agost a la Safor!)*.
   - **Consum al Portàtil:** El dimoni de Python al portàtil consumeix només **`15 MB` de RAM** i **`< 0.1%` de CPU**.

3. 🛠️ **Simplicitat Unix i Màxima Sobirania:**
   - Codi en Python 3 estàndard, net, transparent i directament editable.
   - Registres i logs immediats amb `journalctl --user -u caseta-guardian -f`.

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

```bash
caseta
```

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

## 🚀 Instal·lació i Desplegament
```bash
git clone git@github.com:CasimirVictoria/caseta-guardian.git
cd caseta-guardian
cp config.example.json config.json
./install.sh
```

---

## 📜 Llicència
Projecte lliure sota llicència MIT. Dissenyat per a màxima resiliència, autarquia i sostenibilitat domèstica.

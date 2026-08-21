# 🛡️ Caseta Guardian - Victron ESS & Battery Health Manager

Dimoni natiu en Python 3 i tauler de telemetria CLI per a la gestió autònoma, protecció química de la bateria Pylontech US3000C, resiliència SAI i climatització d'una instal·lació Victron ESS (MultiPlus-II 24/3000 o 48/3000, Cerbo GX, Inversor Solar en AC-Out).

## 🏛️ Les 4 Lleis de Prioritat:
1. **🛡️ Salut de la Bateria (Prioritat 0):**
   - Top-balancing automàtic nocturn al 100%.
   - Reconnexió immediata a xarxa si la descàrrega supera 15 A (>750 W) durant més de 5 segons en mode aïllat.
   - Escut de seguretat de climatització: Apagada automàtica de l'aire condicionat si el SoC baixa del **65%**.
2. **🔌 Resiliència i SAI (Prioritat 1):**
   - Avaluació diària de risc d'onada de calor i tall mitjançant l'API d'Open-Meteo per blindar la bateria al 95-100% de nit si hi ha perill.
3. **🏝️ Zero Regal (Prioritat 2):**
   - Aïllament a *Inverter Only* (Mode 2) si SoC > 84%, injecció > 100 W sostinguda per 30 segons i bateria descansada (<2 A).
4. **☀️ Màxim Aprofitament Solar (Prioritat 3):**
   - Reducció matinal a les 07:00h del llindar de reserva al 70% per absorbir l'excedent solar de migdia.

## ⚙️ Configuració (`config.json`):
Pots copiar `config.example.json` a `config.json` per personalitzar les teues adreces IP locals, canals de notificació i coordenades:
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

## 📱 Notificacions Push (Ntfy):
- Canal Ntfy configurable via `NTFY_TOPIC`.
- Alerta precoç al mòbil quan la bateria arriba al **67% SoC**.
- Notificació instantània de canvis de mode del MultiPlus.

## 💻 Tauler de Control Ràpid (CLI):
```bash
caseta
```

## 🚀 Instal·lació:
```bash
git clone <url-del-repo>
cd caseta-guardian
cp config.example.json config.json # Edita amb les teues dades
./install.sh
```

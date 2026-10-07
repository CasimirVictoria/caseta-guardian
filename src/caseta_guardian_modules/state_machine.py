"""
Màquina d'estats i lògica de decisions del Guardià.
"""

import logging
import time

log = logging.getLogger("caseta-guardian")


class StateMachine:
    """Implementa la màquina d'estats i les lleis de prioritat del Guardià."""

    def __init__(self, tuya_manager, notification_manager):
        self.tuya = tuya_manager
        self.notifications = notification_manager

    def evaluate_state_machine(self, guardian, now_madrid):
        """Avalua l'estat del sistema i aplica les lleis de prioritat."""
        now = time.time()

        # ☀️ Avaluació del Desviador d'Excedents Solar per al Termo
        self.evaluate_termo_surplus(guardian, now_madrid)

        # 🚨 ESGLLO 1 (SoC < 65%): Tall incondicional del Termo Elèctric
        if 0 < guardian.soc < 65.0 and guardian.termo_status.get("is_on", False):
            self.tuya.send_termo_command(power=False, reason="♨️ Escut SoC: Bateria <65% -> Apagat incondicional del Termo")
            guardian.termo_low_power_start_time = None

        # 🚨 ESGLLO 2 (SoC < 60%): Apagat preventiu de l'Aire Condicionat
        if 0 < guardian.soc < 60.0 and guardian.ac_current_power != 0:
            self.tuya.send_ac_command(power=0, reason="❄️ Escut SoC: Bateria <60% -> Apagat de l'AC")

        # 🚨 ESGLLO 3 (SoC < 50%): Tall Crític de Cuina per blindar >12h de reserva SAI
        if 0 < guardian.soc < 50.0:
            if guardian.doble_status.get("ch1_on", False):
                self.tuya.send_doble_command(1, False, reason="🚨 Blindatge SAI: Bateria <50% -> Desconnexió Microones/Torradora")
            if guardian.doble_status.get("ch2_on", False):
                self.tuya.send_doble_command(2, False, reason="🚨 Blindatge SAI: Bateria <50% -> Desconnexió Cafetera")
        elif guardian.soc >= 65.0:
            guardian.termo_cut_off_today = False

        # ⚡ PROTECCIÓ C-RATE A: Pic de Sobrecàrrega 1C (>=70A / ~3.5kW) en Cascada
        if guardian.bat_i <= -70.0:
            if guardian.c1_discharge_start_time is None:
                guardian.c1_discharge_start_time = now
            dt_c1 = now - guardian.c1_discharge_start_time

            # Fase 1 (als 5s): Apaguem NOMÉS el Termo primer (-1.280W)
            if dt_c1 >= 5.0 and guardian.termo_status.get("is_on", False):
                self.tuya.send_termo_command(power=False, reason="⚡ Tall 1C Cascada (Fase 1 - 5s): Desconnexió Termo (-1.280W)")
                guardian.termo_low_power_start_time = None
                log.info("⚡ [CASCADA 1C] Termo apagat per alleujar sobrecàrrega i salvar el café/AC.")

            # Fase 2 (als 15s): Si encara continua >70A, apaguem l'AC (-850W)
            if dt_c1 >= 15.0 and guardian.ac_current_power != 0:
                self.tuya.send_ac_command(power=0, reason="⚡ Tall 1C Cascada (Fase 2 - 15s): Desconnexió AC (-850W)")

            # Fase 3 (als 30s): Últim recurs si continua la sobrecàrrega extrema
            if dt_c1 >= 30.0:
                if guardian.doble_status.get("ch1_on", False):
                    self.tuya.send_doble_command(1, False, reason="🚨 Tall 1C Cascada (Fase 3 - 30s): Desconnexió Microones/Torradora")
                if guardian.doble_status.get("ch2_on", False):
                    self.tuya.send_doble_command(2, False, reason="🚨 Tall 1C Cascada (Fase 3 - 30s): Desconnexió Cafetera")
                self.notifications.send_notification(
                    "🚨 Sobrecorrent Crític Bateria 1C",
                    f"Descàrrega a {abs(guardian.bat_i):.1f}A (>=70A) durant >30s! S'ha completat la cascada de desconnexió per protegir les cel·les LiFePO4.",
                    "high",
                    "warning"
                )
                guardian.c1_discharge_start_time = None
        else:
            guardian.c1_discharge_start_time = None

        # ⚡ PROTECCIÓ C-RATE B: Sobrecàrrega Sostinguda 0.5C (>=34A / ~1.7kW) en Cascada
        if guardian.bat_i <= -34.0:
            if guardian.c05_discharge_start_time is None:
                guardian.c05_discharge_start_time = now
            dt_c05 = now - guardian.c05_discharge_start_time

            # Fase 1 (als 30s): Apaguem Termo
            if dt_c05 >= 30.0 and guardian.termo_status.get("is_on", False):
                self.tuya.send_termo_command(power=False, reason="⚡ Tall 0.5C Cascada (30s): Desconnexió Termo")
                guardian.termo_low_power_start_time = None

            # Fase 2 (als 120s / 2 min): Apaguem AC
            if dt_c05 >= 120.0 and guardian.ac_current_power != 0:
                self.tuya.send_ac_command(power=0, reason="⚡ Tall 0.5C Cascada (2 min): Desconnexió AC")

            # Fase 3 (als 180s / 3 min): Apaguem Cuina si la descàrrega persisteix
            if dt_c05 >= 180.0:
                if guardian.doble_status.get("ch1_on", False):
                    self.tuya.send_doble_command(1, False, reason="🚨 Tall 0.5C Cascada (3 min): Desconnexió Microones/Torradora")
                if guardian.doble_status.get("ch2_on", False):
                    self.tuya.send_doble_command(2, False, reason="🚨 Tall 0.5C Cascada (3 min): Desconnexió Cafetera")
                self.notifications.send_notification(
                    "🚨 Sobrecàrrega Sostinguda Bateria 0.5C",
                    f"Descàrrega a {abs(guardian.bat_i):.1f}A (>=34A) durant >3 minuts! Protecció tèrmica aplicada.",
                    "high",
                    "warning"
                )
                guardian.c05_discharge_start_time = None
        else:
            guardian.c05_discharge_start_time = None

        # 🚨 Control de Baixa Tensió / Caiguda de Xarxa Rural (<190V durant >2 minuts)
        if guardian.vebus_mode != 2:
            if guardian.grid_v < 190.0:
                guardian.grid_recovery_start_time = None
                if guardian.low_voltage_start_time is None:
                    guardian.low_voltage_start_time = now
                elif now - guardian.low_voltage_start_time >= 120.0:
                    if not guardian.grid_outage_notified:
                        self.notifications.send_notification(
                            "🚨 Tensió Xarxa Crítica",
                            f"Tensió rural caiguda a {guardian.grid_v:.1f}V (<190V durant >2 minuts). El sistema opera en mode SAI/aïllat de seguretat.",
                            "high",
                            "warning"
                        )
                        guardian.grid_outage_notified = True
            else:
                guardian.low_voltage_start_time = None
                if guardian.grid_outage_notified:
                    if guardian.grid_v >= 200.0:
                        if guardian.grid_recovery_start_time is None:
                            guardian.grid_recovery_start_time = now
                        elif now - guardian.grid_recovery_start_time >= 30.0:
                            self.notifications.send_notification(
                                "✅ Xarxa Elèctrica Restablida",
                                f"La xarxa elèctrica rural s'ha restablit ({guardian.grid_v:.1f}V estable durant >30s). Sistema normalitzat!",
                                "high",
                                "electric_plug"
                            )
                            guardian.grid_outage_notified = False
                            guardian.grid_recovery_start_time = None
                    else:
                        guardian.grid_recovery_start_time = None
                else:
                    guardian.grid_recovery_start_time = None
        else:
            guardian.low_voltage_start_time = None
            guardian.grid_recovery_start_time = None

        if guardian.vebus_mode == 2:
            # ♨️ 0. RECONNEXIÓ IMMEDIATA SI EL TERMO ESTÀ ACTIU (0s d'espera per evitar cap estiró de bateria)
            termo_p_now = float(guardian.termo_status.get("power_w", 0.0)) if guardian.termo_status else 0.0
            termo_on_now = bool(guardian.termo_status.get("is_on", False)) if guardian.termo_status else False
            if termo_on_now and termo_p_now >= 100.0:
                guardian.set_multiplus_mode(3, f"♨️ Termo Actiu ({termo_p_now:.0f}W) -> Reconnexió Immediata a Xarxa (Suport 4.5A)")
                guardian.high_discharge_start_time = None
                return

            if guardian.bat_i < -15.0:
                if guardian.high_discharge_start_time is None:
                    guardian.high_discharge_start_time = now
                dt_discharge = now - guardian.high_discharge_start_time

                # 1. Descàrrega extrema (>35A / ~1.8kW, límit 0.5C Pylontech): Reconnexió ràpida als 15s
                if guardian.bat_i <= -35.0:
                    if dt_discharge >= 15.0:
                        guardian.set_multiplus_mode(3, f"Descàrrega extrema de bateria ({abs(guardian.bat_i):.1f}A >= 35.0A per >15s)")
                        guardian.high_discharge_start_time = None
                        return
                # 2. Descàrrega amb bateria baixa (SoC < 75%): Reconnexió en 30s per preservar reserva SAI
                elif guardian.soc < 75.0:
                    if dt_discharge >= 30.0:
                        guardian.set_multiplus_mode(3, f"Descàrrega amb bateria baixa ({guardian.soc:.1f}% < 75% a {abs(guardian.bat_i):.1f}A per >30s)")
                        guardian.high_discharge_start_time = None
                        return
                # 3. Descàrrega de cuina / cafetera / microones (fins a 32A amb SoC >= 75%): Permet fins a 4 minuts (240s) sense commutar relé!
                else:
                    if dt_discharge >= 240.0:
                        guardian.set_multiplus_mode(3, f"Descàrrega prolongada de cuina ({abs(guardian.bat_i):.1f}A per >4 minuts)")
                        guardian.high_discharge_start_time = None
                        return
            else:
                guardian.high_discharge_start_time = None

            if guardian.soc < 70.0:
                guardian.set_multiplus_mode(3, f"Bateria ha baixat del sòl segur ({guardian.soc:.1f}% < 70.0%)")
                return

            if guardian.pv_p < 50.0 and guardian.ac_loads > 300.0 and guardian.soc <= 85.0:
                guardian.set_multiplus_mode(3, f"Sol esgotat ({guardian.pv_p:.0f}W) i consum a casa ({guardian.ac_loads:.0f}W)")
                return

        elif guardian.vebus_mode == 3:
            termo_on = guardian.termo_status.get("is_on", False) if guardian.termo_status else False
            if termo_on:
                # Amb el termo encès, mai ens desconnectem a Inverter Only (cal suport de xarxa)
                guardian.export_start_time = None
                return

            if guardian.grid_p is not None and guardian.grid_p < -50.0 and guardian.soc >= 88.0:
                # Abans de desconnectar a Inverter Only, comprovem si podem encendre el Termo com a desviador d'excedents!
                can_heat = (getattr(guardian, "termo_est_temp", 60.0) < 58.0) and not getattr(guardian, "termo_surplus_done", False)
                if not termo_on and can_heat:
                    log.info(f"⚡ Abocament detectat ({abs(guardian.grid_p):.0f}W) amb SoC {guardian.soc:.1f}% -> Encenent Termo com a desviador d'excedents!")
                    self.start_termo_safely(
                        guardian,
                        f"⚡ Desviador Anti-Abocament ({abs(guardian.grid_p):.0f}W)",
                        "⚡ Termo Engegat (Desviador Anti-Abocament)",
                        f"Detectat abocament de {abs(guardian.grid_p):.0f}W amb bateria al {guardian.soc:.1f}%. Escalfant termo per no injectar a xarxa!",
                        "electric_plug"
                    )
                    guardian.export_start_time = None
                    return

                if guardian.export_start_time is None:
                    guardian.export_start_time = now
                    log.info(f"⚠️ Detectat abocament de {abs(guardian.grid_p):.0f}W amb SoC {guardian.soc:.1f}%. Iniciant compte enrere de 30s...")
                elif now - guardian.export_start_time >= 30.0:
                    guardian.set_multiplus_mode(2, f"Abocament sostingut de {abs(guardian.grid_p):.0f}W durant >30s amb SoC {guardian.soc:.1f}%")
                    guardian.export_start_time = None
                    return
            else:
                guardian.export_start_time = None

    def start_termo_safely(self, guardian, reason: str, notif_title: str, notif_msg: str, notif_icon: str = "sun"):
        """Encesa en 3 passos segurs:
        1. Assegurar xarxa física connectada (Mode 3)
        2. Pre-calcular i establir Grid Setpoint per no superar 800W de bateria
        3. Engegar endoll Tuya
        """
        now = time.time()
        # Pas 1: Si estem en Mode 2 (Aïllat) o sense xarxa connectada:
        grid_ready = (guardian.vebus_mode == 3) and (getattr(guardian, "grid_v", 0.0) >= 190.0) and (guardian.grid_p is not None)
        if not grid_ready:
            if guardian.vebus_mode == 2:
                guardian.set_multiplus_mode(3, f"🔌 Reconnexió a xarxa prèvia a l'encesa del termo ({reason})")
            guardian.termo_pending_turn_on = {
                "reason": reason,
                "notif_title": notif_title,
                "notif_msg": notif_msg,
                "notif_icon": notif_icon,
                "time": now
            }
            log.info(f"⏳ [TERMO PRE-START] Esperant que el relé de xarxa estiga enclavat en Mode 3 abans d'engegar el termo ({reason})...")
            return

        # Pas 2: Pre-fixar el Grid Setpoint abans d'engegar l'endoll!
        expected_loads = guardian.ac_loads + 1400.0
        expected_deficit = max(0.0, expected_loads - guardian.pv_p)
        grid_v_safe = guardian.grid_v if getattr(guardian, "grid_v", 0.0) >= 190.0 else 230.0
        max_grid_w = round(min(1050.0, max(900.0, 4.5 * grid_v_safe)))

        if expected_deficit <= 650.0:
            target = 200.0
        else:
            grid_needed = expected_deficit - 650.0
            target = round(min(max_grid_w, max(200.0, grid_needed)))

        guardian.set_grid_setpoint_direct(target, f"Pre-set per encesa termo ({reason})")
        log.info(f"⚙️ [PRE-SETPOINT] Fixat Grid Setpoint a {target:.0f} W abans d'engegar el termo (Previsió Casa {expected_loads:.0f}W, Sol {guardian.pv_p:.0f}W).")

        # Pas 3: Engegar l'endoll Tuya amb la xarxa ja a punt!
        self.tuya.send_termo_command(power=True, reason=reason)
        self.notifications.send_notification(notif_title, notif_msg, "default", notif_icon)
        guardian.termo_pending_turn_on = None

    def evaluate_termo_surplus(self, guardian, now_madrid):
        """Gestiona l'engegada automàtica del Termo Elèctric (Desviador d'Excedents Solar cap a 60ºC) i Arbitratge P3."""
        now = time.time()
        current_hour = now_madrid.hour
        current_minute = now_madrid.minute
        time_decimal = current_hour + (current_minute / 60.0)

        # ⏳ 0. Comprovació de comanda d'encesa segura pendent (esperant xarxa connectada en Mode 3)
        pending = getattr(guardian, "termo_pending_turn_on", None)
        if pending:
            grid_ready = (guardian.vebus_mode == 3) and (getattr(guardian, "grid_v", 0.0) >= 190.0) and (guardian.grid_p is not None)
            if grid_ready:
                log.info("✅ [TERMO PRE-START] Relé de xarxa enclavat! Procedint amb encesa segura.")
                self.start_termo_safely(guardian, pending["reason"], pending["notif_title"], pending["notif_msg"], pending.get("notif_icon", "sun"))
                return
            elif now - pending.get("time", now) > 60.0:
                log.warning("⚠️ [TERMO PRE-START] Temps d'espera de sincronització de xarxa esgotat (>60s). Cancel·lant.")
                guardian.termo_pending_turn_on = None
            else:
                return

        termo_p = guardian.termo_status.get("power_w", 0.0) if guardian.termo_status else 0.0
        is_on = guardian.termo_status.get("is_on", False) if guardian.termo_status else False

        # 🚨 ESCUT D'EMERGÈNCIA: Apagada de Xarxa Exterior / Xarxa Caiguda (<185V o desconnectada)
        grid_present = (getattr(guardian, "grid_v", 0.0) >= 185.0) and (getattr(guardian, "grid_status", "") != "Sense Xarxa (Apagada)")
        if not grid_present:
            if is_on:
                self.tuya.send_termo_command(
                    power=False,
                    reason="🚨 ESCUT APAGADA: Xarxa elèctrica caiguda (<185V) -> Termo tallat immediatament per preservar la bateria!"
                )
                self.notifications.send_notification(
                    "🚨 Escut Apagada: Termo Tallat",
                    "S'ha detectat tall de xarxa elèctrica. Termo apagat a l'instant per protegir la Pylontech.",
                    "high",
                    "warning"
                )
            return

        # Si el termo està encès, avaluem quan cal apagar-lo o gestionar el termòstat mecànic:
        if is_on:
            # 1. Termòstat Intern Mecànic Ariston Assolit (<50W durant >90s a 60ºC)
            if termo_p < 50.0:
                if guardian.termo_low_power_start_time is None:
                    guardian.termo_low_power_start_time = now
                elif now - guardian.termo_low_power_start_time >= 90.0:
                    guardian.termo_est_temp = 60.0
                    guardian.termo_last_60_ts = now
                    guardian.termo_heated_today = True
                    guardian.termo_surplus_done = True
                    guardian.termo_low_power_start_time = None

                    log.info("♨️ [TERMO] Termòstat intern Ariston ha tallat a 60ºC. Dipòsit ple! Apagant endoll Tuya.")
                    self.tuya.send_termo_command(
                        power=False,
                        reason="♨️ Termòstat Ariston Assolit: Consum <50W -> Aigua calenta a 60ºC Assolida (Dipòsit Ple)!"
                    )
                    self.notifications.send_notification(
                        "♨️ Aigua Calenta a 60ºC Assolida",
                        "El dipòsit de 100L ha arribat als 60ºC i ha tallat el termòstat. Aigua calenta a punt per a la dutxa!",
                        "default",
                        "tada"
                    )
                    return
            else:
                guardian.termo_low_power_start_time = None

            # 2. Sòl de Seguretat Dinàmic de Bateria per Previsió (SAI Prioritari)
            rem_sun = getattr(guardian, "remaining_kwh_today", 3.0)
            today_est = getattr(guardian, "today_kwh_est", 5.0)

            # Previsió bona: sol restant >= 3.5 kWh o dia radiant >= 4.5 kWh abans de les 15:30h -> Sòl 65%
            if (rem_sun >= 3.5 or today_est >= 4.5) and time_decimal < 15.5:
                min_soc_termo = 65.0
            # Previsió dolenta o vesprada (>16:00h) -> Sòl 80%
            elif rem_sun < 2.5 or time_decimal >= 16.0 or getattr(guardian, "rain_today", 0.0) >= 0.5:
                min_soc_termo = 80.0
            else:
                min_soc_termo = 72.0

            if guardian.soc < min_soc_termo:
                self.tuya.send_termo_command(
                    power=False,
                    reason=f"⏸️ Sòl Bateria Assolit: Bateria ha baixat al {guardian.soc:.1f}% (<{min_soc_termo:.0f}%) per preservar reserva SAI"
                )
                guardian.termo_low_power_start_time = None
                return

            # 3. Protecció d'intensitat màxima de descàrrega de bateria (>22A sostinguts per >20s)
            # Només actua com a última línia de defensa si la xarxa no ha pogut assumir la càrrega
            if getattr(guardian, "bat_i", 0.0) < -22.0:
                if guardian.high_discharge_start_time is None:
                    guardian.high_discharge_start_time = now
                elif now - guardian.high_discharge_start_time >= 20.0:
                    self.tuya.send_termo_command(
                        power=False,
                        reason=f"⚡ Escut Bateria: Descàrrega excessiva ({abs(guardian.bat_i):.1f}A > 22A per >20s). Pausa de 3 minuts."
                    )
                    guardian.termo_cooldown_until = now + 180.0
                    guardian.high_discharge_start_time = None
                    return
            else:
                guardian.high_discharge_start_time = None

            # 4. Fi de la Finestra Matinal (a les 06:30h exactes per no solapar esmorzars/cafetera)
            if 6.5 <= time_decimal < 9.5:
                self.tuya.send_termo_command(
                    power=False,
                    reason="🕒 Fi Finestra Matinada (06:30h): Desconnexió per evitar solapar consums de matí (cafetera, microones)"
                )
                guardian.termo_low_power_start_time = None
                return

            # 5. Fi de la Finestra Solar Diürna (a les 17:00h en caure el sol)
            if time_decimal >= 17.0:
                self.tuya.send_termo_command(
                    power=False,
                    reason="🕒 Fi Finestra Solar Diürna (17:00h): Dipòsit calfat per a la nit -> Apagat d'endoll per preservar bateria"
                )
                guardian.termo_low_power_start_time = None
                return

        # Si el termo està apagat:
        else:
            # Respectar temps de refredament si s'ha disparat l'escut de bateria
            if now < getattr(guardian, "termo_cooldown_until", 0.0):
                return

            temp_actual = getattr(guardian, "termo_est_temp", 60.0)

            # Si l'aigua ha baixat per davall de 54ºC (consum d'aigua calenta a la tarda), permetem un nou cicle
            if temp_actual < 54.0:
                guardian.termo_surplus_done = False

            # 🌙 CAS A: Encesa de Matinada Vall P3 (04:00h a 06:15h) - Dutxa Garantida a 60ºC
            if 4.0 <= time_decimal < 6.25 and grid_present and guardian.soc >= 70.0:
                # Si l'aigua ja està a >= 58ºC, NO cal encendre'l gens! (0 € gastats)
                if temp_actual < 58.0 and not getattr(guardian, "termo_morning_done", False):
                    self.start_termo_safely(
                        guardian,
                        f"🌙 Matinada Vall P3 ({now_madrid.strftime('%H:%M')}h): Termo a {temp_actual:.1f}ºC -> Calfament a 60ºC per a la dutxa",
                        "🌙 Termo Engegat a la Matinada (Vall P3)",
                        f"Aigua a {temp_actual:.1f}ºC. Escalfant fins a 60ºC a 0.07 €/kWh per a la dutxa del matí!",
                        "moon"
                    )
                    return

            # ☀️ CAS B: Excedents Solars Diürns (09:30h - 17:00h) - Desviador cap a 60ºC
            can_heat_surplus = (temp_actual < 58.0) and not getattr(guardian, "termo_surplus_done", False)
            detecting_export = (guardian.grid_p is not None and guardian.grid_p < -30.0 and guardian.soc >= 85.0)
            solar_surplus_ok = (guardian.soc >= 88.0 and guardian.pv_p >= 500.0) or (guardian.soc >= 92.0 and guardian.pv_p >= 250.0)

            if 9.5 <= time_decimal < 17.0 and can_heat_surplus and (solar_surplus_ok or detecting_export):
                guardian.termo_notified_knob_60 = False
                motiu = "⚡ Desviador Anti-Abocament" if detecting_export else "☀️ Excedent Solar Diürn"
                self.start_termo_safely(
                    guardian,
                    f"{motiu}: SoC {guardian.soc:.1f}%, Sol {guardian.pv_p:.0f}W, Aigua {temp_actual:.1f}ºC -> Escalfant cap a 60ºC",
                    "♨️ Termo Engegat per Excedents Solars",
                    f"{motiu}! Bateria al {guardian.soc:.1f}% i Sol a {guardian.pv_p:.0f}W. Escalfant dipòsit cap a 60ºC!",
                    "sun"
                )
                return

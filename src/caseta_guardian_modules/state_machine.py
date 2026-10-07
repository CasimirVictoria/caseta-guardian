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

                # 1. Descàrrega extrema (>35A / ~1.8kW bateria, límit 0.5C): Reconnexió ràpida als 5s
                if guardian.bat_i <= -35.0:
                    if dt_discharge >= 5.0:
                        guardian.set_multiplus_mode(3, f"Descàrrega extrema de bateria ({abs(guardian.bat_i):.1f}A >= 35.0A per >5s)")
                        guardian.high_discharge_start_time = None
                        return
                # 2. Descàrrega alta (>25A / ~1.3kW) o bateria moderada (<75%): Reconnexió als 15s
                elif guardian.bat_i <= -25.0 or guardian.soc < 75.0:
                    if dt_discharge >= 15.0:
                        motiu = f"Descàrrega alta ({abs(guardian.bat_i):.1f}A > 25.0A per >15s)" if guardian.bat_i <= -25.0 else f"Descàrrega amb bateria <75% ({abs(guardian.bat_i):.1f}A per >15s)"
                        guardian.set_multiplus_mode(3, motiu)
                        guardian.high_discharge_start_time = None
                        return
                # 3. Càrrega típica de cuina (15A a 25A) amb bateria sana (>=75%):
                else:
                    if dt_discharge >= 90.0:
                        guardian.set_multiplus_mode(3, f"Descàrrega sostinguda de cuina ({abs(guardian.bat_i):.1f}A per >90s)")
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
            if guardian.grid_p is not None and guardian.grid_p < -50.0 and guardian.soc >= 88.0:
                # Abans de desconnectar a Inverter Only, comprovem si podem encendre el Termo com a desviador d'excedents!
                termo_on = guardian.termo_status.get("is_on", False) if guardian.termo_status else False
                can_heat = (getattr(guardian, "termo_est_temp", 60.0) < 78.0) and not getattr(guardian, "termo_surplus_done", False)
                if not termo_on and can_heat:
                    log.info(f"⚡ Abocament detectat ({abs(guardian.grid_p):.0f}W) amb SoC {guardian.soc:.1f}% -> Encenent Termo com a desviador d'excedents!")
                    self.tuya.send_termo_command(power=True, reason=f"⚡ Desviador Anti-Abocament ({abs(guardian.grid_p):.0f}W)")
                    self.notifications.send_notification(
                        "⚡ Termo Engegat (Desviador Anti-Abocament)",
                        f"Detectat abocament de {abs(guardian.grid_p):.0f}W amb bateria al {guardian.soc:.1f}%. Escalfant termo per no injectar a xarxa!",
                        "default",
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

    def evaluate_termo_surplus(self, guardian, now_madrid):
        """Gestiona l'engegada automàtica del Termo Elèctric (Desviador d'Excedents Solar cap a 80ºC) i Arbitratge P3."""
        now = time.time()
        current_hour = now_madrid.hour
        current_minute = now_madrid.minute
        time_decimal = current_hour + (current_minute / 60.0)

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
            # 1. Termòstat Intern Mecànic Ariston Assolit (<50W durant >90s)
            if termo_p < 50.0:
                if guardian.termo_low_power_start_time is None:
                    guardian.termo_low_power_start_time = now
                elif now - guardian.termo_low_power_start_time >= 90.0:
                    # Cas A: Ha tallat al voltant de 60ºC (la rodeta física està a 60ºC)
                    if getattr(guardian, "termo_est_temp", 60.0) < 70.0:
                        guardian.termo_est_temp = 60.0
                        guardian.termo_last_60_ts = now
                        guardian.termo_heated_today = True

                        if not getattr(guardian, "termo_notified_knob_60", False):
                            guardian.termo_notified_knob_60 = True
                            log.info("♨️ [TERMO] Termòstat mecànic ha tallat a 60ºC. Notificant usuari per si vol pujar rodeta a 80ºC.")
                            self.notifications.send_notification(
                                "♨️ Termo a 60ºC (Pots pujar a 80º?)",
                                "El termòstat mecànic del termo ha tallat a 60ºC però hi ha sol! Si vols aprofitar l'excedent, puja la rodeta física a 80ºC.",
                                "default",
                                "bath"
                            )

                        # Si han passat més de 10 minuts en repòs (<50W) i no s'ha pujat la rodeta, apaguem l'endoll
                        if now - guardian.termo_low_power_start_time >= 600.0:
                            self.tuya.send_termo_command(
                                power=False,
                                reason="♨️ Termòstat Ariston tallat a 60ºC per >10 min -> Apagat d'endoll fins a nova ordre o excedent"
                            )
                            guardian.termo_low_power_start_time = None
                            return

                    # Cas B: Ha tallat a la zona alta (>= 70ºC) -> Assolits 80ºC!
                    else:
                        guardian.termo_est_temp = 80.0
                        guardian.termo_surplus_done = True
                        guardian.termo_low_power_start_time = None
                        self.tuya.send_termo_command(
                            power=False,
                            reason="♨️ Termòstat Ariston Assolit: Consum <50W -> Aigua calenta a 80ºC Assolida (Dipòsit Ple)!"
                        )
                        self.notifications.send_notification(
                            "♨️ Aigua Calenta a 80ºC Assolida",
                            "El dipòsit de 100L ha completat el cicle solar complet. Aigua a màxima temperatura (80ºC)!",
                            "default",
                            "tada"
                        )
                        return
            else:
                guardian.termo_low_power_start_time = None
                # Si torna a consumir (>500W) després d'haver estat avisat a 60ºC, l'usuari ha apujat la rodeta a 80ºC!
                if getattr(guardian, "termo_notified_knob_60", False) and termo_p >= 500.0:
                    log.info(f"♨️ [TERMO] Rodeta física apujada per l'usuari! Consum reactivat a {termo_p:.0f}W cap a 80ºC.")
                    guardian.termo_notified_knob_60 = False

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

            # 4. Fi de la Finestra Matinal (passades les 07:00h en P3)
            if 7.0 <= time_decimal < 9.0 and getattr(guardian, "termo_est_temp", 60.0) >= 58.0:
                self.tuya.send_termo_command(
                    power=False,
                    reason="🕒 Fi Finestra Matinada (07:00h): Aigua calenta a punt per a la dutxa"
                )
                guardian.termo_low_power_start_time = None
                return

        # Si el termo està apagat:
        else:
            # Respectar temps de refredament si s'ha disparat l'escut de bateria
            if now < getattr(guardian, "termo_cooldown_until", 0.0):
                return

            temp_actual = getattr(guardian, "termo_est_temp", 60.0)

            # 🌙 CAS A: Encesa de Matinada Vall P3 (04:00h a 06:45h) - Dutxa Garantida a 60ºC
            if 4.0 <= time_decimal < 6.75 and grid_present and guardian.soc >= 70.0:
                # Si l'aigua ja està a >= 58ºC, NO cal encendre'l gens! (0 € gastats)
                if temp_actual < 58.0 and not getattr(guardian, "termo_morning_done", False):
                    if guardian.vebus_mode == 2:
                        guardian.set_multiplus_mode(3, "🌙 Encesa Matinada P3 -> Reconnexió Immediata a Xarxa")

                    self.tuya.send_termo_command(
                        power=True,
                        reason=f"🌙 Matinada Vall P3 ({now_madrid.strftime('%H:%M')}h): Termo a {temp_actual:.1f}ºC -> Calfament a 60ºC per a la dutxa"
                    )
                    self.notifications.send_notification(
                        "🌙 Termo Engegat a la Matinada (Vall P3)",
                        f"Aigua a {temp_actual:.1f}ºC. Escalfant fins a 60ºC a 0.07 €/kWh per a la dutxa del matí!",
                        "default",
                        "moon"
                    )
                    return

            # ☀️ CAS B: Excedents Solars Diürns (09:30h - 17:00h) - Desviador cap a 80ºC
            can_heat_surplus = (temp_actual < 78.0) and not getattr(guardian, "termo_surplus_done", False)
            detecting_export = (guardian.grid_p is not None and guardian.grid_p < -30.0 and guardian.soc >= 85.0)
            solar_surplus_ok = (guardian.soc >= 88.0 and guardian.pv_p >= 500.0) or (guardian.soc >= 92.0 and guardian.pv_p >= 250.0)

            if 9.5 <= time_decimal < 17.0 and can_heat_surplus and (solar_surplus_ok or detecting_export):
                if guardian.vebus_mode == 2:
                    guardian.set_multiplus_mode(3, "☀️ Encesa Termo per Excedents -> Reconnexió Immediata a Xarxa")

                guardian.termo_notified_knob_60 = False
                motiu = "⚡ Desviador Anti-Abocament" if detecting_export else "☀️ Excedent Solar Diürn"
                self.tuya.send_termo_command(
                    power=True,
                    reason=f"{motiu}: SoC {guardian.soc:.1f}%, Sol {guardian.pv_p:.0f}W, Aigua {temp_actual:.1f}ºC -> Escalfant cap a 80ºC"
                )
                self.notifications.send_notification(
                    "♨️ Termo Engegat per Excedents Solars",
                    f"{motiu}! Bateria al {guardian.soc:.1f}% i Sol a {guardian.pv_p:.0f}W. Escalfant dipòsit cap a 80ºC!",
                    "default",
                    "sun"
                )
                return

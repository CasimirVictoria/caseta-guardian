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
        """Gestiona l'engegada automàtica del Termo Elèctric (Desviador d'Excedents Solar) i Arbitratge P3."""
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

        # Si el termo està encès, avaluem quan cal apagar-lo:
        if is_on:
            # 1. Termòstat Intern Assolit (<50W durant >2 minuts) -> Aigua calenta a 60ºC
            if termo_p < 50.0:
                if guardian.termo_low_power_start_time is None:
                    guardian.termo_low_power_start_time = now
                elif now - guardian.termo_low_power_start_time >= 120.0:
                    self.tuya.send_termo_command(
                        power=False,
                        reason="♨️ Termòstat Ariston Assolit: Consum <50W durant >2 min -> Aigua calenta a 60ºC!"
                    )
                    self.notifications.send_notification(
                        "♨️ Aigua Calenta a 60ºC Assolida",
                        f"El termo ha completat el cicle tèrmic ({termo_p:.0f}W). Dipòsit a 60ºC!",
                        "default",
                        "bath"
                    )
                    guardian.termo_heated_today = True
                    guardian.termo_est_temp = 60.0
                    guardian.termo_last_heated_date = now_madrid.strftime("%Y-%m-%d")
                    guardian.termo_last_60_ts = now
                    guardian.termo_low_power_start_time = None
                    return
            else:
                guardian.termo_low_power_start_time = None

            # 2. Pausa per Bateria Caiguda (<65%)
            if guardian.soc < 65.0:
                self.tuya.send_termo_command(
                    power=False,
                    reason=f"⏸️ Pausa de Seguretat: Bateria ha baixat al {guardian.soc:.1f}% (<65%)"
                )
                guardian.termo_low_power_start_time = None
                return

            # 3. Fi de la Finestra Matinal (passades les 06:30h)
            if 6.5 <= time_decimal < 9.0:
                self.tuya.send_termo_command(
                    power=False,
                    reason="🕒 Fi Finestra Matinada (06:30h): Apagat preventiu abans de l'esmorzar (cafetera/microones)"
                )
                guardian.termo_low_power_start_time = None
                return

            # 4. Fi de la Finestra d'Excedents Solars (passades les 16:00h)
            # ELIMINAT: El termo es pot encendre a la vesprada/nit per dutxar els xiquets
            # Els escuts de protecció de bateria (SoC < 65%, < 60%, < 50%) es mantenen actius

        # Si el termo està apagat i encara no ha completat la càrrega d'avui:
        elif not guardian.termo_heated_today and not getattr(guardian, "termo_cut_off_today", False):
            today_est = getattr(guardian, "today_kwh_est", 5.0)
            hours_60 = round((now - guardian.termo_last_60_ts) / 3600.0, 1) if getattr(guardian, "termo_last_60_ts", None) else None
            urgent_heating = (getattr(guardian, "termo_est_temp", 60.0) < 42.0) or (hours_60 is not None and hours_60 >= 48.0) or ((getattr(guardian, "termo_status", {}).get("days_since_60", 0) or 0) >= 2)

            # 🌙 CAS A: Encesa de Matinada Vall P3 (04:00h a 06:30h)
            soc_ok_matinada = (guardian.soc >= 70.0) or (urgent_heating and guardian.soc >= 65.0)
            if 4.0 <= time_decimal < 6.5 and grid_present and soc_ok_matinada:
                grid_v_safe = guardian.grid_v if getattr(guardian, "grid_v", 0.0) >= 190.0 else 230.0
                target_p3 = round(min(1050.0, max(900.0, 4.5 * grid_v_safe)))
                if guardian.vebus_mode == 2:
                    guardian.set_multiplus_mode(3, "🌙 Encesa Matinada P3 -> Reconnexió Immediata a Xarxa")
                # Pre-rampa D-Bus a 4.5A per evitar descàrrega brusca de bateria
                try:
                    import dbus
                    bus = dbus.SystemBus()
                    obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                    obj.SetValue(dbus.Double(target_p3), dbus_interface="com.victronenergy.BusItem")
                    guardian.last_grid_setpoint = target_p3
                    log.info(f"🔌 [PRE-RAMPA] Grid Setpoint a {target_p3:.0f}W (4.5A) per a encesa matinal P3...")
                except Exception as e:
                    log.debug(f"Error pre-rampa D-Bus: {e}")

                motiu_extra = " [Rescat Aigua Freda/Antillegionel·la]" if urgent_heating else ""
                self.tuya.send_termo_command(
                    power=True,
                    reason=f"🌙 Matinada Vall P3{motiu_extra} ({now_madrid.strftime('%H:%M')}h): Encesa a 0.08 €/kWh amb Xarxa Activa ({guardian.grid_v:.0f}V) i Bateria {guardian.soc:.0f}%"
                )
                self.notifications.send_notification(
                    f"🌙 Termo Engegat a la Matinada (Vall P3){motiu_extra}",
                    f"Calfant aigua a 60ºC en horari super-econòmic (0.08 €/kWh). Xarxa activa ({guardian.grid_v:.0f}V) i bateria al {guardian.soc:.0f}%!",
                    "default",
                    "moon"
                )
                return

            # ☀️ CAS B: Excedents Solars Diürns (09:00h - 16:00h)
            soc_ok_diurn = (guardian.soc >= 80.0 and guardian.pv_p >= 500.0) or (urgent_heating and guardian.soc >= 88.0 and guardian.pv_p >= 150.0)
            if 9.0 <= time_decimal < 16.0 and soc_ok_diurn:
                if today_est >= 5.0:
                    pre_target = 200.0
                elif today_est >= 3.5:
                    pre_target = 400.0
                else:
                    pre_target = 800.0

                try:
                    import dbus
                    bus = dbus.SystemBus()
                    obj = bus.get_object("com.victronenergy.settings", "/Settings/CGwacs/AcPowerSetPoint")
                    obj.SetValue(dbus.Double(pre_target), dbus_interface="com.victronenergy.BusItem")
                    guardian.last_grid_setpoint = pre_target
                    log.info(f"🔌 [PRE-RAMPA] Grid Setpoint a {pre_target:.0f}W abans d'engegar el Termo per excedents solars...")
                except Exception as e:
                    log.warning(f"Error establint pre-rampa a D-Bus: {e}")

                motiu_b = f"☀️ Excedent Solar: SoC {guardian.soc:.1f}% >= 80% i Sol {guardian.pv_p:.0f}W >= 500W -> Encesa Termo" if guardian.pv_p >= 500.0 else f"☀️ Rescat Diürn Bateria Plena: SoC {guardian.soc:.1f}% i Sol {guardian.pv_p:.0f}W -> Encesa Termo"
                self.tuya.send_termo_command(
                    power=True,
                    reason=motiu_b
                )
                self.notifications.send_notification(
                    "♨️ Termo Engegat per Excedents Solars",
                    f"Bateria al {guardian.soc:.1f}% i Sol a {guardian.pv_p:.0f}W. Escalfant aigua de franc!",
                    "default",
                    "sun"
                )

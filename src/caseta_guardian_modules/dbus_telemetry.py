"""
Lectura de telemetria des de D-Bus a Cerbo GX.
"""

import logging
import time

log = logging.getLogger("caseta-guardian")


class DBusTelemetry:
    """Llegeix telemetria directament de D-Bus del Cerbo GX."""

    def __init__(self):
        self.last_dbus_poll_time = 0.0
        self._last_cgwacs_check_time = 0.0

    def poll_telemetry(self, guardian):
        """Lectura directa de telemetria des de D-Bus a Cerbo GX."""
        now = time.time()
        if now - self.last_dbus_poll_time < 5.0:
            return
        self.last_dbus_poll_time = now
        try:
            import dbus
            bus = dbus.SystemBus()
            # 1. Bateria SoC, Tensió, Corrent i Potència
            try:
                soc_val = bus.get_object("com.victronenergy.system", "/Dc/Battery/Soc").GetValue()
                if soc_val is not None:
                    guardian.soc = float(soc_val)
            except Exception:
                pass
            try:
                v_val = bus.get_object("com.victronenergy.system", "/Dc/Battery/Voltage").GetValue()
                if v_val is not None:
                    guardian.bat_v = float(v_val)
            except Exception:
                pass
            try:
                i_val = bus.get_object("com.victronenergy.system", "/Dc/Battery/Current").GetValue()
                if i_val is not None:
                    guardian.bat_i = float(i_val)
            except Exception:
                pass
            try:
                p_val = bus.get_object("com.victronenergy.system", "/Dc/Battery/Power").GetValue()
                if p_val is not None:
                    guardian.bat_p = float(p_val)
            except Exception:
                pass
            # 2. Xarxa Tensió i Potència
            try:
                grid_v = bus.get_object("com.victronenergy.vebus.ttyS4", "/Ac/ActiveIn/L1/V").GetValue()
                if grid_v is not None:
                    guardian.grid_v = float(grid_v)
            except Exception:
                pass
            try:
                grid_p = bus.get_object("com.victronenergy.system", "/Ac/Grid/L1/Power").GetValue()
                if grid_p is not None:
                    guardian.grid_p = float(grid_p)
            except Exception:
                pass
            # 3. Consum de la Caseta
            try:
                ac_l = bus.get_object("com.victronenergy.system", "/Ac/Consumption/L1/Power").GetValue()
                if ac_l is not None:
                    guardian.ac_loads = float(ac_l)
            except Exception:
                pass
            # 4. Sol Generat (Inversor Huawei en AC-Out)
            try:
                pv = bus.get_object("com.victronenergy.system", "/Ac/PvOnOutput/L1/Power").GetValue()
                if pv is not None:
                    pv_f = float(pv)
                    guardian.pv_p = 0.0 if (-25.0 <= pv_f <= 20.0) else pv_f
                else:
                    self._check_and_revive_cgwacs()
            except Exception:
                self._check_and_revive_cgwacs()
            # 5. Cel·les Pylontech i SOH
            try:
                bms_bus = bus.get_object("com.victronenergy.battery.socketcan_can1", "/System/MaxCellVoltage")
                c_max = bms_bus.GetValue()
                c_min = bus.get_object("com.victronenergy.battery.socketcan_can1", "/System/MinCellVoltage").GetValue()
                if c_max is not None and c_min is not None:
                    guardian.cell_max = float(c_max)
                    guardian.cell_min = float(c_min)
                    delta_mv = (guardian.cell_max - guardian.cell_min) * 1000.0
                    if delta_mv > guardian.max_cell_delta_today:
                        guardian.max_cell_delta_today = delta_mv
            except Exception:
                pass
            try:
                soh_val = bus.get_object("com.victronenergy.battery.socketcan_can1", "/Soh").GetValue()
                if soh_val is not None:
                    guardian.soh = float(soh_val)
            except Exception:
                pass
            # 6. Mode MultiPlus
            try:
                mode_val = bus.get_object("com.victronenergy.vebus.ttyS4", "/Mode").GetValue()
                if mode_val is not None:
                    guardian.vebus_mode = int(mode_val)
            except Exception:
                pass
        except Exception:
            pass

    def _check_and_revive_cgwacs(self):
        """Watchdog: Si el servei de telemetria solar de Carlo Gavazzi cau de dia, el ressuscita."""
        now = time.time()
        if now - self._last_cgwacs_check_time < 60.0:
            return
        self._last_cgwacs_check_time = now

        import datetime
        try:
            import zoneinfo
            MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
            now_madrid = datetime.datetime.now(MADRID_TZ)
        except Exception:
            now_madrid = datetime.datetime.now()

        if 8 <= now_madrid.hour <= 20:
            try:
                import subprocess
                res = subprocess.run(["svstat", "/service/dbus-cgwacs.ttyUSB1"], capture_output=True, text=True, timeout=2)
                if "down" in res.stdout:
                    log.warning("⚠️ Watchdog: Servei dbus-cgwacs.ttyUSB1 aturat en horari diürn! Executant 'svc -u'...")
                    subprocess.run(["svc", "-u", "/service/dbus-cgwacs.ttyUSB1"], timeout=2)
            except Exception as e:
                log.debug(f"Error al watchdog dbus-cgwacs: {e}")

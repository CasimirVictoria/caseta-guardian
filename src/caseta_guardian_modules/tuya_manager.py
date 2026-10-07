"""
TuyaManager Asíncron: Gestió de dispositius Tuya (Termo, Endoll Doble, Aire Condicionat).
Totalment no bloquejant: consultes i comandes delegades en segon pla amb instantànies immutables.
"""

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.request
from typing import Callable, Optional

try:
    import tinytuya
except ImportError:
    tinytuya = None

from .config import require_config
from .state_models import TermoSnapshot, DoblePlugSnapshot, ACSnapshot

log = logging.getLogger("caseta-guardian")


class TuyaManager:
    """Gestiona la comunicació amb dispositius Tuya via LocalTuya LAN i Tuya Cloud de forma asíncrona."""

    def __init__(self, cfg: dict, on_update_callback: Optional[Callable[[str, object], None]] = None):
        self.cfg = cfg
        self.on_update_callback = on_update_callback

        self._tuya_token = None
        self._tuya_token_time = 0.0

        self._lock = threading.Lock()
        self._running = True

        # Estat en memòria (Instantànies immutables)
        self._termo_snapshot = TermoSnapshot()
        self._doble_snapshot = DoblePlugSnapshot()
        self._ac_snapshot = ACSnapshot()

        # Temporitzadors interns de consulta
        self.last_termo_poll = 0.0
        self.last_doble_poll = 0.0
        self.last_ac_poll = 0.0

        # Cua de comandes per a enviament asíncron
        self._cmd_queue: queue.Queue = queue.Queue(maxsize=50)

        # Fil de sondeig en segon pla
        self._poller_thread = threading.Thread(
            target=self._poller_loop,
            name="TuyaPoller",
            daemon=True
        )

        # Fil d'execució de comandes en segon pla
        self._cmd_thread = threading.Thread(
            target=self._cmd_loop,
            name="TuyaCmdWorker",
            daemon=True
        )

        self._poller_thread.start()
        self._cmd_thread.start()

    # =========================================================================
    # LECTURES D'ESTAT (NO BLOQUEJANTS - 0 ms)
    # =========================================================================

    def get_termo_snapshot(self) -> TermoSnapshot:
        with self._lock:
            return self._termo_snapshot

    def get_doble_snapshot(self) -> DoblePlugSnapshot:
        with self._lock:
            return self._doble_snapshot

    def get_ac_snapshot(self) -> ACSnapshot:
        with self._lock:
            return self._ac_snapshot

    # Mètodes de compatibilitat retroactiva amb el codi antic
    def update_termo_status(self, termo_status: dict, last_update_time: float, last_calc_time: float):
        snap = self.get_termo_snapshot()
        d = snap.to_dict()
        termo_status.update(d)
        return termo_status, snap.timestamp, time.time()

    def update_doble_status(self, doble_status: dict, last_update_time: float, last_calc_time: float):
        snap = self.get_doble_snapshot()
        d = snap.to_dict()
        doble_status.update({
            "ch1_name": "Microones / Torradora",
            "ch1_on": snap.is_on_1,
            "ch2_name": "Cafetera",
            "ch2_on": snap.is_on_2,
            "power_w": snap.power_w,
            "voltage_v": snap.voltage_v,
            "current_a": snap.current_a,
            "source": snap.source,
            "timestamp": snap.timestamp
        })
        return doble_status, snap.timestamp, time.time()

    # =========================================================================
    # COMANDES ASÍNCRONES (NO BLOQUEJANTS)
    # =========================================================================

    def send_termo_command(self, power: bool, reason: str = ""):
        """Envia ordre al Termo asíncronament."""
        # Actualització optimista provisional perquè la màquina d'estats no repeteixi l'ordre
        with self._lock:
            cur = self._termo_snapshot
            self._termo_snapshot = TermoSnapshot(
                is_on=power,
                power_w=cur.power_w if power else 0.0,
                voltage_v=cur.voltage_v,
                current_a=cur.current_a if power else 0.0,
                source=cur.source,
                timestamp=time.time()
            )
        self._cmd_queue.put(("termo", (power, reason)))

    def send_doble_command(self, channel: int, power: bool, reason: str = ""):
        """Envia ordre a l'Endoll Doble asíncronament."""
        with self._lock:
            cur = self._doble_snapshot
            self._doble_snapshot = DoblePlugSnapshot(
                is_on_1=power if channel == 1 else cur.is_on_1,
                is_on_2=power if channel == 2 else cur.is_on_2,
                power_w=cur.power_w,
                voltage_v=cur.voltage_v,
                current_a=cur.current_a,
                source=cur.source,
                timestamp=time.time()
            )
        self._cmd_queue.put(("doble", (channel, power, reason)))

    def send_ac_command(self, power: int = 1, temp: int = 26, mode: int = 0, fan: int = 0, reason: str = ""):
        """Envia ordre a l'Aire Condicionat asíncronament."""
        with self._lock:
            self._ac_snapshot = ACSnapshot(
                power=power,
                temp=temp,
                mode="Fred" if mode == 0 else "Auto",
                timestamp=time.time()
            )
        self._cmd_queue.put(("ac", (power, temp, mode, fan, reason)))

    # =========================================================================
    # FILS EN SEGON PLA (WORKERS)
    # =========================================================================

    def _poller_loop(self):
        """Bucle de sondeig periòdic en segon pla."""
        # Esperem 1s a l'inici per estabilitzar
        time.sleep(1.0)
        self._poll_termo()
        self._poll_doble()
        self._poll_ac()

        while self._running:
            now = time.time()
            if now - self.last_termo_poll >= 30.0:
                self._poll_termo()
            if now - self.last_doble_poll >= 20.0:
                self._poll_doble()
            if now - self.last_ac_poll >= 120.0:
                self._poll_ac()
            time.sleep(1.0)

    def _cmd_loop(self):
        """Processa la cua de comandes sense bloquejar el bucle principal."""
        while self._running:
            try:
                cmd_item = self._cmd_queue.get(timeout=1.0)
                if cmd_item is None:
                    break
                cmd_type, args = cmd_item
                if cmd_type == "termo":
                    self._exec_termo_command(*args)
                elif cmd_type == "doble":
                    self._exec_doble_command(*args)
                elif cmd_type == "ac":
                    self._exec_ac_command(*args)
                self._cmd_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Error processant comanda Tuya a la cua: {e}")

    # =========================================================================
    # IMPLEMENTACIÓ I/O REAL (EXECUTAT EN SEGON PLA)
    # =========================================================================

    def get_tuya_access_token(self, cid: str, sec: str, base_url: str) -> Optional[str]:
        now = time.time()
        if self._tuya_token and (now - self._tuya_token_time < 3600):
            return self._tuya_token

        t_ms = str(int(now * 1000))
        url_path_token = "/v1.0/token?grant_type=1"
        content_hash = hashlib.sha256(b"").hexdigest()
        sign_str = f"{cid}{t_ms}GET\n{content_hash}\n\n{url_path_token}"
        sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
        req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
            "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
        })
        try:
            with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
                token = json.loads(rep_tok.read().decode())["result"]["access_token"]
                self._tuya_token = token
                self._tuya_token_time = now
                return token
        except Exception as e:
            log.warning(f"Error obtenint token Tuya Cloud: {e}")
            return None

    def _poll_termo(self):
        now = time.time()
        self.last_termo_poll = now
        deviceId = self.cfg.get("tuya_termo_device_id")
        if not deviceId:
            return

        ip = self.cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = self.cfg.get("tuya_termo_local_key", "")
        version = float(self.cfg.get("tuya_termo_version", 3.3))

        # 1. Intent LocalTuya LAN
        if tinytuya:
            try:
                d = tinytuya.OutletDevice(deviceId, ip, local_key)
                d.set_version(version)
                d.set_socketPersistent(False)
                data = d.status()
                dps = data.get("dps", {})
                if dps:
                    is_on = bool(dps.get("1", False))
                    current_a = float(dps.get("18", 0)) / 1000.0
                    power_w = float(dps.get("19", 0)) / 10.0
                    voltage_v = float(dps.get("20", 0)) / 10.0
                    with self._lock:
                        self._termo_snapshot = TermoSnapshot(
                            is_on=is_on,
                            power_w=round(power_w, 1),
                            voltage_v=round(voltage_v, 1),
                            current_a=round(current_a, 2),
                            source="localtuya",
                            timestamp=now
                        )
                    if self.on_update_callback:
                        self.on_update_callback("termo", self._termo_snapshot)
                    return
            except Exception as e:
                log.debug(f"LocalTuya termo offline/error: {e}")

        # 2. Fallback Tuya Cloud
        cid = self.cfg.get("tuya_client_id")
        sec = self.cfg.get("tuya_secret")
        base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")
        if cid and sec:
            try:
                token = self.get_tuya_access_token(cid, sec, base_url)
                if token:
                    path_status = f"/v1.0/devices/{deviceId}/status"
                    t_ms = str(int(time.time() * 1000))
                    content_hash = hashlib.sha256(b"").hexdigest()
                    sign_str = f"{cid}{token}{t_ms}GET\n{content_hash}\n\n{path_status}"
                    sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
                    req_status = urllib.request.Request(f"{base_url}{path_status}", headers={
                        "client_id": cid, "access_token": token, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
                    })
                    with urllib.request.urlopen(req_status, timeout=5) as rep:
                        res = json.loads(rep.read().decode())
                        status_list = res.get("result", [])
                        status_map = {item["code"]: item["value"] for item in status_list}
                        is_on = status_map.get("switch_1", False)
                        power_w = status_map.get("cur_power", 0) / 10.0
                        voltage_v = status_map.get("cur_voltage", 0) / 10.0
                        current_a = status_map.get("cur_current", 0) / 1000.0
                        with self._lock:
                            self._termo_snapshot = TermoSnapshot(
                                is_on=is_on,
                                power_w=round(power_w, 1),
                                voltage_v=round(voltage_v, 1),
                                current_a=round(current_a, 2),
                                source="cloud",
                                timestamp=now
                            )
                        if self.on_update_callback:
                            self.on_update_callback("termo", self._termo_snapshot)
            except Exception as e:
                log.debug(f"Tuya Cloud termo error: {e}")

    def _poll_doble(self):
        now = time.time()
        self.last_doble_poll = now
        deviceId = self.cfg.get("tuya_doble_device_id")
        if not deviceId or not tinytuya:
            return

        ip = self.cfg.get("tuya_doble_ip", "192.168.1.101")
        local_key = self.cfg.get("tuya_doble_local_key", "")
        version = float(self.cfg.get("tuya_doble_version", 3.3))

        try:
            d = tinytuya.OutletDevice(deviceId, ip, local_key)
            d.set_version(version)
            d.set_socketPersistent(False)
            data = d.status()
            dps = data.get("dps", {})
            if dps:
                ch1_on = bool(dps.get("1", False))
                ch2_on = bool(dps.get("2", False))
                current_a = float(dps.get("18", 0)) / 1000.0
                power_w = float(dps.get("19", 0)) / 10.0
                voltage_v = float(dps.get("20", 0)) / 10.0
                with self._lock:
                    self._doble_snapshot = DoblePlugSnapshot(
                        is_on_1=ch1_on,
                        is_on_2=ch2_on,
                        power_w=round(power_w, 1),
                        voltage_v=round(voltage_v, 1),
                        current_a=round(current_a, 2),
                        source="localtuya",
                        timestamp=now
                    )
                if self.on_update_callback:
                    self.on_update_callback("doble", self._doble_snapshot)
        except Exception as e:
            log.debug(f"LocalTuya doble error: {e}")

    def _poll_ac(self):
        now = time.time()
        self.last_ac_poll = now
        remote_id = self.cfg.get("tuya_remote_id")
        cid = self.cfg.get("tuya_client_id")
        sec = self.cfg.get("tuya_secret")
        base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

        if not remote_id or not cid or not sec:
            return

        try:
            token = self.get_tuya_access_token(cid, sec, base_url)
            if not token:
                return

            path_status = f"/v1.0/devices/{remote_id}/status"
            t_ms = str(int(time.time() * 1000))
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{token}{t_ms}GET\n{content_hash}\n\n{path_status}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_status = urllib.request.Request(f"{base_url}{path_status}", headers={
                "client_id": cid, "access_token": token, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_status, timeout=5) as rep:
                res = json.loads(rep.read().decode())
                if res.get("success", False):
                    status_list = res.get("result", [])
                    status_map = {item["code"]: item["value"] for item in status_list}
                    pwr_val = status_map.get("power", "0")
                    pwr = 1 if str(pwr_val) in ("1", "true", "True") else 0
                    temp = int(status_map.get("temp", self._ac_snapshot.temp))
                    mode_val = str(status_map.get("mode", "0"))
                    mode_str = "Fred" if mode_val in ("0", "cool") else "Auto"
                    with self._lock:
                        self._ac_snapshot = ACSnapshot(
                            power=pwr,
                            temp=temp,
                            mode=mode_str,
                            timestamp=now
                        )
                    if self.on_update_callback:
                        self.on_update_callback("ac", self._ac_snapshot)
        except Exception as e:
            log.debug(f"Tuya Cloud AC status error: {e}")

    def _exec_termo_command(self, power: bool, reason: str):
        deviceId = self.cfg.get("tuya_termo_device_id")
        ip = self.cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = self.cfg.get("tuya_termo_local_key", "")
        version = float(self.cfg.get("tuya_termo_version", 3.3))

        if tinytuya:
            try:
                d = tinytuya.OutletDevice(deviceId, ip, local_key)
                d.set_version(version)
                d.set_socketPersistent(False)
                res = d.set_status(power, 1)
                log.info(f"♨️ [TERMO LOCALTUYA LAN] Power {'ON' if power else 'OFF'} ({reason}): {res}")
                return
            except Exception as e:
                log.warning(f"Error enviant per LocalTuya LAN: {e}. Provant Tuya Cloud...")

        # Cloud fallback
        cid = self.cfg.get("tuya_client_id")
        sec = self.cfg.get("tuya_secret")
        base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")
        if cid and sec:
            try:
                token = self.get_tuya_access_token(cid, sec, base_url)
                if token:
                    path_cmd = f"/v1.0/devices/{deviceId}/commands"
                    body_dict = {"commands": [{"code": "switch_1", "value": power}]}
                    body_str = json.dumps(body_dict)
                    c_hash = hashlib.sha256(body_str.encode()).hexdigest()
                    t_ms = str(int(time.time() * 1000))
                    sign_str_cmd = f"{cid}{token}{t_ms}POST\n{c_hash}\n\n{path_cmd}"
                    sign_cmd = hmac.new(sec.encode(), sign_str_cmd.encode(), hashlib.sha256).hexdigest().upper()
                    req_cmd = urllib.request.Request(f"{base_url}{path_cmd}", data=body_str.encode(), headers={
                        "client_id": cid, "access_token": token, "sign": sign_cmd, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
                    }, method="POST")
                    with urllib.request.urlopen(req_cmd, timeout=5) as rep:
                        res = json.loads(rep.read().decode())
                        log.info(f"♨️ [TERMO TUYA CLOUD] Termo Power {'ON' if power else 'OFF'} ({reason}): {res}")
            except Exception as e:
                log.error(f"Error fatal enviant comanda Termo Tuya Cloud: {e}")

    def _exec_doble_command(self, channel: int, power: bool, reason: str):
        deviceId = self.cfg.get("tuya_doble_device_id")
        ip = self.cfg.get("tuya_doble_ip", "192.168.1.101")
        local_key = self.cfg.get("tuya_doble_local_key", "")
        version = float(self.cfg.get("tuya_doble_version", 3.3))
        ch_name = "Microones/Torradora (CH1)" if channel == 1 else "Cafetera (CH2)"

        if tinytuya:
            try:
                d = tinytuya.OutletDevice(deviceId, ip, local_key)
                d.set_version(version)
                d.set_socketPersistent(False)
                res = d.set_status(power, channel)
                log.info(f"🥐 [END OLL DOBLE LAN] {ch_name} Power {'ON' if power else 'OFF'} ({reason}): {res}")
            except Exception as e:
                log.warning(f"Error enviant comanda a Endoll Doble per LAN: {e}")

    def _exec_ac_command(self, power: int, temp: int, mode: int, fan: int, reason: str):
        cid = self.cfg.get("tuya_client_id")
        sec = self.cfg.get("tuya_secret")
        infrared_id = self.cfg.get("tuya_infrared_id")
        remote_id = self.cfg.get("tuya_remote_id")
        base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

        if not cid or not sec or not infrared_id or not remote_id:
            return

        try:
            token = self.get_tuya_access_token(cid, sec, base_url)
            if not token:
                return

            def send_sub_cmd(code, val):
                t_ms_sub = str(int(time.time() * 1000))
                url_path_cmd = f"/v2.0/infrareds/{infrared_id}/air-conditioners/{remote_id}/command"
                body_dict = {"code": code, "value": val}
                body_str = json.dumps(body_dict)
                c_hash = hashlib.sha256(body_str.encode()).hexdigest()
                s_to_sign = f"POST\n{c_hash}\n\n{url_path_cmd}"
                s_str = f"{cid}{token}{t_ms_sub}{s_to_sign}"
                s = hmac.new(sec.encode(), s_str.encode(), hashlib.sha256).hexdigest().upper()
                req_cmd = urllib.request.Request(f"{base_url}{url_path_cmd}", data=body_str.encode(), headers={
                    "client_id": cid, "access_token": token, "sign": s, "t": t_ms_sub, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
                }, method="POST")
                with urllib.request.urlopen(req_cmd, timeout=8) as rep_cmd:
                    return json.loads(rep_cmd.read().decode())

            if power == 0:
                res = send_sub_cmd("power", 0)
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Power OFF ({reason}): {res}")
            elif power == 1:
                res = send_sub_cmd("power", 1)
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Power ON a {temp}ºC ({reason}): {res}")
            else:
                res = send_sub_cmd("temp", int(temp))
                log.info(f"❄️ [CLIMA AUTÒNOM] AC Consigna {temp}ºC ({reason}): {res}")
        except Exception as e:
            log.warning(f"Error enviant comanda AC Tuya en segon pla: {e}")

    def stop(self):
        self._running = False
        try:
            self._cmd_queue.put_nowait(None)
        except Exception:
            pass

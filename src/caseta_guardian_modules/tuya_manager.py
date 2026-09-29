"""
Gestió de dispositius Tuya: Termo, Endoll Doble i Aire Condicionat.
"""

import hashlib
import hmac
import json
import logging
import time
import urllib.request

from .config import require_config

log = logging.getLogger("caseta-guardian")


class TuyaManager:
    """Gestiona la comunicació amb dispositius Tuya via LocalTuya LAN i Tuya Cloud."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._tuya_token = None
        self._tuya_token_time = 0.0

    def get_tuya_access_token(self, cid: str, sec: str, base_url: str) -> str:
        """Obté i reutilitza el token d'accés de Tuya Cloud durant 1 hora."""
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
        with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
            token = json.loads(rep_tok.read().decode())["result"]["access_token"]
            self._tuya_token = token
            self._tuya_token_time = now
            return token

    def update_termo_status(self, termo_status: dict, last_update_time: float, last_calc_time: float):
        """Consulta l'estat del Termo Elèctric via LocalTuya LAN o Tuya Cloud."""
        now = time.time()
        if now - last_update_time < 30.0:
            return termo_status, last_update_time, last_calc_time

        deviceId = require_config(self.cfg, "tuya_termo_device_id", "ID del dispositiu Tuya del termo")
        ip = self.cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = require_config(self.cfg, "tuya_termo_local_key", "Clau local del termo")
        version = float(self.cfg.get("tuya_termo_version", 3.3))

        # 1. Intent Directe per LocalTuya (LAN local)
        try:
            import tinytuya
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

                dt_termo = now - last_calc_time
                last_calc_time = now

                termo_status.update({
                    "is_on": is_on,
                    "power_w": round(power_w, 1),
                    "voltage_v": round(voltage_v, 1),
                    "current_a": round(current_a, 2),
                    "source": "localtuya",
                    "timestamp": now
                })
                return termo_status, now, last_calc_time
        except Exception as e_local:
            log.debug(f"LocalTuya status error: {e_local}")

        # 2. Fallback Tuya Cloud OpenAPI
        try:
            cid = require_config(self.cfg, "tuya_client_id", "Client ID de Tuya Cloud")
            sec = require_config(self.cfg, "tuya_secret", "Secret de Tuya Cloud")
            base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

            t_ms = str(int(now * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{t_ms}GET\n{content_hash}\n\n{url_path_token}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
                token = json.loads(rep_tok.read().decode())["result"]["access_token"]

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

                dt_termo = now - last_calc_time
                last_calc_time = now

                termo_status.update({
                    "is_on": is_on,
                    "power_w": round(power_w, 1),
                    "voltage_v": round(voltage_v, 1),
                    "current_a": round(current_a, 2),
                    "source": "cloud",
                    "timestamp": now
                })
                return termo_status, now, last_calc_time
        except Exception:
            pass

        return termo_status, now, last_calc_time

    def send_termo_command(self, power: bool, reason: str = ""):
        """Envia ordre d'encesa/apagada al Termo via LocalTuya amb fallback a Tuya Cloud."""
        deviceId = require_config(self.cfg, "tuya_termo_device_id", "ID del dispositiu Tuya del termo")
        ip = self.cfg.get("tuya_termo_ip", "192.168.1.100")
        local_key = require_config(self.cfg, "tuya_termo_local_key", "Clau local del termo")
        version = float(self.cfg.get("tuya_termo_version", 3.3))

        # 1. Intent Prioritari LocalTuya LAN
        try:
            import tinytuya
            d = tinytuya.OutletDevice(deviceId, ip, local_key)
            d.set_version(version)
            d.set_socketPersistent(False)
            res = d.set_status(power, 1)
            log.info(f"♨️ [TERMO LOCALTUYA LAN] Power {'ON' if power else 'OFF'} ({reason}): {res}")
            return res
        except Exception as e_local:
            log.warning(f"Error enviant per LocalTuya LAN: {e_local}. Reintentant per Tuya Cloud...")

        # 2. Fallback Tuya Cloud OpenAPI
        try:
            cid = require_config(self.cfg, "tuya_client_id", "Client ID de Tuya Cloud")
            sec = require_config(self.cfg, "tuya_secret", "Secret de Tuya Cloud")
            base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

            t_ms = str(int(time.time() * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            sign_str = f"{cid}{t_ms}GET\n{content_hash}\n\n{url_path_token}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=5) as rep_tok:
                token = json.loads(rep_tok.read().decode())["result"]["access_token"]

            path_cmd = f"/v1.0/devices/{deviceId}/commands"
            body_dict = {"commands": [{"code": "switch_1", "value": power}]}
            body_str = json.dumps(body_dict)
            c_hash = hashlib.sha256(body_str.encode()).hexdigest()
            sign_str_cmd = f"{cid}{token}{t_ms}POST\n{c_hash}\n\n{path_cmd}"
            sign_cmd = hmac.new(sec.encode(), sign_str_cmd.encode(), hashlib.sha256).hexdigest().upper()

            req_cmd = urllib.request.Request(f"{base_url}{path_cmd}", data=body_str.encode(), headers={
                "client_id": cid, "access_token": token, "sign": sign_cmd, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            }, method="POST")
            with urllib.request.urlopen(req_cmd, timeout=5) as rep:
                res = json.loads(rep.read().decode())
                log.info(f"♨️ [TERMO TUYA CLOUD] Termo Power {'ON' if power else 'OFF'} ({reason}): {res}")
                return res
        except Exception as e:
            log.error(f"Error fatal enviant comanda Termo Tuya Cloud: {e}")
            return False

    def update_doble_status(self, doble_status: dict, last_update_time: float, last_calc_time: float):
        """Consulta l'estat de l'Endoll Doble Cuina via LocalTuya LAN."""
        now = time.time()
        if now - last_update_time < 20.0:
            return doble_status, last_update_time, last_calc_time

        deviceId = require_config(self.cfg, "tuya_doble_device_id", "ID del dispositiu Tuya de l'endoll doble")
        ip = self.cfg.get("tuya_doble_ip", "192.168.1.101")
        local_key = require_config(self.cfg, "tuya_doble_local_key", "Clau local de l'endoll doble")
        version = float(self.cfg.get("tuya_doble_version", 3.3))

        try:
            import tinytuya
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

                dt = now - last_calc_time
                last_calc_time = now

                doble_status.update({
                    "ch1_name": "Microones / Torradora",
                    "ch1_on": ch1_on,
                    "ch2_name": "Cafetera",
                    "ch2_on": ch2_on,
                    "power_w": round(power_w, 1),
                    "voltage_v": round(voltage_v, 1),
                    "current_a": round(current_a, 2),
                    "source": "localtuya",
                    "timestamp": now
                })
                return doble_status, now, last_calc_time
        except Exception as e:
            log.debug(f"LocalTuya endoll doble error: {e}")

        return doble_status, now, last_calc_time

    def send_doble_command(self, channel: int, power: bool, reason: str = ""):
        """Envia ordre a l'Endoll Doble via LocalTuya LAN."""
        deviceId = require_config(self.cfg, "tuya_doble_device_id", "ID del dispositiu Tuya de l'endoll doble")
        ip = self.cfg.get("tuya_doble_ip", "192.168.1.101")
        local_key = require_config(self.cfg, "tuya_doble_local_key", "Clau local de l'endoll doble")
        version = float(self.cfg.get("tuya_doble_version", 3.3))
        ch_name = "Microones/Torradora (CH1)" if channel == 1 else "Cafetera (CH2)"

        try:
            import tinytuya
            d = tinytuya.OutletDevice(deviceId, ip, local_key)
            d.set_version(version)
            d.set_socketPersistent(False)
            res = d.set_status(power, channel)
            log.info(f"🥐 [END OLL DOBLE LAN] {ch_name} Power {'ON' if power else 'OFF'} ({reason}): {res}")
            return res
        except Exception as e:
            log.warning(f"Error enviant comanda a Endoll Doble per LAN: {e}")
            return False

    def send_ac_command(self, power: int = 1, temp: int = 26, mode: int = 0, fan: int = 0, reason: str = ""):
        """Envia ordres d'infraroigs al Mitsubishi Electric mitjançant Tuya Cloud OpenAPI."""
        now = time.time()
        cid = require_config(self.cfg, "tuya_client_id", "Client ID de Tuya Cloud")
        sec = require_config(self.cfg, "tuya_secret", "Secret de Tuya Cloud")
        infrared_id = require_config(self.cfg, "tuya_infrared_id", "ID de l'infraroig Tuya")
        remote_id = require_config(self.cfg, "tuya_remote_id", "ID del comandament virtual de l'AC")
        base_url = self.cfg.get("tuya_base_url", "https://openapi.tuyaeu.com")

        try:
            # 1. Obtenir Token Tuya
            t_ms = str(int(now * 1000))
            url_path_token = "/v1.0/token?grant_type=1"
            content_hash = hashlib.sha256(b"").hexdigest()
            str_to_sign = f"GET\n{content_hash}\n\n{url_path_token}"
            sign_str = f"{cid}{t_ms}{str_to_sign}"
            sign = hmac.new(sec.encode(), sign_str.encode(), hashlib.sha256).hexdigest().upper()
            req_token = urllib.request.Request(f"{base_url}{url_path_token}", headers={
                "client_id": cid, "sign": sign, "t": t_ms, "sign_method": "HMAC-SHA256", "Content-Type": "application/json"
            })
            with urllib.request.urlopen(req_token, timeout=8) as rep_tok:
                tok_data = json.loads(rep_tok.read().decode())
                token = tok_data.get("result", {}).get("access_token")

            if not token:
                log.warning(f"No s'ha pogut obtenir token Tuya: {tok_data}")
                return False

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

            return True
        except Exception as e:
            log.warning(f"Error enviant comanda AC Tuya: {e}")
            return False

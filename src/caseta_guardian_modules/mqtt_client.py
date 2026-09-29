"""
Client MQTT per Caseta Guardian.
"""

import json
import logging
import time

log = logging.getLogger("caseta-guardian")


class MQTTClient:
    """Gestiona la connexió MQTT amb el Cerbo GX."""

    def __init__(self, cerbo_ip: str, portal_id: str):
        self.cerbo_ip = cerbo_ip
        self.portal_id = portal_id
        self.client = None
        self.last_keepalive_time = 0.0

    def connect(self, on_message_callback):
        """Connecta al broker MQTT del Cerbo GX."""
        import paho.mqtt.client as mqtt

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_message = on_message_callback

        try:
            self.client.connect(self.cerbo_ip, 1883, 60)
        except Exception as e:
            log.error(f"Error fatal connectant al broker MQTT del Cerbo GX ({self.cerbo_ip}): {e}")
            return False

        # Subscripcions quirúrgiques per eliminar el 70% del soroll MQTT innecessari
        self.client.subscribe("N/+/battery/512/#")
        self.client.subscribe("N/+/pvinverter/#")
        self.client.subscribe("N/+/system/0/#")
        self.client.subscribe("N/+/vebus/276/#")
        self.client.subscribe("caseta/#")
        self.client.loop_start()
        return True

    def publish(self, topic: str, payload: str, retain: bool = True):
        """Publica un missatge MQTT."""
        if self.client:
            self.client.publish(topic, payload, retain=retain)

    def publish_to_portal(self, topic: str, payload: str, retain: bool = True):
        """Publica un missatge MQTT al portal específic."""
        if self.client and self.portal_id not in ("c0619ab2xxxx", "+", "#"):
            self.client.publish(f"N/{self.portal_id}/{topic}", payload, retain=retain)

    def send_keepalive(self):
        """Envia un keepalive al Cerbo GX cada 30 segons."""
        now = time.time()
        if now - self.last_keepalive_time >= 30:
            self.publish(f"R/{self.portal_id}/keepalive", "", retain=False)
            self.last_keepalive_time = now

    def disconnect(self):
        """Desconnecta del broker MQTT."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

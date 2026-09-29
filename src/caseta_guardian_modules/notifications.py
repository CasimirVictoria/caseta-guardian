"""
Notificacions ntfy amb silenci nocturn i anti-repetició.
"""

import logging
import time
import urllib.request

log = logging.getLogger("caseta-guardian")


class NotificationManager:
    """Gestiona les notificacions ntfy amb filtres de silenci i anti-repetició."""

    def __init__(self, ntfy_topic: str):
        self.ntfy_topic = ntfy_topic
        self._notif_history = {}
        self.daemon_start_time = time.time()

    def send_notification(self, title: str, message: str, priority: str = "default", tags: str = "zap"):
        """Envia una notificació ntfy amb filtres de silenci nocturn i anti-repetició."""
        import datetime
        try:
            import zoneinfo
            MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
            now_madrid = datetime.datetime.now(MADRID_TZ)
        except Exception:
            now_madrid = datetime.datetime.now()

        # 🌙 0. MODE NO MOLESTAR NOCTURN (23:00h a 08:00h Madrid)
        if (now_madrid.hour >= 23 or now_madrid.hour < 8) and priority != "emergency":
            log.info(f"🌙 [SILENCI NOCTURN DND 23h-08h] Notificació silenciada: {title}")
            return

        # 🔕 1. Silenci d'arrencada: Durant els primers 60 segons
        now = time.time()
        if (now - self.daemon_start_time < 60):
            if priority not in ("urgent", "high", "5", "4", "emergency"):
                log.info(f"🔕 [SILENCI D'ARRENCADA] Notificació rutinària silenciada: {title}")
                return

        # 🔕 2. Filtre anti-repetició (Deduplicació en menys de 10 minuts per a no-crítiques)
        dedup_key = f"{title}_{message[:30]}"
        last_sent = self._notif_history.get(dedup_key, 0.0)
        if (now - last_sent < 600) and priority not in ("urgent", "high", "5", "4"):
            return
        self._notif_history[dedup_key] = now

        try:
            url = f"https://ntfy.sh/{self.ntfy_topic}"
            data = message.encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            clean_title = title.encode('ascii', 'ignore').decode('ascii').strip() or "Caseta Guardian"
            req.add_header("Title", clean_title)
            req.add_header("Priority", priority)
            req.add_header("Tags", tags)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info(f"📱 Notificació enviada al mòbil: {title}")
        except Exception as e:
            log.warning(f"No s'ha pogut enviar notificació ntfy: {e}")

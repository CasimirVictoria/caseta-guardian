"""
Notificacions ntfy asíncrones basades en queue.Queue.
Garanteix zero bloqueig del bucle principal de control de Cerbo GX.
"""

import datetime
import logging
import queue
import threading
import time
import urllib.request
from typing import Optional

try:
    import zoneinfo
    MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None

from .state_models import NotificationItem

log = logging.getLogger("caseta-guardian")


class NotificationManager:
    """Gestiona l'enviament de notificacions a ntfy.sh sense bloquejar el fil principal."""

    def __init__(self, ntfy_topic: str):
        self.ntfy_topic = ntfy_topic
        self._notif_history = {}
        self.daemon_start_time = time.time()

        # Cua thread-safe per a desacoblar la xarxa del bucle principal
        self._queue: queue.Queue[Optional[NotificationItem]] = queue.Queue(maxsize=100)
        self._running = True

        # Fil consumidor en segon pla
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="NtfyWorker",
            daemon=True
        )
        self._worker_thread.start()

    def _get_madrid_now(self) -> datetime.datetime:
        if MADRID_TZ:
            return datetime.datetime.now(MADRID_TZ)
        return datetime.datetime.now()

    def send_notification(self, title: str, message: str, priority: str = "default", tags: str = "zap"):
        """
        Envia una notificació. Aquesta crida és NO BLOQUEJANT:
        filtra en memòria i encola la tasca en menys de 0.1 ms.
        """
        now_madrid = self._get_madrid_now()

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
        if (now - last_sent < 600) and priority not in ("urgent", "high", "5", "4", "emergency"):
            return
        self._notif_history[dedup_key] = now

        # Encolament immediat sense bloqueig
        item = NotificationItem(title=title, message=message, priority=priority, tags=tags)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            log.warning(f"⚠️ Cua de notificacions ntfy plena. Descartant avís: {title}")

    def _worker_loop(self):
        """Bucle consumidor en segon pla que fa les peticions HTTP a ntfy.sh."""
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
                if item is None:  # Sentinella de tancament
                    break
                self._send_http(item)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Error inesperat al consumidor de notificacions: {e}")

    def _send_http(self, item: NotificationItem):
        """Execució real de la petició HTTPS a ntfy.sh."""
        try:
            url = f"https://ntfy.sh/{self.ntfy_topic}"
            data = item.message.encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            clean_title = item.title.encode('ascii', 'ignore').decode('ascii').strip() or "Caseta Guardian"
            req.add_header("Title", clean_title)
            req.add_header("Priority", item.priority)
            req.add_header("Tags", item.tags)

            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info(f"📱 Notificació enviada al mòbil: {item.title}")
        except Exception as e:
            log.warning(f"No s'ha pogut enviar notificació ntfy ({item.title}): {e}")

    def stop(self, timeout: float = 2.0):
        """Atura el fil consumidor de manera neta."""
        self._running = False
        try:
            self._queue.put_nowait(None)
            self._worker_thread.join(timeout=timeout)
        except Exception:
            pass

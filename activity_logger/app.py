"""Wires the monitors, Excel writer, updater, central sync and tray together."""

import queue
import threading
import time
from datetime import date

from . import __version__, central, config
from . import emailer, updater
from .emailer import EmailScheduler
from .events import Event
from .excel_writer import ExcelWriter
from .monitors import MessageWindow, Poller
from .settings import RuntimeSettings
from .tray import Tray


class App:
    def __init__(self):
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._settings = RuntimeSettings()
        self._writer = ExcelWriter(self._queue)
        self._poller = Poller(self._queue, self._settings)
        self._msgwin = MessageWindow(self._queue, self._settings)
        self._tray = Tray(on_quit=self.stop, on_send_now=self._send_now,
                          on_upload_now=self._upload_now,
                          on_check_updates=self._check_updates)
        # Background/automatic sends (scheduled upload, scheduled email, and
        # an admin's on-demand "Request log now") stay silent — popping a
        # toast for something the employee didn't trigger is just noise, and
        # the visible tray icon is already the transparency signal. Only a
        # tray menu click the employee made themselves (_upload_now,
        # _send_now, _check_updates below) shows a notification, since that's
        # feedback for their own action.
        self._email_scheduler = EmailScheduler(notify=None)
        self._central_sync = central.CentralSync(self._settings, notify=None)
        self._request_watcher = central.RequestWatcher(
            self._settings, notify=None, on_force_update=self.stop)
        self._stopping = threading.Event()

    def start(self) -> None:
        # Update before anything else: if an update starts, exit immediately so
        # the swapper can replace this exe.
        try:
            if updater.check_and_update():
                return
        except Exception:
            pass
        # We're proceeding with a normal startup (no update in flight from
        # THIS launch) — safe to sweep up any stuck swapper scripts from a
        # past attempt that never finished.
        try:
            updater.cleanup_stale_update_scripts()
        except Exception:
            pass

        self._writer.start()
        self._poller.start()
        self._msgwin.start()
        self._email_scheduler.start()
        if central.is_configured():
            self._central_sync.start()
            self._request_watcher.start()
        self._queue.put(Event("Start", detail=f"v{__version__}"))

        self._update_thread = threading.Thread(
            target=self._periodic_update_loop, name="Updater", daemon=True
        )
        self._update_thread.start()

        # Tray.run() blocks this (main) thread until Quit is chosen.
        self._tray.run()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._queue.put(Event("Stop"))
        self._poller.stop()
        self._msgwin.stop()
        self._email_scheduler.stop()
        self._central_sync.stop()
        self._request_watcher.stop()
        # Give the writer a moment to flush the final events, then stop it.
        time.sleep(0.5)
        self._writer.stop()
        self._tray.stop()

    def _send_now(self) -> None:
        ok, info = emailer.send_today()
        self._on_email_result(ok, info)

    def _upload_now(self) -> None:
        ok, info = central.upload_logs([date.today()])
        self._on_upload_result(ok, info)

    def _check_updates(self) -> None:
        try:
            _, info = updater.check_now()
        except Exception as exc:
            info = f"Update check failed: {exc}"
        self._tray.notify(info)

    def _on_email_result(self, ok: bool, info: str) -> None:
        prefix = "Log emailed" if ok else "Email not sent"
        self._tray.notify(f"{prefix}: {info}")

    def _on_upload_result(self, ok: bool, info: str) -> None:
        prefix = "Log uploaded" if ok else "Upload not sent"
        self._tray.notify(f"{prefix}: {info}")

    def _periodic_update_loop(self) -> None:
        while not self._stopping.wait(config.UPDATE_CHECK_INTERVAL_SEC):
            try:
                if updater.check_and_update():
                    # New version is downloaded; shut down so it can take over.
                    self.stop()
                    return
            except Exception:
                pass


def main() -> None:
    App().start()

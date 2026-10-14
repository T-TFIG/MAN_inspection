"""Wires the monitors, Excel writer, updater and tray icon together."""

import queue
import threading
import time

from . import __version__, config
from . import updater
from .events import Event
from .excel_writer import ExcelWriter
from .monitors import MessageWindow, Poller
from .tray import Tray


class App:
    def __init__(self):
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._writer = ExcelWriter(self._queue)
        self._poller = Poller(self._queue)
        self._msgwin = MessageWindow(self._queue)
        self._tray = Tray(on_quit=self.stop)
        self._stopping = threading.Event()

    def start(self) -> None:
        # Update before anything else: if an update starts, exit immediately so
        # the swapper can replace this exe.
        try:
            if updater.check_and_update():
                return
        except Exception:
            pass

        self._writer.start()
        self._poller.start()
        self._msgwin.start()
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
        # Give the writer a moment to flush the final events, then stop it.
        time.sleep(0.5)
        self._writer.stop()
        self._tray.stop()

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

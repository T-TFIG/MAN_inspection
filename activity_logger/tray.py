"""System-tray icon so the logger is visible and the user can quit it.

Keeping a tray icon (rather than running invisibly) makes the tool transparent:
anyone on the machine can see it is running, open the logs folder, or stop it.
"""

import os
import threading

import pystray
from PIL import Image, ImageDraw

from . import __version__, config


def _make_icon_image() -> Image.Image:
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(60, 160, 220))
    d.text((24, 22), "M", fill=(255, 255, 255))
    return img


class Tray:
    def __init__(self, on_quit, on_send_now=None, on_upload_now=None,
                on_check_updates=None):
        self._on_quit = on_quit
        self._on_send_now = on_send_now
        self._on_upload_now = on_upload_now
        self._on_check_updates = on_check_updates
        self._icon = pystray.Icon(
            "MAN_inspection",
            icon=_make_icon_image(),
            title=f"MAN_inspection v{__version__}",
            menu=pystray.Menu(
                pystray.MenuItem("Upload today's log now", self._upload_now),
                pystray.MenuItem("Email today's log now", self._send_now),
                pystray.MenuItem("Open logs folder", self._open_logs),
                pystray.MenuItem("Check for updates", self._check_updates),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    def _send_now(self, icon, item):
        if self._on_send_now:
            # Run off the tray thread so SMTP latency never freezes the menu.
            threading.Thread(target=self._on_send_now, daemon=True).start()

    def _upload_now(self, icon, item):
        if self._on_upload_now:
            threading.Thread(target=self._on_upload_now, daemon=True).start()

    def _check_updates(self, icon, item):
        if self._on_check_updates:
            threading.Thread(target=self._on_check_updates, daemon=True).start()

    def _open_logs(self, icon, item):
        try:
            os.startfile(config.log_dir())
        except Exception:
            pass

    def _quit(self, icon, item):
        self._icon.stop()
        self._on_quit()

    def notify(self, message: str, title: str = "MAN_inspection") -> None:
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def run(self) -> None:
        # Blocks on the calling thread until .stop() is called.
        self._icon.run()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass

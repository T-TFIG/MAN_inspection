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
    def __init__(self, on_quit):
        self._on_quit = on_quit
        self._icon = pystray.Icon(
            "MAN_inspection",
            icon=_make_icon_image(),
            title=f"MAN_inspection v{__version__}",
            menu=pystray.Menu(
                pystray.MenuItem("Open logs folder", self._open_logs),
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    def _open_logs(self, icon, item):
        try:
            os.startfile(config.log_dir())
        except Exception:
            pass

    def _quit(self, icon, item):
        self._icon.stop()
        self._on_quit()

    def run(self) -> None:
        # Blocks on the calling thread until .stop() is called.
        self._icon.run()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass

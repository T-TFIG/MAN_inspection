"""Windows activity monitors.

Two pieces run as background threads and push Event objects onto a shared queue:

1. ``Poller`` samples the foreground window and the system idle time on a timer,
   emitting a "Window" segment (with duration) whenever the active window
   changes, and "Idle"/"Active" events around idle periods.

2. ``MessageWindow`` creates a hidden top-level window and listens for Windows
   session-change messages (lock/unlock/logon/logoff) and device-change
   messages (USB / removable-drive arrival and removal).
"""

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace

import psutil
import pythoncom
import win32api
import win32com.client
import win32con
import win32gui
import win32gui_struct
import win32process
import win32ts

from . import config
from .events import Event

# --- idle detection ---------------------------------------------------------


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return max(0.0, millis / 1000.0)


def foreground_window() -> tuple[str, str, int]:
    """Return (process_name, window_title, pid) of the focused window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return "", "", 0
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        title = ""
    proc = ""
    pid = 0
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid:
            proc = psutil.Process(pid).name()
    except Exception:
        proc = ""
    return proc, title, pid


# A whitelist of extensions, not a blacklist of system paths: apps keep all
# sorts of internal bookkeeping files open that look nothing like system
# paths but aren't the user's document either -- e.g. the Windows 11 Store
# Notepad holds a handle on its own session file under
# AppData\Local\Packages\...\TabState\<guid>.bin, not on the .txt it's
# showing. Matching on "this looks like a real document" is far more
# reliable than trying to list every app's private-storage location.
_DOCUMENT_EXTENSIONS = {
    ".txt", ".md", ".rtf", ".csv", ".tsv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".odt", ".ods", ".odp",
    ".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".toml",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rb", ".php", ".sql", ".html", ".css",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp",
    ".mp3", ".wav", ".mp4", ".avi", ".mkv", ".mov",
    ".zip", ".rar", ".7z",
    ".psd", ".ai", ".dwg",
}


def _likely_document_path(pid: int) -> str:
    """Best-effort "what file is open" via the process's own file handles.

    There is no general Windows API for "what document is this app
    displaying" -- each app tracks it differently (if at all). Most apps
    that are actively showing a document (Word, Excel, Notepad, a PDF
    viewer, ...) keep a file handle open on it the whole time, so scanning
    the foreground process's open files for one whose extension looks like
    a real document is a reasonable generic fallback. It can miss cases
    (e.g. an app that reads a file fully into memory and closes the
    handle) or guess wrong if several real candidates are open at once --
    acceptable for an activity log, not meant to be exact.
    """
    try:
        proc = psutil.Process(pid)
        locks = []
        for f in proc.open_files():
            path = f.path
            _, ext = os.path.splitext(path)
            if ext.lower() not in _DOCUMENT_EXTENSIONS:
                continue
            if os.path.basename(path).startswith("~$"):
                locks.append(path)  # Office's hidden owner/lock file, not the document
                continue
            return path
        # Office keeps a "~$name.xlsx" lock file open beside the document and
        # it vanishes when the document closes, so it's useless to request;
        # point at the real document next to it instead.
        for lock in locks:
            real = _document_for_lock_file(lock)
            if real:
                return real
    except Exception:
        pass
    return ""


def _document_for_lock_file(lock_path: str) -> str:
    folder, name = os.path.split(lock_path)
    tail = name[2:]  # Office drops/keeps the leading characters depending on name length
    try:
        siblings = [n for n in os.listdir(folder) if not n.startswith("~$")]
    except OSError:
        return ""
    exact = [n for n in siblings if n == tail]
    fuzzy = [n for n in siblings if tail and n.endswith(tail)]
    for n in exact + fuzzy:
        return os.path.join(folder, n)
    return ""


def foreground_window_path(app: str, pid: int = 0) -> str:
    """Best-effort "what file/folder is open" for the current foreground app.

    File Explorer is resolved precisely via the Shell.Application COM
    automation object, which lists every open Explorer window with its real
    filesystem path. Everything else falls back to _likely_document_path()
    (the process's own open file handles) -- less exact, but far more
    useful than always returning "" for every non-Explorer app.

    Must be called from a thread where COM has been initialized
    (pythoncom.CoInitialize()) — see Poller.run().
    """
    if app.lower() == "explorer.exe":
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            for window in shell.Windows():
                try:
                    if window.HWND != hwnd:
                        continue
                    folder = window.Document.Folder.Self.Path
                    return folder or ""
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    if pid:
        return _likely_document_path(pid)
    return ""


# --- poller thread -----------------------------------------------------------


class Poller(threading.Thread):
    def __init__(self, event_queue: "queue.Queue[Event]", settings=None):
        super().__init__(name="Poller", daemon=True)
        self._queue = event_queue
        # A RuntimeSettings instance the admin can change centrally (poll
        # interval, idle threshold, which event kinds to emit at all). Falls
        # back to the static config defaults if none is supplied.
        self._settings = settings or SimpleNamespace(
            poll_interval_sec=config.POLL_INTERVAL_SEC,
            idle_threshold_sec=config.IDLE_THRESHOLD_SEC,
            track_window=True, track_idle=True,
        )
        self._stop = threading.Event()
        self._cur_app = ""
        self._cur_title = ""
        self._cur_path = ""
        self._seg_start = time.monotonic()
        self._have_segment = False
        self._is_idle = False

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # The Explorer-path lookup uses COM (Shell.Application), which needs
        # the apartment initialized on whichever thread calls it.
        pythoncom.CoInitialize()
        try:
            self._run()
        finally:
            pythoncom.CoUninitialize()

    def _run(self) -> None:
        # Seed with the current window so the first real change has a duration.
        self._cur_app, self._cur_title, pid = foreground_window()
        self._cur_path = foreground_window_path(self._cur_app, pid)
        self._seg_start = time.monotonic()
        self._have_segment = True

        while not self._stop.wait(self._settings.poll_interval_sec):
            self._tick()

        # Flush the final in-progress segment on shutdown.
        self._close_segment()

    def _tick(self) -> None:
        idle = idle_seconds()

        if idle >= self._settings.idle_threshold_sec:
            if not self._is_idle:
                # Entered idle: close the window segment that was running.
                self._close_segment()
                self._is_idle = True
                self._seg_start = time.monotonic()
            return

        # Not idle.
        if self._is_idle:
            idle_duration = time.monotonic() - self._seg_start
            if self._settings.track_idle:
                self._emit(Event("Idle", detail="idle period", duration=idle_duration))
            self._is_idle = False
            # Start a fresh window segment on resume.
            self._cur_app, self._cur_title, pid = foreground_window()
            self._cur_path = foreground_window_path(self._cur_app, pid)
            self._seg_start = time.monotonic()
            self._have_segment = True
            if self._settings.track_idle:
                self._emit(Event("Active", self._cur_app, self._cur_title))
            return

        app, title, pid = foreground_window()
        if app != self._cur_app or title != self._cur_title:
            self._close_segment()
            self._cur_app, self._cur_title = app, title
            self._cur_path = foreground_window_path(app, pid)
            self._seg_start = time.monotonic()
            self._have_segment = True

    def _close_segment(self) -> None:
        if not self._have_segment:
            return
        duration = time.monotonic() - self._seg_start
        if self._settings.track_window and (self._cur_app or self._cur_title):
            self._emit(Event("Window", self._cur_app, self._cur_title,
                             path=self._cur_path, duration=duration))
        self._have_segment = False

    def _emit(self, ev: Event) -> None:
        self._queue.put(ev)


# --- hidden message window (session + device changes) ------------------------

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOGOFF = 0x6
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8

DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_VOLUME = 0x00000002
DBT_DEVTYP_DEVICEINTERFACE = 0x00000005

_SESSION_EVENTS = {
    WTS_SESSION_LOGON: "Logon",
    WTS_SESSION_LOGOFF: "Logoff",
    WTS_SESSION_LOCK: "Lock",
    WTS_SESSION_UNLOCK: "Unlock",
}

GUID_DEVINTERFACE_USB_DEVICE = "{A5DCBF10-6530-11D2-901F-00C04FB951ED}"


def _unitmask_to_drives(mask: int) -> str:
    drives = [f"{chr(ord('A') + i)}:" for i in range(26) if mask & (1 << i)]
    return ", ".join(drives)


class MessageWindow(threading.Thread):
    def __init__(self, event_queue: "queue.Queue[Event]", settings=None):
        super().__init__(name="MessageWindow", daemon=True)
        self._queue = event_queue
        self._settings = settings or SimpleNamespace(track_usb=True,
                                                      track_session=True)
        self._hwnd = None
        self._ready = threading.Event()

    def run(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "ManInspectionHiddenWnd"
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        class_atom = win32gui.RegisterClass(wc)
        self._hwnd = win32gui.CreateWindow(
            class_atom, "MAN_inspection", 0, 0, 0, 0, 0, 0, 0, hinst, None
        )

        # Session (lock/unlock/logon/logoff) notifications for this session.
        try:
            win32ts.WTSRegisterSessionNotification(
                self._hwnd, win32ts.NOTIFY_FOR_THIS_SESSION
            )
        except Exception:
            pass

        # Removable-volume broadcasts already reach top-level windows; also
        # register for USB device-interface notifications for non-storage USB.
        try:
            import pywintypes
            flt = win32gui_struct.PackDEV_BROADCAST_DEVICEINTERFACE(
                pywintypes.IID(GUID_DEVINTERFACE_USB_DEVICE)
            )
            win32gui.RegisterDeviceNotification(
                self._hwnd, flt, win32con.DEVICE_NOTIFY_WINDOW_HANDLE
            )
        except Exception:
            pass

        self._ready.set()
        win32gui.PumpMessages()

    def stop(self) -> None:
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass

    # -- message handling --
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_WTSSESSION_CHANGE:
            kind = _SESSION_EVENTS.get(wparam)
            if kind and self._settings.track_session:
                self._queue.put(Event(kind, detail="session"))
            return 0

        if msg == win32con.WM_DEVICECHANGE:
            self._handle_device_change(wparam, lparam)
            return True

        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0

        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_device_change(self, wparam, lparam):
        if not self._settings.track_usb:
            return
        if wparam not in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE):
            return
        if not lparam:
            return
        action = "Arrival" if wparam == DBT_DEVICEARRIVAL else "Removal"
        try:
            info = win32gui_struct.UnpackDEV_BROADCAST(lparam)
        except Exception:
            return

        if info.devicetype == DBT_DEVTYP_VOLUME:
            drives = _unitmask_to_drives(info.unitmask)
            detail = f"{action} drive {drives}" if drives else action
            self._queue.put(Event("USB", detail=detail))
        elif info.devicetype == DBT_DEVTYP_DEVICEINTERFACE:
            name = getattr(info, "name", "") or "USB device"
            self._queue.put(Event("USB", detail=f"{action} {name}"))

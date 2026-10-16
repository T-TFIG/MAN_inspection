"""Self-update from GitHub Releases.

When running as the built .exe, we check the latest release of the configured
repo. If its version is newer than the running one, we download the new .exe
next to the current one and hand control to a small batch script that waits
for us to exit, swaps the file, and relaunches.

Version checks deliberately avoid the GitHub REST API (api.github.com), which
rate-limits unauthenticated requests to 60/hour **per IP address** — every
machine behind one office's shared public IP draws from the same pool, and
with each machine checking at startup, every 6h, and whenever the admin
console refreshes its dashboard, that budget is easy to exhaust (this is
exactly what happened in testing: "API rate limit exceeded for <office IP>").
Instead we use GitHub's stable release-asset redirect URL
(github.com/<owner>/<repo>/releases/latest/download/<asset>), which resolves
through github.com itself — not the API host — and isn't subject to that
60/hour cap. One HEAD request reads the redirect's Location header, which
already contains the release tag (".../releases/download/v1.2.3/<asset>"),
so this gets us the version AND a working download URL in a single request.
"""

import os
import re
import subprocess
import sys
import tempfile
import time

import requests
from packaging.version import InvalidVersion, Version

from . import __version__, config


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _parse(tag: str):
    tag = (tag or "").lstrip("vV").strip()
    try:
        return Version(tag)
    except InvalidVersion:
        return None


def _latest_release(asset_name: str = config.RELEASE_ASSET_NAME) -> tuple[object, str] | None:
    """Return (Version, download_url) for the latest release's ``asset_name``.

    Both the employee logger and the admin console self-update from the same
    Release; each passes its own asset name (default is the employee one, for
    existing callers). See the module docstring for why this doesn't call
    api.github.com.
    """
    url = (f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
          f"/releases/latest/download/{asset_name}")
    try:
        resp = requests.head(url, timeout=15, allow_redirects=False)
        location = resp.headers.get("Location", "")
    except Exception:
        return None

    match = re.search(r"/releases/download/v?([^/]+)/", location)
    if not match:
        return None
    version = _parse(match.group(1))
    if version is None:
        return None
    # `url` itself (not `location`) is the download URL to use: it always
    # redirects to whatever the current latest release is, which also means
    # check_and_update() downloads the actual latest build even if a newer
    # one shipped in the few seconds since this check.
    return version, url


_UPDATE_COOLDOWN_SEC = 300  # 5 minutes


def _cooldown_path(asset_name: str) -> str:
    # Keyed by asset name so the employee logger and admin console -- which
    # can both be installed on the same machine (e.g. an admin's own PC) --
    # don't share one cooldown and suppress each other's update attempts.
    return os.path.join(config.data_dir(), f"last_update_attempt_{asset_name}.txt")


def _recently_attempted(asset_name: str) -> bool:
    try:
        with open(_cooldown_path(asset_name), "r", encoding="ascii") as fh:
            last = float(fh.read().strip())
    except Exception:
        return False
    return (time.time() - last) < _UPDATE_COOLDOWN_SEC


def _mark_attempt(asset_name: str) -> None:
    try:
        os.makedirs(config.data_dir(), exist_ok=True)
        with open(_cooldown_path(asset_name), "w", encoding="ascii") as fh:
            fh.write(str(time.time()))
    except Exception:
        pass


def _clear_cooldown(asset_name: str) -> None:
    try:
        os.remove(_cooldown_path(asset_name))
    except Exception:
        pass


def check_and_update(asset_name: str = config.RELEASE_ASSET_NAME) -> bool:
    """Returns True if an update was started (caller should then exit).

    If a swap was attempted recently but the running exe still reports the
    old version (the swap's `move` failed, e.g. antivirus briefly locking the
    fresh download, so the relaunch came back up on the old build), skip
    re-attempting for a few minutes. Without this, a stuck swap turns into an
    unbounded exit-relaunch-exit loop: this is called before any other
    thread starts, so the app never reaches CentralSync/RequestWatcher,
    which from the admin's side looks like the machine stopped responding to
    everything, not just updates.
    """
    if not _is_frozen():
        return False

    latest = _latest_release(asset_name)
    if not latest:
        return False
    version, download_url = latest
    if not download_url:
        return False

    try:
        current = Version(__version__)
    except InvalidVersion:
        return False
    if version <= current:
        _clear_cooldown(asset_name)
        return False

    if _recently_attempted(asset_name):
        return False
    _mark_attempt(asset_name)

    exe_path = sys.executable
    new_path = exe_path + ".new"
    try:
        with requests.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            expected_size = r.headers.get("Content-Length")
            expected_size = int(expected_size) if expected_size else None
            downloaded = 0
            with open(new_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)
                    downloaded += len(chunk)
        # A connection can drop mid-stream without requests raising (seen
        # with some proxy/antivirus setups that return 200 then cut the
        # body short) -- that leaves a truncated .new file which would
        # otherwise get swapped in as-is, producing a broken exe ("Failed to
        # load Python DLL" on launch). Treat a size mismatch the same as a
        # failed download.
        if expected_size is not None and downloaded != expected_size:
            _cleanup(new_path)
            return False
    except Exception:
        _cleanup(new_path)
        return False

    _spawn_swapper(new_path, exe_path)
    return True


def check_now(asset_name: str = config.RELEASE_ASSET_NAME) -> tuple[bool, str]:
    """Manual, user-triggered check (e.g. the tray's "Check for updates").

    Unlike check_and_update(), this always returns a human-readable reason,
    including "already up to date" and "couldn't reach GitHub" — the silent
    periodic check doesn't need that, but a button someone just clicked does.
    """
    latest = _latest_release(asset_name)
    if not latest:
        return False, "Could not reach GitHub to check for updates."
    version, download_url = latest

    try:
        current = Version(__version__)
    except InvalidVersion:
        current = None
    if current is not None and version <= current:
        return False, f"Up to date (v{__version__})."
    if not _is_frozen():
        return False, f"Update v{version} is available (not applied in a dev run)."
    if not download_url:
        return False, f"Update v{version} found, but it has no download asset."

    if check_and_update(asset_name):
        return True, f"Updating to v{version} — the app will restart shortly."
    return False, f"Update v{version} found, but the download failed. Try again later."


def _spawn_swapper(new_path: str, cur_path: str) -> None:
    """Write + launch the batch script that does the actual file swap.

    Earlier version waited for our PID to disappear from `tasklist` before
    moving the file, in a loop with no timeout. That hung forever on at
    least one real machine — almost certainly PID reuse (Windows handed our
    just-freed PID to an unrelated process, so the "has it exited?" check
    kept seeing a match) — leaving a `.new` file + a stuck batch script and
    the app never coming back up. No PID wait now: our process does no slow
    cleanup before exiting, so a short fixed delay is enough in the normal
    case; `move` is retried a bounded number of times for the rare case of a
    transient lock (e.g. antivirus briefly scanning the fresh download), and
    the script relaunches the app and deletes itself either way — even if
    the move itself never succeeds, the app comes back up instead of the
    whole thing hanging silently.
    """
    batch = (
        "@echo off\r\n"
        "ping -n 3 127.0.0.1 >NUL\r\n"
        "set \"tries=0\"\r\n"
        ":trymove\r\n"
        f'move /Y "{new_path}" "{cur_path}" >NUL 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "  set /a tries+=1\r\n"
        "  if %tries% GEQ 20 goto relaunch\r\n"
        "  ping -n 2 127.0.0.1 >NUL\r\n"
        "  goto trymove\r\n"
        ")\r\n"
        ":relaunch\r\n"
        f'start "" "{cur_path}" --post-update\r\n'
        'del "%~f0"\r\n'
    )
    fd, bat_path = tempfile.mkstemp(suffix="_update.bat")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(batch)

    # Launch the batch through a VBScript run via wscript.exe instead of
    # invoking cmd.exe directly. CREATE_NO_WINDOW + STARTUPINFO/SW_HIDE
    # (Win32-level flags on the cmd.exe process) were still reported to
    # flash a visible console on a real machine -- Windows 11's default
    # Windows Terminal console host can intercept a newly spawned console
    # process into a new tab under some configurations regardless of
    # those flags, since it manages console allocation itself.
    # wscript.exe has no console subsystem at all (it's a plain Win32 GUI
    # app), so there is nothing console-related for any terminal host to
    # intercept; WScript.Shell.Run's window-style argument (0 = hidden)
    # is a COM-level directive, a completely different mechanism, and is
    # the standard bulletproof technique for a batch script that must
    # never show a window under any console-host configuration.
    vbs = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run "cmd /c ""{bat_path}""", 0, False\r\n'
        'On Error Resume Next\r\n'
        'CreateObject("Scripting.FileSystemObject").DeleteFile WScript.ScriptFullName\r\n'
    )
    vfd, vbs_path = tempfile.mkstemp(suffix="_update.vbs")
    with os.fdopen(vfd, "w", encoding="ascii") as fh:
        fh.write(vbs)

    creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    subprocess.Popen(["wscript.exe", "//B", "//Nologo", vbs_path],
                     creationflags=creationflags, startupinfo=startupinfo,
                     env=_clean_child_env(), close_fds=True)


def _clean_child_env() -> dict:
    """Our environment minus PyInstaller's onefile bookkeeping, for the relaunch.

    The frozen exe's bootloader sets _PYI_APPLICATION_HOME_DIR (this process's
    _MEIxxxx unpack folder), _PYI_ARCHIVE_FILE (our exe's path) and
    _PYI_PARENT_PROCESS_LEVEL. Inherited through wscript -> cmd -> start,
    they reach the relaunched exe; because the swap puts it at the SAME
    path as _PYI_ARCHIVE_FILE, its bootloader takes them at face value,
    assumes it's the child half of an already-unpacked process, and tries
    to load python3xx.dll from our _MEI folder -- which was deleted when
    we exited. That is the "Failed to load Python DLL ... _MEIxxxx" crash
    after every self-update (reproduced exactly: same exe, inherited vars
    -> exits -1; vars stripped -> starts normally). Launching the exe by
    hand never showed it because a fresh shell has none of these vars.
    """
    meipass = (getattr(sys, "_MEIPASS", "") or "").lower()
    env = {}
    for key, value in os.environ.items():
        if key.upper().startswith("_PYI_") or key.upper() == "_MEIPASS2":
            continue
        if meipass and meipass in value.lower():
            if key.upper() != "PATH":
                continue  # e.g. TCL_LIBRARY/TK_LIBRARY pointing into our _MEI
            value = os.pathsep.join(p for p in value.split(os.pathsep)
                                    if meipass not in p.lower())
        env[key] = value
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def was_just_relaunched_from_update() -> bool:
    """True if this process was started by the swapper (see _spawn_swapper).

    A second, storage-independent safety net against the update-restart
    loop (v0.4.4's cooldown file lives in data_dir() and could in principle
    fail to persist on some real machine for a reason we can't fully rule
    out from here -- restricted account, antivirus, disk state). This flag
    costs nothing to check and can't fail to "persist": it's baked into the
    very relaunch command, present on EVERY relaunch regardless of whether
    the move succeeded or fell back to the old exe. The caller should skip
    its own check_and_update() call for this one run when true, guaranteeing
    the app reaches its normal threads (heartbeat, request handling) at
    least once per relaunch no matter what the cooldown file says.
    """
    return "--post-update" in sys.argv[1:]


def cleanup_stale_update_scripts() -> None:
    """Best-effort sweep of leftover *_update.bat/*_update.vbs from past attempts.

    Call once at normal startup (after we know we're NOT mid-update). Only
    removes files old enough (10+ minutes) that they cannot be a swap that's
    still legitimately in progress.
    """
    try:
        tmp_dir = tempfile.gettempdir()
        cutoff = time.time() - 600
        for name in os.listdir(tmp_dir):
            if not name.endswith(("_update.bat", "_update.vbs")):
                continue
            path = os.path.join(tmp_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

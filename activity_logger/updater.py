"""Self-update from GitHub Releases.

When running as the built .exe, we ask the GitHub API for the latest release of
the configured repo. If its version is newer than the running one, we download
the new .exe next to the current one and hand control to a small batch script
that waits for us to exit, swaps the file, and relaunches.
"""

import os
import subprocess
import sys
import tempfile
import time

import requests
from packaging.version import InvalidVersion, Version

from . import __version__, config

_API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"


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
    existing callers).
    """
    url = _API.format(owner=config.GITHUB_OWNER, repo=config.GITHUB_REPO)
    try:
        resp = requests.get(url, timeout=15,
                            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    version = _parse(data.get("tag_name", ""))
    if version is None:
        return None

    for asset in data.get("assets", []):
        if asset.get("name") == asset_name:
            return version, asset.get("browser_download_url", "")
    return None


def check_and_update(asset_name: str = config.RELEASE_ASSET_NAME) -> bool:
    """Returns True if an update was started (caller should then exit)."""
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
        return False

    exe_path = sys.executable
    new_path = exe_path + ".new"
    try:
        with requests.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(new_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)
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
        "  if %tries% GEQ 10 goto relaunch\r\n"
        "  ping -n 2 127.0.0.1 >NUL\r\n"
        "  goto trymove\r\n"
        ")\r\n"
        ":relaunch\r\n"
        f'start "" "{cur_path}"\r\n'
        'del "%~f0"\r\n'
    )
    fd, path = tempfile.mkstemp(suffix="_update.bat")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(batch)
    creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", path], creationflags=creationflags,
                     close_fds=True)


def cleanup_stale_update_scripts() -> None:
    """Best-effort sweep of leftover *_update.bat files from past attempts.

    Call once at normal startup (after we know we're NOT mid-update). Only
    removes files old enough (10+ minutes) that they cannot be a swap that's
    still legitimately in progress.
    """
    try:
        tmp_dir = tempfile.gettempdir()
        cutoff = time.time() - 600
        for name in os.listdir(tmp_dir):
            if not name.endswith("_update.bat"):
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

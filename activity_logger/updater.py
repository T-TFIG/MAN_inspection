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


def _latest_release() -> tuple[object, str] | None:
    """Return (Version, download_url) for the latest release, or None."""
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
        if asset.get("name") == config.RELEASE_ASSET_NAME:
            return version, asset.get("browser_download_url", "")
    return None


def check_and_update() -> bool:
    """Returns True if an update was started (caller should then exit)."""
    if not _is_frozen():
        return False

    latest = _latest_release()
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

    _spawn_swapper(os.getpid(), new_path, exe_path)
    return True


def _spawn_swapper(pid: int, new_path: str, cur_path: str) -> None:
    batch = (
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >NUL\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'move /Y "{new_path}" "{cur_path}" >NUL\r\n'
        f'start "" "{cur_path}"\r\n'
        'del "%~f0"\r\n'
    )
    fd, path = tempfile.mkstemp(suffix="_update.bat")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(batch)
    creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", path], creationflags=creationflags,
                     close_fds=True)


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

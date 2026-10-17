"""Backfill from Windows' own System event log.

The logger can only record while it is running, so days before it was
installed (or while it was stopped) are empty. Windows keeps its own
history of power and sign-in events in the System log, which an ordinary
(non-admin) user can read, and that history reaches back weeks. It cannot
say WHICH application or window was in use -- only coarse machine state --
and lock/unlock (Security log, off by default and admin-only) is not
available, so this fills gaps rather than replacing the real log.
"""

import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# (provider, event id) -> label shown in the Detail column
EVENTS = {
    ("Microsoft-Windows-Kernel-General", 12): "Windows started",
    ("Microsoft-Windows-Kernel-General", 13): "Windows shut down",
    ("Microsoft-Windows-Winlogon", 7001): "User logon",
    ("Microsoft-Windows-Winlogon", 7002): "User logoff",
    ("Microsoft-Windows-Kernel-Power", 42): "Sleep",
    ("Microsoft-Windows-Power-Troubleshooter", 1): "Wake from sleep",
    ("Microsoft-Windows-Kernel-Power", 41): "Unexpected restart (power loss or crash)",
    ("EventLog", 6008): "Unexpected shutdown (previous)",
    ("User32", 1074): "Shutdown/restart requested",
}

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def _to_utc_text(local_naive: datetime) -> str:
    return local_naive.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _query(start: datetime, end: datetime) -> str:
    clauses = " or ".join(
        f"(Provider[@Name='{prov}'] and EventID={eid})" for prov, eid in EVENTS)
    return (f"*[System[({clauses}) and TimeCreated[@SystemTime>='{_to_utc_text(start)}' "
            f"and @SystemTime<='{_to_utc_text(end)}']]]")


_QUERY_TIMEOUT_SEC = 45


def _run_wevtutil(query: str, timeout: float) -> bytes:
    """Run wevtutil with a hard deadline, output to a temp file.

    subprocess.run(timeout=...) waits again for the pipes after killing the
    child, which can block if the Event Log service is wedged; here we kill
    and walk away. Output goes to a file (not a pipe) and stdin is closed,
    which also avoids invalid-handle errors in a windowless frozen exe.
    """
    import os
    import tempfile
    import time

    fd, out_path = tempfile.mkstemp(suffix="_wevt.xml")
    try:
        with os.fdopen(fd, "wb") as out:
            proc = subprocess.Popen(
                ["wevtutil", "qe", "System", f"/q:{query}", "/f:xml", "/e:Events"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
            deadline = time.time() + timeout
            while proc.poll() is None:
                if time.time() > deadline:
                    proc.kill()
                    return b""
                time.sleep(0.2)
            if proc.returncode != 0:
                return b""
        with open(out_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def collect(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """(local time, label) for Windows events in [start, end] (naive local datetimes).

    Returns [] if the log can't be read -- backfill is best-effort and must
    never break a log request.
    """
    try:
        stdout = _run_wevtutil(_query(start, end), timeout=_QUERY_TIMEOUT_SEC)
        if not stdout.strip():
            return []
        root = ET.fromstring(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return []

    out = []
    for ev in root.iter(f"{_NS}Event"):
        sysn = ev.find(f"{_NS}System")
        if sysn is None:
            continue
        prov = sysn.find(f"{_NS}Provider").get("Name")
        eid = int(sysn.find(f"{_NS}EventID").text)
        label = EVENTS.get((prov, eid))
        stamp = sysn.find(f"{_NS}TimeCreated").get("SystemTime")
        if not label or not stamp:
            continue
        # SystemTime is UTC with 7 fractional digits; trim to what Python parses.
        base = stamp.rstrip("Z").split(".")[0]
        when = (datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None))
        out.append((when, label))
    out.sort(key=lambda x: x[0])
    return out

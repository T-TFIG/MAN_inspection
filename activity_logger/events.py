"""The Event record passed from monitors to the Excel writer."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    """One logged activity row.

    kind      : short category, e.g. "Window", "Idle", "Active", "Lock",
                "Unlock", "Logon", "Logoff", "USB", "Start", "Stop".
    app       : application / process name (may be empty).
    detail    : free text detail (window title, drive letter, etc.).
    path      : a resolved file-system path for this window, when we could
                get one (currently: the folder open in a File Explorer
                window). Blank for everything else — most apps don't expose
                "what file is open" through any common OS API.
    duration  : seconds the previous state lasted (only for "Window"/"Idle").
    time      : when the event happened.
    """

    kind: str
    app: str = ""
    detail: str = ""
    path: str = ""
    duration: Optional[float] = None
    time: datetime = field(default_factory=datetime.now)

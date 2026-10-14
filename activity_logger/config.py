"""Central configuration for the activity logger."""

import os
import sys

# --- GitHub repository used for self-update --------------------------------
# The built .exe checks this repo's latest Release and updates itself.
GITHUB_OWNER = "T-TFIG"
GITHUB_REPO = "MAN_inspection"
# Name of the .exe asset attached to each Release (must match the PyInstaller
# output name in .github/workflows/build.yml).
RELEASE_ASSET_NAME = "MAN_inspection.exe"

# --- Behaviour tuning -------------------------------------------------------
POLL_INTERVAL_SEC = 2.0          # how often we sample the foreground window
IDLE_THRESHOLD_SEC = 60.0        # no keyboard/mouse for this long => "Idle"
UPDATE_CHECK_INTERVAL_SEC = 6 * 60 * 60   # re-check for updates every 6 hours


def base_dir() -> str:
    """Folder that logs/ live next to.

    When frozen (running as the .exe) we use the folder containing the exe so
    the Excel files sit right beside the program. In development we use the
    repository root.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir() -> str:
    d = os.path.join(base_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d

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

# --- Central (admin-controlled) email policy --------------------------------
# Endpoints pull this JSON from the repo. The admin changes it in one place
# (edit remote_policy.json on GitHub) and every machine follows on next refresh.
# It contains NO secrets (recipient + schedule only).
POLICY_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/main/"
    "remote_policy.json"
)
POLICY_REFRESH_INTERVAL_SEC = 30 * 60   # re-pull the policy every 30 minutes

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


def data_dir() -> str:
    """Machine-wide folder for admin config, policy cache and state.

    Prefers %PROGRAMDATA%\\MAN_inspection (so an admin can lock it down and
    deploy it centrally); falls back to the folder beside the program.
    """
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        d = os.path.join(program_data, "MAN_inspection")
    else:
        d = base_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = base_dir()
    return d

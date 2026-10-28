"""Central configuration for the activity logger."""

import os
import sys

# --- GitHub repository used for self-update --------------------------------
# The built .exe checks this repo's latest Release and updates itself.
GITHUB_OWNER = "T-TFIG"
GITHUB_REPO = "MAN_inspection"
# Names of the .exe assets attached to each Release (must match the
# PyInstaller output names in .github/workflows/build.yml). Both the employee
# logger and the admin console self-update from the SAME release/version;
# each just looks for its own asset name.
RELEASE_ASSET_NAME = "MAN_inspection.exe"
ADMIN_RELEASE_ASSET_NAME = "MAN_inspection_admin.exe"

# --- Private data backend (policy / machine status / uploaded logs) --------
# A SEPARATE private repo, holding no source code, used as a lightweight
# central store: policy/policy.json, status/<machine_id>.json (heartbeats),
# logs/<machine_id>/*.xlsx (uploaded reports). Employee machines write to this
# repo using a token baked in at build time (see activity_logger/_embedded.py),
# scoped to ONLY this repo's Contents — never the public code repo above.
DATA_REPO_OWNER = "T-TFIG"
DATA_REPO_NAME = "MAN_inspection-data"
POLICY_PATH = "policy/policy.json"

HEARTBEAT_INTERVAL_SEC_DEFAULT = 5 * 60   # overridden by policy.json if set

# How often each machine checks for an admin "send your log now" request.
# A conditional GET (ETag) is used so a steady "nothing requested" doesn't
# count against the shared GitHub rate limit — safe to poll this often even
# with many machines sharing one client token.
REQUEST_POLL_INTERVAL_SEC = 15

# --- Behaviour tuning -------------------------------------------------------
POLL_INTERVAL_SEC = 2.0          # how often we sample the foreground window
IDLE_THRESHOLD_SEC = 60.0        # no keyboard/mouse for this long => "Idle"
UPDATE_CHECK_INTERVAL_SEC = 6 * 60 * 60   # re-check for updates every 6 hours
# A machine that just booted/logged on may not have network back yet for the
# startup update check's one HEAD request. These ride out that window: a few
# quick retries right at startup, and if all of those also fail, the
# periodic loop's first follow-up comes much sooner than the steady-state
# 6h interval instead of stranding the machine on the old version that long.
STARTUP_UPDATE_CHECK_ATTEMPTS = 3
STARTUP_UPDATE_CHECK_RETRY_SEC = 8
UPDATE_RECHECK_SOON_SEC = 10 * 60


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

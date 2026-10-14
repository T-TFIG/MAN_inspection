"""Employee-side sync with the central data repo.

One background thread (``CentralSync``) does three things on one timer
(``heartbeat_interval_sec``, default 5 minutes, itself admin-controlled):

1. Writes ``status/<machine_id>.json`` — this machine's heartbeat, which is
   what the Admin Console's dashboard reads to show "online/offline".
2. Reads ``policy/policy.json`` and applies the ``monitoring`` section to the
   shared :class:`~activity_logger.settings.RuntimeSettings`, so the admin's
   changes take effect without reinstalling anything.
3. Checks the ``upload`` schedule (daily/weekly/monthly, same mechanism as
   email) and, when due, uploads the covered day(s)' Excel file(s) to
   ``logs/<machine_id>/`` — this is the "better than plain Gmail" transport:
   the file lands in a place only the admin can read, with no per-machine
   mail credentials involved at all.

Everything here degrades to a no-op if ``_embedded.CLIENT_DATA_TOKEN`` is
empty (a local dev build, or a build where the CI secret wasn't set) — same
pattern as the email feature when its local secret file is missing.
"""

import json
import os
import threading
from datetime import datetime

from . import __version__, config, data_backend, machine_id, scheduling
from ._embedded import CLIENT_DATA_TOKEN
from .excel_writer import _path_for


def is_configured() -> bool:
    return bool(CLIENT_DATA_TOKEN)


# --- local cache / state paths ----------------------------------------------


def _policy_cache_path() -> str:
    return os.path.join(config.data_dir(), "central_policy_cache.json")


def _upload_state_path() -> str:
    return os.path.join(config.data_dir(), "upload_state.json")


def _own_sha_cache_path() -> str:
    return os.path.join(config.data_dir(), "central_sha_cache.json")


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


DEFAULT_POLICY = {
    "version": 2,
    "monitoring": {
        "poll_interval_sec": config.POLL_INTERVAL_SEC,
        "idle_threshold_sec": config.IDLE_THRESHOLD_SEC,
        "track_window": True,
        "track_idle": True,
        "track_usb": True,
        "track_session": True,
    },
    "heartbeat_interval_sec": config.HEARTBEAT_INTERVAL_SEC_DEFAULT,
    "upload": {
        "enabled": False,
        "schedule": "daily",
        "send_time": "18:00",
        "day_of_week": "mon",
        "day_of_month": 1,
    },
    "email": {
        "enabled": False,
        "recipient_email": "",
        "schedule": "daily",
        "send_time": "18:00",
        "day_of_week": "mon",
        "day_of_month": 1,
    },
}


def _merge_policy(data: dict) -> dict:
    merged = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    for key in ("monitoring", "upload", "email"):
        if isinstance(data.get(key), dict):
            merged[key].update(data[key])
    if "heartbeat_interval_sec" in data:
        merged["heartbeat_interval_sec"] = data["heartbeat_interval_sec"]
    return merged


def fetch_policy() -> dict:
    """Pull the central policy; fall back to the last cache, then defaults."""
    if not is_configured():
        return dict(DEFAULT_POLICY)
    try:
        data, _ = data_backend.get_json(CLIENT_DATA_TOKEN, config.POLICY_PATH)
        if isinstance(data, dict):
            merged = _merge_policy(data)
            _save_json(_policy_cache_path(), merged)
            return merged
    except Exception:
        pass
    cached = _load_json(_policy_cache_path(), None)
    if isinstance(cached, dict):
        return _merge_policy(cached)
    return dict(DEFAULT_POLICY)


# --- heartbeat ---------------------------------------------------------------


def _status_path() -> str:
    return f"status/{machine_id.get_machine_id()}.json"


def send_heartbeat(extra: dict | None = None) -> None:
    if not is_configured():
        return
    cache = _load_json(_own_sha_cache_path(), {})
    status_sha = cache.get("status_sha")
    body = {
        "machine_id": machine_id.get_machine_id(),
        "hostname": machine_id.get_hostname(),
        "app_version": __version__,
        "last_seen": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        body.update(extra)
    try:
        new_sha = data_backend.put_json_retrying(
            CLIENT_DATA_TOKEN, _status_path(), body,
            f"heartbeat {body['hostname']}", cached_sha=status_sha,
        )
        cache["status_sha"] = new_sha
        _save_json(_own_sha_cache_path(), cache)
    except Exception:
        pass  # a missed heartbeat just makes this machine look briefly offline


# --- central upload (replaces per-machine email as the default transport) --


def upload_logs(dates: list, label: str = "") -> tuple[bool, str]:
    if not is_configured():
        return False, "central upload not configured on this build"
    mid = machine_id.get_machine_id()
    uploaded = 0
    for day in dates:
        src = _path_for(day)
        if not os.path.exists(src):
            continue
        try:
            with open(src, "rb") as fh:
                content = fh.read()
        except Exception:
            continue
        path = f"logs/{mid}/{os.path.basename(src)}"
        try:
            data_backend.put_bytes(CLIENT_DATA_TOKEN, path, content,
                                   f"upload log {os.path.basename(src)}")
            uploaded += 1
        except Exception as exc:
            return False, f"upload failed: {exc}"
    if uploaded == 0:
        return False, "no log files for the selected period yet"
    return True, f"uploaded {uploaded} file(s) to the central store"


class CentralSync(threading.Thread):
    """One timer driving heartbeat + policy pull + settings apply + upload."""

    def __init__(self, settings, notify=None):
        super().__init__(name="CentralSync", daemon=True)
        self._settings = settings
        self._notify = notify
        self._stop = threading.Event()
        self._upload_last_key = _load_json(_upload_state_path(), {}).get("last_key")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not is_configured():
            return
        while True:
            self._cycle()
            if self._stop.wait(self._settings.heartbeat_interval_sec):
                return

    def _cycle(self) -> None:
        policy = fetch_policy()
        self._settings.apply_monitoring(policy["monitoring"])
        self._settings.heartbeat_interval_sec = float(
            policy.get("heartbeat_interval_sec", self._settings.heartbeat_interval_sec))

        send_heartbeat()
        self._maybe_upload(policy["upload"])

    def _maybe_upload(self, upload_cfg: dict) -> None:
        if not upload_cfg.get("enabled"):
            return
        now = datetime.now()
        key = scheduling.due_key(upload_cfg, now)
        if key is None or key == self._upload_last_key:
            return
        dates = scheduling.dates_for_schedule(upload_cfg.get("schedule", "daily"),
                                              now.date())
        ok, info = upload_logs(dates)
        if ok:
            self._upload_last_key = key
            _save_json(_upload_state_path(), {"last_key": key})
        if self._notify:
            self._notify(ok, info)

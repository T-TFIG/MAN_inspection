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

A second, much faster thread (``RequestWatcher``) lets an admin pull data on
demand instead of waiting for a scheduled upload:

* ``requests/<machine_id>.json`` — "send your log now" (today's Excel file).
* ``file_requests/<machine_id>.json`` — "send me this one specific local
  file" (an admin-supplied absolute path), uploaded to
  ``files/<machine_id>/<flattened-path>`` if it exists and is under the size
  cap (20 MB).
* ``update_requests/<machine_id>.json`` — "check for and apply an update
  right now" instead of waiting for the next periodic check (every 6h) or
  the user noticing and clicking it themselves.

Both are polled every few seconds using a conditional GET (ETag), which
GitHub does not count against the API rate limit when nothing changed, so
this stays cheap even with many machines sharing one token.

Everything here degrades to a no-op if ``_embedded.CLIENT_DATA_TOKEN`` is
empty (a local dev build, or a build where the CI secret wasn't set) — same
pattern as the email feature when its local secret file is missing.
"""

import json
import os
import re
import threading
from datetime import date, datetime, timedelta

from . import __version__, config, data_backend, machine_id, scheduling, updater
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
        # How many days of uploaded logs to keep in the central data repo
        # before a machine deletes its own older ones. Keeps the (public-
        # facing-cost-wise private, but still a real git history) repo from
        # growing forever. 0 disables cleanup (keep everything).
        "retention_days": 10,
    },
    "email": {
        "enabled": False,
        "recipient_email": "",
        "schedule": "daily",
        "send_time": "18:00",
        "day_of_week": "mon",
        "day_of_month": 1,
    },
    # Master switch for the admin's "Request log now" feature. Off by
    # default-safe choice would be True (keeps existing behavior); admins can
    # flip it off to stop machines from reacting to on-demand requests at all
    # — e.g. if a flurry of clicks produced more uploads than wanted.
    "on_demand": {
        "enabled": True,
    },
}


def _merge_policy(data: dict) -> dict:
    merged = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    for key in ("monitoring", "upload", "email", "on_demand"):
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
            # _retrying (not plain put_bytes): re-uploading the SAME day's
            # file (e.g. a second "Request log now") must overwrite the
            # existing one, which needs its current sha — this was silently
            # failing every time before (422, no sha supplied) since nothing
            # surfaced the error once background notifications went quiet.
            data_backend.put_bytes_retrying(CLIENT_DATA_TOKEN, path, content,
                                            f"upload log {os.path.basename(src)}")
            uploaded += 1
        except Exception as exc:
            return False, f"upload failed: {exc}"
    if uploaded == 0:
        return False, "no log files for the selected period yet"
    return True, f"uploaded {uploaded} file(s) to the central store"


_LOG_NAME_RE = re.compile(r"^activity_(\d{4}-\d{2}-\d{2})\.xlsx$")


def _retention_state_path() -> str:
    return os.path.join(config.data_dir(), "retention_state.json")


def cleanup_old_logs(retention_days: int) -> None:
    """Delete this machine's own uploaded logs older than ``retention_days``.

    Keeps the central repo from growing forever — every machine prunes only
    its own files. retention_days <= 0 disables this (keep everything).
    """
    if not is_configured() or retention_days <= 0:
        return
    mid = machine_id.get_machine_id()
    try:
        entries = data_backend.list_dir(CLIENT_DATA_TOKEN, f"logs/{mid}")
    except Exception:
        return
    cutoff = date.today() - timedelta(days=retention_days)
    for entry in entries:
        match = _LOG_NAME_RE.match(entry.get("name", ""))
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        try:
            data_backend.delete_file(CLIENT_DATA_TOKEN, entry["path"], entry["sha"],
                                     f"retention cleanup ({retention_days}d)")
        except Exception:
            pass  # a missed delete just gets retried on the next cleanup pass


class CentralSync(threading.Thread):
    """One timer driving heartbeat + policy pull + settings apply + upload."""

    def __init__(self, settings, notify=None):
        super().__init__(name="CentralSync", daemon=True)
        self._settings = settings
        self._notify = notify
        self._stop = threading.Event()
        self._upload_last_key = _load_json(_upload_state_path(), {}).get("last_key")
        self._last_cleanup_date = _load_json(_retention_state_path(), {}).get("date")

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
        self._settings.on_demand_requests_enabled = bool(
            policy.get("on_demand", {}).get("enabled", True))

        send_heartbeat()
        self._maybe_upload(policy["upload"])
        self._maybe_cleanup(policy["upload"].get("retention_days", 10))

    def _maybe_cleanup(self, retention_days: int) -> None:
        # Cheap throttle: at most once per calendar day — this does a
        # directory listing plus potentially several deletes, no need to run
        # it every heartbeat.
        today_str = date.today().isoformat()
        if self._last_cleanup_date == today_str:
            return
        cleanup_old_logs(retention_days)
        self._last_cleanup_date = today_str
        _save_json(_retention_state_path(), {"date": today_str})

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


def _request_path() -> str:
    return f"requests/{machine_id.get_machine_id()}.json"


def _file_request_path() -> str:
    return f"file_requests/{machine_id.get_machine_id()}.json"


def _range_request_path() -> str:
    return f"range_requests/{machine_id.get_machine_id()}.json"


MAX_RANGE_DAYS = 31
_RANGE_FMT = "%Y-%m-%d %H:%M:%S"


def build_log_range(start_text: str, end_text: str,
                    include_windows: bool = False) -> tuple[bool, str, dict]:
    """Collect the activity rows between two local timestamps into one file.

    Reads the per-day activity_*.xlsx files covering the range, keeps rows
    whose own Date+Time fall inside [start, end] (a Window row is stamped
    when its segment ENDS, so a segment straddling `start` is not included),
    and uploads the result for the admin to pick up. Returns
    (ok, message, extra_fields) like download_requested_file().

    With include_windows, also merges Windows' own boot/shutdown/sleep/wake/
    logon events (see winevents.py) so days with no program log -- before
    installation, or while it wasn't running -- still show machine state.
    """
    from io import BytesIO

    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font

    from .excel_writer import HEADERS

    if not is_configured():
        return False, "central upload not configured on this build", {}
    try:
        start = datetime.strptime(start_text, _RANGE_FMT)
        end = datetime.strptime(end_text, _RANGE_FMT)
    except Exception:
        return False, "invalid start/end time", {}
    if end < start:
        return False, "end is before start", {}
    if (end.date() - start.date()).days + 1 > MAX_RANGE_DAYS:
        return False, f"range too long (max {MAX_RANGE_DAYS} days)", {}

    wb = Workbook()
    ws = wb.active
    ws.title = "activity"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    collected = []  # (datetime, row) from the program's own logs and Windows
    day = start.date()
    while day <= end.date():
        src = _path_for(day)
        if os.path.exists(src):
            try:
                # Copy first: the writer may have the live file open/locked.
                with open(src, "rb") as fh:
                    snapshot = BytesIO(fh.read())
                sheet = load_workbook(snapshot, read_only=True).active
                header = None
                for row in sheet.iter_rows(values_only=True):
                    if header is None:
                        header = list(row)
                        continue
                    rec = dict(zip(header, row))
                    stamp = f"{rec.get('Date') or ''} {rec.get('Time') or ''}".strip()
                    try:
                        when = datetime.strptime(stamp, _RANGE_FMT)
                    except Exception:
                        continue
                    if start <= when <= end:
                        collected.append((when, [rec.get(h) for h in HEADERS]))
            except Exception as exc:
                return False, f"could not read {os.path.basename(src)}: {exc}", {}
        day += timedelta(days=1)

    windows_rows = 0
    if include_windows:
        from . import winevents
        for when, label in winevents.collect(start, end):
            collected.append((when, [when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S"),
                                     "Windows", "Windows Event Log", label, None, None]))
            windows_rows += 1

    collected.sort(key=lambda item: item[0])  # stable: same-second rows keep their order
    for _, row in collected:
        ws.append(row)
    rows_out = len(collected)

    if rows_out == 0:
        return True, "no activity recorded in that range", {"rows": 0}

    buf = BytesIO()
    wb.save(buf)
    content = buf.getvalue()
    if len(content) > MAX_FILE_REQUEST_BYTES:
        return False, "result too large; choose a shorter range", {}

    mid = machine_id.get_machine_id()
    name = (f"range_{start.strftime('%Y%m%d-%H%M')}_to_"
            f"{end.strftime('%Y%m%d-%H%M')}.xlsx")
    try:
        data_backend.put_bytes_retrying(CLIENT_DATA_TOKEN, f"range_logs/{mid}/{name}",
                                        content, f"upload log range {name}")
    except Exception as exc:
        return False, f"upload failed: {exc}", {}
    return True, "uploaded", {"remote_file": name, "rows": rows_out,
                              "windows_rows": windows_rows}


def _update_request_path() -> str:
    return f"update_requests/{machine_id.get_machine_id()}.json"


# Kept well under GitHub's practical Contents-API limit: a PUT embeds the
# file as base64 (~33% bigger) inside one JSON body, and large uploads get
# unreliable well before the API's documented 100 MB ceiling.
MAX_FILE_REQUEST_BYTES = 20 * 1024 * 1024


def _safe_remote_name(path: str) -> str:
    """Turn an absolute local path into one flat, collision-resistant name.

    Keeps the original path visible in the name (for the admin's own
    reference) rather than just the bare filename, since two different
    requested files can easily share a basename.
    """
    cleaned = path.replace(":", "").replace("\\", "_").replace("/", "_").strip("_ ")
    return cleaned or "file"


def download_requested_file(path: str) -> tuple[bool, str, dict]:
    """Upload one specific, admin-requested local file for pickup.

    Returns (ok, message, extra_fields) — extra_fields is merged into the
    request's "done" status (remote_file, size) on success.
    """
    if not is_configured():
        return False, "central upload not configured on this build", {}
    if not path or not os.path.isfile(path):
        return False, "file not found", {}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"could not read file size: {exc}", {}
    if size > MAX_FILE_REQUEST_BYTES:
        limit_mb = MAX_FILE_REQUEST_BYTES // (1024 * 1024)
        return False, f"file too large ({size / (1024*1024):.1f} MB, limit {limit_mb} MB)", {}
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except PermissionError:
        return False, "permission denied reading this file", {}
    except Exception as exc:
        return False, f"could not read file: {exc}", {}

    mid = machine_id.get_machine_id()
    remote_name = _safe_remote_name(path)
    remote_path = f"files/{mid}/{remote_name}"
    try:
        data_backend.put_bytes_retrying(CLIENT_DATA_TOKEN, remote_path, content,
                                        f"upload requested file {remote_name}")
    except Exception as exc:
        return False, f"upload failed: {exc}", {}
    return True, "uploaded", {"remote_file": remote_name, "size": size}


class RequestWatcher(threading.Thread):
    """Polls for admin requests every few seconds: "send your log now",
    "send me this specific file", and "check for and apply an update now".

    Kept separate from CentralSync's slow heartbeat/upload cycle so an admin
    debugging a machine doesn't have to wait out the full heartbeat interval.
    """

    def __init__(self, settings, notify=None, on_force_update=None):
        super().__init__(name="RequestWatcher", daemon=True)
        self._settings = settings
        self._notify = notify
        # Zero-arg callback the App wires to its own clean-shutdown path
        # (central.py has no reference to the App/tray, so it can't exit the
        # process itself) — called once an update has actually been staged
        # and this process needs to get out of the way for the swap script.
        self._on_force_update = on_force_update
        self._stop = threading.Event()
        self._etag: str | None = None
        self._file_etag: str | None = None
        self._update_etag: str | None = None
        self._range_etag: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not is_configured():
            return
        while not self._stop.wait(config.REQUEST_POLL_INTERVAL_SEC):
            if not self._settings.on_demand_requests_enabled:
                # Disabled by policy: skip the check entirely (no API call at
                # all, not even a conditional one) until it's turned back on.
                continue
            try:
                self._check_once()
            except Exception:
                pass  # a missed check just gets retried next cycle
            try:
                self._check_file_request_once()
            except Exception:
                pass
            try:
                self._check_range_request_once()
            except Exception:
                pass
            try:
                self._check_update_request_once()
            except Exception:
                pass

    def _check_once(self) -> None:
        path = _request_path()
        changed, raw, sha, etag = data_backend.get_file_conditional(
            CLIENT_DATA_TOKEN, path, self._etag)
        if not changed:
            return
        self._etag = etag
        if raw is None:
            return  # no request file (404) — nothing pending

        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}

        # We wrote this "done" status ourselves on a previous poll; our own
        # write changed the ETag, which is why this poll saw "changed" again.
        # Nothing new to do — just remember the new ETag and stop.
        if request.get("status") == "done":
            return
        if request.get("action") != "upload_now":
            return

        today = datetime.now().date()
        ok, info = upload_logs([today], label="on-demand request")

        if ok:
            # Leave the request file in place but mark it done, so the Admin
            # Console can show "ready" with the exact file name instead of
            # the admin having to guess from a bare file listing. A fresh
            # "Request log now" click overwrites this with a new pending
            # request, replacing whatever was here before.
            done_body = dict(request)
            done_body.update({
                "status": "done",
                "completed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "file": os.path.basename(_path_for(today)),
            })
            try:
                data_backend.put_json(CLIENT_DATA_TOKEN, path, done_body,
                                      "request fulfilled", sha=sha)
            except Exception:
                pass
        # Either way, force a plain (non-conditional) GET next poll: our own
        # write above changed the file's ETag, and on failure we don't know
        # the current ETag either.
        self._etag = None

        if self._notify:
            self._notify(ok, info)

    def _check_file_request_once(self) -> None:
        path = _file_request_path()
        changed, raw, sha, etag = data_backend.get_file_conditional(
            CLIENT_DATA_TOKEN, path, self._file_etag)
        if not changed:
            return
        self._file_etag = etag
        if raw is None:
            return  # no request file (404) — nothing pending

        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}

        # Already handled (our own write triggered this "changed" poll) —
        # "done" or "error" are both terminal, nothing more to do.
        if request.get("status") in ("done", "error", "collected"):
            return
        if request.get("action") != "download_file":
            return

        requested_path = request.get("path", "")
        ok, message, extra = download_requested_file(requested_path)

        body = dict(request)
        body["completed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        if ok:
            body["status"] = "done"
            body.update(extra)
        else:
            body["status"] = "error"
            body["error"] = message
        try:
            data_backend.put_json(CLIENT_DATA_TOKEN, path, body,
                                  "file request handled", sha=sha)
        except Exception:
            pass
        self._file_etag = None

        if self._notify:
            label = requested_path or "(no path)"
            self._notify(ok, f"file request {label}: {message}")

    def _check_range_request_once(self) -> None:
        path = _range_request_path()
        changed, raw, sha, etag = data_backend.get_file_conditional(
            CLIENT_DATA_TOKEN, path, self._range_etag)
        if not changed:
            return
        self._range_etag = etag
        if raw is None:
            return

        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}
        if request.get("status") in ("done", "error", "collected"):
            return
        if request.get("action") != "log_range":
            return

        ok, message, extra = build_log_range(request.get("start", ""),
                                             request.get("end", ""),
                                             bool(request.get("include_windows")))
        body = dict(request)
        body["completed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        if ok:
            body["status"] = "done"
            body.update(extra)
            body["note"] = message
        else:
            body["status"] = "error"
            body["error"] = message
        try:
            data_backend.put_json(CLIENT_DATA_TOKEN, path, body,
                                  "range request handled", sha=sha)
        except Exception:
            pass
        self._range_etag = None
        if self._notify:
            self._notify(ok, f"log range request: {message}")

    def _check_update_request_once(self) -> None:
        path = _update_request_path()
        changed, raw, sha, etag = data_backend.get_file_conditional(
            CLIENT_DATA_TOKEN, path, self._update_etag)
        if not changed:
            return
        self._update_etag = etag
        if raw is None:
            return  # no request file (404) — nothing pending

        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}

        if request.get("status") == "done":
            return  # our own write; nothing new to do
        if request.get("action") != "force_update":
            return

        # check_now() does the actual check-and-apply: if a newer release
        # exists it downloads it and spawns the swapper, same as the
        # periodic 6h check or the tray's "Check for updates" click.
        started, message = updater.check_now()

        body = dict(request)
        body["status"] = "done"
        body["completed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        body["result"] = message
        try:
            data_backend.put_json(CLIENT_DATA_TOKEN, path, body,
                                  "update request handled", sha=sha)
        except Exception:
            pass
        self._update_etag = None

        if self._notify:
            self._notify(started, f"force update: {message}")

        if started and self._on_force_update:
            # An update is staged and this process needs to exit so the
            # swapper can replace the exe. Let the App decide how (it owns
            # the tray/other threads) rather than calling os._exit() here.
            self._on_force_update()

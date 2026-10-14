"""Send the Excel log(s) by email via Gmail SMTP — now an OPTIONAL channel.

The default, always-available transport is the central upload in
:mod:`activity_logger.central` (no per-machine secret needed at all). Email is
kept as an extra channel for admins who still want a copy in their inbox.

Two layers of configuration, same split as before:

* **Local secret** (``email_config.json``) — the sending Gmail account and its
  App Password. Deployed ONCE per machine by the admin. Never committed to the
  repo, never baked into the released .exe.

* **Central policy** (the ``email`` section of ``policy/policy.json`` in the
  private data repo) — enabled flag, recipient and schedule. Shares the same
  fetch/cache as the rest of the central policy (see :mod:`central`), so this
  module does not make its own network calls.

Gmail requires an *App Password* (2-step verification must be on); a normal
account password will not work for SMTP.
"""

import json
import os
import shutil
import smtplib
import ssl
import tempfile
import threading
from datetime import date, datetime

from . import central, config, scheduling
from .excel_writer import _path_for

XLSX_MIME = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Local file: secrets + SMTP server (admin-deployed, per machine).
DEFAULT_LOCAL = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "",
    "app_password": "",
    "recipient_email": "",   # fallback recipient if the policy omits one
}


# --- local secret config ----------------------------------------------------


def _local_candidate_paths() -> list[str]:
    paths = []
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        paths.append(os.path.join(program_data, "MAN_inspection",
                                  "email_config.json"))
    paths.append(os.path.join(config.base_dir(), "email_config.json"))
    return paths


def config_path() -> str:
    for path in _local_candidate_paths():
        if os.path.exists(path):
            return path
    return _local_candidate_paths()[-1]


def load_local_config() -> dict:
    for path in _local_candidate_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            break
        merged = dict(DEFAULT_LOCAL)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    return dict(DEFAULT_LOCAL)


# --- effective settings (central policy's "email" section + local secret) --


def effective_config() -> dict:
    local = load_local_config()
    policy = central.fetch_policy()["email"]
    return {
        "enabled": bool(policy.get("enabled", False)),
        "recipient_email": (policy.get("recipient_email")
                            or local.get("recipient_email", "")),
        "schedule": policy.get("schedule", "daily"),
        "send_time": policy.get("send_time", "18:00"),
        "day_of_week": policy.get("day_of_week", "mon"),
        "day_of_month": int(policy.get("day_of_month", 1) or 1),
        "sender_email": local.get("sender_email", ""),
        "app_password": local.get("app_password", ""),
        "smtp_host": local.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(local.get("smtp_port", 587) or 587),
    }


# --- sending ----------------------------------------------------------------


def _attach_file(msg, path: str) -> bool:
    from email.message import EmailMessage
    assert isinstance(msg, EmailMessage)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        shutil.copyfile(path, tmp)  # copy first: writer may be mid-save
        with open(tmp, "rb") as fh:
            payload = fh.read()
    except Exception:
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    msg.add_attachment(payload, maintype="application", subtype=XLSX_MIME,
                       filename=os.path.basename(path))
    return True


def send_logs(dates: list, label: str = "") -> tuple[bool, str]:
    from email.message import EmailMessage

    cfg = effective_config()
    if not cfg["enabled"]:
        return False, "email disabled by policy"
    if not cfg["sender_email"] or not cfg["app_password"]:
        return False, "this machine has no sender/App Password (email_config.json)"
    if not cfg["recipient_email"]:
        return False, "no recipient set (policy or email_config.json)"

    existing = [_path_for(d) for d in dates]
    existing = [p for p in existing if os.path.exists(p)]
    if not existing:
        return False, "no log files for the selected period yet"

    msg = EmailMessage()
    msg["From"] = cfg["sender_email"]
    msg["To"] = cfg["recipient_email"]
    span = label or (dates[0].isoformat() if len(dates) == 1
                     else f"{dates[0].isoformat()}..{dates[-1].isoformat()}")
    msg["Subject"] = f"MAN_inspection activity log {span}"
    msg.set_content(
        f"Attached are the activity log(s) for {span} from MAN_inspection "
        f"on this computer ({len(existing)} file(s))."
    )

    attached = sum(1 for p in existing if _attach_file(msg, p))
    if not attached:
        return False, "could not read any log file"

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(cfg["sender_email"], cfg["app_password"])
            smtp.send_message(msg)
    except Exception as exc:
        return False, f"send failed: {exc}"

    return True, f"sent {attached} file(s) to {cfg['recipient_email']}"


def send_today() -> tuple[bool, str]:
    """Manual 'Send today's log now' — always just today's single file."""
    return send_logs([date.today()])


# --- scheduler --------------------------------------------------------------


class EmailScheduler(threading.Thread):
    """Fires the configured schedule (daily/weekly/monthly) once per period.

    De-duplication uses a per-period key persisted to disk so a restart on the
    same day does not re-send.
    """

    def __init__(self, notify=None):
        super().__init__(name="EmailScheduler", daemon=True)
        self._stop = threading.Event()
        self._notify = notify
        self._last_key = self._load_state()

    def stop(self) -> None:
        self._stop.set()

    def _state_path(self) -> str:
        return os.path.join(config.data_dir(), "email_state.json")

    def _load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                return json.load(fh).get("last_key")
        except Exception:
            return None

    def _save_state(self, key) -> None:
        try:
            with open(self._state_path(), "w", encoding="utf-8") as fh:
                json.dump({"last_key": key}, fh)
        except Exception:
            pass

    def run(self) -> None:
        while not self._stop.wait(30):
            try:
                self._maybe_send()
            except Exception:
                pass

    def _maybe_send(self) -> None:
        cfg = effective_config()
        if not cfg["enabled"]:
            return
        now = datetime.now()
        key = scheduling.due_key(cfg, now)
        if key is None or key == self._last_key:
            return

        dates = scheduling.dates_for_schedule(cfg["schedule"], now.date())
        ok, info = send_logs(dates)
        if ok:
            # Only mark done on success, so a transient failure retries later.
            self._last_key = key
            self._save_state(key)
        if self._notify:
            self._notify(ok, info)

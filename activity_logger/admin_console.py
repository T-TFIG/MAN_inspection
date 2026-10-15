"""Admin Console: GUI for the admin to view machine status and edit policy.

Two tabs:

* **Dashboard** — every machine's last heartbeat (hostname, version, online /
  offline, last seen), refreshed automatically, with a way to browse and open
  a machine's uploaded Excel logs directly — no email needed at all.
* **Policy** — the settings every employee machine follows: which events to
  track, poll/idle timing, heartbeat interval, and the upload/email send
  schedules. Saved straight to the private data repo via the GitHub API, so
  every machine picks it up on its next sync (a few minutes later).

Everything here talks to the private "T-TFIG/MAN_inspection-data" repo only
(see activity_logger/config.py), using a GitHub token the admin pastes in
once. That token needs Contents: Read/write on that ONE repo — nothing else,
and never the public source-code repo.
"""

import json
import os
import queue
import tempfile
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import messagebox, ttk

from . import __version__, config, data_backend
from .central import DEFAULT_POLICY

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ONLINE_WITHIN_SEC = 15 * 60  # heartbeat default is 5 min; allow for a couple misses


# --- local admin settings (token) -------------------------------------------


def _settings_path() -> str:
    return os.path.join(config.data_dir(), "admin_settings.json")


def load_token() -> str:
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            return json.load(fh).get("github_token", "")
    except Exception:
        return ""


def save_token(token: str) -> None:
    try:
        with open(_settings_path(), "w", encoding="utf-8") as fh:
            json.dump({"github_token": token}, fh)
    except Exception:
        pass


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _relative_time(iso_ts: str) -> str:
    try:
        when = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return iso_ts or "?"
    delta = datetime.now(timezone.utc) - when
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"


def _is_online(iso_ts: str) -> bool:
    try:
        when = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() <= ONLINE_WITHIN_SEC


# --- a reusable "schedule" widget group (used for both upload & email) -----


class ScheduleFields:
    """schedule combobox + send-time + day-of-week/month, as one unit."""

    def __init__(self, parent, row: int, pad: dict):
        self.var_schedule = tk.StringVar(value="daily")
        self.var_time = tk.StringVar(value="18:00")
        self.var_dow = tk.StringVar(value="mon")
        self.var_dom = tk.IntVar(value=1)

        ttk.Label(parent, text="Schedule:").grid(row=row, column=0, sticky="e", **pad)
        self.cb_schedule = ttk.Combobox(parent, textvariable=self.var_schedule,
                                        width=10, state="readonly",
                                        values=["daily", "weekly", "monthly"])
        self.cb_schedule.grid(row=row, column=1, sticky="w", **pad)
        self.cb_schedule.bind("<<ComboboxSelected>>", lambda e: self._refresh())

        ttk.Label(parent, text="Time (HH:MM):").grid(row=row, column=2,
                                                      sticky="e", **pad)
        ttk.Entry(parent, textvariable=self.var_time, width=8).grid(
            row=row, column=3, sticky="w", **pad)

        self.lbl_dow = ttk.Label(parent, text="Day of week:")
        self.cb_dow = ttk.Combobox(parent, textvariable=self.var_dow, width=10,
                                   state="readonly", values=_WEEKDAYS)
        self.lbl_dom = ttk.Label(parent, text="Day of month:")
        self.sp_dom = ttk.Spinbox(parent, textvariable=self.var_dom, from_=1,
                                  to=28, width=6)
        self.lbl_dow.grid(row=row + 1, column=0, sticky="e", **pad)
        self.cb_dow.grid(row=row + 1, column=1, sticky="w", **pad)
        self.lbl_dom.grid(row=row + 1, column=2, sticky="e", **pad)
        self.sp_dom.grid(row=row + 1, column=3, sticky="w", **pad)
        self._refresh()

    def _refresh(self) -> None:
        sched = self.var_schedule.get()
        weekly, monthly = sched == "weekly", sched == "monthly"
        self.cb_dow.configure(state="readonly" if weekly else "disabled")
        self.sp_dom.configure(state="normal" if monthly else "disabled")

    def get(self) -> dict | None:
        try:
            hh, mm = (int(x) for x in self.var_time.get().strip().split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            messagebox.showerror("Invalid time", 'Send time must be "HH:MM" (24h).')
            return None
        return {
            "schedule": self.var_schedule.get(),
            "send_time": f"{hh:02d}:{mm:02d}",
            "day_of_week": self.var_dow.get(),
            "day_of_month": int(self.var_dom.get()),
        }

    def set(self, cfg: dict) -> None:
        self.var_schedule.set(cfg.get("schedule", "daily"))
        self.var_time.set(cfg.get("send_time", "18:00"))
        self.var_dow.set(cfg.get("day_of_week", "mon"))
        try:
            self.var_dom.set(int(cfg.get("day_of_month", 1)))
        except Exception:
            self.var_dom.set(1)
        self._refresh()


# --- GUI --------------------------------------------------------------------


class AdminConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        # Workaround for a Tk/Windows bug: on non-English system locales (e.g.
        # Thai, cp874) Tcl can decode clipboard paste using the OS ANSI
        # codepage instead of UTF-8, silently corrupting pasted ASCII secrets
        # (a GitHub token turns into garbled Thai characters). Forcing Tcl's
        # system encoding to utf-8 up front avoids that misinterpretation.
        try:
            self.tk.call("encoding", "system", "utf-8")
        except Exception:
            pass

        self.title(f"MAN_inspection — Admin Console v{__version__}")
        self.geometry("720x520")
        self._sha = None
        self._machines: dict[str, dict] = {}

        # Background threads never touch Tk widgets directly (Tk is not
        # thread-safe). They only push callables here; drained on a timer.
        self._ui_queue: "queue.Queue[callable]" = queue.Queue()
        self.after(100, self._drain_ui_queue)

        self.var_token = tk.StringVar(value=load_token())
        self.var_status = tk.StringVar(value="Ready")

        self._build()
        if self.var_token.get().strip():
            self.after(200, self._load_policy)
            self.after(400, self._refresh_dashboard)

    # -- layout --
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(top, text=f"Data repo: {config.DATA_REPO_OWNER}/{config.DATA_REPO_NAME}"
                  ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(top, text="Access token:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(top, textvariable=self.var_token, width=36, show="*").grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Button(top, text="Paste", width=6, command=self._paste_token).grid(
            row=1, column=2, sticky="w")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self._build_dashboard_tab(nb)
        self._build_policy_tab(nb)

        ttk.Label(self, textvariable=self.var_status, relief="sunken",
                  anchor="w").pack(fill="x", padx=10, pady=(0, 10))

    def _build_dashboard_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Dashboard")

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(6, 4))
        ttk.Button(btns, text="Refresh now", command=self._refresh_dashboard).pack(
            side="left", padx=4)
        ttk.Button(btns, text="Request log now",
                  command=self._request_log_now).pack(side="left", padx=4)
        ttk.Button(btns, text="View logs for selected…",
                  command=self._open_logs_for_selected).pack(side="left", padx=4)

        cols = ("hostname", "machine_id", "version", "status", "last_seen")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=14)
        headings = {"hostname": "Hostname", "machine_id": "Machine ID",
                    "version": "Version", "status": "Status",
                    "last_seen": "Last seen"}
        widths = {"hostname": 160, "machine_id": 110, "version": 70,
                  "status": 80, "last_seen": 110}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree.tag_configure("online", foreground="#1a7f37")
        self.tree.tag_configure("offline", foreground="#8a8f98")

        # Auto-refresh every 60s.
        self.after(60_000, self._auto_refresh)

    def _build_policy_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Policy")
        pad = {"padx": 8, "pady": 3}

        mon = ttk.LabelFrame(tab, text="Monitoring (what gets logged, on every machine)")
        mon.pack(fill="x", padx=4, pady=6)
        self.var_track_window = tk.BooleanVar(value=True)
        self.var_track_idle = tk.BooleanVar(value=True)
        self.var_track_usb = tk.BooleanVar(value=True)
        self.var_track_session = tk.BooleanVar(value=True)
        ttk.Checkbutton(mon, text="Track active window", variable=self.var_track_window
                        ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track idle/active", variable=self.var_track_idle
                        ).grid(row=0, column=1, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track USB", variable=self.var_track_usb
                        ).grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track lock/logon", variable=self.var_track_session
                        ).grid(row=1, column=1, sticky="w", **pad)

        self.var_poll = tk.DoubleVar(value=2.0)
        self.var_idle_threshold = tk.IntVar(value=60)
        self.var_heartbeat = tk.IntVar(value=5)
        ttk.Label(mon, text="Poll interval (sec):").grid(row=2, column=0,
                                                         sticky="e", **pad)
        ttk.Spinbox(mon, textvariable=self.var_poll, from_=1, to=60, increment=0.5,
                   width=8).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(mon, text="Idle threshold (sec):").grid(row=3, column=0,
                                                          sticky="e", **pad)
        ttk.Spinbox(mon, textvariable=self.var_idle_threshold, from_=10, to=3600,
                   width=8).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(mon, text="Heartbeat interval (min):").grid(row=3, column=2,
                                                              sticky="e", **pad)
        ttk.Spinbox(mon, textvariable=self.var_heartbeat, from_=1, to=120,
                   width=8).grid(row=3, column=3, sticky="w", **pad)

        upl = ttk.LabelFrame(tab, text="Central upload (default transport — no email needed)")
        upl.pack(fill="x", padx=4, pady=6)
        self.var_upload_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(upl, text="Enable central upload", variable=self.var_upload_enabled
                        ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.upload_sched = ScheduleFields(upl, row=1, pad=pad)

        eml = ttk.LabelFrame(tab, text="Email (optional, in addition to upload)")
        eml.pack(fill="x", padx=4, pady=6)
        self.var_email_enabled = tk.BooleanVar(value=False)
        self.var_recipient = tk.StringVar()
        ttk.Checkbutton(eml, text="Enable email", variable=self.var_email_enabled
                        ).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(eml, text="Recipient:").grid(row=0, column=1, sticky="e", **pad)
        ttk.Entry(eml, textvariable=self.var_recipient, width=28).grid(
            row=0, column=2, columnspan=2, sticky="w", **pad)
        self.email_sched = ScheduleFields(eml, row=1, pad=pad)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="Load current", command=self._load_policy).pack(
            side="left", padx=4)
        ttk.Button(btns, text="Save & Publish", command=self._save_policy).pack(
            side="left", padx=4)

    # -- status / ui queue --
    def _status(self, msg: str) -> None:
        self.var_status.set(msg)
        self.update_idletasks()

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain_ui_queue)

    def _paste_token(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception as exc:
            messagebox.showerror("Clipboard", f"Could not read clipboard: {exc}")
            return
        text = text.strip()
        if not _is_ascii(text):
            messagebox.showerror(
                "Pasted text looks corrupted",
                "The clipboard text contains non-ASCII characters, which a "
                "GitHub token never does. Copy the token again and paste once more.",
            )
            return
        self.var_token.set(text)
        self._status("Token pasted.")

    def _require_token(self) -> str | None:
        token = self.var_token.get().strip()
        if not token:
            messagebox.showwarning("Token needed", "Enter a GitHub access token first.")
            return None
        if not _is_ascii(token):
            messagebox.showerror(
                "Token looks corrupted",
                "The token field contains non-ASCII characters (a GitHub "
                "token is always plain ASCII). This usually happens when "
                "pasting on a non-English Windows locale — clear the field "
                "and use the 'Paste' button instead of Ctrl+V.",
            )
            return None
        save_token(token)
        return token

    # -- dashboard --
    def _auto_refresh(self) -> None:
        if self.var_token.get().strip():
            self._refresh_dashboard()
        self.after(60_000, self._auto_refresh)

    def _refresh_dashboard(self) -> None:
        token = self._require_token()
        if not token:
            return
        self._status("Refreshing dashboard...")
        threading.Thread(target=self._refresh_worker, args=(token,),
                         daemon=True).start()

    def _request_log_now(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a machine", "Select a row in the table first.")
            return
        token = self._require_token()
        if not token:
            return
        mid = sel[0]
        hostname = self._machines.get(mid, {}).get("hostname", mid)
        self._status(f"Requesting log from {hostname}...")
        threading.Thread(target=self._request_log_worker, args=(token, mid, hostname),
                         daemon=True).start()

    def _request_log_worker(self, token: str, mid: str, hostname: str) -> None:
        body = {
            "action": "upload_now",
            "requested_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            data_backend.put_json(token, f"requests/{mid}.json", body,
                                  f"request log from {hostname}")
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Request failed: {exc}"))
            return
        self._ui_queue.put(lambda: self._status(
            f"Requested — {hostname} should upload within ~15-20 sec "
            f"(if it's online). Refresh the dashboard shortly, or use "
            f"'View logs for selected…'."))

    def _refresh_worker(self, token: str) -> None:
        try:
            entries = data_backend.list_dir(token, "status")
            machines = {}
            for entry in entries:
                name = entry.get("name", "")
                if not name.endswith(".json"):
                    continue
                data, _ = data_backend.get_json(token, entry["path"])
                if isinstance(data, dict) and data.get("machine_id"):
                    machines[data["machine_id"]] = data
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Dashboard refresh failed: {exc}"))
            return
        self._ui_queue.put(lambda: self._apply_dashboard(machines))

    def _apply_dashboard(self, machines: dict) -> None:
        self._machines = machines
        self.tree.delete(*self.tree.get_children())
        for mid, m in sorted(machines.items(),
                             key=lambda kv: kv[1].get("hostname", "")):
            last_seen = m.get("last_seen", "")
            online = _is_online(last_seen)
            self.tree.insert(
                "", "end", iid=mid,
                values=(m.get("hostname", "?"), mid, m.get("app_version", "?"),
                       "Online" if online else "Offline", _relative_time(last_seen)),
                tags=("online" if online else "offline",),
            )
        self._status(f"Dashboard updated — {len(machines)} machine(s) known.")

    def _open_logs_for_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a machine", "Select a row in the table first.")
            return
        token = self._require_token()
        if not token:
            return
        mid = sel[0]
        self._status(f"Listing logs for {mid}...")
        threading.Thread(target=self._list_logs_worker, args=(token, mid),
                         daemon=True).start()

    def _list_logs_worker(self, token: str, mid: str) -> None:
        try:
            entries = data_backend.list_dir(token, f"logs/{mid}")
            names = sorted(e["name"] for e in entries if e["name"].endswith(".xlsx"))
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Could not list logs: {exc}"))
            return
        self._ui_queue.put(lambda: self._show_log_picker(token, mid, names))

    def _show_log_picker(self, token: str, mid: str, names: list[str]) -> None:
        self._status("Ready")
        win = tk.Toplevel(self)
        win.title(f"Logs for {mid}")
        win.geometry("360x320")
        if not names:
            ttk.Label(win, text="No uploaded logs for this machine yet.").pack(
                padx=12, pady=12)
            return
        lb = tk.Listbox(win)
        for n in names:
            lb.insert("end", n)
        lb.pack(fill="both", expand=True, padx=8, pady=8)

        def do_open():
            sel = lb.curselection()
            if not sel:
                return
            name = names[sel[0]]
            threading.Thread(target=self._download_and_open, args=(token, mid, name),
                             daemon=True).start()

        ttk.Button(win, text="Download & open", command=do_open).pack(pady=(0, 8))

    def _download_and_open(self, token: str, mid: str, name: str) -> None:
        try:
            raw, _ = data_backend.get_file(token, f"logs/{mid}/{name}")
            if raw is None:
                raise RuntimeError("file no longer exists")
            fd, path = tempfile.mkstemp(suffix=f"_{name}")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Download failed", str(exc)))
            return
        self._ui_queue.put(lambda: os.startfile(path))

    # -- policy: load --
    def _load_policy(self) -> None:
        token = self._require_token()
        if not token:
            return
        self._status("Loading current policy...")
        threading.Thread(target=self._load_policy_worker, args=(token,),
                         daemon=True).start()

    def _load_policy_worker(self, token: str) -> None:
        try:
            data, sha = data_backend.get_json(token, config.POLICY_PATH)
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Load failed: {exc}"))
            return
        policy = json.loads(json.dumps(DEFAULT_POLICY))
        if isinstance(data, dict):
            for key in ("monitoring", "upload", "email"):
                if isinstance(data.get(key), dict):
                    policy[key].update(data[key])
            if "heartbeat_interval_sec" in data:
                policy["heartbeat_interval_sec"] = data["heartbeat_interval_sec"]
        self._ui_queue.put(lambda: self._apply_policy(policy, sha))

    def _apply_policy(self, policy: dict, sha: str | None) -> None:
        self._sha = sha
        mon = policy["monitoring"]
        self.var_track_window.set(bool(mon.get("track_window", True)))
        self.var_track_idle.set(bool(mon.get("track_idle", True)))
        self.var_track_usb.set(bool(mon.get("track_usb", True)))
        self.var_track_session.set(bool(mon.get("track_session", True)))
        self.var_poll.set(float(mon.get("poll_interval_sec", 2.0)))
        self.var_idle_threshold.set(int(mon.get("idle_threshold_sec", 60)))
        self.var_heartbeat.set(max(1, int(policy.get(
            "heartbeat_interval_sec", config.HEARTBEAT_INTERVAL_SEC_DEFAULT) // 60)))

        self.var_upload_enabled.set(bool(policy["upload"].get("enabled", False)))
        self.upload_sched.set(policy["upload"])

        self.var_email_enabled.set(bool(policy["email"].get("enabled", False)))
        self.var_recipient.set(policy["email"].get("recipient_email", ""))
        self.email_sched.set(policy["email"])

        self._status("Loaded current policy.")

    # -- policy: save --
    def _collect_policy(self) -> dict | None:
        upload_sched = self.upload_sched.get()
        email_sched = self.email_sched.get()
        if upload_sched is None or email_sched is None:
            return None
        if self.var_email_enabled.get() and not self.var_recipient.get().strip():
            messagebox.showerror("Recipient needed",
                                 "Set a recipient email, or uncheck email Enable.")
            return None
        return {
            "version": 2,
            "monitoring": {
                "poll_interval_sec": float(self.var_poll.get()),
                "idle_threshold_sec": int(self.var_idle_threshold.get()),
                "track_window": self.var_track_window.get(),
                "track_idle": self.var_track_idle.get(),
                "track_usb": self.var_track_usb.get(),
                "track_session": self.var_track_session.get(),
            },
            "heartbeat_interval_sec": int(self.var_heartbeat.get()) * 60,
            "upload": {"enabled": self.var_upload_enabled.get(), **upload_sched},
            "email": {
                "enabled": self.var_email_enabled.get(),
                "recipient_email": self.var_recipient.get().strip(),
                **email_sched,
            },
        }

    def _save_policy(self) -> None:
        token = self._require_token()
        if not token:
            return
        policy = self._collect_policy()
        if policy is None:
            return
        self._status("Publishing policy...")
        threading.Thread(target=self._save_policy_worker, args=(token, policy),
                         daemon=True).start()

    def _save_policy_worker(self, token: str, policy: dict) -> None:
        try:
            new_sha = data_backend.put_json_retrying(
                token, config.POLICY_PATH, policy, "Update policy via admin console",
                cached_sha=self._sha)
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Publish failed: {exc}"))
            return
        self._sha = new_sha
        self._ui_queue.put(lambda: self._status(
            "Published. Machines will apply it within a few minutes."))


def main() -> None:
    AdminConsole().mainloop()

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

UI note: built on ttkbootstrap (a themed drop-in replacement for tkinter.ttk)
purely for a nicer, modern look — every widget here is API-compatible with
plain ttk, so the business logic below is unaffected by the theming.
"""

import json
import os
import queue
import tempfile
import threading
import tkinter as tk
from datetime import datetime, timezone

import ttkbootstrap as ttk
from ttkbootstrap.constants import *  # noqa: F403  (LEFT/RIGHT/X/BOTH/... constants)
from tkinter import filedialog, messagebox

from . import __version__, config, data_backend, updater
from .central import DEFAULT_POLICY, MAX_FILE_REQUEST_BYTES

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ONLINE_WITHIN_SEC = 15 * 60  # heartbeat default is 5 min; allow for a couple misses
# Shared, admin-editable "which human/desk is this machine" labels — lives in
# the data repo (like policy.json) so every admin console sees the same
# names, not just whoever set them.
LABELS_PATH = "labels/machine_labels.json"
_THEME = "bootstrap-light"


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


# --- local "what did I already download" record -----------------------------
# Lives only on the admin's own machine — remembers the last file downloaded
# per machine_id, so the Machine details dialog can show "already have this"
# instead of the admin having to remember/guess.


def _downloads_state_path() -> str:
    return os.path.join(config.data_dir(), "downloads_state.json")


def _load_downloads_state() -> dict:
    try:
        with open(_downloads_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _record_download(mid: str, name: str, path: str) -> None:
    state = _load_downloads_state()
    state[mid] = {
        "file": name,
        "path": path,
        "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(_downloads_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
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


class ScrollableFrame(ttk.Frame):
    """A vertically-scrolling container — keeps content reachable at any
    window size instead of letting the bottom of a tall tab get clipped."""

    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0,
                           background=self.winfo_toplevel().style.colors.bg)
        vsb = ttk.Scrollbar(self, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        vsb.pack(side=RIGHT, fill=Y)

        self.body = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=self.body, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        self.body.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_width)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))


# --- GUI --------------------------------------------------------------------


class AdminConsole(ttk.Window):
    def __init__(self):
        super().__init__(title=f"MAN_inspection — Admin Console v{__version__}",
                         theme=_THEME, size=(980, 820), minsize=(860, 700),
                         resizable=(True, True))
        # Workaround for a Tk/Windows bug: on non-English system locales (e.g.
        # Thai, cp874) Tcl can decode clipboard paste using the OS ANSI
        # codepage instead of UTF-8, silently corrupting pasted ASCII secrets
        # (a GitHub token turns into garbled Thai characters). Forcing Tcl's
        # system encoding to utf-8 up front avoids that misinterpretation.
        try:
            self.tk.call("encoding", "system", "utf-8")
        except Exception:
            pass

        try:
            updater.cleanup_stale_update_scripts()
        except Exception:
            pass

        self._sha = None
        self._machines: dict[str, dict] = {}
        self._labels: dict[str, dict] = {}
        self._labels_sha: str | None = None

        # Background threads never touch Tk widgets directly (Tk is not
        # thread-safe). They only push callables here; drained on a timer.
        self._ui_queue: "queue.Queue[callable]" = queue.Queue()
        self.after(100, self._drain_ui_queue)

        self.var_token = tk.StringVar(value=load_token())
        self.var_status = tk.StringVar(value="Ready")
        self.var_summary = tk.StringVar(value="")

        self._build()
        if self.var_token.get().strip():
            self.after(200, self._load_policy)
            self.after(400, self._refresh_dashboard)
        # Silent startup update check — same self-update mechanism as the
        # employee app, just pointed at the admin console's own asset name.
        # Skipped entirely if we were JUST relaunched by the swapper -- see
        # updater.was_just_relaunched_from_update() for why this check can't
        # rely solely on the cooldown file.
        if not updater.was_just_relaunched_from_update():
            self.after(800, self._check_for_updates_startup)

    # -- layout --
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        header = ttk.Frame(self, padding=(16, 14, 16, 6))
        header.pack(fill=X)
        title_row = ttk.Frame(header)
        title_row.pack(fill=X)
        ttk.Label(title_row, text="MAN_inspection", font=("Segoe UI", 16, "bold")
                  ).pack(side=LEFT)
        ttk.Label(title_row, text="  Admin Console", font=("Segoe UI", 16),
                  bootstyle=SECONDARY).pack(side=LEFT)
        ttk.Button(title_row, text="Check for updates", bootstyle=(SECONDARY, OUTLINE),
                  command=self._check_for_updates).pack(side=RIGHT)
        ttk.Label(header, text=f"repo: {config.DATA_REPO_OWNER}/{config.DATA_REPO_NAME}",
                  bootstyle=SECONDARY, font=("Segoe UI", 9)).pack(anchor=W, pady=(2, 0))

        token_bar = ttk.Frame(self, padding=(16, 0, 16, 10))
        token_bar.pack(fill=X)
        ttk.Label(token_bar, text="Access token:").pack(side=LEFT, padx=(0, 6))
        ttk.Entry(token_bar, textvariable=self.var_token, show="*").pack(
            side=LEFT, fill=X, expand=True, padx=(0, 6))
        ttk.Button(token_bar, text="Paste", bootstyle=(SECONDARY, OUTLINE),
                  width=8, command=self._paste_token).pack(side=LEFT)

        nb = ttk.Notebook(self, padding=(12, 8))
        nb.pack(fill=BOTH, expand=True, padx=12)
        self._build_dashboard_tab(nb)
        self._build_policy_tab(nb)

        status_bar = ttk.Frame(self, bootstyle=SECONDARY)
        status_bar.pack(fill=X, side=BOTTOM)
        ttk.Label(status_bar, textvariable=self.var_status, bootstyle=(INVERSE, SECONDARY),
                  anchor=W, padding=(10, 6)).pack(fill=X)

    def _build_dashboard_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  Dashboard  ")

        btns = ttk.Frame(tab)
        btns.pack(fill=X, pady=(0, 6))
        ttk.Button(btns, text="Refresh now", bootstyle=PRIMARY,
                  command=self._refresh_dashboard).pack(side=LEFT, padx=(0, 6))
        self.btn_request_log = ttk.Button(btns, text="Request log now", bootstyle=WARNING,
                                          command=self._request_log_now)
        self.btn_request_log.pack(side=LEFT, padx=(0, 6))
        ttk.Button(btns, text="Machine details…", bootstyle=SECONDARY,
                  command=self._open_machine_details).pack(side=LEFT, padx=(0, 6))
        ttk.Button(btns, text="Delete machine…", bootstyle=(DANGER, OUTLINE),
                  command=self._delete_machine).pack(side=LEFT)

        ttk.Label(tab, textvariable=self.var_summary, bootstyle=SECONDARY,
                  font=("Segoe UI", 10)).pack(anchor=W, pady=(0, 6))

        table_frame = ttk.Frame(tab)
        table_frame.pack(fill=BOTH, expand=True)

        cols = ("hostname", "owner", "machine_id", "version", "status", "last_seen")
        self._dashboard_columns = cols
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                                 height=16, bootstyle=PRIMARY)
        headings = {"hostname": "Hostname", "owner": "Assigned to",
                    "machine_id": "Machine ID", "version": "Version", "status": "Status",
                    "last_seen": "Last seen"}
        widths = {"hostname": 140, "owner": 120, "machine_id": 100, "version": 170,
                  "status": 80, "last_seen": 100}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")

        vsb = ttk.Scrollbar(table_frame, orient=VERTICAL, command=self.tree.yview,
                            bootstyle=ROUND)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("online", foreground="#1a7f37")
        self.tree.tag_configure("offline", foreground="#8a8f98")
        self.tree.tag_configure("outdated", foreground="#b35900")
        self.tree.bind("<Double-1>", lambda e: self._open_machine_details())

        # Auto-refresh every 60s.
        self.after(60_000, self._auto_refresh)

    def _build_policy_tab(self, nb: ttk.Notebook) -> None:
        outer = ttk.Frame(nb, padding=10)
        nb.add(outer, text="  Policy  ")
        scroller = ScrollableFrame(outer)
        scroller.pack(fill=BOTH, expand=True)
        tab = scroller.body
        pad = {"padx": 8, "pady": 3}

        mon = ttk.Labelframe(tab, text="Monitoring  —  what gets logged, on every machine",
                             bootstyle=INFO, padding=10)
        mon.pack(fill="x", pady=(0, 10))
        self.var_track_window = tk.BooleanVar(value=True)
        self.var_track_idle = tk.BooleanVar(value=True)
        self.var_track_usb = tk.BooleanVar(value=True)
        self.var_track_session = tk.BooleanVar(value=True)
        ttk.Checkbutton(mon, text="Track active window", variable=self.var_track_window,
                        bootstyle="round-toggle").grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track idle/active", variable=self.var_track_idle,
                        bootstyle="round-toggle").grid(row=0, column=1, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track USB", variable=self.var_track_usb,
                        bootstyle="round-toggle").grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(mon, text="Track lock/logon", variable=self.var_track_session,
                        bootstyle="round-toggle").grid(row=1, column=1, sticky="w", **pad)

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
        ttk.Label(mon, text="Heartbeat interval (min):").grid(row=4, column=0,
                                                              sticky="e", **pad)
        ttk.Spinbox(mon, textvariable=self.var_heartbeat, from_=1, to=120,
                   width=8).grid(row=4, column=1, sticky="w", **pad)

        upl = ttk.Labelframe(tab, text="Central upload  —  default transport, no email needed",
                             bootstyle=SUCCESS, padding=10)
        upl.pack(fill="x", pady=(0, 10))
        self.var_upload_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(upl, text="Enable central upload", variable=self.var_upload_enabled,
                        bootstyle="round-toggle").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)
        self.upload_sched = ScheduleFields(upl, row=1, pad=pad)
        self.var_retention_days = tk.IntVar(value=10)
        ttk.Label(upl, text="Keep uploaded logs for (days, 0 = forever):").grid(
            row=3, column=0, columnspan=2, sticky="e", **pad)
        ttk.Spinbox(upl, textvariable=self.var_retention_days, from_=0, to=365,
                   width=8).grid(row=3, column=2, sticky="w", **pad)

        ond = ttk.Labelframe(tab, text="On-demand requests  —  the Dashboard's "
                             "\"Request log now\" button", bootstyle=WARNING, padding=10)
        ond.pack(fill="x", pady=(0, 10))
        self.var_on_demand_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(ond, text="Enable on-demand requests",
                        variable=self.var_on_demand_enabled,
                        bootstyle="round-toggle").pack(anchor=W, padx=8, pady=(3, 0))
        ttk.Label(ond, text="Turn off to stop every machine from reacting to "
                 "\"Request log now\" (e.g. after too many were sent).",
                 bootstyle=SECONDARY, wraplength=820).pack(anchor=W, padx=8, pady=(2, 3))

        eml = ttk.Labelframe(tab, text="Email  —  optional, in addition to upload",
                             bootstyle=SECONDARY, padding=10)
        eml.pack(fill="x", pady=(0, 10))
        self.var_email_enabled = tk.BooleanVar(value=False)
        self.var_recipient = tk.StringVar()
        ttk.Checkbutton(eml, text="Enable email", variable=self.var_email_enabled,
                        bootstyle="round-toggle").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(eml, text="Recipient:").grid(row=0, column=1, sticky="e", **pad)
        ttk.Entry(eml, textvariable=self.var_recipient, width=28).grid(
            row=0, column=2, columnspan=2, sticky="w", **pad)
        self.email_sched = ScheduleFields(eml, row=1, pad=pad)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Load current", bootstyle=SECONDARY,
                  command=self._load_policy).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Save & Publish", bootstyle=SUCCESS,
                  command=self._save_policy).pack(side="left")

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

    # -- self-update (admin console's own .exe) --
    def _check_for_updates_startup(self) -> None:
        threading.Thread(target=self._update_worker, args=(False,), daemon=True).start()

    def _check_for_updates(self) -> None:
        self._status("Checking for updates...")
        threading.Thread(target=self._update_worker, args=(True,), daemon=True).start()

    def _update_worker(self, manual: bool) -> None:
        try:
            if manual:
                started, message = updater.check_now(config.ADMIN_RELEASE_ASSET_NAME)
            else:
                started = updater.check_and_update(config.ADMIN_RELEASE_ASSET_NAME)
                message = "Updating — the app will restart shortly." if started else None
        except Exception as exc:
            started, message = False, f"Update check failed: {exc}"

        if started:
            self._ui_queue.put(lambda: self._begin_update_shutdown(message))
        elif message:
            self._ui_queue.put(lambda: self._status(message))

    def _begin_update_shutdown(self, message: str) -> None:
        self._status(message)
        # Give the status message a moment to actually be seen before the
        # window disappears and the swapper takes over.
        self.after(1500, self._exit_for_update)

    def _exit_for_update(self) -> None:
        try:
            self.destroy()
        finally:
            # Hard-exit rather than letting Python unwind normally: we want
            # this process gone immediately so the swapper's "move" step (and
            # the relaunch that follows) isn't left waiting on anything.
            os._exit(0)

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
        if not self.var_on_demand_enabled.get():
            messagebox.showwarning(
                "On-demand requests are disabled",
                "Turn on \"Enable on-demand requests\" in the Policy tab and "
                "Save & Publish first.",
            )
            return
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
            "status": "pending",
            "requested_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            # put_json_retrying (not plain put_json): a previous request file
            # (e.g. a now-stale "done" one) is usually still there since we
            # stopped deleting it on completion, so this write needs the
            # current sha — fetch-and-retry handles that automatically.
            data_backend.put_json_retrying(token, f"requests/{mid}.json", body,
                                           f"request log from {hostname}")
        except Exception as exc:
            self._ui_queue.put(lambda: self._status(f"Request failed: {exc}"))
            return
        self._ui_queue.put(lambda: self._status(
            f"Requested — {hostname} should upload within ~15-20 sec "
            f"(if it's online). Open Machine details to track it."))

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

        try:
            labels, labels_sha = data_backend.get_json(token, LABELS_PATH)
            labels = labels or {}
        except Exception:
            labels, labels_sha = {}, None
        self._labels_sha = labels_sha

        # Public info, no token needed — best-effort, dashboard still works
        # without it (version column just won't flag anything as outdated).
        latest = None
        try:
            found = updater._latest_release()
            if found:
                latest = found[0]
        except Exception:
            pass

        self._ui_queue.put(lambda: self._apply_dashboard(machines, labels, latest))

    def _apply_dashboard(self, machines: dict, labels: dict, latest=None) -> None:
        self._machines = machines
        self._labels = labels
        self.tree.delete(*self.tree.get_children())
        online_count = 0
        outdated_count = 0
        for mid, m in sorted(machines.items(),
                             key=lambda kv: kv[1].get("hostname", "")):
            last_seen = m.get("last_seen", "")
            online = _is_online(last_seen)
            online_count += 1 if online else 0
            version_text = m.get("app_version", "?")
            is_outdated = False
            if latest is not None:
                current = updater._parse(version_text)
                if current is not None and current < latest:
                    version_text = f"{version_text} (v{latest} available)"
                    is_outdated = True
                    outdated_count += 1
            owner = labels.get(mid, {}).get("owner", "")
            # One tag per row (not stacked) — avoids relying on Tk's tag
            # priority rules, which are easy to get backwards.
            tag = "outdated" if is_outdated else ("online" if online else "offline")
            self.tree.insert(
                "", "end", iid=mid,
                values=(m.get("hostname", "?"), owner, mid, version_text,
                       "Online" if online else "Offline", _relative_time(last_seen)),
                tags=(tag,),
            )
        self.var_summary.set(
            f"{len(machines)} machine(s)  ·  {online_count} online  ·  "
            f"{outdated_count} outdated"
        )
        suffix = f" — latest release is v{latest}" if latest is not None else ""
        self._status(f"Dashboard updated — {len(machines)} machine(s) known.{suffix}")

    def _details_save_owner(self, win, token, mid) -> None:
        owner = win.var_owner.get().strip()
        # Feedback goes on the dialog itself (win.var_save_status), not just
        # self._status() on the main Admin Console window -- that window is
        # hidden behind this dialog for anyone actually using it, so a
        # click-and-nothing-visibly-happens Save looked like it silently
        # failed even though the write genuinely succeeded.
        win.var_save_status.set("Saving...")
        self._status("Saving assigned-to...")

        def work():
            try:
                # Fetch fresh (not the possibly-stale sha the dialog loaded
                # with) since this file is shared across every machine —
                # another admin or another dialog could have written to it
                # since we last read it.
                current, sha = data_backend.get_json(token, LABELS_PATH)
                current = current or {}
                entry = dict(current.get(mid, {}))
                entry["owner"] = owner
                entry["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                current[mid] = entry
                data_backend.put_json_retrying(token, LABELS_PATH, current,
                                               f"set owner for {mid}", cached_sha=sha)
            except Exception as exc:
                msg = f"Save failed: {exc}"
                self._ui_queue.put(lambda: self._status(msg))
                if win.winfo_exists():
                    self._ui_queue.put(lambda: win.var_save_status.set(msg))
                return
            self._ui_queue.put(lambda: self._status("Saved."))
            if win.winfo_exists():
                self._ui_queue.put(lambda: win.var_save_status.set("Saved ✓"))
            # The Dashboard table shows this same owner as a column -- keep
            # it in sync without the admin having to know to hit its
            # separate Refresh button too.
            self._ui_queue.put(lambda: self._update_dashboard_owner(mid, owner))

        threading.Thread(target=work, daemon=True).start()

    def _update_dashboard_owner(self, mid: str, owner: str) -> None:
        if mid in self._machines:
            self._machines[mid]["owner"] = owner
        if self.tree.exists(mid):
            values = list(self.tree.item(mid, "values"))
            if "owner" in self._dashboard_columns:
                idx = self._dashboard_columns.index("owner")
                if idx < len(values):
                    values[idx] = owner
                    self.tree.item(mid, values=values)

    # -- delete machine (status, requests, label, and all uploaded logs) --
    def _delete_machine(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a machine", "Select a row in the table first.")
            return
        token = self._require_token()
        if not token:
            return
        mid = sel[0]
        hostname = self._machines.get(mid, {}).get("hostname", mid)

        win = ttk.Toplevel(title="Delete machine")
        win.geometry("440x260")
        win.minsize(440, 260)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill=BOTH, expand=True)
        ttk.Label(frame, text=f"Delete {hostname}?", font=("Segoe UI", 12, "bold"),
                 bootstyle=DANGER).pack(anchor=W)
        ttk.Label(frame, wraplength=400, bootstyle=SECONDARY, text=(
            "Permanently removes this machine's status, assigned-to label, "
            "pending requests, and ALL uploaded logs from the data repo. "
            "This cannot be undone. It does NOT stop the machine itself from "
            "running or reporting back in -- it'll just show up again at its "
            "next heartbeat, as a fresh entry with no history (uninstall "
            "separately on the machine if you want it to stop entirely)."
        )).pack(anchor=W, pady=(6, 10))
        ttk.Label(frame, text=f'Type the hostname to confirm ("{hostname}"):').pack(anchor=W)
        var_confirm = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=var_confirm)
        entry.pack(fill=X, pady=(4, 10))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=X)
        btn_delete = ttk.Button(
            btn_row, text="Delete permanently", bootstyle=DANGER, state="disabled",
            command=lambda: self._delete_machine_confirmed(win, token, mid, hostname))
        btn_delete.pack(side=LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel", bootstyle=SECONDARY,
                  command=win.destroy).pack(side=LEFT)

        def _check(*_args):
            btn_delete.configure(state="normal" if var_confirm.get() == hostname else "disabled")
        var_confirm.trace_add("write", _check)
        entry.focus_set()

    def _delete_machine_confirmed(self, win, token, mid, hostname) -> None:
        win.destroy()
        self._status(f"Deleting {hostname}...")
        threading.Thread(target=self._delete_machine_worker,
                         args=(token, mid, hostname), daemon=True).start()

    def _delete_machine_worker(self, token, mid, hostname) -> None:
        errors = []

        def _try_delete(path):
            try:
                _, sha = data_backend.get_file(token, path)
                if sha:
                    data_backend.delete_file(token, path, sha,
                                             f"delete machine {hostname} ({mid})")
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        for p in (f"status/{mid}.json", f"requests/{mid}.json",
                 f"file_requests/{mid}.json", f"update_requests/{mid}.json"):
            _try_delete(p)

        try:
            entries = data_backend.list_dir(token, f"logs/{mid}")
            for e in entries:
                if e.get("name", "").startswith("."):
                    continue
                try:
                    data_backend.delete_file(token, e["path"], e["sha"],
                                             f"delete machine {hostname} ({mid}) logs")
                except Exception as exc:
                    errors.append(f"{e['path']}: {exc}")
        except Exception:
            pass  # no logs directory for this machine -- nothing to clean up

        try:
            labels, labels_sha = data_backend.get_json(token, LABELS_PATH)
            if labels and mid in labels:
                del labels[mid]
                data_backend.put_json_retrying(token, LABELS_PATH, labels,
                                               f"remove label for deleted machine {mid}",
                                               cached_sha=labels_sha)
        except Exception as exc:
            errors.append(f"label: {exc}")

        self._machines.pop(mid, None)
        self._ui_queue.put(lambda: self._after_delete_machine(mid, hostname, errors))

    def _after_delete_machine(self, mid, hostname, errors) -> None:
        if self.tree.exists(mid):
            self.tree.delete(mid)
        if errors:
            self._status(f"Deleted {hostname} with some errors")
            messagebox.showwarning(
                "Partially deleted",
                f"{hostname} was mostly removed, but some parts failed:\n\n"
                + "\n".join(errors))
        else:
            self._status(f"Deleted {hostname}.")

    # -- machine details dialog (status, pending/cancel, download, history) --
    def _open_machine_details(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a machine", "Select a row in the table first.")
            return
        token = self._require_token()
        if not token:
            return
        mid = sel[0]
        hostname = self._machines.get(mid, {}).get("hostname", mid)
        self._status(f"Loading details for {hostname}...")
        threading.Thread(target=self._machine_details_worker,
                         args=(token, mid, hostname, None), daemon=True).start()

    def _machine_details_worker(self, token: str, mid: str, hostname: str, win) -> None:
        try:
            request, _ = data_backend.get_json(token, f"requests/{mid}.json")
        except Exception:
            request = None
        try:
            file_request, _ = data_backend.get_json(token, f"file_requests/{mid}.json")
        except Exception:
            file_request = None
        try:
            update_request, _ = data_backend.get_json(token, f"update_requests/{mid}.json")
        except Exception:
            update_request = None
        try:
            labels, labels_sha = data_backend.get_json(token, LABELS_PATH)
            labels = labels or {}
        except Exception:
            labels, labels_sha = {}, None
        try:
            entries = data_backend.list_dir(token, f"logs/{mid}")
            names = sorted((e["name"] for e in entries if e["name"].endswith(".xlsx")),
                          reverse=True)
        except Exception:
            names = []
        self._ui_queue.put(lambda: self._render_machine_details(
            token, mid, hostname, request, file_request, update_request,
            labels, labels_sha, names, win))

    def _render_machine_details(self, token, mid, hostname, request, file_request,
                                update_request, labels, labels_sha, names, win=None):
        self._status("Ready")
        if win is None or not win.winfo_exists():
            win = ttk.Toplevel(title=f"Machine details — {hostname}")
            win.geometry("720x900")
            win.minsize(680, 600)
            self._build_machine_details_widgets(win, token, mid, hostname)
        win.labels_sha = labels_sha
        win.var_owner.set(labels.get(mid, {}).get("owner", ""))
        self._fill_request_section(win, token, mid, hostname, request)
        self._fill_file_request_section(win, token, mid, hostname, file_request)
        self._fill_update_section(win, token, mid, hostname, update_request)
        self._fill_logs_section(win, token, mid, names)
        self._fill_last_download_section(win, mid)

    def _build_machine_details_widgets(self, win, token: str, mid: str, hostname: str) -> None:
        # Pack the Refresh button as a footer FIRST (side=BOTTOM), before the
        # scrollable content claims the rest of the window with expand=True
        # -- packing it last instead would leave it fighting an already-full
        # cavity. This dialog has grown a section at a time (file requests,
        # force update, assigned-to, ...) without the window's fixed
        # geometry ever growing to match, so the bottom -- previously this
        # same Refresh button, packed directly on the window with no
        # scrolling at all -- could end up clipped below the visible window
        # with no indication it was still there. The scroll area keeps
        # everything above this footer reachable regardless of how tall the
        # content gets.
        ttk.Button(win, text="Refresh", bootstyle=SECONDARY, command=lambda: (
            threading.Thread(target=self._machine_details_worker,
                             args=(token, mid, hostname, win), daemon=True).start()
        )).pack(side=BOTTOM, pady=(0, 12))

        win.scroll = ScrollableFrame(win)
        win.scroll.pack(fill=BOTH, expand=True)
        body = win.scroll.body

        info = ttk.Frame(body, padding=(14, 12, 14, 6))
        info.pack(fill=X)
        ttk.Label(info, text=hostname, font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(info, text=f"machine id: {mid}", bootstyle=SECONDARY).pack(anchor=W)

        owner_row = ttk.Frame(info)
        owner_row.pack(fill=X, pady=(6, 0))
        ttk.Label(owner_row, text="Assigned to:").pack(side=LEFT, padx=(0, 6))
        win.var_owner = tk.StringVar()
        owner_entry = ttk.Entry(owner_row, textvariable=win.var_owner, width=30)
        owner_entry.pack(side=LEFT, padx=(0, 6))
        owner_entry.bind("<Return>", lambda e: self._details_save_owner(win, token, mid))
        ttk.Button(owner_row, text="Save", bootstyle=(SECONDARY, OUTLINE),
                  command=lambda: self._details_save_owner(win, token, mid)
                  ).pack(side=LEFT, padx=(0, 6))
        win.var_save_status = tk.StringVar()
        ttk.Label(owner_row, textvariable=win.var_save_status,
                 bootstyle=SUCCESS).pack(side=LEFT)
        ttk.Label(info, text="Shown on the Dashboard — helps spot who a machine "
                 "belongs to (e.g. for following up on unusual activity).",
                 bootstyle=SECONDARY, wraplength=560).pack(anchor=W, pady=(2, 0))

        win.request_frame = ttk.Labelframe(body, text="Request status", bootstyle=WARNING,
                                           padding=10)
        win.request_frame.pack(fill=X, padx=14, pady=(4, 8))

        win.file_request_frame = ttk.Labelframe(
            body, text="Request a specific file  —  by full path on that computer",
            bootstyle=DANGER, padding=10)
        win.file_request_frame.pack(fill=X, padx=14, pady=(0, 8))
        win.var_file_path = tk.StringVar()
        path_row = ttk.Frame(win.file_request_frame)
        path_row.pack(fill=X)
        ttk.Entry(path_row, textvariable=win.var_file_path).pack(
            side=LEFT, fill=X, expand=True, padx=(0, 6))
        ttk.Button(path_row, text="Request file", bootstyle=DANGER,
                  command=lambda: self._details_file_request(win, token, mid, hostname)
                  ).pack(side=LEFT)
        ttk.Label(win.file_request_frame,
                 text=f"e.g. C:\\Users\\<name>\\Documents\\report.xlsx  "
                      f"(max {MAX_FILE_REQUEST_BYTES // (1024*1024)} MB)",
                 bootstyle=SECONDARY).pack(anchor=W, pady=(4, 0))
        win.file_status_box = ttk.Frame(win.file_request_frame)
        win.file_status_box.pack(fill=X, pady=(6, 0))

        win.update_frame = ttk.Labelframe(
            body, text="Force update  —  check for and apply a new version now",
            bootstyle=INFO, padding=10)
        win.update_frame.pack(fill=X, padx=14, pady=(0, 8))
        ttk.Button(win.update_frame, text="Force update now", bootstyle=INFO,
                  command=lambda: self._details_force_update(win, token, mid, hostname)
                  ).pack(anchor=W)
        win.update_status_box = ttk.Frame(win.update_frame)
        win.update_status_box.pack(fill=X, pady=(6, 0))

        win.download_frame = ttk.Labelframe(body, text="Last downloaded on this computer",
                                            bootstyle=INFO, padding=10)
        win.download_frame.pack(fill=X, padx=14, pady=(0, 8))
        win.var_last_download = tk.StringVar(value="Not downloaded yet.")
        ttk.Label(win.download_frame, textvariable=win.var_last_download,
                 wraplength=480).pack(anchor=W)

        logs_box = ttk.Labelframe(body, text="All uploaded logs", padding=10)
        logs_box.pack(fill=X, padx=14, pady=(0, 12))
        win.listbox = tk.Listbox(logs_box, relief="flat", borderwidth=0,
                                 highlightthickness=1, height=8)
        win.listbox.pack(fill=X)
        btn_row = ttk.Frame(logs_box)
        btn_row.pack(fill=X, pady=(8, 0))
        ttk.Button(btn_row, text="Download & open", bootstyle=PRIMARY,
                  command=lambda: self._details_download(win, token, mid, open_after=True)
                  ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Download…", bootstyle=(SECONDARY, OUTLINE),
                  command=lambda: self._details_download(win, token, mid, open_after=False)
                  ).pack(side=LEFT)

    def _fill_request_section(self, win, token, mid, hostname, request) -> None:
        for child in win.request_frame.winfo_children():
            child.destroy()

        if not request:
            ttk.Label(win.request_frame,
                     text="No request sent yet.").pack(anchor=W, pady=(0, 6))
            ttk.Button(win.request_frame, text="Request log now", bootstyle=WARNING,
                      command=lambda: self._details_request(win, token, mid, hostname)
                      ).pack(anchor=W)
            return

        status = request.get("status", "pending")
        if status == "done":
            ttk.Label(win.request_frame,
                     text=f"✅ Ready — {request.get('file', '?')}\n"
                          f"completed {_relative_time(request.get('completed_at', ''))}"
                     ).pack(anchor=W, pady=(0, 6))
            row = ttk.Frame(win.request_frame)
            row.pack(anchor=W)
            ttk.Button(row, text="Download & open", bootstyle=PRIMARY,
                      command=lambda: self._details_download(
                          win, token, mid, open_after=True,
                          forced_name=request.get("file"))).pack(side=LEFT, padx=(0, 6))
            ttk.Button(row, text="Download…", bootstyle=(SECONDARY, OUTLINE),
                      command=lambda: self._details_download(
                          win, token, mid, open_after=False,
                          forced_name=request.get("file"))).pack(side=LEFT, padx=(0, 6))
            ttk.Button(row, text="Request again", bootstyle=(WARNING, OUTLINE),
                      command=lambda: self._details_request(win, token, mid, hostname)
                      ).pack(side=LEFT)
        else:
            ttk.Label(win.request_frame,
                     text=f"⏳ Pending — requested "
                          f"{_relative_time(request.get('requested_at', ''))}"
                     ).pack(anchor=W, pady=(0, 6))
            ttk.Button(win.request_frame, text="Cancel request", bootstyle=DANGER,
                      command=lambda: self._details_cancel(win, token, mid, hostname)
                      ).pack(anchor=W)

    def _fill_file_request_section(self, win, token, mid, hostname, request) -> None:
        for child in win.file_status_box.winfo_children():
            child.destroy()

        if not request:
            return  # nothing pending/ready — just the entry + button stays

        status = request.get("status", "pending")
        path = request.get("path", "?")
        if status == "done":
            ttk.Label(win.file_status_box,
                     text=f"✅ Ready — {path}\n"
                          f"completed {_relative_time(request.get('completed_at', ''))}  "
                          f"({request.get('size', 0) / 1024:.0f} KB)",
                     wraplength=480).pack(anchor=W, pady=(0, 6))
            ttk.Label(win.file_status_box,
                     text="Downloading removes it from the server (one-time pickup).",
                     bootstyle=SECONDARY, wraplength=480).pack(anchor=W, pady=(0, 4))
            row = ttk.Frame(win.file_status_box)
            row.pack(anchor=W)
            remote_name = request.get("remote_file")
            ttk.Button(row, text="Download & open", bootstyle=PRIMARY,
                      command=lambda: self._details_file_download(
                          win, token, mid, hostname, remote_name, open_after=True)
                      ).pack(side=LEFT, padx=(0, 6))
            ttk.Button(row, text="Download…", bootstyle=(SECONDARY, OUTLINE),
                      command=lambda: self._details_file_download(
                          win, token, mid, hostname, remote_name, open_after=False)
                      ).pack(side=LEFT)
        elif status == "collected":
            ttk.Label(win.file_status_box,
                     text=f"📦 Collected — {path}\ndownloaded and removed from server "
                          f"{_relative_time(request.get('collected_at', ''))}",
                     wraplength=480).pack(anchor=W, pady=(0, 6))
            ttk.Button(win.file_status_box, text="Request again",
                      bootstyle=(DANGER, OUTLINE),
                      command=lambda: self._details_file_request(win, token, mid, hostname)
                      ).pack(anchor=W)
        elif status == "error":
            ttk.Label(win.file_status_box,
                     text=f"⚠ Failed — {path}\n{request.get('error', 'unknown error')}",
                     wraplength=480, bootstyle=DANGER).pack(anchor=W)
        else:
            ttk.Label(win.file_status_box,
                     text=f"⏳ Pending — {path}\nrequested "
                          f"{_relative_time(request.get('requested_at', ''))}",
                     wraplength=480).pack(anchor=W, pady=(0, 6))
            ttk.Button(win.file_status_box, text="Cancel request", bootstyle=DANGER,
                      command=lambda: self._details_file_cancel(win, token, mid, hostname)
                      ).pack(anchor=W)

    def _details_file_request(self, win, token, mid, hostname) -> None:
        path = win.var_file_path.get().strip()
        if not path:
            messagebox.showwarning("Path needed", "Enter a full file path first.")
            return
        self._status(f"Requesting {path} from {hostname}...")

        def work():
            body = {
                "action": "download_file",
                "status": "pending",
                "path": path,
                "requested_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            try:
                data_backend.put_json_retrying(token, f"file_requests/{mid}.json", body,
                                               f"request file from {hostname}")
            except Exception as exc:
                self._ui_queue.put(lambda: self._status(f"Request failed: {exc}"))
                return
            self._machine_details_worker(token, mid, hostname, win)

        threading.Thread(target=work, daemon=True).start()

    def _details_file_cancel(self, win, token, mid, hostname) -> None:
        def work():
            try:
                _, sha = data_backend.get_file(token, f"file_requests/{mid}.json")
                if sha:
                    data_backend.delete_file(token, f"file_requests/{mid}.json", sha,
                                             "file request cancelled")
            except Exception as exc:
                self._ui_queue.put(lambda: self._status(f"Cancel failed: {exc}"))
                return
            self._ui_queue.put(lambda: self._status("File request cancelled."))
            self._machine_details_worker(token, mid, hostname, win)

        threading.Thread(target=work, daemon=True).start()

    def _fill_update_section(self, win, token, mid, hostname, request) -> None:
        for child in win.update_status_box.winfo_children():
            child.destroy()

        if not request:
            return  # nothing requested yet — just the button stays

        status = request.get("status", "pending")
        if status == "done":
            ttk.Label(win.update_status_box,
                     text=f"{request.get('result', '?')}\n"
                          f"checked {_relative_time(request.get('completed_at', ''))}",
                     wraplength=480).pack(anchor=W)
        else:
            ttk.Label(win.update_status_box,
                     text=f"⏳ Checking — requested "
                          f"{_relative_time(request.get('requested_at', ''))}",
                     wraplength=480).pack(anchor=W)

    def _details_force_update(self, win, token, mid, hostname) -> None:
        self._status(f"Requesting an update check on {hostname}...")

        def work():
            body = {
                "action": "force_update",
                "status": "pending",
                "requested_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            try:
                data_backend.put_json_retrying(token, f"update_requests/{mid}.json", body,
                                               f"force update on {hostname}")
            except Exception as exc:
                self._ui_queue.put(lambda: self._status(f"Request failed: {exc}"))
                return
            self._ui_queue.put(lambda: self._status(
                f"Requested — {hostname} will check within ~15-20 sec (if online). "
                f"If an update is found it restarts automatically; use Refresh to see "
                f"the result."))
            self._machine_details_worker(token, mid, hostname, win)

        threading.Thread(target=work, daemon=True).start()

    def _details_file_download(self, win, token, mid, hostname, remote_name, *,
                               open_after: bool) -> None:
        dest = None
        if not open_after:
            dest = filedialog.asksaveasfilename(
                parent=win, title="Save file as", initialfile=remote_name,
                filetypes=[("All files", "*.*")],
            )
            if not dest:
                return
        threading.Thread(target=self._file_download_worker,
                         args=(win, token, mid, hostname, remote_name, open_after, dest),
                         daemon=True).start()

    def _file_download_worker(self, win, token, mid, hostname, remote_name,
                              open_after, dest) -> None:
        remote_path = f"files/{mid}/{remote_name}"
        try:
            raw, sha = data_backend.get_file(token, remote_path)
            if raw is None:
                raise RuntimeError("file no longer exists on the server")
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Download failed", str(exc)))
            return

        try:
            if open_after:
                fd, local_path = tempfile.mkstemp(suffix=f"_{remote_name}")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw)
            else:
                local_path = dest
                with open(local_path, "wb") as fh:
                    fh.write(raw)
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Save failed", str(exc)))
            return
        _record_download(mid, remote_name, local_path)

        # One-shot pickup: remove it from the server now that the admin has
        # a local copy, and mark the request "collected" (not deleted) so
        # the dialog shows a clear terminal state instead of stale Download
        # buttons pointing at a file that's now gone.
        try:
            if sha:
                data_backend.delete_file(token, remote_path, sha,
                                         "requested file collected")
        except Exception:
            pass
        try:
            req_raw, req_sha = data_backend.get_file(token, f"file_requests/{mid}.json")
            if req_raw:
                body = json.loads(req_raw.decode("utf-8"))
                body["status"] = "collected"
                body["collected_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                data_backend.put_json(token, f"file_requests/{mid}.json", body,
                                      "file collected", sha=req_sha)
        except Exception:
            pass

        if open_after:
            self._ui_queue.put(lambda: os.startfile(local_path))
        else:
            self._ui_queue.put(lambda: self._status(f"Saved to {local_path}"))
        self._machine_details_worker(token, mid, hostname, win)

    def _fill_logs_section(self, win, token, mid, names) -> None:
        win.listbox.delete(0, "end")
        win.log_names = names
        for n in names:
            win.listbox.insert("end", n)
        if not names:
            win.listbox.insert("end", "(no uploaded logs yet)")
            win.listbox.configure(state="disabled")
        else:
            win.listbox.configure(state="normal")
            win.listbox.selection_set(0)  # newest first (sorted reverse)

    def _fill_last_download_section(self, win, mid: str) -> None:
        record = _load_downloads_state().get(mid)
        if record:
            win.var_last_download.set(
                f"{record['file']}\nsaved to {record['path']}\nat {record['downloaded_at']}")
        else:
            win.var_last_download.set("Not downloaded yet.")

    def _details_request(self, win, token, mid, hostname) -> None:
        self._status(f"Requesting log from {hostname}...")

        def work():
            self._request_log_worker(token, mid, hostname)
            self._machine_details_worker(token, mid, hostname, win)

        threading.Thread(target=work, daemon=True).start()

    def _details_cancel(self, win, token, mid, hostname) -> None:
        def work():
            try:
                _, sha = data_backend.get_file(token, f"requests/{mid}.json")
                if sha:
                    data_backend.delete_file(token, f"requests/{mid}.json", sha,
                                             "request cancelled")
            except Exception as exc:
                self._ui_queue.put(lambda: self._status(f"Cancel failed: {exc}"))
                return
            self._ui_queue.put(lambda: self._status("Request cancelled."))
            self._machine_details_worker(token, mid, hostname, win)

        threading.Thread(target=work, daemon=True).start()

    def _details_download(self, win, token, mid, *, open_after: bool,
                          forced_name: str | None = None,
                          remote_dir: str = "logs") -> None:
        if forced_name:
            name = forced_name
        else:
            sel = win.listbox.curselection()
            name = win.log_names[sel[0]] if sel and getattr(win, "log_names", None) else None
        if not name:
            return
        if open_after:
            threading.Thread(target=self._download_and_open,
                             args=(token, mid, name, remote_dir), daemon=True).start()
        else:
            dest = filedialog.asksaveasfilename(
                parent=win, title="Save file as", initialfile=name,
                filetypes=[("All files", "*.*")],
            )
            if not dest:
                return
            threading.Thread(target=self._download_only,
                             args=(token, mid, name, dest, remote_dir), daemon=True).start()
        # Refresh the "last downloaded" panel shortly after, once the
        # background download has had time to record itself.
        win.after(1500, lambda: self._fill_last_download_section(win, mid))

    def _fetch_log_bytes(self, token: str, mid: str, name: str,
                         remote_dir: str = "logs") -> bytes:
        raw, _ = data_backend.get_file(token, f"{remote_dir}/{mid}/{name}")
        if raw is None:
            raise RuntimeError("file no longer exists")
        return raw

    def _download_only(self, token: str, mid: str, name: str, dest: str,
                       remote_dir: str = "logs") -> None:
        try:
            raw = self._fetch_log_bytes(token, mid, name, remote_dir)
            with open(dest, "wb") as fh:
                fh.write(raw)
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Download failed", str(exc)))
            return
        _record_download(mid, name, dest)
        self._ui_queue.put(lambda: self._status(f"Saved to {dest}"))

    def _download_and_open(self, token: str, mid: str, name: str,
                           remote_dir: str = "logs") -> None:
        try:
            raw = self._fetch_log_bytes(token, mid, name, remote_dir)
            fd, path = tempfile.mkstemp(suffix=f"_{name}")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Download failed", str(exc)))
            return
        _record_download(mid, name, path)
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
        self.var_retention_days.set(int(policy["upload"].get("retention_days", 10)))

        self.var_on_demand_enabled.set(bool(policy.get("on_demand", {}).get("enabled", True)))
        self._update_request_button_state()

        self.var_email_enabled.set(bool(policy["email"].get("enabled", False)))
        self.var_recipient.set(policy["email"].get("recipient_email", ""))
        self.email_sched.set(policy["email"])

        self._status("Loaded current policy.")

    def _update_request_button_state(self) -> None:
        """Reflects the PUBLISHED on_demand.enabled value, not unsaved edits."""
        enabled = self.var_on_demand_enabled.get()
        self.btn_request_log.configure(state="normal" if enabled else "disabled")

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
            "upload": {"enabled": self.var_upload_enabled.get(),
                      "retention_days": int(self.var_retention_days.get()), **upload_sched},
            "on_demand": {"enabled": self.var_on_demand_enabled.get()},
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
        self._ui_queue.put(self._update_request_button_state)
        self._ui_queue.put(lambda: self._status(
            "Published. Machines will apply it within a few minutes."))


def main() -> None:
    AdminConsole().mainloop()

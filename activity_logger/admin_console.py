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
from .central import DEFAULT_POLICY

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ONLINE_WITHIN_SEC = 15 * 60  # heartbeat default is 5 min; allow for a couple misses
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

        self._sha = None
        self._machines: dict[str, dict] = {}

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
                  command=self._open_machine_details).pack(side=LEFT)

        ttk.Label(tab, textvariable=self.var_summary, bootstyle=SECONDARY,
                  font=("Segoe UI", 10)).pack(anchor=W, pady=(0, 6))

        table_frame = ttk.Frame(tab)
        table_frame.pack(fill=BOTH, expand=True)

        cols = ("hostname", "machine_id", "version", "status", "last_seen")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                                 height=16, bootstyle=PRIMARY)
        headings = {"hostname": "Hostname", "machine_id": "Machine ID",
                    "version": "Version", "status": "Status",
                    "last_seen": "Last seen"}
        widths = {"hostname": 160, "machine_id": 100, "version": 190,
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

        # Public info, no token needed — best-effort, dashboard still works
        # without it (version column just won't flag anything as outdated).
        latest = None
        try:
            found = updater._latest_release()
            if found:
                latest = found[0]
        except Exception:
            pass

        self._ui_queue.put(lambda: self._apply_dashboard(machines, latest))

    def _apply_dashboard(self, machines: dict, latest=None) -> None:
        self._machines = machines
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
            # One tag per row (not stacked) — avoids relying on Tk's tag
            # priority rules, which are easy to get backwards.
            tag = "outdated" if is_outdated else ("online" if online else "offline")
            self.tree.insert(
                "", "end", iid=mid,
                values=(m.get("hostname", "?"), mid, version_text,
                       "Online" if online else "Offline", _relative_time(last_seen)),
                tags=(tag,),
            )
        self.var_summary.set(
            f"{len(machines)} machine(s)  ·  {online_count} online  ·  "
            f"{outdated_count} outdated"
        )
        suffix = f" — latest release is v{latest}" if latest is not None else ""
        self._status(f"Dashboard updated — {len(machines)} machine(s) known.{suffix}")

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
            entries = data_backend.list_dir(token, f"logs/{mid}")
            names = sorted((e["name"] for e in entries if e["name"].endswith(".xlsx")),
                          reverse=True)
        except Exception:
            names = []
        self._ui_queue.put(lambda: self._render_machine_details(
            token, mid, hostname, request, names, win))

    def _render_machine_details(self, token, mid, hostname, request, names, win=None):
        self._status("Ready")
        if win is None or not win.winfo_exists():
            win = ttk.Toplevel(title=f"Machine details — {hostname}")
            win.geometry("720x680")
            win.minsize(680, 500)
            self._build_machine_details_widgets(win, token, mid, hostname)
        self._fill_request_section(win, token, mid, hostname, request)
        self._fill_logs_section(win, token, mid, names)
        self._fill_last_download_section(win, mid)

    def _build_machine_details_widgets(self, win, token: str, mid: str, hostname: str) -> None:
        info = ttk.Frame(win, padding=(14, 12, 14, 6))
        info.pack(fill=X)
        ttk.Label(info, text=hostname, font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(info, text=f"machine id: {mid}", bootstyle=SECONDARY).pack(anchor=W)

        win.request_frame = ttk.Labelframe(win, text="Request status", bootstyle=WARNING,
                                           padding=10)
        win.request_frame.pack(fill=X, padx=14, pady=(4, 8))

        win.download_frame = ttk.Labelframe(win, text="Last downloaded on this computer",
                                            bootstyle=INFO, padding=10)
        win.download_frame.pack(fill=X, padx=14, pady=(0, 8))
        win.var_last_download = tk.StringVar(value="Not downloaded yet.")
        ttk.Label(win.download_frame, textvariable=win.var_last_download,
                 wraplength=480).pack(anchor=W)

        logs_box = ttk.Labelframe(win, text="All uploaded logs", padding=10)
        logs_box.pack(fill=BOTH, expand=True, padx=14, pady=(0, 8))
        win.listbox = tk.Listbox(logs_box, relief="flat", borderwidth=0,
                                 highlightthickness=1)
        win.listbox.pack(fill=BOTH, expand=True)
        btn_row = ttk.Frame(logs_box)
        btn_row.pack(fill=X, pady=(8, 0))
        ttk.Button(btn_row, text="Download & open", bootstyle=PRIMARY,
                  command=lambda: self._details_download(win, token, mid, open_after=True)
                  ).pack(side=LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Download…", bootstyle=(SECONDARY, OUTLINE),
                  command=lambda: self._details_download(win, token, mid, open_after=False)
                  ).pack(side=LEFT)

        ttk.Button(win, text="Refresh", bootstyle=SECONDARY, command=lambda: (
            threading.Thread(target=self._machine_details_worker,
                             args=(token, mid, hostname, win), daemon=True).start()
        )).pack(pady=(0, 12))

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
                          forced_name: str | None = None) -> None:
        if forced_name:
            name = forced_name
        else:
            sel = win.listbox.curselection()
            name = win.log_names[sel[0]] if sel and getattr(win, "log_names", None) else None
        if not name:
            return
        if open_after:
            threading.Thread(target=self._download_and_open, args=(token, mid, name),
                             daemon=True).start()
        else:
            dest = filedialog.asksaveasfilename(
                parent=win, title="Save log as", initialfile=name,
                defaultextension=".xlsx",
                filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
            )
            if not dest:
                return
            threading.Thread(target=self._download_only, args=(token, mid, name, dest),
                             daemon=True).start()
        # Refresh the "last downloaded" panel shortly after, once the
        # background download has had time to record itself.
        win.after(1500, lambda: self._fill_last_download_section(win, mid))

    def _fetch_log_bytes(self, token: str, mid: str, name: str) -> bytes:
        raw, _ = data_backend.get_file(token, f"logs/{mid}/{name}")
        if raw is None:
            raise RuntimeError("file no longer exists")
        return raw

    def _download_only(self, token: str, mid: str, name: str, dest: str) -> None:
        try:
            raw = self._fetch_log_bytes(token, mid, name)
            with open(dest, "wb") as fh:
                fh.write(raw)
        except Exception as exc:
            self._ui_queue.put(lambda: messagebox.showerror("Download failed", str(exc)))
            return
        _record_download(mid, name, dest)
        self._ui_queue.put(lambda: self._status(f"Saved to {dest}"))

    def _download_and_open(self, token: str, mid: str, name: str) -> None:
        try:
            raw = self._fetch_log_bytes(token, mid, name)
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
            "upload": {"enabled": self.var_upload_enabled.get(), **upload_sched},
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

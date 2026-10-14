"""Admin Console: a small GUI for the admin to change the central email policy.

The admin opens this program, edits the settings (enabled, recipient, schedule,
time), and clicks "Save & Publish". It writes ``remote_policy.json`` straight to
the GitHub repo through the GitHub API, so every employee machine picks up the
change on its next refresh (within ~30 minutes). No hand-editing of JSON and no
local git clone required.

The admin needs a GitHub token with write access to the repo's contents, pasted
in once and stored locally (never in the repo). Everything here is admin-only:
employees run the logger, not this console.
"""

import base64
import json
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import requests

from . import __version__, config
from .emailer import DEFAULT_POLICY

_API = f"https://api.github.com/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
_POLICY_FILE = "remote_policy.json"
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --- GitHub contents API ----------------------------------------------------


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_remote_policy(token: str) -> tuple[dict, str | None]:
    """Return (policy, sha). sha is None if the file does not exist yet."""
    resp = requests.get(f"{_API}/contents/{_POLICY_FILE}", params={"ref": "main"},
                        headers=_headers(token), timeout=20)
    if resp.status_code == 404:
        return dict(DEFAULT_POLICY), None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    policy = dict(DEFAULT_POLICY)
    try:
        loaded = json.loads(content)
        if isinstance(loaded, dict):
            policy.update(loaded)
    except Exception:
        pass
    return policy, data.get("sha")


def put_remote_policy(token: str, policy: dict, sha: str | None) -> str:
    text = json.dumps(policy, indent=2) + "\n"
    body = {
        "message": "Update email policy via admin console",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(f"{_API}/contents/{_POLICY_FILE}",
                        headers=_headers(token), json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


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


# --- GUI --------------------------------------------------------------------


class AdminConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MAN_inspection — Admin Console v{__version__}")
        self.resizable(False, False)
        self._sha = None

        self.var_enabled = tk.BooleanVar(value=False)
        self.var_recipient = tk.StringVar()
        self.var_schedule = tk.StringVar(value="daily")
        self.var_time = tk.StringVar(value="18:00")
        self.var_dow = tk.StringVar(value="mon")
        self.var_dom = tk.IntVar(value=1)
        self.var_token = tk.StringVar(value=load_token())
        self.var_status = tk.StringVar(value="Ready")

        self._build()
        # Try to load the live policy on startup if a token is already saved.
        if self.var_token.get().strip():
            self.after(200, self._load)

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        policy = ttk.LabelFrame(self, text="Email policy (applies to ALL machines)")
        policy.grid(row=0, column=0, sticky="ew", padx=10, pady=8)

        ttk.Checkbutton(policy, text="Enable email sending",
                        variable=self.var_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(policy, text="Recipient email:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(policy, textvariable=self.var_recipient, width=34).grid(
            row=1, column=1, sticky="w", **pad)

        ttk.Label(policy, text="Schedule:").grid(row=2, column=0, sticky="e", **pad)
        cb = ttk.Combobox(policy, textvariable=self.var_schedule, width=12,
                          state="readonly", values=["daily", "weekly", "monthly"])
        cb.grid(row=2, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_schedule_fields())

        ttk.Label(policy, text="Send time (HH:MM):").grid(row=3, column=0, sticky="e", **pad)
        ttk.Entry(policy, textvariable=self.var_time, width=10).grid(
            row=3, column=1, sticky="w", **pad)

        self.lbl_dow = ttk.Label(policy, text="Day of week:")
        self.cb_dow = ttk.Combobox(policy, textvariable=self.var_dow, width=12,
                                   state="readonly", values=_WEEKDAYS)
        self.lbl_dom = ttk.Label(policy, text="Day of month:")
        self.sp_dom = ttk.Spinbox(policy, textvariable=self.var_dom, from_=1,
                                  to=28, width=8)
        self.lbl_dow.grid(row=4, column=0, sticky="e", **pad)
        self.cb_dow.grid(row=4, column=1, sticky="w", **pad)
        self.lbl_dom.grid(row=5, column=0, sticky="e", **pad)
        self.sp_dom.grid(row=5, column=1, sticky="w", **pad)

        gh = ttk.LabelFrame(self, text="GitHub (how changes reach the machines)")
        gh.grid(row=1, column=0, sticky="ew", padx=10, pady=8)
        ttk.Label(gh, text=f"Repo: {config.GITHUB_OWNER}/{config.GITHUB_REPO}").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(gh, text="Access token:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(gh, textvariable=self.var_token, width=34, show="*").grid(
            row=1, column=1, sticky="w", **pad)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        ttk.Button(btns, text="Load current", command=self._load).grid(
            row=0, column=0, padx=4)
        ttk.Button(btns, text="Save & Publish", command=self._save).grid(
            row=0, column=1, padx=4)

        ttk.Label(self, textvariable=self.var_status, relief="sunken",
                  anchor="w").grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))

        self._refresh_schedule_fields()

    def _refresh_schedule_fields(self) -> None:
        sched = self.var_schedule.get()
        weekly = sched == "weekly"
        monthly = sched == "monthly"
        for w in (self.lbl_dow, self.cb_dow):
            w.configure(state=("normal" if weekly else "disabled")
                        if w is self.lbl_dow else ("readonly" if weekly else "disabled"))
        for w in (self.lbl_dom, self.sp_dom):
            w.configure(state="normal" if monthly else "disabled")

    def _status(self, msg: str) -> None:
        self.var_status.set(msg)
        self.update_idletasks()

    # -- actions (network off the UI thread) --
    def _load(self) -> None:
        token = self.var_token.get().strip()
        if not token:
            messagebox.showwarning("Token needed", "Enter a GitHub access token first.")
            return
        save_token(token)
        self._status("Loading current policy...")
        threading.Thread(target=self._load_worker, args=(token,), daemon=True).start()

    def _load_worker(self, token: str) -> None:
        try:
            policy, sha = get_remote_policy(token)
        except Exception as exc:
            self.after(0, lambda: self._status(f"Load failed: {exc}"))
            return
        self.after(0, lambda: self._apply_policy(policy, sha))

    def _apply_policy(self, policy: dict, sha: str | None) -> None:
        self._sha = sha
        self.var_enabled.set(bool(policy.get("enabled", False)))
        self.var_recipient.set(policy.get("recipient_email", ""))
        self.var_schedule.set(policy.get("schedule", "daily"))
        self.var_time.set(policy.get("send_time", "18:00"))
        self.var_dow.set(policy.get("day_of_week", "mon"))
        try:
            self.var_dom.set(int(policy.get("day_of_month", 1)))
        except Exception:
            self.var_dom.set(1)
        self._refresh_schedule_fields()
        self._status("Loaded current policy.")

    def _collect(self) -> dict | None:
        time_str = self.var_time.get().strip()
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            messagebox.showerror("Invalid time", 'Send time must be "HH:MM" (24h).')
            return None
        if self.var_enabled.get() and not self.var_recipient.get().strip():
            messagebox.showerror("Recipient needed",
                                 "Set a recipient email, or uncheck Enable.")
            return None
        return {
            "enabled": self.var_enabled.get(),
            "recipient_email": self.var_recipient.get().strip(),
            "schedule": self.var_schedule.get(),
            "send_time": f"{hh:02d}:{mm:02d}",
            "day_of_week": self.var_dow.get(),
            "day_of_month": int(self.var_dom.get()),
        }

    def _save(self) -> None:
        token = self.var_token.get().strip()
        if not token:
            messagebox.showwarning("Token needed", "Enter a GitHub access token first.")
            return
        policy = self._collect()
        if policy is None:
            return
        save_token(token)
        self._status("Publishing to GitHub...")
        threading.Thread(target=self._save_worker, args=(token, policy),
                         daemon=True).start()

    def _save_worker(self, token: str, policy: dict) -> None:
        try:
            # Re-fetch sha if we never loaded, to avoid a stale-update conflict.
            sha = self._sha
            if sha is None:
                _, sha = get_remote_policy(token)
            new_sha = put_remote_policy(token, policy, sha)
        except Exception as exc:
            self.after(0, lambda: self._status(f"Publish failed: {exc}"))
            return
        self._sha = new_sha
        self.after(0, lambda: self._status(
            "Published. Machines will apply it within ~30 minutes."))


def main() -> None:
    AdminConsole().mainloop()

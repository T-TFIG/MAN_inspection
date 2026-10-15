"""Runtime settings the admin controls centrally (via policy.json).

A single mutable instance is shared between the policy-sync thread (which
writes it) and the monitor threads (which read it each tick). Plain
dataclass-with-float/bool fields are fine to read/write across threads
without extra locking: CPython's GIL makes each individual attribute
read/write atomic, and these values are only ever read as a whole at the top
of a loop iteration, not used for multi-step invariants.
"""

from dataclasses import dataclass

from . import config


@dataclass
class RuntimeSettings:
    poll_interval_sec: float = config.POLL_INTERVAL_SEC
    idle_threshold_sec: float = config.IDLE_THRESHOLD_SEC
    track_window: bool = True
    track_idle: bool = True
    track_usb: bool = True
    track_session: bool = True
    heartbeat_interval_sec: float = config.HEARTBEAT_INTERVAL_SEC_DEFAULT
    # Master switch for the admin's "Request log now" — RequestWatcher reads
    # this (set by CentralSync from policy.json) instead of fetching policy
    # itself every 15s, so toggling it costs no extra API calls.
    on_demand_requests_enabled: bool = True

    def apply_monitoring(self, monitoring: dict) -> None:
        self.poll_interval_sec = float(
            monitoring.get("poll_interval_sec", self.poll_interval_sec))
        self.idle_threshold_sec = float(
            monitoring.get("idle_threshold_sec", self.idle_threshold_sec))
        self.track_window = bool(monitoring.get("track_window", self.track_window))
        self.track_idle = bool(monitoring.get("track_idle", self.track_idle))
        self.track_usb = bool(monitoring.get("track_usb", self.track_usb))
        self.track_session = bool(monitoring.get("track_session", self.track_session))

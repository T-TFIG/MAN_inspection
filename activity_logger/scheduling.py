"""Shared "when is a periodic job due" logic for daily/weekly/monthly cadences.

Used by both the email sender and the central-upload sender so the two share
one tested implementation instead of two copies.
"""

from datetime import date, datetime, timedelta

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def dates_for_schedule(schedule: str, ref: date) -> list[date]:
    """Which day(s) a send on ``ref`` should cover, for each schedule kind."""
    if schedule == "weekly":
        start = ref - timedelta(days=ref.weekday())  # Monday of this week
        return [start + timedelta(days=i) for i in range((ref - start).days + 1)]
    if schedule == "monthly":
        return [ref.replace(day=d) for d in range(1, ref.day + 1)]
    return [ref]  # daily


def due_key(cfg: dict, now: datetime) -> str | None:
    """A unique string for "this period's send", or None if not due yet.

    ``cfg`` needs: schedule ("daily"/"weekly"/"monthly"), send_time ("HH:MM"),
    and for weekly/monthly: day_of_week / day_of_month.
    """
    try:
        hh, mm = (int(x) for x in str(cfg.get("send_time", "")).split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return None
    if now < target:
        return None

    schedule = cfg.get("schedule", "daily")
    if schedule == "weekly":
        want = WEEKDAYS.get(str(cfg.get("day_of_week", "mon")).lower()[:3], 0)
        if now.weekday() != want:
            return None
        iso = now.isocalendar()
        return f"W{iso[0]}-{iso[1]}"
    if schedule == "monthly":
        if now.day != int(cfg.get("day_of_month", 1) or 1):
            return None
        return f"M{now.year}-{now.month:02d}"
    return f"D{now.date().isoformat()}"

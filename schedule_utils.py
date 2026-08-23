"""Refresh-schedule model: presets normalized to canonical 5-field cron.

One schedule object serves the global refresh setting and per-table
overrides (Prompt 13 Part B):

    {"mode": "daily" | "weekly" | "monthly" | "interval" | "cron",
     "time": "HH:MM" | None,          # daily / weekly / monthly
     "weekdays": [0-6] | None,        # weekly; cron convention (0=Sun … 6=Sat)
     "monthly_days": [1-28, "last"] | None,
     "every_minutes": int | None,     # interval; ≥ 15
     "cron": "m h dom mon dow" | None,
     "enabled": bool}

Every mode normalizes to a LIST of cron strings (usually one — monthly with
"last" plus numeric days becomes two, because croniter's `l` cannot be mixed
into a numeric day list). Next fire = min over the list via croniter. Days
29–31 are rejected on purpose: they silently skip shorter months; 28 or
"last" always fires. Interval uses fixed-mark `*/N` semantics (N=15 fires at
:00/:15/:30/:45), the convention the established schedulers use.

Times are container-local naive, exactly like the old daily refresh_time —
the admin UI shows the computed next run so the semantics stay visible.

Pure leaf module: croniter + stdlib only. Never raises except the explicit
ValueError from validate_schedule (callers map it to HTTP 400).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from croniter import croniter

VALID_MODES = ("daily", "weekly", "monthly", "interval", "cron")
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 24 * 60
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # copy — module stays a leaf

_DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _parse_time(t) -> tuple[int, int]:
    m = _TIME_RE.match(str(t or ""))
    if not m:
        raise ValueError("time must be HH:MM (24h)")
    return int(m.group(1)), int(m.group(2))


def validate_schedule(obj) -> dict:
    """Normalize + validate a schedule dict. Raises ValueError with an
    admin-readable message; returns the canonical dict (mode-irrelevant
    fields set to None)."""
    if not isinstance(obj, dict):
        raise ValueError("Unknown schedule mode.")
    mode = str(obj.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError("Unknown schedule mode.")
    enabled = bool(obj.get("enabled", True))
    out = {"mode": mode, "time": None, "weekdays": None, "monthly_days": None,
           "every_minutes": None, "cron": None, "enabled": enabled}

    if mode in ("daily", "weekly", "monthly"):
        h, m = _parse_time(obj.get("time") or "00:00")
        out["time"] = f"{h:02d}:{m:02d}"

    if mode == "weekly":
        raw = obj.get("weekdays")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Pick at least one weekday.")
        days = []
        for d in raw:
            try:
                d = int(d)
            except Exception:
                raise ValueError("Weekdays must be integers 0-6 (0=Sunday).")
            if not 0 <= d <= 6:
                raise ValueError("Weekdays must be integers 0-6 (0=Sunday).")
            if d not in days:
                days.append(d)
        out["weekdays"] = sorted(days)

    elif mode == "monthly":
        raw = obj.get("monthly_days")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Pick at least one day of month.")
        days: list = []
        for d in raw:
            if isinstance(d, str) and d.strip().lower() == "last":
                if "last" not in days:
                    days.append("last")
                continue
            try:
                d = int(d)
            except Exception:
                raise ValueError("Monthly days must be 1-28 or \"last\".")
            if 29 <= d <= 31:
                raise ValueError("Days 29–31 are not allowed — use 28 or "
                                 "'last' so the schedule fires every month.")
            if not 1 <= d <= 28:
                raise ValueError("Monthly days must be 1-28 or \"last\".")
            if d not in days:
                days.append(d)
        out["monthly_days"] = ([d for d in sorted(x for x in days if isinstance(x, int))]
                               + (["last"] if "last" in days else []))

    elif mode == "interval":
        try:
            n = int(obj.get("every_minutes"))
        except Exception:
            raise ValueError("Interval must be a number of minutes.")
        if n < MIN_INTERVAL_MINUTES:
            raise ValueError("Interval must be at least 15 minutes.")
        if n > MAX_INTERVAL_MINUTES:
            raise ValueError("Interval must be at most 24 hours.")
        if n >= 60 and n % 60 != 0:
            raise ValueError("Interval must be under 60 minutes or a whole "
                             "number of hours.")
        out["every_minutes"] = n

    elif mode == "cron":
        expr = str(obj.get("cron") or "").strip()
        if len(expr.split()) != 5 or not croniter.is_valid(expr):
            raise ValueError("Invalid cron expression — expected 5 fields "
                             "(minute hour day month weekday).")
        out["cron"] = expr

    return out


def schedule_from_settings(stg: dict) -> dict:
    """The effective schedule from a settings dict — migration seam. A stored
    `schedule` object wins; otherwise the legacy {refresh_time,
    refresh_enabled} pair maps to daily. Garbage falls back to a disabled-
    aware daily/00:00 (never raises)."""
    stg = stg if isinstance(stg, dict) else {}
    stored = stg.get("schedule")
    if isinstance(stored, dict):
        try:
            return validate_schedule(stored)
        except ValueError:
            pass
    t = str(stg.get("refresh_time") or "00:00")
    if not _TIME_RE.match(t):
        t = "00:00"
    return {"mode": "daily", "time": t, "weekdays": None, "monthly_days": None,
            "every_minutes": None, "cron": None,
            "enabled": bool(stg.get("refresh_enabled", False))}


def to_crons(sched: dict) -> list[str]:
    """Canonical cron strings for a validated schedule."""
    mode = sched.get("mode")
    if mode == "cron":
        return [sched["cron"]]
    if mode == "interval":
        n = int(sched["every_minutes"])
        if n < 60:
            return [f"*/{n} * * * *"]
        return [f"0 */{n // 60} * * *"]
    h, m = _parse_time(sched.get("time") or "00:00")
    if mode == "daily":
        return [f"{m} {h} * * *"]
    if mode == "weekly":
        dows = ",".join(str(d) for d in sched["weekdays"])
        return [f"{m} {h} * * {dows}"]
    if mode == "monthly":
        crons = []
        nums = [d for d in sched["monthly_days"] if isinstance(d, int)]
        if nums:
            crons.append(f"{m} {h} {','.join(str(d) for d in nums)} * *")
        if "last" in sched["monthly_days"]:
            crons.append(f"{m} {h} l * *")
        return crons
    raise ValueError("Unknown schedule mode.")


def next_fire(sched: dict, after: datetime) -> Optional[datetime]:
    """Earliest fire strictly after `after`, or None when disabled/invalid.
    Never raises."""
    try:
        if not sched or not sched.get("enabled"):
            return None
        fires = [croniter(c, after).get_next(datetime) for c in to_crons(sched)]
        return min(fires) if fires else None
    except Exception:
        return None


def due_now(sched: dict, last_fired_at: Optional[datetime], now: datetime) -> bool:
    """True when a fire moment passed since last_fired_at. None last_fired_at
    is NOT due — callers initialize-and-persist it first (no storm on
    upgrade); a moment missed while down IS due once (catch-up)."""
    if last_fired_at is None:
        return False
    nxt = next_fire(sched, last_fired_at)
    return nxt is not None and nxt <= now


def describe_schedule(sched: dict) -> str:
    """Human echo for the admin UI. Never raises."""
    try:
        mode = sched.get("mode")
        t = sched.get("time") or "00:00"
        if mode == "daily":
            return f"Daily at {t}"
        if mode == "weekly":
            names = ", ".join(_DOW_NAMES[d] for d in sched.get("weekdays") or [])
            return f"Weekly on {names} at {t}"
        if mode == "monthly":
            days = sched.get("monthly_days") or []
            nums = [str(d) for d in days if isinstance(d, int)]
            parts = []
            if nums:
                parts.append("days " + ", ".join(nums))
            if "last" in days:
                parts.append("the last day")
            return f"Monthly on {' and '.join(parts)} at {t}"
        if mode == "interval":
            n = int(sched.get("every_minutes") or 0)
            if n >= 60 and n % 60 == 0:
                h = n // 60
                return f"Every {h} hour{'s' if h != 1 else ''}"
            return f"Every {n} minutes"
        if mode == "cron":
            return f"Cron: {sched.get('cron')}"
    except Exception:
        pass
    return "Custom schedule"


def preview(sched: dict, now: datetime, n: int = 3) -> list[datetime]:
    """Next n fire datetimes from `now` (UI preview). Never raises."""
    out: list[datetime] = []
    cur = now
    try:
        for _ in range(n):
            nxt = next_fire(sched, cur)
            if nxt is None:
                break
            out.append(nxt)
            cur = nxt
    except Exception:
        pass
    return out

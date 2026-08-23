"""schedule_utils — pure next-run computation, normalization, validation,
migration (Prompt 13 Part B). Fixed datetimes throughout; no I/O, no store.

The croniter `l` (last day of month) pin test is the executable guarantee
behind the requirements.txt croniter==6.0.0 comment — if a future re-pin
loses `l`, this fails loudly and monthly-"last" must switch to a
calendar.monthrange daily-check (schedule shape unchanged).
"""
from datetime import datetime

import pytest

import schedule_utils as su

BASE = datetime(2026, 7, 28, 10, 30)   # a Tuesday


def _v(obj):
    return su.validate_schedule(obj)


# --- normalization (to_crons exact strings) ----------------------------------

def test_to_crons_per_mode():
    assert su.to_crons(_v({"mode": "daily", "time": "02:30"})) == ["30 2 * * *"]
    assert su.to_crons(_v({"mode": "weekly", "time": "06:00",
                           "weekdays": [4, 1]})) == ["0 6 * * 1,4"]
    assert su.to_crons(_v({"mode": "monthly", "time": "00:15",
                           "monthly_days": [15, 1]})) == ["15 0 1,15 * *"]
    assert su.to_crons(_v({"mode": "monthly", "time": "02:30",
                           "monthly_days": ["last"]})) == ["30 2 l * *"]
    # mixed numeric + last → TWO crons ("l" never mixed into a numeric list)
    assert su.to_crons(_v({"mode": "monthly", "time": "02:30",
                           "monthly_days": [15, "last"]})) == \
        ["30 2 15 * *", "30 2 l * *"]
    assert su.to_crons(_v({"mode": "interval", "every_minutes": 15})) == \
        ["*/15 * * * *"]
    assert su.to_crons(_v({"mode": "interval", "every_minutes": 120})) == \
        ["0 */2 * * *"]
    assert su.to_crons(_v({"mode": "cron", "cron": "*/5 * * * *"})) == \
        ["*/5 * * * *"]


# --- next_fire ---------------------------------------------------------------

def test_daily_next_fire():
    s = _v({"mode": "daily", "time": "11:00"})
    assert su.next_fire(s, BASE) == datetime(2026, 7, 28, 11, 0)
    s2 = _v({"mode": "daily", "time": "09:00"})
    assert su.next_fire(s2, BASE) == datetime(2026, 7, 29, 9, 0)


def test_weekly_multi_day_across_week_boundary():
    s = _v({"mode": "weekly", "time": "06:00", "weekdays": [1, 4]})  # Mon, Thu
    fri = datetime(2026, 8, 21, 7, 0)                                # Friday
    assert su.next_fire(s, fri) == datetime(2026, 8, 24, 6, 0)       # next Mon
    wed = datetime(2026, 8, 19, 7, 0)                                # Wednesday
    assert su.next_fire(s, wed) == datetime(2026, 8, 20, 6, 0)       # Thu


def test_monthly_days_and_last_incl_february():
    s = _v({"mode": "monthly", "time": "02:30", "monthly_days": [1, 15, 28]})
    assert su.next_fire(s, datetime(2026, 2, 16, 0, 0)) == datetime(2026, 2, 28, 2, 30)
    last = _v({"mode": "monthly", "time": "02:30", "monthly_days": ["last"]})
    assert su.next_fire(last, datetime(2026, 2, 10)) == datetime(2026, 2, 28, 2, 30)
    assert su.next_fire(last, datetime(2028, 2, 10)) == datetime(2028, 2, 29, 2, 30)  # leap
    mixed = _v({"mode": "monthly", "time": "02:30", "monthly_days": [15, "last"]})
    assert su.next_fire(mixed, datetime(2026, 2, 16)) == datetime(2026, 2, 28, 2, 30)
    assert su.next_fire(mixed, datetime(2026, 2, 1)) == datetime(2026, 2, 15, 2, 30)


def test_interval_fixed_mark_semantics():
    s15 = _v({"mode": "interval", "every_minutes": 15})
    assert su.next_fire(s15, datetime(2026, 7, 28, 10, 7)) == datetime(2026, 7, 28, 10, 15)
    assert su.next_fire(s15, datetime(2026, 7, 28, 10, 45)) == datetime(2026, 7, 28, 11, 0)
    s2h = _v({"mode": "interval", "every_minutes": 120})
    assert su.next_fire(s2h, datetime(2026, 7, 28, 10, 30)) == datetime(2026, 7, 28, 12, 0)
    # fixed-mark unevenness of */45 is pinned (documented, accepted)
    s45 = _v({"mode": "interval", "every_minutes": 45})
    assert su.next_fire(s45, datetime(2026, 7, 28, 10, 46)) == datetime(2026, 7, 28, 11, 0)


def test_cron_passthrough():
    s = _v({"mode": "cron", "cron": "*/5 * * * *"})
    assert su.next_fire(s, datetime(2026, 7, 28, 10, 31)) == datetime(2026, 7, 28, 10, 35)


def test_disabled_never_fires():
    s = _v({"mode": "daily", "time": "11:00", "enabled": False})
    assert su.next_fire(s, BASE) is None
    assert su.due_now(s, BASE, datetime(2026, 8, 1)) is False


# --- due_now -----------------------------------------------------------------

def test_due_now_catchup_and_none_last():
    s = _v({"mode": "daily", "time": "06:00"})
    assert su.due_now(s, None, BASE) is False           # init-first, no storm
    # fired yesterday 06:00, container down over 06:00 → due once now
    assert su.due_now(s, datetime(2026, 7, 27, 6, 0), BASE) is True
    # already fired today 06:00 → not due again
    assert su.due_now(s, datetime(2026, 7, 28, 6, 0), BASE) is False


# --- migration ---------------------------------------------------------------

def test_migration_old_settings_map_to_daily():
    g = su.schedule_from_settings({"refresh_time": "02:30", "refresh_enabled": True})
    assert g["mode"] == "daily" and g["time"] == "02:30" and g["enabled"] is True


def test_migration_stored_schedule_wins():
    stg = {"refresh_time": "02:30", "refresh_enabled": True,
           "schedule": {"mode": "weekly", "time": "06:00", "weekdays": [1],
                        "enabled": False}}
    g = su.schedule_from_settings(stg)
    assert g["mode"] == "weekly" and g["enabled"] is False


def test_migration_garbage_falls_back_to_daily():
    g = su.schedule_from_settings({"schedule": {"mode": "nonsense"},
                                   "refresh_time": "bogus",
                                   "refresh_enabled": True})
    assert g["mode"] == "daily" and g["time"] == "00:00"
    assert su.schedule_from_settings(None)["mode"] == "daily"


# --- croniter `l` pin --------------------------------------------------------

def test_croniter_last_day_of_month_pin():
    from croniter import croniter
    it = croniter("30 2 l * *", datetime(2026, 2, 10))
    assert it.get_next(datetime) == datetime(2026, 2, 28, 2, 30)
    it2 = croniter("30 2 l * *", datetime(2028, 2, 10))
    assert it2.get_next(datetime) == datetime(2028, 2, 29, 2, 30)


# --- validation errors (exact messages — the API surfaces them verbatim) -----

@pytest.mark.parametrize("obj,msg", [
    ({"mode": "interval", "every_minutes": 14}, "Interval must be at least 15 minutes."),
    ({"mode": "interval", "every_minutes": 90},
     "Interval must be under 60 minutes or a whole number of hours."),
    ({"mode": "interval", "every_minutes": "x"}, "Interval must be a number of minutes."),
    ({"mode": "weekly", "time": "06:00", "weekdays": []}, "Pick at least one weekday."),
    ({"mode": "weekly", "time": "06:00", "weekdays": [7]},
     "Weekdays must be integers 0-6 (0=Sunday)."),
    ({"mode": "monthly", "time": "06:00", "monthly_days": []},
     "Pick at least one day of month."),
    ({"mode": "monthly", "time": "06:00", "monthly_days": [29]},
     "Days 29–31 are not allowed — use 28 or 'last' so the schedule fires every month."),
    ({"mode": "cron", "cron": "* * * *"},
     "Invalid cron expression — expected 5 fields (minute hour day month weekday)."),
    ({"mode": "cron", "cron": "99 99 * * *"},
     "Invalid cron expression — expected 5 fields (minute hour day month weekday)."),
    ({"mode": "daily", "time": "25:00"}, "time must be HH:MM (24h)"),
    ({"mode": "hourly"}, "Unknown schedule mode."),
    ("not-a-dict", "Unknown schedule mode."),
])
def test_validation_messages(obj, msg):
    with pytest.raises(ValueError) as ei:
        su.validate_schedule(obj)
    assert str(ei.value) == msg


# --- describe / preview ------------------------------------------------------

def test_describe_and_preview():
    assert su.describe_schedule(_v({"mode": "daily", "time": "02:30"})) == "Daily at 02:30"
    assert su.describe_schedule(_v({"mode": "weekly", "time": "06:00",
                                    "weekdays": [1, 4]})) == \
        "Weekly on Mon, Thu at 06:00"
    assert su.describe_schedule(_v({"mode": "monthly", "time": "00:00",
                                    "monthly_days": [1, 15, "last"]})) == \
        "Monthly on days 1, 15 and the last day at 00:00"
    assert su.describe_schedule(_v({"mode": "interval", "every_minutes": 120})) == \
        "Every 2 hours"
    assert su.describe_schedule(_v({"mode": "interval", "every_minutes": 30})) == \
        "Every 30 minutes"
    runs = su.preview(_v({"mode": "interval", "every_minutes": 15}),
                      datetime(2026, 7, 28, 10, 0), 3)
    assert runs == [datetime(2026, 7, 28, 10, 15), datetime(2026, 7, 28, 10, 30),
                    datetime(2026, 7, 28, 10, 45)]

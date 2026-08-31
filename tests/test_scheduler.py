"""The catch-up rule.

Spec section 4.5 concedes this complexity is self-inflicted by moving the
scheduler inside the container, and section 9 designates it the piece whose
failure is silent. Hence: pure functions, exhaustive table.
"""

from datetime import datetime, timedelta, timezone

import pytest

import scheduler

UTC = timezone.utc


def dt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


DAILY = scheduler.Schedule(
    job="collect", kind="daily", at="06:00", every_minutes=None, grace_minutes=120
)
EVERY_30 = scheduler.Schedule(
    job="monitor", kind="interval", at=None, every_minutes=30, grace_minutes=10
)
# 2026-08-30 is a Sunday; 2026-08-31 a Monday. ISO weekday 7 = Sunday.
SUNDAY_ONLY = scheduler.Schedule(
    job="weekly",
    kind="daily",
    at="21:00",
    every_minutes=None,
    grace_minutes=180,
    weekdays=(7,),
)


@pytest.mark.parametrize(
    "now,expected",
    [
        (dt(2026, 8, 31, 6, 0), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 1), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 5, 59), dt(2026, 8, 30, 6, 0)),
        (dt(2026, 8, 31, 23, 59), dt(2026, 8, 31, 6, 0)),
    ],
)
def test_previous_fire_daily(now, expected):
    assert scheduler.previous_fire(DAILY, now) == expected


@pytest.mark.parametrize(
    "now,expected",
    [
        (dt(2026, 8, 31, 6, 0), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 29), dt(2026, 8, 31, 6, 0)),
        (dt(2026, 8, 31, 6, 30), dt(2026, 8, 31, 6, 30)),
        (dt(2026, 8, 31, 0, 5), dt(2026, 8, 31, 0, 0)),
    ],
)
def test_previous_fire_interval(now, expected):
    assert scheduler.previous_fire(EVERY_30, now) == expected


def test_runs_when_due_and_nothing_recorded():
    d = scheduler.decide(DAILY, now=dt(2026, 8, 31, 6, 0), last_scheduled_for=None)
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_skips_when_this_fire_time_already_ran():
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 9, 0),
        last_scheduled_for=dt(2026, 8, 31, 6, 0),
    )
    assert d.action == "skip"


def test_redeploy_inside_the_grace_window_runs_the_missed_job():
    """The defining case: a deploy 30 seconds after the scheduled fire time."""
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 6, 0) + timedelta(seconds=30),
        last_scheduled_for=None,
    )
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_redeploy_outside_the_grace_window_does_not_resurrect_the_job():
    """The other defining case: a deploy at 14:00 must not produce a brief."""
    d = scheduler.decide(DAILY, now=dt(2026, 8, 31, 14, 0), last_scheduled_for=None)
    assert d.action == "missed"
    assert d.scheduled_for == dt(2026, 8, 31, 6, 0)


def test_grace_boundary_is_inclusive():
    d = scheduler.decide(DAILY, now=dt(2026, 8, 31, 8, 0), last_scheduled_for=None)
    assert d.action == "run"


def test_one_second_past_grace_is_missed():
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 8, 31, 8, 0) + timedelta(seconds=1),
        last_scheduled_for=None,
    )
    assert d.action == "missed"


def test_coalesces_to_latest_never_replays_a_backlog():
    """Three days down: exactly one run, for the most recent fire time."""
    d = scheduler.decide(
        DAILY,
        now=dt(2026, 9, 3, 6, 30),
        last_scheduled_for=dt(2026, 8, 31, 6, 0),
    )
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 9, 3, 6, 0)


def test_a_grace_shorter_than_one_tick_is_rejected_at_construction():
    """A polled scheduler observes a fire time seconds late, so a zero-minute
    grace can never be satisfied and the job is recorded missed forever. An
    earlier draft of this plan asserted that behaviour as CORRECT and would have
    shipped a permanently dead weekly report with a green suite."""
    with pytest.raises(ValueError, match="grace"):
        scheduler.Schedule(
            job="weekly",
            kind="daily",
            at="21:00",
            every_minutes=None,
            grace_minutes=0,
        )


@pytest.mark.parametrize(
    "now,expected",
    [
        # Sunday 21:00 exactly, and later that evening.
        (dt(2026, 8, 30, 21, 0), dt(2026, 8, 30, 21, 0)),
        (dt(2026, 8, 30, 23, 59), dt(2026, 8, 30, 21, 0)),
        # Sunday before the hour: the previous Sunday, a week earlier.
        (dt(2026, 8, 30, 20, 59), dt(2026, 8, 23, 21, 0)),
        # Monday and mid-week both look back to Sunday.
        (dt(2026, 8, 31, 9, 0), dt(2026, 8, 30, 21, 0)),
        (dt(2026, 9, 2, 12, 0), dt(2026, 8, 30, 21, 0)),
    ],
)
def test_previous_fire_respects_weekdays(now, expected):
    assert scheduler.previous_fire(SUNDAY_ONLY, now) == expected


def test_weekly_does_not_fire_on_a_monday():
    """The bug this test exists for: a plain daily schedule would produce seven
    weekly reports a week, each marking the paper book to market."""
    monday_evening = dt(2026, 8, 31, 21, 0)
    d = scheduler.decide(
        SUNDAY_ONLY, now=monday_evening, last_scheduled_for=dt(2026, 8, 30, 21, 0)
    )
    assert d.action == "skip"


def test_weekly_fires_on_sunday_within_grace():
    d = scheduler.decide(
        SUNDAY_ONLY, now=dt(2026, 8, 30, 21, 0, 4), last_scheduled_for=None
    )
    assert d.action == "run"
    assert d.scheduled_for == dt(2026, 8, 30, 21, 0)


def test_every_real_schedule_is_satisfiable():
    """Guards the whole class: every configured job must be able to fire at all."""
    for spec in scheduler.SCHEDULES:
        assert spec.grace_minutes * 60 > scheduler.TICK_SECONDS, (
            f"{spec.job}: grace {spec.grace_minutes}m is not longer than one "
            f"{scheduler.TICK_SECONDS}s tick, so it can never fire"
        )
        assert (spec.at is None) != (spec.every_minutes is None)


def test_the_configured_weekly_matches_the_cron_entry_it_replaces():
    """0 21 * * 0 — Sunday only. The cutover must change when nothing."""
    weekly = next(s for s in scheduler.SCHEDULES if s.job == "weekly")
    assert weekly.at == "21:00"
    assert weekly.weekdays == (7,)

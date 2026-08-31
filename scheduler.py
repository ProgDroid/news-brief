#!/usr/bin/env python3
"""Schedule arithmetic and the catch-up decision.

Pure: no I/O, no database, no subprocess, no project imports. Spec section 4.5
records that this rule exists only because the scheduler moved inside the
container, and section 9 makes it the most heavily tested logic in the system —
which is affordable exactly because it is a function of its arguments.

Two trigger kinds plus a weekday filter cover every job we have, so there is no
cron expression parser (spec section 4.2). The weekday filter is not optional
sugar: `weekly` is `0 21 * * 0`, and a plain daily schedule would produce seven
weekly reports a week.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

# The supervisor's poll interval lives here, beside the rule that depends on it:
# a grace window shorter than one tick can never be satisfied.
TICK_SECONDS = 30


@dataclass(frozen=True)
class Schedule:
    job: str
    kind: str  # "daily" | "interval"
    at: str | None  # "HH:MM", UTC, for kind="daily"
    every_minutes: int | None  # for kind="interval"
    grace_minutes: int  # how late a run may start and still happen
    weekdays: tuple[int, ...] | None = None  # ISO 1=Mon..7=Sun; None = every day

    def __post_init__(self) -> None:
        if (self.at is None) == (self.every_minutes is None):
            raise ValueError(f"{self.job}: set exactly one of at / every_minutes")
        if self.grace_minutes * 60 <= TICK_SECONDS:
            # A polled scheduler always observes a fire time some seconds late,
            # so a sub-tick grace is never satisfiable: every fire time is
            # recorded `missed` and the job never runs. Silently, and with a
            # green suite — which is exactly how it got into an earlier draft.
            raise ValueError(
                f"{self.job}: grace_minutes={self.grace_minutes} is not longer "
                f"than one {TICK_SECONDS}s tick, so this job could never fire"
            )
        if self.weekdays is not None and self.kind != "daily":
            raise ValueError(f"{self.job}: weekdays only apply to daily schedules")


@dataclass(frozen=True)
class Decision:
    action: str  # "run" | "skip" | "missed"
    scheduled_for: datetime
    reason: str


# Times and days match the cron entries being retired exactly, so the cutover
# changes when nothing:
#   0 20 * * *  submit    0  6 * * *  collect
#   0 21 * * 0  weekly    0  *  * * *  monitor
# Grace is sized to the job: monitor is cheap and hourly, so a stale catch-up is
# worthless; collect is the day's brief and worth being hours late; weekly is
# dated content, so its grace is wide enough to survive a restart but stops
# before midnight so it can never arrive on the wrong day.
SCHEDULES: tuple[Schedule, ...] = (
    Schedule("submit", "daily", "20:00", None, grace_minutes=60),
    Schedule("collect", "daily", "06:00", None, grace_minutes=120),
    Schedule("weekly", "daily", "21:00", None, grace_minutes=180, weekdays=(7,)),
    Schedule("monitor", "interval", None, 60, grace_minutes=15),
)


def previous_fire(spec: Schedule, now: datetime) -> datetime:
    """The most recent moment this schedule was due, at or before `now`."""
    if spec.kind == "daily":
        hh, mm = (int(x) for x in spec.at.split(":"))
        candidate = datetime.combine(now.date(), time(hh, mm), tzinfo=now.tzinfo)
        if candidate > now:
            candidate -= timedelta(days=1)
        if not spec.weekdays:
            return candidate
        # Walk back to the most recent allowed weekday — at most seven steps.
        for _ in range(7):
            if candidate.isoweekday() in spec.weekdays:
                return candidate
            candidate -= timedelta(days=1)
        raise ValueError(f"{spec.job}: weekdays={spec.weekdays} matches no day")

    if spec.kind == "interval":
        midnight = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
        elapsed = int((now - midnight).total_seconds() // 60)
        return midnight + timedelta(minutes=elapsed - (elapsed % spec.every_minutes))

    raise ValueError(f"unknown schedule kind: {spec.kind}")


def decide(
    spec: Schedule, now: datetime, last_scheduled_for: datetime | None
) -> Decision:
    """Run at most once for the latest due fire time; never replay a backlog.

    `last_scheduled_for` is the greatest scheduled_for already recorded for this
    job in job_runs. Only the most recent fire time is ever considered, which is
    APScheduler's `coalesce="latest"` expressed as three lines rather than a
    dependency (spec section 4.6).
    """
    due = previous_fire(spec, now)

    if last_scheduled_for is not None and last_scheduled_for >= due:
        return Decision("skip", due, "already recorded for this fire time")

    lateness = now - due
    if lateness <= timedelta(minutes=spec.grace_minutes):
        return Decision("run", due, f"due {int(lateness.total_seconds())}s ago")

    return Decision(
        "missed",
        due,
        f"missed_start_deadline: {int(lateness.total_seconds())}s late, "
        f"grace is {spec.grace_minutes}m",
    )

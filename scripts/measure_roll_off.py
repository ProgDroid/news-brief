#!/usr/bin/env python3
"""Per-feed roll-off, and the poll intervals it justifies (news-brief-b42.2).

Read-only and model-free: it SELECTs, computes, prints, and writes nothing. Safe
against production.

Cadence is not free. 26 feeds at five-minute intervals is ~7500 requests a day
on one user agent, brief.py already documents Nitter 429ing at the current
cadence with adjacent feeds silently dropping, and volume concentrates in about
four mostly state-funded wires -- so throughput is the wrong target and a single
global interval is the wrong instrument. This measures what each feed actually
does and proposes an interval per feed against a request budget.

WHAT THIS CAN AND CANNOT SEE, because it decides how to read every number below:

`feed_sightings` records an item that was SEEN. An item published and gone
between two polls leaves no row anywhere -- so the current 30-minute cadence
bounds what is observable about the cadence, and every loss figure here is a
FLOOR, never a ceiling. A feed reported as losing nothing has been observed not
to lose anything AT THIS INTERVAL; that is a different claim from "it loses
nothing", and only the first one is evidence.

Three metrics per feed:

  window       items whose [first_seen_at, last_seen_at] spans a poll. The
               table reports the median rather than the mean: a wire that
               spikes is exactly the case an interval has to survive.
  overlap      the share of one poll's items still present at the next. This is
               what the interval is set against -- high overlap means the
               interval sits comfortably inside the window, and overlap falling
               towards zero is direct evidence of loss.
  singles      items seen in exactly one poll. They survived less than two
               intervals, so they are the population that would vanish first if
               the interval grew: the closest observable proxy for what cannot
               be seen at all.

Two denominators are load-bearing, and both are the same mistake in different
clothes -- counting a poll that never happened as a poll that saw nothing:

  * Only `failure IS NULL` polls are observations. A feed 403ing for a day
    otherwise reads as its entire window turning over at once, which is the
    large, clean, fictitious signal `capture.rolled_off`'s docstring warns
    about.
  * Only pairs of successful polls within 1.5x the nominal interval are
    compared. Two successes either side of an outage measure a two-hour
    interval, and using that to justify a thirty-minute one is evidence from
    the wrong experiment.

Run it on the host, where the data is:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/measure_roll_off.py

(The ENTRYPOINT takes a MODE, so the override is what lets a script run at all --
same invocation as scripts/score_comprehension.py, which the Dockerfile documents
at the COPY that puts this directory in the image.)
"""

import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

# Run as a PATH, `scripts/` is sys.path[0] and the repo root is nowhere -- so
# `import db` fails in the image while every test passes, because pytest imports
# this as `scripts.measure_roll_off` with the root already on the path. Both
# older scripts carry this shim; `tests/test_packaging.py` now holds it there.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402  (path shim above must run first)


class Sighting(NamedTuple):
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime


class Poll(NamedTuple):
    polled_at: datetime
    failure: str | None
    entries_seen: int


# A pair spanning more than this multiple of the schedule measures a different
# interval than the one being justified. 1.5 admits ordinary jitter and the
# occasional late pass while excluding a skipped one.
MAX_GAP_FACTOR = 1.5

# The window should be sampled several times over, not just before it empties:
# an interval equal to the turnover time keeps items only if every poll lands
# perfectly. Four is the smallest factor that leaves room for a late pass and a
# publishing burst at once.
SAFETY = 4

INTERVAL_LO = 10
INTERVAL_HI = 240

# One user agent's daily request budget across every feed. Below what the
# 30-minute global cadence spends today (26 feeds x 48 = 1248), because the
# point of per-feed intervals is to spend it where it buys something.
BUDGET_PER_DAY = 1200

NOMINAL_MINUTES = 30


def window_at(sightings: list[Sighting], at: datetime) -> set[str]:
    """The feed's window as it stood at `at`.

    `feed_sightings` stores one row per item with a first and last sighting
    rather than per-poll membership, so membership is reconstructed: an item was
    in the window when the poll falls within that closed interval. Both ends
    inclusive -- the endpoints ARE polls at which the item was seen.
    """
    return {
        s.content_hash for s in sightings if s.first_seen_at <= at <= s.last_seen_at
    }


def overlap(sightings: list[Sighting], before: datetime, after: datetime):
    """The share of `before`'s window still present at `after`, or None.

    None rather than 0.0 for an empty starting window. Zero out of zero renders
    as "everything rolled off", which is the strongest claim available, made
    from no evidence at all -- and it would then earn the feed the shortest
    interval in the table.
    """
    was = window_at(sightings, before)
    if not was:
        return None
    return len(was & window_at(sightings, after)) / len(was)


def poll_pairs(polls: list[Poll], nominal_minutes: int) -> list[tuple[Poll, Poll]]:
    """Consecutive SUCCESSFUL polls close enough together to compare."""
    ok = sorted((p for p in polls if p.failure is None), key=lambda p: p.polled_at)
    limit = timedelta(minutes=nominal_minutes * MAX_GAP_FACTOR)
    return [(a, b) for a, b in zip(ok, ok[1:]) if b.polled_at - a.polled_at <= limit]


def single_sighting_fraction(sightings: list[Sighting], newest_ok: datetime):
    """Share of items seen exactly once, over items whose residence has ENDED.

    An item last seen at the newest successful poll is right-censored: it is
    still in the window and its lifetime is unfinished. Counting it as a single
    sighting inflates the fraction with items that simply have not left yet, and
    does so worst on the busiest feeds -- the ones the interval is for.
    """
    closed = [s for s in sightings if s.last_seen_at < newest_ok]
    if not closed:
        return None
    return sum(1 for s in closed if s.first_seen_at == s.last_seen_at) / len(closed)


def full_turnover_minutes(overlap_value, gap_minutes: float):
    """How long the whole window takes to be replaced, at the observed rate.

    None when nothing rolled off. An unchanged window sets no upper bound on the
    interval, so any number here would be inventing the one fact the measurement
    exists to supply.
    """
    if overlap_value is None or overlap_value >= 1.0:
        return None
    return gap_minutes / (1.0 - overlap_value)


def proposed_interval_minutes(
    turnover_minutes, safety: int = SAFETY, lo: int = INTERVAL_LO, hi: int = INTERVAL_HI
):
    """The interval a turnover time justifies, clamped at both ends."""
    if turnover_minutes is None:
        return None
    return int(min(hi, max(lo, turnover_minutes / safety)))


def fit_budget(intervals: dict[str, int], budget_per_day: int) -> dict[str, int]:
    """Slow every feed proportionally until the total fits the budget.

    Proportional on purpose. Dropping the feeds that do not fit would be the
    silent-loss failure capture exists to make impossible, and it would drop
    them by cadence -- which is to say, drop the busiest.
    """
    per_day = sum(1440 / minutes for minutes in intervals.values())
    if not intervals or per_day <= budget_per_day:
        return dict(intervals)
    factor = per_day / budget_per_day
    return {name: int(round(minutes * factor)) for name, minutes in intervals.items()}


def feed_stats(
    polls: list[Poll], sightings: list[Sighting], nominal_minutes: int
) -> dict:
    """Everything the table prints for one feed. UNKNOWN survives as None."""
    pairs = poll_pairs(polls, nominal_minutes)
    overlaps, gaps = [], []
    for before, after in pairs:
        measured = overlap(sightings, before.polled_at, after.polled_at)
        if measured is None:
            continue
        overlaps.append(measured)
        gaps.append((after.polled_at - before.polled_at).total_seconds() / 60)

    ok = [p for p in polls if p.failure is None]
    newest_ok = max((p.polled_at for p in ok), default=None)
    median_overlap = statistics.median(overlaps) if overlaps else None
    turnover = full_turnover_minutes(
        median_overlap, statistics.median(gaps) if gaps else nominal_minutes
    )
    return {
        "polls": len(polls),
        "polls_ok": len(ok),
        "pairs": len(pairs),
        "median_window": (
            statistics.median([len(window_at(sightings, p.polled_at)) for p in ok])
            if ok
            else None
        ),
        "median_overlap": median_overlap,
        "singles": (
            single_sighting_fraction(sightings, newest_ok) if newest_ok else None
        ),
        "turnover_minutes": turnover,
        "proposed_minutes": proposed_interval_minutes(turnover),
    }


def _load(conn):
    polls: dict[str, list[Poll]] = {}
    for name, polled_at, failure, entries in conn.execute(
        "SELECT source_name, polled_at, failure, entries_seen FROM feed_polls"
    ).fetchall():
        polls.setdefault(name, []).append(Poll(polled_at, failure, entries))

    sightings: dict[str, list[Sighting]] = {}
    for name, content_hash, first, last in conn.execute(
        "SELECT source_name, content_hash, first_seen_at, last_seen_at "
        "FROM feed_sightings"
    ).fetchall():
        sightings.setdefault(name, []).append(Sighting(content_hash, first, last))
    return polls, sightings


def _show(value, spec: str = "") -> str:
    """UNKNOWN prints as a dash. A blank column reads as a small number."""
    return "—" if value is None else format(value, spec)


def main() -> int:
    if not db.is_configured():
        print("No DATABASE_URL: this reads the host's capture telemetry.")
        return 2

    with db.connect() as conn:
        polls, sightings = _load(conn)

    if not polls:
        print(
            "No feed_polls rows at all. That is a finding about capture's "
            "LIVENESS, not about roll-off, and nothing below would mean "
            "anything. Check `capture_runs` and CAPTURE_ENABLED first."
        )
        return 1

    every = [p.polled_at for rows in polls.values() for p in rows]
    span_days = (max(every) - min(every)).total_seconds() / 86400
    expected = len(polls) * (1440 / NOMINAL_MINUTES) * span_days
    print(
        f"{len(every)} polls over {len(polls)} feeds, "
        f"{min(every):%Y-%m-%d %H:%M} to {max(every):%Y-%m-%d %H:%M} UTC "
        f"({span_days:.1f} days).\n"
        f"At {NOMINAL_MINUTES}-minute intervals that predicts ~{expected:.0f}. "
        f"A large shortfall is a liveness finding, and this table would be "
        f"measuring an outage rather than a window.\n"
    )

    stats = {
        name: feed_stats(rows, sightings.get(name, []), NOMINAL_MINUTES)
        for name, rows in sorted(polls.items())
    }

    print(
        f"{'feed':<34}{'ok':>6}{'pairs':>7}{'window':>8}{'overlap':>9}"
        f"{'singles':>9}{'turnover':>10}{'propose':>9}"
    )
    for name, s in sorted(
        stats.items(),
        key=lambda kv: (
            kv[1]["proposed_minutes"] is None,
            kv[1]["proposed_minutes"] or 0,
        ),
    ):
        print(
            f"{name[:33]:<34}{s['polls_ok']:>6}{s['pairs']:>7}"
            f"{_show(s['median_window'], '.0f'):>8}"
            f"{_show(s['median_overlap'], '.2f'):>9}"
            f"{_show(s['singles'], '.2f'):>9}"
            f"{_show(s['turnover_minutes'], '.0f'):>10}"
            f"{_show(s['proposed_minutes'], 'd'):>9}"
        )

    known = {
        name: s["proposed_minutes"]
        for name, s in stats.items()
        if s["proposed_minutes"] is not None
    }
    unknown = sorted(set(stats) - set(known))
    raw_per_day = sum(1440 / m for m in known.values()) if known else 0
    fitted = fit_budget(known, BUDGET_PER_DAY)
    print(
        f"\n{len(known)} feed(s) measured, {len(unknown)} UNKNOWN "
        f"(no usable pair, or nothing observed rolling off).\n"
        f"Unconstrained that is {raw_per_day:.0f} requests/day against a budget "
        f"of {BUDGET_PER_DAY}."
    )
    if fitted != known:
        print("Over budget, so every measured feed slows proportionally:")
        for name in sorted(fitted, key=lambda n: fitted[n]):
            print(f"  {name[:40]:<42}{known[name]:>5} -> {fitted[name]:>5} min")
    if unknown:
        print(
            "\nUNKNOWN feeds keep the current interval. They have not been "
            "shown to be slow; they have not been measured, and the two want "
            "opposite responses:\n  " + "\n  ".join(unknown)
        )
    print(
        "\nEvery loss figure above is a FLOOR. An item published and gone "
        f"between two polls leaves no row, so a feed that looks lossless is "
        f"lossless AT {NOMINAL_MINUTES} MINUTES and unmeasured below that."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

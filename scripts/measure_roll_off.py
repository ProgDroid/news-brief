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

Per feed:

  src          gnews or native. Section 3 makes this the comparison b42.2 owes:
               for the 8 capped proxies the loss is truncation INSIDE one poll
               at the 100-entry cap, not roll-off between polls. A feed the
               list does not carry reads '?', never 'native'.
  window       items whose [first_seen_at, last_seen_at] spans a poll, median.
  entries      what the poll ACTUALLY returned (feed_polls.entries_seen).
  flick        window / entries. Above 1.00 means the reconstruction is
               interpolating over absences -- "seen, gone, came back" looks
               identical to "continuously present" -- and no poll interval
               fixes flicker.
  ovl / worst  median and WORST pairwise overlap. Reported together because
               loss lives in the tail: a burst that empties a window is one
               pair in two hundred, and the median erases it.
  singles      items seen in exactly one poll. They survived less than two
               intervals, so they are the population that would vanish first if
               the interval grew: the closest observable proxy for what cannot
               be seen at all.
  dep/turnover departures over the whole span, and the turnover they imply.

THE INTERVAL IS SET FROM THE RATE, NOT FROM A PAIR. The first host run (2026-09-08,
4.2 days) showed why: for a feed losing an item every few hours, most 30-minute
pairs see nothing, so a median pairwise overlap pins at 1.00 and the turnover
comes back UNKNOWN for exactly the feeds where slowing is safest -- 17 of 26
landed there. Counting departures across the span uses every observation.

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

import math
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


def departures(sightings: list[Sighting], newest_ok: datetime) -> int:
    """Items whose residence ENDED inside the observed span.

    Same censoring rule as the singles fraction: an item last seen at the newest
    successful poll is still in the window and has not departed. Counting it
    would inflate the departure rate with items whose lifetime is unfinished,
    and so propose a shorter interval than the evidence supports.
    """
    return sum(1 for s in sightings if s.last_seen_at < newest_ok)


def turnover_from_rate(window, departed: int, span_minutes: float):
    """How long the window takes to be replaced, counted over the WHOLE span.

    This is the estimator the first host run showed was needed. Median pairwise
    overlap answers "what does a typical 30 minutes look like", and for a feed
    that loses an item every few hours the answer is "nothing happened" — so the
    median pins at 1.00 and the turnover comes back UNKNOWN for exactly the
    feeds where slowing is safest. 17 of 26 feeds landed there. Counting
    departures across 4.2 days uses every observation instead of summarising
    each pair and discarding the tail.

    Still None when nothing departed: zero departures is an infinite turnover,
    which would render as the safest feed on the board rather than the least
    measured one.
    """
    if not window or departed <= 0 or span_minutes <= 0:
        return None
    return window * span_minutes / departed


def overlap_floor(overlaps: list[float], pct: float):
    """The pct-th percentile overlap by nearest rank; `pct=0` is the worst pair.

    Loss happens in the tail. A publishing burst that empties a window shows up
    in one pair out of two hundred, and a median erases it completely — so the
    floor is reported beside the median rather than instead of it, and the two
    disagreeing is the signal.
    """
    if not overlaps:
        return None
    ordered = sorted(overlaps)
    if pct <= 0:
        return ordered[0]
    return ordered[max(1, math.ceil(pct / 100 * len(ordered))) - 1]


def flicker_ratio(window, entries_seen):
    """Reconstructed window against what the poll ACTUALLY returned.

    `window_at` interpolates: an item seen at poll 1 and poll 50 counts as
    present throughout, so "seen, gone, came back" is indistinguishable from
    "continuously present". `feed_polls.entries_seen` does not interpolate, so
    the ratio measures that inflation directly. Above 1.0 means items are
    flickering — which no poll interval fixes, and which on a Google News proxy
    is more likely ranking churn than roll-off.

    BELOW 1.0 is the other direction and equally worth reading: the
    reconstruction sees FEWER items than the poll returned, so entries are
    arriving that never became sightings. That is a capture-side gap, not a
    cadence one, and it means every window figure for that feed understates.

    None on zero entries: a feed that never returned anything is unmeasured, not
    perfectly faithful.
    """
    if not entries_seen or window is None:
        return None
    return window / entries_seen


PROXY_HOST = "news.google.com"


def is_proxy(url: str) -> bool:
    """A Google News search feed rather than a publisher's own.

    Spec section 3 makes this the comparison `b42.2` owes: for the 8 capped
    proxies the loss is truncation INSIDE a single poll at the 100-entry cap,
    not roll-off between polls. Different mode, different remedy — a narrower
    query or a native feed, never a shorter interval.
    """
    return PROXY_HOST in url.lower()


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
    median_window = (
        statistics.median([len(window_at(sightings, p.polled_at)) for p in ok])
        if ok
        else None
    )
    span_minutes = (
        (newest_ok - min(p.polled_at for p in ok)).total_seconds() / 60 if ok else 0
    )
    departed = departures(sightings, newest_ok) if newest_ok else 0
    # The rate, not the pair, decides the interval — see turnover_from_rate. The
    # pairwise number is kept beside it because the two disagreeing is itself a
    # finding: a feed with slow turnover and a collapsing worst pair is bursty.
    turnover_rate = turnover_from_rate(median_window, departed, span_minutes)
    return {
        "polls": len(polls),
        "polls_ok": len(ok),
        "pairs": len(pairs),
        "median_window": median_window,
        "median_entries": (
            statistics.median([p.entries_seen for p in ok]) if ok else None
        ),
        "flicker": flicker_ratio(
            median_window,
            statistics.median([p.entries_seen for p in ok]) if ok else None,
        ),
        "median_overlap": median_overlap,
        "worst_overlap": overlap_floor(overlaps, 0),
        "floor_overlap": overlap_floor(overlaps, 5),
        "singles": (
            single_sighting_fraction(sightings, newest_ok) if newest_ok else None
        ),
        "departed": departed,
        "turnover_minutes": full_turnover_minutes(
            median_overlap, statistics.median(gaps) if gaps else nominal_minutes
        ),
        "turnover_rate_minutes": turnover_rate,
        "proposed_minutes": proposed_interval_minutes(turnover_rate),
    }


def feed_kinds(sources: list[dict]) -> dict[str, str]:
    """source_name -> 'gnews' or 'native', from the feed list itself.

    Takes the list rather than fetching it, so the classification is testable
    without dragging in `brief`. A name the list does not carry is simply
    ABSENT here and renders as '?' — never 'native', because the two are
    different facts and defaulting an unknown onto one side of the comparison
    would quietly answer section 3's question with a guess.
    """
    return {
        feed["name"]: "gnews" if is_proxy(feed.get("url", "")) else "native"
        for feed in sources
        if feed.get("name")
    }


def _capture_sources() -> list[dict]:
    """The feed list, or nothing. Fail-safe: an unclassifiable table is still
    worth printing, and every feed reads '?' rather than the script dying after
    the measurement has already been computed."""
    try:
        import capture

        return capture.capture_sources()
    except Exception as e:  # noqa: BLE001 - reporting is not worth a crash here
        print(f"(could not read the feed list, so src is unknown: {e})")
        return []


def _report_by_kind(stats: dict, kinds: dict[str, str]) -> None:
    """Section 3's question: do the CAPPED PROXIES lose more than native wires?

    Medians across feeds within each kind, with the feed counts, because two
    groups of unequal size invite a mean to be quoted as if it were comparable.
    """
    groups: dict[str, list[dict]] = {}
    for name, s in stats.items():
        groups.setdefault(kinds.get(name, "?"), []).append(s)

    print("\nBy source kind — spec section 3: do the capped proxies lose more?")
    for kind in sorted(groups):
        rows = groups[kind]
        singles = [r["singles"] for r in rows if r["singles"] is not None]
        flick = [r["flicker"] for r in rows if r["flicker"] is not None]
        print(
            f"  {kind:<8}{len(rows):>3} feeds   median singles "
            f"{_show(statistics.median(singles) if singles else None, '.3f'):>6}"
            f"   median flicker "
            f"{_show(statistics.median(flick) if flick else None, '.2f'):>6}"
        )
    print(
        "  A proxy's singles need not be roll-off: a Google News query reranks, so\n"
        "  an item can leave and come back. Flicker above 1.00 is that happening,\n"
        "  and no poll interval fixes it — a narrower query or a native feed does."
    )


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
    kinds = feed_kinds(_capture_sources())

    print(
        f"{'feed':<32}{'src':>7}{'ok':>5}{'window':>8}{'entries':>8}{'flick':>7}"
        f"{'ovl':>6}{'worst':>7}{'singles':>9}{'dep':>6}{'turnover':>10}"
        f"{'propose':>9}"
    )
    for name, s in sorted(
        stats.items(),
        key=lambda kv: (
            kv[1]["proposed_minutes"] is None,
            kv[1]["proposed_minutes"] or 0,
        ),
    ):
        print(
            f"{name[:31]:<32}{kinds.get(name, '?'):>7}{s['polls_ok']:>5}"
            f"{_show(s['median_window'], '.0f'):>8}"
            f"{_show(s['median_entries'], '.0f'):>8}"
            f"{_show(s['flicker'], '.2f'):>7}"
            f"{_show(s['median_overlap'], '.2f'):>6}"
            f"{_show(s['worst_overlap'], '.2f'):>7}"
            f"{_show(s['singles'], '.2f'):>9}"
            f"{s['departed']:>6}"
            f"{_show(s['turnover_rate_minutes'], '.0f'):>10}"
            f"{_show(s['proposed_minutes'], 'd'):>9}"
        )

    _report_by_kind(stats, kinds)

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

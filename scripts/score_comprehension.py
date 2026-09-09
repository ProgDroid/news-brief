"""Score the comprehension pipeline against its PRE-REGISTERED gate.

Spec section 8. Read-only: it writes nothing and is safe against production.

Each check below is a function that RETURNS its result rather than printing
and exiting inline, so tests/test_score_comprehension.py can call them
directly against a fixture connection. `main()` is the only thing that
prints and decides the exit code.

Run:  py scripts/score_comprehension.py              # the gate, one-shot
      py scripts/score_comprehension.py --cohorts   # observation only
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402  (path shim above must run first)

# Pre-registered 2026-09-04, BEFORE the first real run. Do not tune these to
# make a run pass -- a threshold moved after seeing the data measures nothing.
MIN_DISTINCT_AT_10PCT = 2
MAX_SINGLE_VALUE_SHARE = 0.90
CORROBORATION_FLOOR = 0.10
CORROBORATION_CEILING = 0.60

# NOT pre-registered, and deliberately declared below the block that is: the
# exposure horizon is a free parameter of the cohort OBSERVATION, not a
# threshold anything passes or fails. It exists to hold event age constant
# between two cohorts of different ages; ANY value does that, which is
# exactly why no single one can be defended. The default is therefore a
# SWEEP, not a number: three horizons spanning half a day either side of the
# hourly cadence, so the reader sees the parameter move instead of trusting
# a constant somebody picked. 6 is short enough to survive a one-day
# post-cutover window; 24 is long enough that a next-morning follow-up from
# a second outlet still lands inside it.
DEFAULT_EXPOSURE_HOURS = (6.0, 12.0, 24.0)

ENUMS = [
    ("events", "type"),
    ("events", "commitment_state"),
    ("assertions", "standing"),
]

# All five reasons item_triage can carry for a MATERIAL row (spec 5.1.1).
# tracked_entity starts EMPTY -- it needs this very pipeline to populate
# `entities` first -- and tracked_claim is the only tracked source live at
# flag-flip, written from existing `claims` immediately (comprehend.py:421).
# tracked_story needs `stories`, which nothing in production writes yet.
# Listing all five, rather than a subset picked for what happens to have
# data on day one, is what lets a reader tell an empty arm from an arm
# nobody looked at.
REASONS = ("tracked_entity", "tracked_claim", "tracked_story", "topical", "sampled")


def distribution(conn, table, column, reason=None):
    if reason:
        # `item_triage` keys on item_id, which `assertions` has directly but
        # `events` does not -- an event reaches an item only through the
        # assertion that links them. F6 extended the reason filter to
        # `events` for the first time; without this branch it built
        # `t.item_id = a.item_id` against a table with no item_id column and
        # failed with UndefinedColumn on the very first per-arm events query.
        # Counted per assertion (one row per item-event edge), matching how
        # `assertions.standing` was already counted -- an event asserted by
        # two items with the same reason contributes two rows, weighting by
        # evidence volume rather than by distinct event.
        if table == "events":
            join = (
                f"FROM {table} a "
                "JOIN assertions ta ON ta.event_id = a.id "
                "JOIN item_triage t ON t.item_id = ta.item_id"
            )
        else:
            join = f"FROM {table} a JOIN item_triage t ON t.item_id = a.item_id"
        rows = conn.execute(
            f"SELECT a.{column}, count(*) {join} "
            f"WHERE a.{column} IS NOT NULL AND t.reason = %s GROUP BY 1",
            (reason,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {column}, count(*) FROM {table} "
            f"WHERE {column} IS NOT NULL GROUP BY 1"
        ).fetchall()
    total = sum(c for _, c in rows)
    return {v: c / total for v, c in rows} if total else {}, total


def score_enum(shares, total):
    if total == 0:
        return False, "no rows"
    at_10 = sum(1 for s in shares.values() if s >= 0.10)
    top = max(shares.values())
    ok = at_10 >= MIN_DISTINCT_AT_10PCT and top <= MAX_SINGLE_VALUE_SHARE
    detail = ", ".join(
        f"{v}={s:.0%}" for v, s in sorted(shares.items(), key=lambda x: -x[1])
    )
    return ok, f"n={total} {detail} (>=10%: {at_10}, top {top:.0%})"


def per_arm_breakdown(conn):
    """`standing` broken down by triage reason, across all three tracked
    enums (spec 8.1 asks for the enums, not one of them), for all five
    reasons a material row can carry (see REASONS above).

    Returns {(table, column): {reason: (ok, detail)}}. Not gating: the point
    is to compare the tracked arms against `sampled` as an unconfounded
    control, not to fail the run on a topical arm's own variance.
    """
    result = {}
    for table, column in ENUMS:
        result[(table, column)] = {
            reason: score_enum(*distribution(conn, table, column, reason))
            for reason in REASONS
        }
    return result


def by_depth_tier(conn, table, column):
    """Enum distribution split by the source item's body length.

    Spec 12.3 amendment 1. Measured 2026-09-05: 15 of 24 outlets have a median
    body under 150 characters, and 41% of captured volume is Google News proxy
    items whose body is the headline restated. An aggregate distribution over
    that corpus substantially measures feed composition rather than model
    judgement, so the tiers are reported alongside it.
    """
    if table == "events":
        join = (
            "FROM events e "
            "JOIN assertions a ON a.event_id = e.id "
            "JOIN items i ON i.id = a.item_id"
        )
        col = f"e.{column}"
    else:
        join = "FROM assertions a JOIN items i ON i.id = a.item_id"
        col = f"a.{column}"
    return conn.execute(
        "SELECT CASE WHEN coalesce(length(i.body), 0) < 150 THEN '<150' "
        "            WHEN length(i.body) < 350 THEN '150-350' "
        "            ELSE '350+' END AS tier, "
        f"       {col} AS value, count(*) AS n "
        f"{join} "
        f"WHERE {col} IS NOT NULL "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    ).fetchall()


def corroboration_by_outlet(conn, since=None, until=None, horizon_hours=None):
    """First of spec 8.2's two directions: what fraction of events are
    reported by 2+ distinct outlets. Returns (rate, total, multi).

    With no arguments this is the whole-KB figure the gate reads, unchanged.

    `since`/`until` scope the DENOMINATOR by `events.created_at`, half-open
    [since, until). The candidate-ranking cutover (51c850c) left the KB
    holding two populations, and the design spec makes the missing window a
    requirement: a run spanning it produces a number attributable to neither.

    `horizon_hours` caps each event's EXPOSURE -- only assertions written
    within that long of the event's own creation count toward its outlet
    count. Corroboration accrues as a second outlet's item arrives, so
    without this a young post-cutover cohort is compared against a mature
    pre-cutover one and the ranking change is charged for its own recency.
    An event's creating assertion shares its transaction, hence its `now()`,
    so every event survives the cap carrying at least one outlet.
    """
    where, params = [], []
    if since is not None:
        where.append("e.created_at >= %s")
        params.append(since)
    if until is not None:
        where.append("e.created_at < %s")
        params.append(until)
    if horizon_hours is not None:
        where.append("a.created_at <= e.created_at + %s")
        params.append(timedelta(hours=horizon_hours))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total, multi = conn.execute(
        "WITH per_event AS ("
        "  SELECT e.id, count(DISTINCT i.outlet_id) AS outlets "
        "  FROM events e "
        "  JOIN assertions a ON a.event_id = e.id "
        "  JOIN items i ON i.id = a.item_id "
        f"  {clause} "
        "  GROUP BY e.id) "
        "SELECT count(*), count(*) FILTER (WHERE outlets >= 2) FROM per_event",
        params or None,
    ).fetchone()
    return (multi / total if total else 0.0), total, multi


def deploy_anchor(conn, version=None):
    """The cutover timestamp, read from the ledger the SYSTEM wrote.

    The ranking design spec required the cutover to be recorded on the bead
    when it deployed. It was not, and a remembered time is exactly the recall
    question that goes unanswered. Migrations run at container boot, so
    `schema_migrations.applied_at` for the newest version is the moment that
    image began serving -- an answer to recognise rather than reconstruct.

    VALID ONLY IF that migration shipped in the same image as the code change
    being measured. Confirmed for 0011 vs 51c850c: one host pull, taken after
    both. Returns (version, applied_at), or None if the ledger is empty.
    """
    if version is None:
        row = conn.execute(
            "SELECT version, applied_at FROM schema_migrations "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT version, applied_at FROM schema_migrations WHERE version = %s",
            (version,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def corroboration_cohorts(conn, cutover, horizon_hours, now=None):
    """Post-cutover corroboration against an equal-length, equal-exposure
    pre-cutover control.

    Two confounds have to die together. The window kills the cutover blend;
    the horizon kills event age. What is left compares two rankings over
    spans of equal length, each event scored over an identical slice of its
    own life.

    `now` defaults to the DATABASE clock, not the client's -- every timestamp
    it is compared against was written by `now()` on that server.
    """
    if now is None:
        now = conn.execute("SELECT now()").fetchone()[0]
    post_end = now - timedelta(hours=horizon_hours)
    if post_end <= cutover:
        elapsed = (now - cutover).total_seconds() / 3600
        return {
            "measurable": False,
            "reason": (
                f"{elapsed:.1f}h since the cutover: the {horizon_hours:g}h "
                f"exposure horizon has not elapsed for a single post-cutover "
                f"event, so there is nothing to measure -- which is not a "
                f"rate of 0.0"
            ),
            "horizon_hours": horizon_hours,
            "now": now,
            "post": None,
            "pre": None,
        }
    span = post_end - cutover
    pre_start = cutover - span
    return {
        "measurable": True,
        "reason": "",
        "horizon_hours": horizon_hours,
        "now": now,
        "post_span": (cutover, post_end),
        "pre_span": (pre_start, cutover),
        "post": corroboration_by_outlet(
            conn, since=cutover, until=post_end, horizon_hours=horizon_hours
        ),
        "pre": corroboration_by_outlet(
            conn, since=pre_start, until=cutover, horizon_hours=horizon_hours
        ),
    }


def corroboration_sweep(conn, cutover, horizons, now=None):
    """One cohort comparison per horizon, all sharing a single pinned `now`.

    The horizon is a free parameter, so any single value states something
    about the horizon as much as about the ranking. Varying it is the only
    cheap way to tell those apart: a pre/post gap that survives 6h, 12h and
    24h is a property of the ranking; one that appears at exactly one value
    is a property of the constant somebody chose.
    """
    if now is None:
        now = conn.execute("SELECT now()").fetchone()[0]
    return [
        (horizon, corroboration_cohorts(conn, cutover, horizon, now=now))
        for horizon in horizons
    ]


def print_sweep(conn, cutover, horizons, now=None):
    """Print the sweep. Deliberately renders NO verdict and no failing exit
    code: the spec-8 gate is a one-shot instrument that `bqa.11` is still
    holding, and a threshold cannot honestly be re-run after looking.
    """
    rows = corroboration_sweep(conn, cutover, horizons, now=now)
    post_head = "post-cutover (entity rank)"
    pre_head = "pre-cutover control (recency)"
    print("=== OBSERVATION: corroboration_by_outlet by cohort (bqa.19) ===")
    print("Not the pre-registered gate, which stays unspent for bqa.11.")
    print(f"cutover: {cutover:%Y-%m-%d %H:%M %Z}")
    print(f"reference only: spec 8.2's floor is {CORROBORATION_FLOOR:.0%}")
    print()
    print(f"{'horizon':>8}  {post_head:>29}  {pre_head:>29}  {'delta':>8}")
    for horizon, report in rows:
        if not report["measurable"]:
            print(f"{horizon:>7g}h  not measurable: {report['reason']}")
            continue
        cells = []
        for key in ("post", "pre"):
            rate, total, multi = report[key]
            cells.append(
                f"n={total} multi={multi} {rate:.1%}" if total else "no events"
            )
        post_rate, post_total, _ = report["post"]
        pre_rate, pre_total, _ = report["pre"]
        delta = (
            f"{(post_rate - pre_rate) * 100:+.1f}pp"
            if post_total and pre_total
            else "--"
        )
        print(f"{horizon:>7g}h  {cells[0]:>29}  {cells[1]:>29}  {delta:>8}")
    first = rows[0][1] if rows else None
    if first is not None and first["measurable"]:
        start, end = first["post_span"]
        print()
        print(
            f"spans differ per horizon; at {rows[0][0]:g}h each cohort "
            f"covers {(end - start).total_seconds() / 3600:.1f}h"
        )
    if len(rows) > 1:
        shortest = min(h for h, _ in rows)
        longest = max(h for h, _ in rows)
        print()
        print("rows are NESTED, not independent: a longer horizon ends the")
        print(f"window earlier, so the {longest:g}h row's events are a SUBSET of")
        print(f"the {shortest:g}h row's. Agreement across them is close to one")
        print("observation, not several.")
    print()
    print("Every event is scored over an identical slice of its own life and")
    print("the control spans exactly as long as the post-cutover cohort, so")
    print("neither event age nor window length is doing the work here. A gap")
    print("that does not hold across horizons belongs to the horizon rather")
    print("than to the ranking.")
    return rows


def score_match_rate_corroboration(conn):
    """Second of spec 8.2's two directions, and F5's fix: `events_matched /
    (events_matched + events_created)` lives only in the per-run in-memory
    `Tally` -- logged as text, persisted to no table, so a read-only script
    cannot read it back. It CAN be derived exactly from what IS stored.

    `assertions` is (item_id, event_id): an event gets one assertion from the
    item that created it, and one more for every later item that matches it
    (assertions dedupes on (item_id, event_id), so one item matching the same
    event twice still counts once). So across the whole KB:

        count(assertions) = count(events) + matches
        matched + created = (A - E) + E = A
        rate = matches / A = (count(assertions) - count(events)) / count(assertions)

    Exact given the write path, not approximate. Checked against the §8.2
    floor only (not the ceiling) per final-fix-3's F5 -- the ceiling's
    over-merging concern is a property of the matcher, which the outlet-based
    direction above already gates.

    Returns (ok, measured, detail). measured=False (zero assertions) must
    read as "nothing to measure", never as a rate of 0.0.
    """
    assertions_n, events_n = conn.execute(
        "SELECT (SELECT count(*) FROM assertions), (SELECT count(*) FROM events)"
    ).fetchone()
    if assertions_n == 0:
        return False, False, "no assertions to measure"
    rate = (assertions_n - events_n) / assertions_n
    ok = rate >= CORROBORATION_FLOOR
    detail = f"assertions={assertions_n} events={events_n} matched-rate={rate:.1%}"
    if not ok:
        detail += f" (below the {CORROBORATION_FLOOR:.0%} floor)"
    return ok, True, detail


def standing_variance_by_outlet(conn):
    """standing's likely failure mode is not low variance -- it is becoming a
    function of the OUTLET rather than of the item, which looks healthy in
    aggregate while carrying no per-item information (spec 12.3 amendment 2).
    No aggregate distribution can show that; this can.

    Returns (rows, measured). measured=False (no outlet has reached 10
    assertions yet) must be reported explicitly -- it is indistinguishable
    from "checked, all healthy" if the caller prints nothing (F7).
    """
    rows = conn.execute(
        "SELECT o.name, count(DISTINCT a.standing) AS distinct_standing, "
        "       count(*) AS n "
        "FROM assertions a "
        "JOIN items i ON i.id = a.item_id "
        "JOIN outlets o ON o.id = i.outlet_id "
        "GROUP BY o.name HAVING count(*) >= 10 ORDER BY 2, 3 DESC"
    ).fetchall()
    return rows, bool(rows)


def run_gate(conn) -> list[str]:
    """Run every check against `conn`, print its output, return the
    failures. Split from main() so tests can drive it against a fixture
    connection instead of opening their own via db.connect()."""
    failures = []

    print("=== Enum variance (spec 8.1) ===")
    for table, column in ENUMS:
        shares, total = distribution(conn, table, column)
        ok, detail = score_enum(shares, total)
        print(f"{'PASS' if ok else 'FAIL'}  {table}.{column}: {detail}")
        if not ok:
            failures.append(f"{table}.{column}")

    print("\n=== Per-arm, with `sampled` as the unconfounded control (8.1) ===")
    print("A topical arm that varies while sampled does not means the")
    print("extractor is being flattered by its sibling's selection.")
    breakdown = per_arm_breakdown(conn)
    for table, column in ENUMS:
        print(f"  {table}.{column}:")
        for reason in REASONS:
            _, detail = breakdown[(table, column)][reason]
            print(f"    [{reason}]: {detail}")

    print("\n=== Corroboration, BOTH directions (spec 8.2) ===")
    rate, total, multi = corroboration_by_outlet(conn)
    print(f"events={total} multi-outlet={multi} rate={rate:.1%}")
    if total == 0:
        failures.append("corroboration: no events")
        print("FAIL  no events to measure")
    elif rate < CORROBORATION_FLOOR:
        failures.append("corroboration below floor")
        print(
            f"FAIL  below the {CORROBORATION_FLOOR:.0%} floor: the event "
            f"layer bought nothing over the claim ledger"
        )
    elif rate > CORROBORATION_CEILING:
        failures.append("corroboration above ceiling")
        print(
            f"FAIL  above the {CORROBORATION_CEILING:.0%} ceiling: suspect "
            f"the matcher is OVER-MERGING distinct events"
        )
    else:
        print("PASS")

    ok, measured, detail = score_match_rate_corroboration(conn)
    print(f"\n{'PASS' if ok else 'FAIL'}  match-rate corroboration: {detail}")
    if not ok:
        failures.append(
            "match-rate corroboration: no assertions"
            if not measured
            else "match-rate corroboration below floor"
        )
    print("Both directions come from independent sources -- outlet diversity")
    print("and the write-path arithmetic above. If they disagree, trust the")
    print("disagreement -- a secondary signal contradicting the headline is")
    print("the tell that a probe measured the wrong layer.")

    # Spec 12.3 amendment 1. Measured 2026-09-05: 15 of 24 outlets have a
    # median body under 150 chars and 41% of volume is Google News proxy
    # items whose body is the headline restated. An aggregate enum
    # distribution therefore substantially measures FEED COMPOSITION.
    print("\n=== Enum variance by body-depth tier (spec 12.3) ===")
    for table, column in ENUMS:
        print(f"  {table}.{column}:")
        for tier, value, n in by_depth_tier(conn, table, column):
            print(f"    [{tier}] {value}: {n}")

    print("\n=== standing variance WITHIN each outlet (spec 12.3) ===")
    print("A field constant within every outlet is degenerate however")
    print("varied it looks overall -- severity's failure in disguise.")
    rows, measured = standing_variance_by_outlet(conn)
    if not measured:
        print("  not yet measurable: no outlet has 10+ assertions")
    else:
        for name, distinct, n in rows:
            flag = "  <-- CONSTANT" if distinct <= 1 else ""
            print(f"  {name}: {distinct} distinct over n={n}{flag}")
        constant = [r[0] for r in rows if r[1] <= 1]
        if len(constant) == len(rows):
            failures.append("standing is constant within every outlet")
            print("FAIL  standing carries no per-item information")

    print("\n=== Triage reason distribution (spec 5.1.1) ===")
    print("Interpretable only AFTER entities has accumulated. tracked_story")
    print("is expected at ZERO: nothing in production writes `stories`.")
    for reason, n in conn.execute(
        "SELECT reason, count(*) FROM item_triage GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {reason}: {n}")

    return failures


def _parse_cutover(text):
    """ISO-8601. A naive input is read as UTC, because `events.created_at` is
    `timestamptz` and comparing it against a naive local time shifts the whole
    window by the host offset -- silently, and in the direction nobody checks.
    """
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _explain_missing_cutover(conn) -> int:
    """--cohorts without --cutover. Measure nothing, and say what to answer.

    The first live run defaulted to the newest migration's applied_at and
    produced a table whose "post-cutover" column was not the change being
    measured: `schema_migrations` dates MIGRATIONS, and the ranking fix
    shipped without one, so the newest migration had deployed three hours
    BEFORE that fix was even committed. The ledger cannot date an arbitrary
    commit, so it is offered as a hint with its validity condition attached
    rather than silently used as an answer.
    """
    print("--cohorts needs --cutover: the instant the code you are measuring")
    print("began serving. schema_migrations dates MIGRATIONS, not commits, so")
    print("it answers this only for a change that shipped alongside one.")
    anchor = deploy_anchor(conn)
    if anchor is None:
        print("\nThe migration ledger is empty, so there is no hint to offer.")
    else:
        version, applied = anchor
        stamp = f"{applied:%Y-%m-%d %H:%M %Z}"
        print(f"\nHint: migration {version} was applied {stamp}.")
        print("Use it as the cutover ONLY if your change was committed before")
        print(f"{stamp}. If it was committed after, it shipped in a LATER image")
        print("and this timestamp predates the thing you want to measure.")
        print("`git log --format='%h %cI %s' <commit>` settles it.")
    print("\nThen re-run with: --cutover YYYY-MM-DDTHH:MM:SSZ")
    print("A naive timestamp is read as UTC.")
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the comprehension pipeline against spec section 8."
    )
    parser.add_argument(
        "--cohorts",
        action="store_true",
        help="OBSERVE corroboration_by_outlet on each side of the deploy "
        "cutover instead of running the gate. Renders no verdict and no "
        "failing exit code: the gate is one-shot and bqa.11 still holds it.",
    )
    parser.add_argument(
        "--cutover",
        help="ISO-8601 cutover for --cohorts. Defaults to the newest "
        "migration's applied_at, which is when that image began serving.",
    )
    parser.add_argument(
        "--horizon-hours",
        type=float,
        nargs="+",
        default=list(DEFAULT_EXPOSURE_HOURS),
        help="Exposure horizons per event, in hours; several are swept into "
        f"one table (default {' '.join(f'{h:g}' for h in DEFAULT_EXPOSURE_HOURS)}). "
        "A pre/post gap that does not hold across all of them belongs to the "
        "horizon rather than to the ranking.",
    )
    args = parser.parse_args(argv)

    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    with db.connect() as conn:
        if args.cohorts:
            if not args.cutover:
                return _explain_missing_cutover(conn)
            print_sweep(conn, _parse_cutover(args.cutover), args.horizon_hours)
            return 0

        failures = run_gate(conn)

    print("\n" + ("GATE FAILED: " + ", ".join(failures) if failures else "GATE PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Score the comprehension pipeline against its PRE-REGISTERED gate.

Spec section 8. Read-only: it writes nothing and is safe against production.

Each check below is a function that RETURNS its result rather than printing
and exiting inline, so tests/test_score_comprehension.py can call them
directly against a fixture connection. `main()` is the only thing that
prints and decides the exit code.

Run:  py scripts/score_comprehension.py
"""

import sys
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


def corroboration_by_outlet(conn):
    """First of spec 8.2's two directions: what fraction of events are
    reported by 2+ distinct outlets. Returns (rate, total, multi)."""
    row = conn.execute(
        "WITH per_event AS ("
        "  SELECT a.event_id, count(DISTINCT i.outlet_id) AS outlets "
        "  FROM assertions a JOIN items i ON i.id = a.item_id "
        "  GROUP BY a.event_id) "
        "SELECT count(*), count(*) FILTER (WHERE outlets >= 2) FROM per_event"
    ).fetchone()
    total, multi = row
    return (multi / total if total else 0.0), total, multi


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


def main() -> int:
    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    with db.connect() as conn:
        failures = run_gate(conn)

    print("\n" + ("GATE FAILED: " + ", ".join(failures) if failures else "GATE PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Score the comprehension pipeline against its PRE-REGISTERED gate.

Spec section 8. Read-only: it writes nothing and is safe against production.

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


def distribution(conn, table, column, reason=None):
    if reason:
        rows = conn.execute(
            f"SELECT a.{column}, count(*) FROM {table} a "
            "JOIN item_triage t ON t.item_id = a.item_id "
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


def corroboration(conn):
    row = conn.execute(
        "WITH per_event AS ("
        "  SELECT a.event_id, count(DISTINCT i.outlet_id) AS outlets "
        "  FROM assertions a JOIN items i ON i.id = a.item_id "
        "  GROUP BY a.event_id) "
        "SELECT count(*), count(*) FILTER (WHERE outlets >= 2) FROM per_event"
    ).fetchone()
    total, multi = row
    return (multi / total if total else 0.0), total, multi


def main() -> int:
    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    failures = []
    with db.connect() as conn:
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
        for reason in ("tracked_entity", "topical", "sampled"):
            shares, total = distribution(conn, "assertions", "standing", reason)
            ok, detail = score_enum(shares, total)
            print(f"  standing[{reason}]: {detail}")

        print("\n=== Corroboration, BOTH directions (spec 8.2) ===")
        rate, total, multi = corroboration(conn)
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

        created, matched = conn.execute(
            "SELECT count(*) FILTER (WHERE prompt_version IS NOT NULL), count(*) "
            "FROM events"
        ).fetchone()
        print(f"\nmechanism check: events rows={matched}, extractor-stamped={created}")
        print("If corroboration disagrees with the match rate, trust the")
        print("disagreement -- a secondary signal contradicting the headline is")
        print("the tell that the probe measured the wrong layer.")

        # Spec 12.3 amendment 1. Measured 2026-09-05: 15 of 24 outlets have a
        # median body under 150 chars and 41% of volume is Google News proxy
        # items whose body is the headline restated. An aggregate enum
        # distribution therefore substantially measures FEED COMPOSITION.
        print("\n=== Enum variance by body-depth tier (spec 12.3) ===")
        for table, column in ENUMS:
            print(f"  {table}.{column}:")
            for tier, value, n in by_depth_tier(conn, table, column):
                print(f"    [{tier}] {value}: {n}")

        # Spec 12.3 amendment 2. standing is the field at risk, and its likely
        # failure is NOT low variance -- it is becoming a function of the
        # OUTLET rather than of the item, which looks healthy in aggregate
        # while carrying no per-item information. No aggregate distribution
        # can show that; this can.
        print("\n=== standing variance WITHIN each outlet (spec 12.3) ===")
        print("A field constant within every outlet is degenerate however")
        print("varied it looks overall -- severity's failure in disguise.")
        rows = conn.execute(
            "SELECT o.name, count(DISTINCT a.standing) AS distinct_standing, "
            "       count(*) AS n "
            "FROM assertions a "
            "JOIN items i ON i.id = a.item_id "
            "JOIN outlets o ON o.id = i.outlet_id "
            "GROUP BY o.name HAVING count(*) >= 10 ORDER BY 2, 3 DESC"
        ).fetchall()
        for name, distinct, n in rows:
            flag = "  <-- CONSTANT" if distinct <= 1 else ""
            print(f"  {name}: {distinct} distinct over n={n}{flag}")
        constant = [r[0] for r in rows if r[1] <= 1]
        if rows and len(constant) == len(rows):
            failures.append("standing is constant within every outlet")
            print("FAIL  standing carries no per-item information")

        print("\n=== Triage reason distribution (spec 5.1.1) ===")
        print("Interpretable only AFTER entities has accumulated. tracked_story")
        print("is expected at ZERO: nothing in production writes `stories`.")
        for reason, n in conn.execute(
            "SELECT reason, count(*) FROM item_triage GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {reason}: {n}")

    print("\n" + ("GATE FAILED: " + ", ".join(failures) if failures else "GATE PASSED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

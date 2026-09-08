"""Why is corroboration low? Ranking, the model, or no overlap at all?

news-brief-bqa.18. Read-only and model-free: it SELECTs, computes, prints, and
writes nothing. Safe against production.

`events_matched / (events_matched + events_created)` fell from 26% to 10.2%
across 2026-09-08, against a pre-registered floor of 0.10 that spec section 8.2
makes the EXISTENCE test for the event layer -- "below this the event layer
bought essentially nothing over the claim ledger". Before designing a fix, find
out which of three things is true, because they have nothing in common:

  A  the duplicate WAS offered and the model declined to match it
     -> the prompt or the model is the bottleneck
  B  the duplicate existed but was never offered
     -> `candidate_events` ranking is the bottleneck
  C  no duplicate existed
     -> the outlets do not cover the same events, and section 8.2's premise is
        wrong rather than its implementation

C is the one nobody has tested, and no amount of retrieval work rescues it.

THE DETECTOR IS CALIBRATED, NOT INVENTED. A similarity threshold picked by hand
would make a null result uninterpretable, because a detector that finds
duplicates by evidence-of-duplication bounds the true count from BELOW: "few
hits" and "weak detector" look identical. So the threshold is measured off the
model's OWN matches -- item pairs already sharing an event_id across different
outlets, which are same-event pairs by the model's judgment.

Run:  docker compose run --rm --entrypoint python newsbrief \\
          scripts/probe_corroboration.py [days]
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import comprehend  # noqa: E402  (path shim above must run first)
import db  # noqa: E402

# How far back to look. Kept separate from CANDIDATE_WINDOW_DAYS: this bounds
# the probe's own corpus, that bounds what the matcher may retrieve.
DEFAULT_DAYS = 7

# Two items reporting the same event land within hours of each other, not days.
# Wider than the news cycle would pair unrelated coverage of a running story.
PAIR_WINDOW_HOURS = 48

# Below this many calibration pairs, the threshold is not measured -- it is
# read off noise, and every number downstream inherits that.
MIN_CALIBRATION_PAIRS = 20

# Which quantile of KNOWN duplicates the threshold sits at. 0.25 keeps three
# quarters of real duplicates above the line, trading precision for the recall
# that matters here: a MISSED miss understates the problem.
CALIBRATION_QUANTILE = 0.25

_WORD = re.compile(r"[a-z0-9]+")
# Not a linguistic stopword list -- these are the tokens that make unrelated
# headlines look similar. Keeping them inflates every score toward the mean and
# flattens the very distinction the threshold depends on.
_NOISE = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "s",
    "the",
    "to",
    "with",
    "after",
    "over",
    "says",
    "said",
    "new",
    "up",
    "down",
    "amid",
    "its",
    "his",
    "her",
    "their",
    "that",
    "this",
}


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE}


def similarity(a: str, b: str) -> float:
    """Jaccard over content tokens. Deliberately crude and deliberately the
    SAME function on both sides: calibration and detection must share it, or
    the threshold measures one thing and the search another."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def calibration_pairs(conn, days: int) -> list[tuple[float, str, str]]:
    """Item-title pairs the MODEL already judged to be the same event, from
    different outlets. This is the positive control: it is what a genuine
    duplicate looks like in text space, measured rather than assumed."""
    rows = conn.execute(
        "SELECT a.event_id, i.title, i.outlet_id "
        "FROM assertions a JOIN items i ON i.id = a.item_id "
        "JOIN events e ON e.id = a.event_id "
        "WHERE e.created_at >= now() - make_interval(days => %s) "
        "ORDER BY a.event_id",
        (days,),
    ).fetchall()

    by_event = defaultdict(list)
    for event_id, title, outlet_id in rows:
        by_event[event_id].append((title, outlet_id))

    pairs = []
    for members in by_event.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                (t1, o1), (t2, o2) = members[i], members[j]
                if o1 == o2:
                    continue  # same outlet is republication, not corroboration
                pairs.append((similarity(t1, t2), t1, t2))
    return pairs


def event_rows(conn, days: int) -> list[dict]:
    """One row per (event, asserting item). An event asserted by two items
    appears twice, which is correct: the pairing below is over ITEMS."""
    rows = conn.execute(
        "SELECT e.id, e.created_at, e.occurred_at, i.id, i.title, i.outlet_id "
        "FROM events e "
        "JOIN assertions a ON a.event_id = e.id "
        "JOIN items i ON i.id = a.item_id "
        "WHERE e.created_at >= now() - make_interval(days => %s)",
        (days,),
    ).fetchall()
    return [
        {
            "event_id": r[0],
            "created_at": r[1],
            "occurred_at": r[2],
            "item_id": r[3],
            "title": r[4],
            "outlet_id": r[5],
        }
        for r in rows
    ]


def entities_by_event(conn, days: int) -> dict[int, set[int]]:
    rows = conn.execute(
        "SELECT ee.event_id, ee.entity_id FROM event_entities ee "
        "JOIN events e ON e.id = ee.event_id "
        "WHERE e.created_at >= now() - make_interval(days => %s)",
        (days,),
    ).fetchall()
    out = defaultdict(set)
    for event_id, entity_id in rows:
        out[event_id].add(entity_id)
    return out


def probable_misses(rows, ents, threshold):
    """Cross-outlet item pairs above the calibrated threshold whose events are
    DIFFERENT -- duplicates the matcher did not merge.

    Blocked on a shared entity, mirroring `candidate_events`: a pair with no
    shared entity could never have been offered under any ranking, so counting
    it would blame retrieval for something retrieval was never asked to do.
    """
    by_entity = defaultdict(list)
    for row in rows:
        for entity_id in ents.get(row["event_id"], ()):
            by_entity[entity_id].append(row)

    seen, misses = set(), []
    for members in by_entity.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["event_id"] == b["event_id"]:
                    continue  # already merged: this is a SUCCESS, not a miss
                if a["outlet_id"] == b["outlet_id"]:
                    continue
                key = tuple(sorted((a["event_id"], b["event_id"])))
                if key in seen:
                    continue  # the same pair shares several entities
                gap = abs((a["created_at"] - b["created_at"]).total_seconds())
                if gap > PAIR_WINDOW_HOURS * 3600:
                    continue
                score = similarity(a["title"], b["title"])
                if score < threshold:
                    continue
                seen.add(key)
                earlier, later = sorted((a, b), key=lambda r: r["created_at"])
                misses.append({"score": score, "earlier": earlier, "later": later})
    return sorted(misses, key=lambda m: -m["score"])


def was_retrievable(conn, miss, ents) -> bool:
    """Could `candidate_events` have offered the earlier event when the later
    one was created?

    Deliberately GENEROUS -- it uses the ideal entity set attached to the later
    event rather than the narrower set that batch actually matched, and it
    applies no entity cap. A miss under ideal conditions is therefore decisive
    about ranking rather than suggestive. `created_at <` is what makes this a
    reconstruction instead of a query about today.
    """
    entity_ids = list(ents.get(miss["later"]["event_id"], ()))
    if not entity_ids:
        return False
    rows = conn.execute(
        # `occurred_at` is in the select list because SELECT DISTINCT requires
        # every ORDER BY expression to be -- the same reason candidate_events
        # selects it. Dropping it to "tidy up" breaks the query outright.
        "SELECT DISTINCT e.id, e.occurred_at FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id "
        "WHERE ee.entity_id = ANY(%s) "
        "  AND e.occurred_at >= %s::timestamptz - make_interval(days => %s) "
        "  AND e.created_at < %s "
        # MUST mirror candidate_events' ranking, not merely its filters. That
        # function orders by occurred_at DESC, and ordering IS the mechanism
        # under investigation -- reconstructing it with a different ORDER BY
        # would answer a question adjacent to the one being asked.
        "ORDER BY e.occurred_at DESC LIMIT %s",
        (
            entity_ids,
            miss["later"]["created_at"],
            comprehend.CANDIDATE_WINDOW_DAYS,
            miss["later"]["created_at"],
            comprehend.CANDIDATE_EVENT_CAP,
        ),
    ).fetchall()
    return miss["earlier"]["event_id"] in {r[0] for r in rows}


def current_rate(conn) -> tuple[int, int, float]:
    events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    assertions = conn.execute("SELECT count(*) FROM assertions").fetchone()[0]
    rate = (assertions - events) / assertions if assertions else 0.0
    return events, assertions, rate


def report(conn, days: int) -> int:
    print(f"=== Corpus: events created in the last {days} day(s) ===")
    events, assertions, rate = current_rate(conn)
    print(f"events={events} assertions={assertions} match rate={rate:.1%}")

    print("\n=== Calibration: what a KNOWN duplicate looks like ===")
    cal = calibration_pairs(conn, days)
    scores = [s for s, _, _ in cal]
    print(f"cross-outlet pairs the model itself merged: n={len(scores)}")
    if len(scores) < MIN_CALIBRATION_PAIRS:
        print(
            f"NOT MEASURABLE: fewer than {MIN_CALIBRATION_PAIRS} calibration "
            "pairs. Every number below would inherit a threshold read off "
            "noise, so the probe stops rather than reporting a shaped guess."
        )
        print(
            "\nThat scarcity is itself the finding: the model has merged "
            "almost nothing across outlets. Re-run with more --days, or treat "
            "outcome C as live."
        )
        return 2
    for q in (0.10, 0.25, 0.50, 0.75):
        print(f"  p{int(q * 100):02d} similarity = {quantile(scores, q):.3f}")
    threshold = quantile(scores, CALIBRATION_QUANTILE)
    print(f"threshold = p{int(CALIBRATION_QUANTILE * 100)} = {threshold:.3f}")

    print("\n=== Probable MISSES: same story, different event rows ===")
    rows = event_rows(conn, days)
    ents = entities_by_event(conn, days)
    misses = probable_misses(rows, ents, threshold)
    print(f"probable missed duplicate pairs: {len(misses)}")

    if not misses:
        print(
            "\nOUTCOME C is indicated: above a threshold calibrated on real "
            "duplicates, no unmerged cross-outlet pair was found. Read this as "
            "a FLOOR, never a proof -- a weak detector looks identical here."
        )
        return 0

    retrievable = [m for m in misses if was_retrievable(conn, m, ents)]
    n_a, n_b = len(retrievable), len(misses) - len(retrievable)
    print(f"  A  offered but not matched : {n_a}  ({n_a / len(misses):.0%})")
    print(f"  B  never offered           : {n_b}  ({n_b / len(misses):.0%})")

    print("\n=== Ceiling: the rate if EVERY probable miss had merged ===")
    # Bound the problem rather than estimate it. Each merged pair removes one
    # event and keeps both assertions, so the numerator gains one per pair.
    if assertions:
        ceiling = (assertions - (events - len(misses))) / assertions
        print(f"  best case = {ceiling:.1%} (floor is 10.0%)")
        if ceiling < 0.10:
            print(
                "  Even perfect matching FAILS the floor. No retrieval work "
                "rescues this; the premise is what needs revisiting."
            )
        else:
            print("  Perfect matching would clear the floor: the headroom is real.")

    print("\n=== Sample pairs -- do these look like the same story? ===")
    print("(same/different judgment; if they are NOT duplicates the detector")
    print(" is wrong and every count above is inflated)")
    for m in misses[:10]:
        flag = "OFFERED" if was_retrievable(conn, m, ents) else "NOT OFFERED"
        print(f"\n  [{m['score']:.2f}] {flag}")
        print(f"    earlier: {m['earlier']['title']}")
        print(f"    later  : {m['later']['title']}")
    return 0


def main() -> int:
    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2
    days = DEFAULT_DAYS
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"days must be an integer, got {sys.argv[1]!r}")
            return 2
    with db.connect() as conn:
        return report(conn, days)


if __name__ == "__main__":
    sys.exit(main())

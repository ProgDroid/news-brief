"""Why is corroboration low? Ranking, the model, or no overlap at all?

news-brief-bqa.18. Read-only and model-free: it SELECTs, computes, prints, and
writes nothing. Safe against production.

Spec section 8.2 makes cross-outlet corroboration the EXISTENCE test for the
event layer -- "below this the event layer bought essentially nothing over the
claim ledger". Before designing a fix, find out which of three things is true,
because they share no fix:

  A  the duplicate WAS offered and the model declined to match it
     -> the prompt or the model is the bottleneck
  B  the duplicate existed but was never offered
     -> `candidate_events` ranking is the bottleneck
  C  no duplicate existed
     -> the outlets do not cover the same events, and 8.2's premise is wrong

WHAT THE FIRST VERSION OF THIS SCRIPT GOT WRONG, 2026-09-08, and why the
structure below looks the way it does:

- It calibrated the threshold on the POSITIVE class only. That controls recall
  and says nothing about false positives, so p25-of-duplicates came out at
  0.059 and the probe reported 58,310 "probable misses" across 2,753 events --
  about 21 per event, which is not credible. A threshold now needs a NEGATIVE
  class to separate against, and no single threshold is trusted: results are
  reported BANDED, so the reader sees where the answer changes.
- Its ceiling counted PAIRS as merges. Merging a cluster of k events removes
  k-1 events, not C(k,2), so the "best case" came out at 1779%. Merges are now
  counted by union-find over the pair graph and capped at events - clusters.
- Its single aggregate (82% B) DISAGREED with its own top-scoring samples,
  which were mostly offered-and-not-matched. An aggregate over a population
  that is mostly noise describes the noise. Hence the stratification by
  similarity AND by entity frequency: the hypothesis worth testing is that B
  dominates on hub entities (Iran, UN -- where 30 recency slots cover hours)
  while A dominates elsewhere.
- It could not see SYNDICATION. Several "duplicates" differed only by a
  "- Reuters" suffix or curly quotes: the same wire copy in two feeds, which is
  feed duplication rather than independent confirmation. Corroboration is now
  reported with and without it, because clearing the floor on syndication alone
  would prove less than 8.2 intends.

Run:  docker compose run --rm --entrypoint python newsbrief \\
          scripts/probe_corroboration.py [days]
"""

import random
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import comprehend  # noqa: E402  (path shim above must run first)
import db  # noqa: E402

DEFAULT_DAYS = 7

# Two items reporting the same event land within hours of each other.
PAIR_WINDOW_HOURS = 48

# The NEGATIVE class: cross-outlet pairs sharing an entity but separated by
# this much time. Two items about Iran a week apart are almost certainly
# different events, which makes this a clean non-duplicate sample -- far
# cleaner than sampling all pairs, where true duplicates would contaminate it.
NEGATIVE_MIN_DAYS = 5
NEGATIVE_MAX_PAIRS = 4000

MIN_CALIBRATION_PAIRS = 20

# Reported bands rather than one threshold. A single number would hide exactly
# the place where the A/B answer changes.
BANDS = [(0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.70), (0.70, 1.01)]

# `candidate_events` offers CANDIDATE_EVENT_CAP events, so an entity carrying
# more than that in the window can bury a duplicate by recency alone. Buckets
# straddle the cap deliberately.
HUB_BUCKETS = [(0, 10), (10, 30), (30, 100), (100, 500), (500, 10**9)]

# Pairwise comparison is quadratic within an entity. A hub entity with several
# thousand events would dominate the runtime; the cap is REPORTED, because a
# cap nobody sees is indistinguishable from an absence of data.
MAX_EVENTS_PER_ENTITY = 1500

_WORD = re.compile(r"[a-z0-9]+")
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
# " - Reuters", " | Al Jazeera", " - reuters.com". Bounded word count so a real
# headline clause ("Iran - what happens next in the long war over enrichment")
# is not amputated.
_SOURCE_SUFFIX = re.compile(r"\s*[-|–—]\s*[\w.\s]{1,25}$")


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def normalise_title(text: str) -> str:
    """Strip what syndication varies and meaning does not: unicode quotes and
    dashes, a trailing source suffix, punctuation, case, spacing."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = _SOURCE_SUFFIX.sub("", text)
    return " ".join(_WORD.findall(text.lower()))


def is_syndication(a: str, b: str) -> bool:
    """The SAME wire copy in two feeds, rather than two outlets independently
    confirming an event. Corroboration built on this proves feed duplication,
    which is not what 8.2 is asking for."""
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return False
    return na == nb or similarity(na, nb) >= 0.85


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def calibration_pairs(conn, days: int) -> list[tuple[float, str, str]]:
    """POSITIVES: item-title pairs the model itself merged into one event,
    across different outlets. Same-event by its own judgment."""
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
                if o1 != o2:
                    pairs.append((similarity(t1, t2), t1, t2))
    return pairs


def negative_pairs(conn, days: int, rng: random.Random) -> list[float]:
    """NEGATIVES: cross-outlet, entity-sharing pairs separated by at least
    NEGATIVE_MIN_DAYS. Without this class the threshold controls recall and
    nothing else -- which is exactly how the first version of this probe
    produced 21 "duplicates" per event."""
    rows = event_rows(conn, days)
    ents = entities_by_event(conn, days)
    by_entity = defaultdict(list)
    for row in rows:
        for entity_id in ents.get(row["event_id"], ()):
            by_entity[entity_id].append(row)

    seen, scores = set(), []
    entities = list(by_entity)
    rng.shuffle(entities)
    for entity_id in entities:
        members = by_entity[entity_id]
        if len(members) < 2:
            continue
        for _ in range(min(200, len(members))):
            a, b = rng.choice(members), rng.choice(members)
            if a["event_id"] == b["event_id"] or a["outlet_id"] == b["outlet_id"]:
                continue
            gap = abs((a["created_at"] - b["created_at"]).total_seconds())
            if gap < NEGATIVE_MIN_DAYS * 86400:
                continue
            key = tuple(sorted((a["event_id"], b["event_id"])))
            if key in seen:
                continue
            seen.add(key)
            scores.append(similarity(a["title"], b["title"]))
            if len(scores) >= NEGATIVE_MAX_PAIRS:
                return scores
    return scores


def event_rows(conn, days: int) -> list[dict]:
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


def entity_event_counts(conn, days: int) -> dict[int, int]:
    """How many events each entity carries in the window -- its "hubness".
    An entity above CANDIDATE_EVENT_CAP can bury a duplicate by recency."""
    rows = conn.execute(
        "SELECT ee.entity_id, count(DISTINCT ee.event_id) FROM event_entities ee "
        "JOIN events e ON e.id = ee.event_id "
        "WHERE e.created_at >= now() - make_interval(days => %s) "
        "GROUP BY 1",
        (days,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def probable_misses(rows, ents, threshold, counts=None):
    """Cross-outlet pairs above `threshold` whose events are DIFFERENT.

    Blocked on a shared entity, mirroring candidate_events: a pair with no
    shared entity could never have been offered under any ranking.
    """
    counts = counts or {}
    by_entity = defaultdict(list)
    for row in rows:
        for entity_id in ents.get(row["event_id"], ()):
            by_entity[entity_id].append(row)

    seen, misses, capped = set(), [], 0
    for members in by_entity.values():
        if len(members) > MAX_EVENTS_PER_ENTITY:
            capped += 1
            members = members[:MAX_EVENTS_PER_ENTITY]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["event_id"] == b["event_id"] or a["outlet_id"] == b["outlet_id"]:
                    continue
                key = tuple(sorted((a["event_id"], b["event_id"])))
                if key in seen:
                    continue
                gap = abs((a["created_at"] - b["created_at"]).total_seconds())
                if gap > PAIR_WINDOW_HOURS * 3600:
                    continue
                score = similarity(a["title"], b["title"])
                if score < threshold:
                    continue
                seen.add(key)
                earlier, later = sorted((a, b), key=lambda r: r["created_at"])
                misses.append(
                    {
                        "score": score,
                        "earlier": earlier,
                        "later": later,
                        "syndicated": is_syndication(a["title"], b["title"]),
                        "hubness": max(
                            (counts.get(e, 0) for e in ents.get(later["event_id"], ())),
                            default=0,
                        ),
                    }
                )
    return sorted(misses, key=lambda m: -m["score"]), capped


def was_retrievable(conn, miss, ents) -> bool:
    """Could candidate_events have offered the earlier event when the later one
    was created? Deliberately GENEROUS: the ideal entity set, no entity cap.
    `created_at <` is what makes this a reconstruction rather than a query
    about today."""
    entity_ids = list(ents.get(miss["later"]["event_id"], ()))
    if not entity_ids:
        return False
    rows = conn.execute(
        # occurred_at is in the select list because SELECT DISTINCT requires
        # every ORDER BY expression to be. The ORDER BY must mirror
        # candidate_events: ordering IS the mechanism under investigation.
        "SELECT DISTINCT e.id, e.occurred_at FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id "
        "WHERE ee.entity_id = ANY(%s) "
        "  AND e.occurred_at >= %s::timestamptz - make_interval(days => %s) "
        "  AND e.created_at < %s "
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


def merges_from_pairs(misses) -> int:
    """Union-find over the pair graph. Merging a cluster of k events removes
    k-1 events, NOT C(k,2) -- counting pairs as merges is what produced a
    1779% ceiling in the first version of this script."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in misses:
        a, b = find(m["earlier"]["event_id"]), find(m["later"]["event_id"])
        if a != b:
            parent[a] = b
    sizes = defaultdict(int)
    for node in parent:
        sizes[find(node)] += 1
    return sum(size - 1 for size in sizes.values())


def band_of(score: float):
    for lo, hi in BANDS:
        if lo <= score < hi:
            return (lo, hi)
    return None


def hub_bucket(n: int):
    for lo, hi in HUB_BUCKETS:
        if lo <= n < hi:
            return (lo, hi)
    return HUB_BUCKETS[-1]


def report(conn, days: int, rng=None) -> int:
    rng = rng or random.Random(20260908)
    events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    assertions = conn.execute("SELECT count(*) FROM assertions").fetchone()[0]
    rate = (assertions - events) / assertions if assertions else 0.0
    print(f"=== Corpus: events created in the last {days} day(s) ===")
    print(f"events={events} assertions={assertions} cumulative match rate={rate:.1%}")

    print("\n=== Calibration: POSITIVES vs NEGATIVES ===")
    pos = [s for s, _, _ in calibration_pairs(conn, days)]
    print(f"positives (model's own cross-outlet merges): n={len(pos)}")
    if len(pos) < MIN_CALIBRATION_PAIRS:
        print(
            f"NOT MEASURABLE: fewer than {MIN_CALIBRATION_PAIRS} positives. "
            "Every number below would inherit a threshold read off noise."
        )
        return 2
    neg = negative_pairs(conn, days, rng)
    print(f"negatives (cross-outlet, >={NEGATIVE_MIN_DAYS}d apart): n={len(neg)}")
    for q in (0.50, 0.75, 0.90, 0.99):
        print(
            f"  p{int(q * 100):02d}  positives={quantile(pos, q):.3f}"
            f"   negatives={quantile(neg, q):.3f}"
        )
    separator = quantile(neg, 0.99) if neg else 0.35
    kept = sum(1 for s in pos if s >= separator) / len(pos)
    print(f"p99 of NEGATIVES = {separator:.3f}  (keeps {kept:.0%} of positives)")
    print("Read this as: above that line, a pair is unlikely to be coincidence.")

    print("\n=== A vs B, BANDED (no single threshold is trusted) ===")
    rows = event_rows(conn, days)
    ents = entities_by_event(conn, days)
    counts = entity_event_counts(conn, days)
    misses, capped = probable_misses(rows, ents, BANDS[0][0], counts)
    if capped:
        print(f"NOTE: {capped} entity/entities truncated at {MAX_EVENTS_PER_ENTITY}")
    if not misses:
        print("No unmerged cross-outlet pair above the lowest band.")
        print("OUTCOME C indicated -- but read it as a FLOOR, never a proof.")
        return 0

    print(
        f"{'band':>12}  {'pairs':>6}  {'A offered':>10}  {'B never':>8}  {'syndic':>7}"
    )
    for lo, hi in BANDS:
        band = [m for m in misses if lo <= m["score"] < hi]
        if not band:
            continue
        a = sum(1 for m in band if was_retrievable(conn, m, ents))
        syn = sum(1 for m in band if m["syndicated"])
        print(
            f"{lo:.2f}-{hi:.2f}  {len(band):>6}  {a / len(band):>9.0%}  "
            f"{1 - a / len(band):>7.0%}  {syn / len(band):>6.0%}"
        )

    print("\n=== The hub hypothesis: A vs B by entity frequency ===")
    print("(events carried by the busiest entity on the later event; the cap is")
    print(
        f" {comprehend.CANDIDATE_EVENT_CAP}, so above it recency alone can bury a duplicate)"
    )
    strong = [m for m in misses if m["score"] >= separator]
    print(f"restricted to the {len(strong)} pairs above the negative-class line")
    print(f"{'entity events':>14}  {'pairs':>6}  {'A offered':>10}  {'B never':>8}")
    for lo, hi in HUB_BUCKETS:
        bucket = [m for m in strong if hub_bucket(m["hubness"]) == (lo, hi)]
        if not bucket:
            continue
        a = sum(1 for m in bucket if was_retrievable(conn, m, ents))
        label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
        print(
            f"{label:>14}  {len(bucket):>6}  {a / len(bucket):>9.0%}  "
            f"{1 - a / len(bucket):>7.0%}"
        )

    print("\n=== Ceiling, by union-find over the pair graph ===")
    for label, subset in (
        ("all pairs above the line", strong),
        ("excluding syndication", [m for m in strong if not m["syndicated"]]),
    ):
        merges = min(merges_from_pairs(subset), max(0, events - 1))
        ceiling = (assertions - (events - merges)) / assertions if assertions else 0.0
        verdict = "clears" if ceiling >= 0.10 else "STILL FAILS"
        print(f"  {label:>26}: merges={merges:<6} best case={ceiling:.1%}  ({verdict})")
    print("  Syndication is the same wire copy in two feeds. Clearing the floor")
    print("  on it alone would prove feed duplication, not confirmation.")

    print("\n=== Sample pairs -- same story or not? ===")
    for m in strong[:10]:
        flag = "OFFERED" if was_retrievable(conn, m, ents) else "NOT OFFERED"
        syn = " SYNDICATED" if m["syndicated"] else ""
        print(f"\n  [{m['score']:.2f}] {flag}{syn}  hub={m['hubness']}")
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

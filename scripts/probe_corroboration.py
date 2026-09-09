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

# Used ONLY when the negative class cannot exist yet. Never printed as a
# measurement -- see report().
FALLBACK_SEPARATOR = 0.35

# Bake-off evaluation set. Bounded because each miss costs one pool query; the
# size is REPORTED, since a silently truncated evaluation reads exactly like a
# small corpus.
BAKEOFF_MIN_SCORE = 0.20
# news-brief-bqa.22: per BAND, not off the top of one arm's ranking.
BAKEOFF_PER_BAND = 160
BAKEOFF_MAX_PAIRS = 800

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
    was created? It ASKS candidate_events, rather than reproducing its ORDER BY
    (news-brief-bqa.26).

    The reimplementation this replaces ordered by `occurred_at DESC` and
    carried a comment saying the ordering must mirror production, because
    "ordering IS the mechanism under investigation". Production moved to entity
    overlap on 2026-09-08 and to trigram similarity on 2026-09-09; the
    reconstruction moved neither time, so the A/B and hub tables described a
    retired ranking while reading as current. Nothing failed, and the bake-off
    control could not notice because both of its sides were recency.

    Still deliberately GENEROUS, which is what makes a miss decisive: the ideal
    entity set from event_entities, with no entity cap. It ranks against the
    later item's title ALONE rather than a batch -- the single-item upper bound
    the batch-dilution table exists to measure the shortfall from.
    """
    entity_ids = list(ents.get(miss["later"]["event_id"], ()))
    if not entity_ids:
        return False
    offered = comprehend.candidate_events(
        conn,
        entity_ids,
        [miss["later"]["title"]],
        comprehend.Tally(),
        as_of=miss["later"]["created_at"],
    )
    return miss["earlier"]["event_id"] in {e["id"] for e in offered}


# How many slots each arm of the hybrid gets. They sum to CANDIDATE_EVENT_CAP
# so the bake-off compares strategies at EQUAL budget -- a hybrid given more
# slots than the baseline would win by size rather than by ranking.
HYBRID_SPLIT = (10, 10, 10)


def candidate_pool(conn, miss, ents):
    """Every event candidate_events COULD rank at this moment, before any cap.

    The cap is what the strategies compete over, so it must not be applied
    here: fetching a capped pool would hand every strategy the same 30 rows and
    the bake-off would measure nothing.
    """
    entity_ids = list(ents.get(miss["later"]["event_id"], ()))
    if not entity_ids:
        return []
    rows = conn.execute(
        "SELECT e.id, e.occurred_at, e.summary, array_agg(DISTINCT ee2.entity_id) "
        "FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id AND ee.entity_id = ANY(%s) "
        "JOIN event_entities ee2 ON ee2.event_id = e.id "
        "WHERE e.occurred_at >= %s::timestamptz - make_interval(days => %s) "
        "  AND e.created_at < %s "
        "GROUP BY e.id, e.occurred_at, e.summary",
        (
            entity_ids,
            miss["later"]["created_at"],
            comprehend.CANDIDATE_WINDOW_DAYS,
            miss["later"]["created_at"],
        ),
    ).fetchall()
    return [
        {"id": r[0], "occurred_at": r[1], "summary": r[2], "entities": set(r[3] or ())}
        for r in rows
    ]


def rank_recency(pool, miss, ents, conn=None):
    """The incumbent. Reproducing it in Python is what makes the bake-off
    trustworthy: if it disagrees with was_retrievable's SQL, every other
    strategy's number is measured against the wrong baseline."""
    return sorted(pool, key=lambda e: e["occurred_at"], reverse=True)


def rank_entity_overlap(pool, miss, ents, conn=None):
    """Most entities in common, recency breaking ties. The hypothesis: an event
    sharing three actors with this item is likelier to BE this item's event
    than an unrelated one that happens to be newer."""
    want = ents.get(miss["later"]["event_id"], set())
    return sorted(
        pool,
        key=lambda e: (len(want & e["entities"]), e["occurred_at"]),
        reverse=True,
    )


def rank_title_similarity(pool, miss, ents, conn=None):
    """Lexical ranking, despite lexical DETECTION being weak here. Ranking is
    an easier problem than detection: it needs the true match to beat its
    neighbours, not to clear an absolute line."""
    title = miss["later"]["title"]
    return sorted(pool, key=lambda e: similarity(title, e["summary"]), reverse=True)


def rank_hybrid(pool, miss, ents, conn=None):
    """Reserved slots per signal. Hedges instead of betting the cap on one
    ranking being right about every kind of event."""
    arms = (rank_entity_overlap, rank_title_similarity, rank_recency)
    picked, seen = [], set()
    for fn, slots in zip(arms, HYBRID_SPLIT):
        for event in fn(pool, miss, ents):
            if len(picked) >= sum(HYBRID_SPLIT):
                break
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            picked.append(event)
            slots -= 1
            if slots == 0:
                break
    return picked


def trgm_scores(conn, text, summaries):
    """pg_trgm similarity for every candidate at once, computed IN POSTGRES.

    Deliberately not reimplemented in Python. This is the exact predicate an
    `ORDER BY similarity(...)` ranking would use, and a Python re-derivation
    of trigram similarity would be a second implementation free to drift from
    the one production runs -- measuring the wrong thing while looking right.
    """
    summaries = list(summaries)
    if not summaries:
        return []
    rows = conn.execute(
        "SELECT similarity(%s, s.summary) "
        "FROM unnest(%s::text[]) WITH ORDINALITY AS s(summary, idx) "
        "ORDER BY s.idx",
        (text, summaries),
    ).fetchall()
    return [float(r[0]) for r in rows]


def _by_score(items, scores, key=lambda x: x):
    """Descending by score, ties broken by original position. sorted() over
    (score, dict) pairs would compare the dicts on a tie and raise."""
    order = sorted(range(len(items)), key=lambda i: (-scores[i], i))
    return [key(items[i]) for i in order]


def rank_pg_trgm(pool, miss, ents, conn=None):
    """Lexical ranking as production could actually run it (bqa.24).

    The 48% batched recall that makes the case for a lexical ranking was
    measured with this module's token-Jaccard `similarity()`. pg_trgm is a
    DIFFERENT signal -- character trigrams, so "Iran"/"Iranian" score high,
    and so does a shared "- Reuters" suffix. This arm exists so the predicate
    that would SHIP is the predicate that gets MEASURED, rather than
    inheriting a number earned by its cousin (news-brief-bqa.21).
    """
    if conn is None:
        raise ValueError("rank_pg_trgm scores in Postgres and needs a connection")
    scores = trgm_scores(conn, miss["later"]["title"], [e["summary"] for e in pool])
    # Mirrors `ORDER BY best DESC, e.occurred_at DESC, e.id DESC` exactly.
    # Tie-breaking any other way would make the control below disagree with
    # production on ties, which is noise dressed as a finding.
    ordered = sorted(
        zip(scores, pool),
        key=lambda sp: (-sp[0], -sp[1]["occurred_at"].timestamp(), -sp[1]["id"]),
    )
    return [event for _, event in ordered]


def batch_pg_trgm(pool, batch, ents, cap, conn=None):
    """One list ranked by the BEST trigram match to any item in the batch --
    the same shape batch_similarity uses, so the two stay comparable and the
    difference between them is the SIGNAL rather than the strategy."""
    if conn is None:
        raise ValueError("batch_pg_trgm scores in Postgres and needs a connection")
    summaries = [e["summary"] for e in pool]
    best = [0.0] * len(pool)
    for row in batch:
        for i, score in enumerate(trgm_scores(conn, row["title"], summaries)):
            if score > best[i]:
                best[i] = score
    return _by_score(pool, best, key=lambda e: e["id"])[:cap]


STRATEGIES = {
    "recency (today)": rank_recency,
    "entity overlap": rank_entity_overlap,
    "title similarity": rank_title_similarity,
    "pg_trgm (SQL)": rank_pg_trgm,
    "hybrid 10/10/10": rank_hybrid,
}


def bake_off(conn, misses, ents, cap):
    """recall@cap per strategy: of the duplicates we know exist, how many would
    each ranking have put in front of the model?

    Returns (results, agreement) where `agreement` is the control: the arm
    standing in for PRODUCTION must reproduce was_retrievable's verdict. A
    bake-off whose baseline disagrees with the live query is measuring an
    adjacent system.

    It compares the pg_trgm arm, not recency. Comparing recency against a
    recency reconstruction was symmetric -- both sides moved together, so the
    control stayed green through two ranking changes and could not have caught
    either (news-brief-bqa.26).
    """
    results = {name: 0 for name in STRATEGIES}
    agree = disagree = 0
    for miss in misses:
        pool = candidate_pool(conn, miss, ents)
        if not pool:
            continue
        target = miss["earlier"]["event_id"]
        for name, fn in STRATEGIES.items():
            top = {e["id"] for e in fn(pool, miss, ents, conn=conn)[:cap]}
            if target in top:
                results[name] += 1
        baseline = target in {
            e["id"] for e in rank_pg_trgm(pool, miss, ents, conn=conn)[:cap]
        }
        if baseline == was_retrievable(conn, miss, ents):
            agree += 1
        else:
            disagree += 1
    return results, (agree, disagree)


def integrate_batch_size() -> int:
    """Production's real batch size, read from the live knob rather than
    assumed. The whole point of this section is that 30 slots are shared by
    THIS many items, so a hardcoded 5 would measure a system nobody runs."""
    try:
        return max(1, int(comprehend.common.COMPREHEND_INTEGRATE_BATCH))
    except Exception:
        return 5


def batch_neighbours(rows, miss, size):
    """The other items that would share this miss's candidate list.

    Production chunks `ORDER BY i.id LIMIT 300` into groups of `size`, so a
    batch is items processed in the same pass. Nearest-in-time is the closest
    reconstruction available: exact historical batches were never recorded.
    """
    target = miss["later"]["created_at"]
    seen = {miss["later"]["event_id"], miss["earlier"]["event_id"]}
    others = []
    for row in sorted(
        rows, key=lambda r: abs((r["created_at"] - target).total_seconds())
    ):
        if row["event_id"] in seen:
            continue
        seen.add(row["event_id"])
        others.append(row)
        if len(others) >= size - 1:
            break
    return [miss["later"]] + others


def candidate_pool_for(conn, entity_ids, at):
    """The uncapped pool for an explicit entity set. Split out from
    candidate_pool so a BATCH's union of entities can be passed in -- which is
    what production actually retrieves against."""
    if not entity_ids:
        return []
    rows = conn.execute(
        "SELECT e.id, e.occurred_at, e.summary, array_agg(DISTINCT ee2.entity_id) "
        "FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id AND ee.entity_id = ANY(%s) "
        "JOIN event_entities ee2 ON ee2.event_id = e.id "
        "WHERE e.occurred_at >= %s::timestamptz - make_interval(days => %s) "
        "  AND e.created_at < %s "
        "GROUP BY e.id, e.occurred_at, e.summary",
        (list(entity_ids), at, comprehend.CANDIDATE_WINDOW_DAYS, at),
    ).fetchall()
    return [
        {"id": r[0], "occurred_at": r[1], "summary": r[2], "entities": set(r[3] or ())}
        for r in rows
    ]


def batch_recency(pool, batch, ents, cap, conn=None):
    """Production today: one list, ordered by recency, shared by the batch."""
    return [
        e["id"] for e in sorted(pool, key=lambda e: e["occurred_at"], reverse=True)
    ][:cap]


def batch_similarity(pool, batch, ents, cap, conn=None):
    """One list ranked by the BEST similarity to any item in the batch. The
    simplest adaptation of the winning single-item ranking, and the one that
    dilution would hurt: five items compete for the same 30 slots."""
    titles = [row["title"] for row in batch]
    scored = sorted(
        pool,
        key=lambda e: max(similarity(t, e["summary"]) for t in titles),
        reverse=True,
    )
    return [e["id"] for e in scored][:cap]


def batch_reserved(pool, batch, ents, cap, conn=None):
    """Slots reserved PER ITEM, so a loud item cannot crowd out a quiet one.
    Each item gets cap // len(batch) picks ranked against its own title; any
    remainder is filled by the batch-wide ranking, so the budget is identical."""
    per_item = max(1, cap // max(1, len(batch)))
    picked, seen = [], set()
    for row in batch:
        ranked = sorted(
            pool, key=lambda e: similarity(row["title"], e["summary"]), reverse=True
        )
        taken = 0
        for event in ranked:
            if taken >= per_item or len(picked) >= cap:
                break
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            picked.append(event["id"])
            taken += 1
    for event_id in batch_similarity(pool, batch, ents, cap):
        if len(picked) >= cap:
            break
        if event_id not in seen:
            seen.add(event_id)
            picked.append(event_id)
    return picked


def batch_entity_overlap(pool, batch, ents, cap, conn=None):
    """The SHIPPED ranking (51c850c) in production's batched shape.

    news-brief-bqa.21. `rank_entity_overlap` was measured single-item at 57%
    and deployed, but it was never carried into BATCH_STRATEGIES, so the only
    number we had for the thing actually running described a shape production
    never uses. Batching is where the loss lives -- recency went 42% to 18% --
    and this arm has a specific reason to suffer it: ranking on the BATCH's
    entity union means one hub entity can dominate every slot for the whole
    batch, which is recency's failure wearing a different hat.
    """
    want = set()
    for row in batch:
        want |= ents.get(row["event_id"], set())
    ranked = sorted(
        pool,
        key=lambda e: (len(want & e["entities"]), e["occurred_at"]),
        reverse=True,
    )
    return [e["id"] for e in ranked][:cap]


# Which single-item arm each batched arm is the counterpart of. Declared
# rather than inferred: the names do not match, and a wrong pairing quietly
# reports one arm's dilution as another's.
SHIPPED_RANKING = "entity overlap"
# The arms that score in Postgres and therefore cannot run in a pure test.
# Named here rather than inferred, so the CI-safe tests can assert that the
# set they skip is EXACTLY this one -- a new SQL-scored arm then fails those
# tests instead of quietly escaping their coverage.
SQL_SCORED = frozenset({"pg_trgm (SQL)", "pg_trgm, batched"})

DILUTION_PAIRS = (
    ("entity overlap", "entity overlap, batched"),
    ("pg_trgm (SQL)", "pg_trgm, batched"),
    ("title similarity", "similarity, batched"),
    ("recency (today)", "recency, batched"),
)

BATCH_STRATEGIES = {
    "recency, batched": batch_recency,
    "entity overlap, batched": batch_entity_overlap,
    "similarity, batched": batch_similarity,
    "pg_trgm, batched": batch_pg_trgm,
    "reserved per item": batch_reserved,
}


def batch_bake_off(conn, misses, ents, rows, cap, size):
    """recall@cap when one candidate list is shared by `size` items.

    The single-item bake-off is an UPPER BOUND: it gave every slot to one item.
    This is what production would actually see, and the gap between them is the
    cost of batching.
    """
    results = {name: 0 for name in BATCH_STRATEGIES}
    evaluated = 0
    for miss in misses:
        batch = batch_neighbours(rows, miss, size)
        entity_union = set()
        for row in batch:
            entity_union |= ents.get(row["event_id"], set())
        pool = candidate_pool_for(conn, entity_union, miss["later"]["created_at"])
        if not pool:
            continue
        evaluated += 1
        target = miss["earlier"]["event_id"]
        for name, fn in BATCH_STRATEGIES.items():
            if target in fn(pool, batch, ents, cap, conn=conn):
                results[name] += 1
    return results, evaluated


def clusters_from_pairs(misses) -> list[set]:
    """Connected components of the pair graph. Merging a cluster of k events
    removes k-1 events, NOT C(k,2) -- counting pairs as merges is what
    produced a 1779% ceiling in the first version of this script.

    Returns the components themselves rather than just their sizes, because
    the outlet direction needs to know WHICH events merged: a cluster's
    corroboration is the union of its members' outlets, and a member that was
    already multi-outlet must not be counted twice.
    """
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
    groups = defaultdict(set)
    for node in parent:
        groups[find(node)].add(node)
    return list(groups.values())


def merges_from_pairs(misses) -> int:
    return sum(len(c) - 1 for c in clusters_from_pairs(misses))


def outlet_ceiling(misses, outlets_by_event, total, multi):
    """Ceiling on `corroboration_by_outlet` -- the FAILING spec 8.2 direction.

    news-brief-bqa.20. The ceiling this script shipped with was
    `(assertions - (events - merges)) / assertions`, which is
    `score_match_rate_corroboration`: the OTHER direction, and one that
    already clears 10% with no merges at all. It printed "(clears)" for a
    metric nobody is worried about while the failing one went uncomputed.
    Same conflation that created bqa.19, surviving inside the diagnostic
    built to investigate it.

    Merging changes BOTH sides of this fraction, which is why it cannot be
    eyeballed from the merge count: the denominator loses k-1 events per
    cluster, and the numerator gains a cluster only if the union of its
    members' outlets reaches two -- minus any member that was already
    multi-outlet on its own, or it is counted twice.

    `probable_misses` only ever pairs DIFFERENT outlets, so in practice every
    cluster qualifies; the union is computed anyway rather than assumed.

    Returns (rate, events_after, multi_after).
    """
    clusters = clusters_from_pairs(misses)
    merges = min(sum(len(c) - 1 for c in clusters), max(0, total - 1))
    was_multi_inside = 0
    becomes_multi = 0
    for cluster in clusters:
        union = set()
        for event_id in cluster:
            outlets = outlets_by_event.get(event_id, set())
            union |= outlets
            if len(outlets) >= 2:
                was_multi_inside += 1
        if len(union) >= 2:
            becomes_multi += 1
    after = max(1, total - merges)
    multi_after = multi - was_multi_inside + becomes_multi
    return (multi_after / after if after else 0.0), after, multi_after


def corroboration_by_outlet(conn, days):
    """The failing direction, measured rather than remembered (bqa.20).

    Scoped to the same window as the miss population, so the ceiling above is
    not a windowed numerator over a whole-KB denominator (news-brief-bqa.23
    still covers the older lines that mix the two).
    """
    total, multi = conn.execute(
        "WITH per_event AS ("
        "  SELECT e.id, count(DISTINCT i.outlet_id) AS outlets "
        "  FROM events e "
        "  JOIN assertions a ON a.event_id = e.id "
        "  JOIN items i ON i.id = a.item_id "
        "  WHERE e.created_at >= now() - make_interval(days => %s) "
        "  GROUP BY e.id) "
        "SELECT count(*), count(*) FILTER (WHERE outlets >= 2) FROM per_event",
        (days,),
    ).fetchone()
    return (multi / total if total else 0.0), total, multi


def sample_by_band(misses, per_band, rng):
    """A random sample WITHIN each similarity band (news-brief-bqa.22).

    The bake-off's subset was `[m for m in misses if score >= 0.20][:800]` --
    the top 800 by title-to-title similarity -- while `rank_title_similarity`
    sorts on title-to-summary similarity, the same signal through one
    paraphrase step. That arm was therefore measured exactly where it is
    strongest, and the set excluded the majority class: positives reach down
    to p50=0.143, so most of the model's own cross-outlet merges never
    entered it.

    Sampling inside a band holds the selection constant across arms, and
    reporting per band shows whether a lexical arm's edge survives into the
    low-overlap bands where most true duplicates actually live.
    """
    by_band = defaultdict(list)
    for miss in misses:
        band = band_of(miss["score"])
        if band:
            by_band[band].append(miss)
    return {
        band: rng.sample(members, min(per_band, len(members)))
        for band, members in sorted(by_band.items())
    }


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
    # An empty negative class must NOT be printed as a measured p99. The first
    # run did exactly that -- it fell back to 0.35 and labelled it "p99 of
    # NEGATIVES", which is an invented number wearing a measurement's name.
    if neg:
        separator = quantile(neg, 0.99)
        source = "p99 of NEGATIVES (measured)"
    else:
        separator = FALLBACK_SEPARATOR
        source = "FALLBACK, NOT MEASURED"
        print(
            f"\n  !! No negative pairs exist: the class needs items "
            f"{NEGATIVE_MIN_DAYS}+ days apart and the KB is younger than that.\n"
            f"  !! Using {FALLBACK_SEPARATOR} as an ARBITRARY cut. Every "
            "'above the line' count below\n"
            "  !! inherits that choice. Comparisons BETWEEN buckets stay valid "
            "(one cut for all);\n"
            "  !! absolute counts do not. Re-run once the corpus spans "
            f"{NEGATIVE_MIN_DAYS}+ days."
        )
    kept = sum(1 for s in pos if s >= separator) / len(pos)
    print(f"\nseparator = {separator:.3f}  [{source}]  keeps {kept:.0%} of positives")
    if kept < 0.5:
        print(
            f"  NOTE: it discards {1 - kept:.0%} of KNOWN duplicates, so every "
            "count below is a\n  FLOOR. Positives reach down to "
            f"p50={quantile(pos, 0.50):.3f}: two outlets covering one event "
            "genuinely\n  share little vocabulary, so lexical detection cannot "
            "separate the classes here."
        )

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

    outlets_by_event = defaultdict(set)
    for row in rows:
        outlets_by_event[row["event_id"]].add(row["outlet_id"])
    base_rate, base_total, base_multi = corroboration_by_outlet(conn, days)

    print("\n=== Ceiling, by union-find over the pair graph ===")
    print("corroboration_by_outlet is the FAILING 8.2 direction and the only")
    print("one the 10% floor belongs to. An earlier version of this script")
    print("compared the MATCH-RATE direction to that floor and printed")
    print("'clears' -- for a number that already clears with zero merges.")
    print(
        f"today, scoped to {days}d: events={base_total} multi-outlet="
        f"{base_multi} rate={base_rate:.1%}"
    )
    for label, subset in (
        ("all pairs above the line", strong),
        ("excluding syndication", [m for m in strong if not m["syndicated"]]),
    ):
        ceiling, after, multi_after = outlet_ceiling(
            subset, outlets_by_event, base_total, base_multi
        )
        verdict = "clears" if ceiling >= 0.10 else "STILL FAILS"
        print(
            f"  {label:>26}: events={after:<6} multi={multi_after:<5} "
            f"best case={ceiling:.1%}  ({verdict})"
        )
    print("  Syndication is the same wire copy in two feeds. Clearing the floor")
    print("  on it alone would prove feed duplication, not confirmation.")
    print("  THIS CEILING IS A FLOOR. The separator keeps only a fraction of")
    print("  known positives (see the calibration block), and a detector bounds")
    print("  duplicates from BELOW, so the true ceiling is higher by roughly")
    print("  the reciprocal of that fraction.")
    print("  The OTHER direction, for reference only -- it is not what the 10%")
    print("  floor gates:")
    for label, subset in (
        ("all pairs above the line", strong),
        ("excluding syndication", [m for m in strong if not m["syndicated"]]),
    ):
        merges = min(merges_from_pairs(subset), max(0, events - 1))
        rate_after = (
            (assertions - (events - merges)) / assertions if assertions else 0.0
        )
        print(f"    match rate, {label}: merges={merges:<6} -> {rate_after:.1%}")

    cap = comprehend.CANDIDATE_EVENT_CAP
    print("")
    print(f"=== Ranking bake-off: recall@{cap}, sampled WITHIN each band ===")
    print("The old subset was the top 800 by title-title similarity, which is")
    print("the signal one arm ranks on -- it measured that arm where it is")
    print("strongest and excluded the majority class (positives reach p50")
    print("well below the separator). Sampling inside a band holds the")
    print("selection constant across arms, and the trend ACROSS bands is the")
    print("answer to whether a lexical edge survives into low overlap, where")
    print("most true duplicates live.")
    banded = sample_by_band(misses, BAKEOFF_PER_BAND, rng)
    subset = [m for members in banded.values() for m in members]
    print(f"evaluated on {len(subset)} pairs, <= {BAKEOFF_PER_BAND} per band")
    header = "".join(f"{name:>18}" for name in STRATEGIES)
    print(f"{'band':>12}{'n':>6}{header}")
    totals = dict.fromkeys(STRATEGIES, 0)
    for band, members in banded.items():
        results, (agree, disagree) = bake_off(conn, members, ents, cap)
        if disagree:
            print(f"  !! band {band}: recency arm disagreed with the live SQL")
        for name in STRATEGIES:
            totals[name] += results[name]
        cells = "".join(
            f"{results[name] / len(members):>18.0%}" if members else f"{'--':>18}"
            for name in STRATEGIES
        )
        print(f"{band[0]:>5.2f}-{band[1]:<6.2f}{len(members):>6}{cells}")
    print("  Equal budget: every strategy ranks the SAME pool into the same")
    print("  number of slots, so a win is ranking rather than volume.")

    size = integrate_batch_size()
    print("")
    print(f"=== Batch dilution: {cap} slots shared by {size} items ===")
    print("The table above gave every slot to ONE item, which is an upper")
    print("bound. Production shares one candidate list across a batch, so the")
    print("gap between the two tables IS the cost of batching.")
    if subset:
        bresults, evaluated = batch_bake_off(conn, subset, ents, rows, cap, size)
        print(f"evaluated on {evaluated} pairs")
        if evaluated:
            bbest = max(bresults.values())
            for name, hits in sorted(bresults.items(), key=lambda kv: -kv[1]):
                mark = "  <-- best" if hits == bbest and bbest else ""
                print(
                    f"  {name:>24}: {hits:>4}/{evaluated}  "
                    f"recall={hits / evaluated:.0%}{mark}"
                )
            print("  dilution, per arm that has both shapes:")
            for single_name, batch_name in DILUTION_PAIRS:
                if single_name not in totals or batch_name not in bresults:
                    continue
                single = totals[single_name] / len(subset)
                batched = bresults[batch_name] / evaluated
                shipped = "  <-- SHIPPED" if single_name == SHIPPED_RANKING else ""
                print(
                    f"    {single_name:>18}: {single:.0%} single-item -> "
                    f"{batched:.0%} batched ({batched - single:+.0%}){shipped}"
                )

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

"""Can grouping replace the visibility that batching takes away?

news-brief-vlg, step 2: the spike the phase-2 plan rewrite waits on. Read-only
and model-free: it SELECTs, computes, prints, and writes nothing. Safe against
production, and safe during the gate window, because it touches no table the
gate reads from except to read it.

THE QUESTION. Real time commits after every micro-batch of 5, so micro-batch k
of a pass is offered the events micro-batches 1..k-1 created. The only blind
pairs are two items in the SAME micro-batch. Phase 2 builds every request of a
submission before any of them runs, so requests are mutually blind, and the
blind set grows to "same submission". Only grouping gives the visibility back:
items in one request can link to each other's NEW events (spec 5.4). This
probe measures how much it gives back.

THE POPULATION. Every pair of assertions on one event from DIFFERENT outlets
whose items were captured within PAIR_GAP_HOURS of each other. Capture time,
not publication, because triage runs on capture and so decides which
submission an item lands in.

Two biases, both stated rather than corrected:
  - The old matcher found these pairs by title-vs-summary trigram. They lean
    lexically similar, so the title variants' recall is a CEILING.
  - Pairs that shared a historical micro-batch were never linked, so they are
    absent altogether.
The historical material set is also denser than phase 1's: entity-only items
no longer become material by rule. Denser windows split pairs across more RT
chunks, which helps RT, and crowd the cluster cap, which hurts grouping. Both
effects go against batching, so the comparison is conservative.

THE COMPARATOR IS REAL TIME, NOT 1.0 (phase-2 red-team (a), fix 3). Real time
already loses same-micro-batch pairs. The question is how many MORE batching
loses:

  RT     1h windows (a real-time pass never waits), id DESC, chunks of
         COMPREHEND_INTEGRATE_BATCH. Visible if the pair is in DIFFERENT chunks.
  T      title pg_trgm >= CLUSTER_SIMILARITY, seed-star, cap CLUSTER_MAX_ITEMS:
         spec 5.3 and the draft plan's `cluster` as written.
  T-cc   the same edge, as capped BFS (transitive; the red-team's A-B-C case).
  TE-cc  title edge OR a shared entity hit that EXISTED BEFORE the submission.

For the batching variants, visible means the pair is in the SAME request. Every
variant counts a pair whose items fall in different windows as visible: the
later submission is offered the earlier one's events.

THE ENTITY EDGE CANNOT USE TODAY'S INDEX AS IT STANDS. SurfaceIndex.build loads
every entity, including the ones these very items minted when they were
integrated. Unfiltered, a pair would "share an entity" because item A created
it, which inflates the entity edge exactly where it matters (new stories). An
entity counts only if it was BORN before the window opened. Birth is the
earliest capture among items asserting an event tagged with it, or
entities.created_at if that is earlier. entities.created_at alone is wrong: the
backlog integrated items days after capture, so it is late by the backlog's lag.

PRE-REGISTERED 2026-09-26, before any run (bead news-brief-vlg's spike child):
  guesses: T 40-60%; TE-cc 85%+, with components over the cap on hub days;
           RT 85-95%.
  RULE (operator's choice): at W = 2h, the SIMPLEST variant in the order
  T, T-cc, TE-cc whose visible share is within TOLERANCE of RT's is what the
  plan rewrite builds. If none qualifies, the rewrite redesigns the blindness
  before any batching work. A disagreement with the guesses is REPORTED, not
  reconciled.

Run:  docker compose run --rm --entrypoint python newsbrief \\
          scripts/probe_clustering.py
"""

import datetime as dt
import math
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import common  # noqa: E402  (path shim above must run first)
import comprehend  # noqa: E402
import db  # noqa: E402

PAIR_GAP_HOURS = 2
WINDOW_HOURS = (1, 2)
DECISION_WINDOW_HOURS = 2
RT_WINDOW_HOURS = 1

# Spec 5.3's values, copied because phase 2 has not built them yet. When it
# does, this probe must import them instead (reconstruction-drifts-from-
# production).
CLUSTER_SIMILARITY = 0.35
CLUSTER_MAX_ITEMS = 8

TOLERANCE = 0.05
MIN_PAIRS = 30
VARIANTS = ("T", "T-cc", "TE-cc")  # simplest first: the rule's order
REDESIGN = "NONE: redesign the blindness before batching"
NOT_MEASURABLE = "NOT MEASURABLE: too few pairs"

# Similarity bands for the co-windowed pairs, so the reader can see what a
# different threshold would buy without re-running.
BANDS = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.7), (0.7, 1.01)]

# Items the exactness control re-matches through SurfaceIndex.match itself.
EXACTNESS_SAMPLE = 200

_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class Pair:
    a_id: int
    b_id: int
    a_at: dt.datetime
    b_at: dt.datetime


# --- Pure: windows and request shapes.


def window_key(at: dt.datetime, hours: int) -> int:
    """Clock-aligned: a submission takes what triage produced since the last."""
    return int(at.timestamp() // (hours * 3600))


def rt_groups(ids, size: int) -> dict[int, int]:
    """Real time's micro-batches: id DESC (phase 1, D4), chunks of `size`."""
    ordered = sorted(ids, reverse=True)
    return {i: n // size for n, i in enumerate(ordered)}


def _neighbours(edges: dict) -> dict[int, list[tuple[float, int]]]:
    out: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for (a, b), s in edges.items():
        out[a].append((s, b))
        out[b].append((s, a))
    for v in out.values():
        v.sort(key=lambda t: (-t[0], -t[1]))
    return out


def seed_star(ids_newest_first, edges: dict, cap: int) -> list[list[int]]:
    """The draft plan's `cluster`, verbatim in behaviour: newest unassigned
    seed, then its unassigned DIRECT neighbours by descending similarity."""
    nb = _neighbours(edges)
    assigned: set[int] = set()
    groups = []
    for seed in ids_newest_first:
        if seed in assigned:
            continue
        group = [seed]
        assigned.add(seed)
        for _, other in nb.get(seed, []):
            if len(group) >= cap:
                break
            if other not in assigned:
                group.append(other)
                assigned.add(other)
        groups.append(group)
    return groups


def capped_bfs(ids_newest_first, edges: dict, cap: int) -> list[list[int]]:
    """Transitive: breadth-first from the newest unassigned seed, strongest
    edge first, until the cap. What is left of a component seeds the next."""
    nb = _neighbours(edges)
    assigned: set[int] = set()
    groups = []
    for seed in ids_newest_first:
        if seed in assigned:
            continue
        group, frontier = [seed], [seed]
        assigned.add(seed)
        while frontier and len(group) < cap:
            nxt = []
            for node in frontier:
                for _, other in nb.get(node, []):
                    if len(group) >= cap:
                        break
                    if other not in assigned:
                        group.append(other)
                        assigned.add(other)
                        nxt.append(other)
            frontier = nxt
        groups.append(group)
    return groups


def pack_requests(groups, size: int) -> list[list[int]]:
    """Clusters are requests; singletons are packed `size` per request,
    newest first, as today. A pair packed into one singleton request is
    visible too: in-request linking does not care why items share a request."""
    clustered = [g for g in groups if len(g) > 1]
    singles = sorted((g[0] for g in groups if len(g) == 1), reverse=True)
    return clustered + [singles[i : i + size] for i in range(0, len(singles), size)]


def entity_edges(hits: dict[int, set[int]]) -> dict[tuple[int, int], float]:
    """Weight 0.0, BELOW every title edge: the BFS takes the strongest edge
    first, and a shared hub entity (Iran, US) is weaker evidence than a shared
    headline. Weighted 1.0 it would outrank every title match."""
    by_entity: dict[int, list[int]] = defaultdict(list)
    for item_id, ents in hits.items():
        for e in ents:
            by_entity[e].append(item_id)
    out = {}
    for members in by_entity.values():
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out[(members[i], members[j])] = 0.0
    return out


def component_sizes(ids, edges: dict) -> list[int]:
    """Uncapped connected components, to show what the cap is cutting."""
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    sizes: dict[int, int] = defaultdict(int)
    for i in ids:
        sizes[find(i)] += 1
    return list(sizes.values())


# --- Pure: visibility and the decision.


def batch_visible(p: Pair, request_of: dict, window_hours: int) -> bool:
    if window_key(p.a_at, window_hours) != window_key(p.b_at, window_hours):
        return True
    return request_of[p.a_id] == request_of[p.b_id]


def rt_visible(p: Pair, chunk_of: dict) -> bool:
    if window_key(p.a_at, RT_WINDOW_HOURS) != window_key(p.b_at, RT_WINDOW_HOURS):
        return True
    return chunk_of[p.a_id] != chunk_of[p.b_id]


def decide(shares: dict[str, float], rt: float, n: int) -> str:
    if n < MIN_PAIRS:
        return NOT_MEASURABLE
    for name in VARIANTS:
        # The epsilon makes "within 5 points" inclusive despite float error.
        if rt - shares[name] <= TOLERANCE + 1e-9:
            return name
    return REDESIGN


def paired_ci(xs: list[bool], ys: list[bool]) -> tuple[float, float, float]:
    """Mean of x - y over the SAME pairs, with a normal 95% interval."""
    d = [int(x) - int(y) for x, y in zip(xs, ys, strict=True)]
    n = len(d)
    if n < 2:
        return (0.0, 0.0, 0.0)
    m = sum(d) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in d) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return (m, m - half, m + half)


# --- Entity hits: exact, but fast enough for a whole corpus.


def _first_token(form: str) -> str | None:
    toks = _WORD.findall((form or "").lower())
    return toks[0] if toks else None


def item_entity_hits(index: "comprehend.SurfaceIndex", items) -> dict[int, set[int]]:
    """{item id: entity ids SurfaceIndex.match would return}, via a prefilter.

    index.match runs re.search per form per item, and re's cache holds 512
    patterns, so a corpus-wide run recompiles on almost every call. The
    prefilter only narrows which forms reach comprehend.form_matches, which
    stays the authority. It is a superset: a match needs the form's first word
    run to appear as a whole word run in the text, because the form is
    bounded by (?<!\\w) on the left and a non-word char or (?!\\w) on the right.
    main() still re-checks a sample through index.match and refuses on any
    disagreement: the argument above is intent, the sample is evidence.
    """
    by_token: dict[str | None, list] = defaultdict(list)
    for sf in index._forms:
        if sf.entity_id is not None:
            by_token[_first_token(sf.form)].append(sf)
    out = {}
    for it in items:
        text = f"{comprehend.clean(it['title'])}\n{comprehend.clean(it['body'])}"
        toks = set(_WORD.findall(text.lower()))
        cands = list(by_token.get(None, []))
        for t in toks:
            cands.extend(by_token.get(t, ()))
        out[it["id"]] = {
            sf.entity_id for sf in cands if comprehend.form_matches(sf.form, text)
        }
    return out


def prior_hits(hits, births, window_start) -> dict[int, set[int]]:
    """Keep only entities born before the submission's window opened."""
    return {
        i: {e for e in ents if e in births and births[e] < window_start}
        for i, ents in hits.items()
    }


def shares_prior_entity(p: Pair, hits, births) -> bool:
    """Could the entity edge join this pair at all? Born before the earlier
    item's decision window opened."""
    opened = dt.datetime.fromtimestamp(
        window_key(min(p.a_at, p.b_at), DECISION_WINDOW_HOURS)
        * DECISION_WINDOW_HOURS
        * 3600,
        tz=dt.UTC,
    )
    prior = prior_hits(
        {p.a_id: hits.get(p.a_id, set()), p.b_id: hits.get(p.b_id, set())},
        births,
        opened,
    )
    return bool(prior[p.a_id] & prior[p.b_id])


# --- Queries.


def cross_outlet_pairs(conn, max_gap_hours: int) -> list[Pair]:
    rows = conn.execute(
        "SELECT DISTINCT ia.id, ib.id, ia.created_at, ib.created_at "
        "FROM assertions aa JOIN assertions ab "
        "  ON ab.event_id = aa.event_id AND ab.item_id > aa.item_id "
        "JOIN items ia ON ia.id = aa.item_id "
        "JOIN items ib ON ib.id = ab.item_id "
        "WHERE ia.outlet_id <> ib.outlet_id "
        "  AND abs(extract(epoch FROM ib.created_at - ia.created_at)) <= %s",
        (max_gap_hours * 3600,),
    ).fetchall()
    return [Pair(r[0], r[1], r[2], r[3]) for r in rows]


def material_items(conn, start, end) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.created_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.verdict = 'material' AND i.created_at >= %s AND i.created_at < %s "
        "ORDER BY i.id",
        (comprehend.TRIAGE_PROMPT_VERSION, start, end),
    ).fetchall()
    # Production drops quote pages at integration (comprehend.run); mirror the
    # predicate by calling it, never by re-deriving it.
    return [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "created_at": r[4],
        }
        for r in rows
        if not common.is_quote_page(r[1])
    ]


def title_edges(conn, ids, threshold: float) -> dict[tuple[int, int], float]:
    """pg_trgm on RAW titles, as candidate_events and the draft plan use them."""
    rows = conn.execute(
        "SELECT a.id, b.id, similarity(a.title, b.title) "
        "FROM items a JOIN items b ON a.id < b.id "
        "WHERE a.id = ANY(%s) AND b.id = ANY(%s) "
        "  AND similarity(a.title, b.title) >= %s",
        (list(ids), list(ids), threshold),
    ).fetchall()
    return {(a, b): float(s) for a, b, s in rows}


def pair_similarity(conn, pairs: list[Pair]) -> dict[tuple[int, int], float]:
    if not pairs:
        return {}
    rows = conn.execute(
        "SELECT a.id, b.id, similarity(a.title, b.title) "
        "FROM unnest(%s::bigint[], %s::bigint[]) AS p(x, y) "
        "JOIN items a ON a.id = p.x JOIN items b ON b.id = p.y",
        ([p.a_id for p in pairs], [p.b_id for p in pairs]),
    ).fetchall()
    return {(a, b): float(s) for a, b, s in rows}


def entity_births(conn) -> dict[int, dt.datetime]:
    rows = conn.execute(
        "SELECT en.id, least(en.created_at, min(i.created_at)) "
        "FROM entities en "
        "LEFT JOIN event_entities ee ON ee.entity_id = en.id "
        "LEFT JOIN assertions a ON a.event_id = ee.event_id "
        "LEFT JOIN items i ON i.id = a.item_id "
        "GROUP BY en.id, en.created_at"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# --- The run.


def _pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def measure(conn, pairs: list[Pair], index, births) -> dict:
    """Every variant's visibility over the same pairs, per window size."""
    start = min(min(p.a_at, p.b_at) for p in pairs) - dt.timedelta(hours=2)
    end = max(max(p.a_at, p.b_at) for p in pairs) + dt.timedelta(hours=2)
    items = material_items(conn, start, end)
    by_id = {it["id"]: it for it in items}
    pair_ids = {p.a_id for p in pairs} | {p.b_id for p in pairs}
    missing = pair_ids - set(by_id)
    hits = item_entity_hits(index, [by_id[i] for i in pair_ids if i in by_id])
    report: dict = {"items": len(items), "missing": len(missing), "by_window": {}}

    # Only pairs whose items are both in the membership can be placed at all.
    usable = [p for p in pairs if p.a_id in by_id and p.b_id in by_id]
    report["usable"] = usable

    # RT: 1h windows, independent of W.
    rt_win: dict[int, list[int]] = defaultdict(list)
    for it in items:
        rt_win[window_key(it["created_at"], RT_WINDOW_HOURS)].append(it["id"])
    chunk_of = {}
    for k, ids in rt_win.items():
        for i, c in rt_groups(ids, int(common.COMPREHEND_INTEGRATE_BATCH)).items():
            chunk_of[i] = (k, c)
    report["rt"] = [rt_visible(p, chunk_of) for p in usable]

    wanted_windows = {
        h: {window_key(by_id[i]["created_at"], h) for i in pair_ids if i in by_id}
        for h in WINDOW_HOURS
    }
    for h in WINDOW_HOURS:
        windows: dict[int, list[dict]] = defaultdict(list)
        for it in items:
            k = window_key(it["created_at"], h)
            if k in wanted_windows[h]:
                windows[k].append(it)
        request_of = {v: {} for v in VARIANTS}
        sizes = {v: [] for v in VARIANTS}
        components: list[int] = []
        for k, members in windows.items():
            ids = sorted((m["id"] for m in members), reverse=True)
            window_start = dt.datetime.fromtimestamp(k * h * 3600, tz=dt.UTC)
            t_edges = title_edges(conn, ids, CLUSTER_SIMILARITY)
            w_hits = item_entity_hits(index, members)
            e_edges = entity_edges(prior_hits(w_hits, births, window_start))
            te_edges = {**e_edges, **t_edges}  # title similarity wins the ordering
            components.extend(component_sizes(ids, te_edges))
            shapes = {
                "T": seed_star(ids, t_edges, CLUSTER_MAX_ITEMS),
                "T-cc": capped_bfs(ids, t_edges, CLUSTER_MAX_ITEMS),
                "TE-cc": capped_bfs(ids, te_edges, CLUSTER_MAX_ITEMS),
            }
            for v, groups in shapes.items():
                reqs = pack_requests(groups, int(common.COMPREHEND_INTEGRATE_BATCH))
                for n, req in enumerate(reqs):
                    sizes[v].append(len(req))
                    for i in req:
                        request_of[v][i] = (k, n)
        report["by_window"][h] = {
            "windows": len(windows),
            "visible": {
                v: [batch_visible(p, request_of[v], h) for p in usable]
                for v in VARIANTS
            },
            "co_windowed": sum(
                window_key(p.a_at, h) == window_key(p.b_at, h) for p in usable
            ),
            "sizes": sizes,
            "components": components,
        }
    report["hits"] = hits
    return report


def exactness_control(index, items, rng) -> int:
    """How many sampled items the prefilter disagrees with index.match on."""
    sample = rng.sample(items, min(EXACTNESS_SAMPLE, len(items)))
    fast = item_entity_hits(index, sample)
    bad = 0
    for it in sample:
        text = f"{comprehend.clean(it['title'])}\n{comprehend.clean(it['body'])}"
        slow = {sf.entity_id for sf in index.match(text) if sf.entity_id is not None}
        bad += fast[it["id"]] != slow
    return bad


def main() -> int:
    rng = random.Random(20260926)
    with db.connect() as conn:
        pairs = cross_outlet_pairs(conn, PAIR_GAP_HOURS)
        print(
            f"Cross-outlet same-event pairs captured <= {PAIR_GAP_HOURS}h apart: {len(pairs)}"
        )
        if len(pairs) < MIN_PAIRS:
            print(f"VERDICT: {NOT_MEASURABLE} (need {MIN_PAIRS})")
            return 0
        index = comprehend.SurfaceIndex.build(conn)
        births = entity_births(conn)
        start = min(min(p.a_at, p.b_at) for p in pairs) - dt.timedelta(hours=2)
        end = max(max(p.a_at, p.b_at) for p in pairs) + dt.timedelta(hours=2)
        bad = exactness_control(index, material_items(conn, start, end), rng)
        print(
            f"Exactness control: {bad} of <= {EXACTNESS_SAMPLE} sampled items disagree"
        )
        if bad:
            print("REFUSING: the prefiltered matcher is not exact on this corpus.")
            return 2

        r = measure(conn, pairs, index, births)
        usable = r["usable"]
        print(
            f"Material items in range: {r['items']}; pair items NOT in it: {r['missing']}"
        )
        print(f"Usable pairs (both items material): {len(usable)}")
        rt = r["rt"]
        rt_share = sum(rt) / len(rt) if rt else 0.0
        print(
            f"\nRT (1h passes, chunks of {common.COMPREHEND_INTEGRATE_BATCH}): {_pct(rt_share)}"
        )

        for h in WINDOW_HOURS:
            w = r["by_window"][h]
            print(
                f"\n--- W = {h}h: {w['windows']} windows, {w['co_windowed']} pairs co-windowed"
            )
            print(
                f"{'variant':8} {'visible':>8} {'vs RT (95% CI)':>28} {'reqs':>6} {'mean':>5} {'max':>4}"
            )
            for v in VARIANTS:
                vis = w["visible"][v]
                m, lo, hi = paired_ci(vis, rt)
                s = w["sizes"][v]
                print(
                    f"{v:8} {_pct(sum(vis) / len(vis))} "
                    f"{100 * m:+6.1f} ({100 * lo:+5.1f}, {100 * hi:+5.1f}) pts "
                    f"{len(s):6d} {sum(s) / len(s):5.1f} {max(s):4d}"
                )
            comps = sorted(w["components"])
            if comps:
                over = sum(c > CLUSTER_MAX_ITEMS for c in comps)
                print(
                    f"TE components (uncapped): p50 {comps[len(comps) // 2]}, "
                    f"p90 {comps[int(0.9 * (len(comps) - 1))]}, max {comps[-1]}, "
                    f"{over} over the cap of {CLUSTER_MAX_ITEMS}"
                )

        sims = pair_similarity(conn, usable)
        print("\nTitle similarity of usable pairs (what another threshold would buy):")
        for lo, hi in BANDS:
            n = sum(lo <= s < hi for s in sims.values())
            print(
                f"  [{lo:.2f}, {hi:.2f}): {n:5d}  {_pct(n / len(sims)) if sims else ''}"
            )
        with_entity = sum(shares_prior_entity(p, r["hits"], births) for p in usable)
        print(
            f"Usable pairs sharing a prior-born entity: {with_entity} of {len(usable)}"
        )

        w = r["by_window"][DECISION_WINDOW_HOURS]
        shares = {v: sum(w["visible"][v]) / len(w["visible"][v]) for v in VARIANTS}
        verdict = decide(shares, rt_share, len(usable))
        print(
            f"\nPRE-REGISTERED guesses: T 40-60%, TE-cc 85%+, RT 85-95%. "
            f"Rule: simplest of {', '.join(VARIANTS)} within {100 * TOLERANCE:.0f} pts of RT "
            f"at W = {DECISION_WINDOW_HOURS}h."
        )
        print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

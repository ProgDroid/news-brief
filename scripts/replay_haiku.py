"""M2: does Haiku 4.5 integrate like Sonnet? (news-brief-y1x)

Phase 2's route choice (news-brief-vlg) waits on two measurements. M1, the
density sweep, is free and done. This is M2, pre-registered in
docs/2026-09-26-clustering-recall-spike-result.md ("Next: M2") before anything
here was built.

READ-ONLY, BUT PAID. It SELECTs, calls the model, prints, and rolls back:
nothing is written, so no item_triage counter moves. The calls are REAL
Anthropic calls and, like scripts/inspect_integration.py's, they are recorded
in neither `comprehend_spend` nor the comprehension token bucket. They show up
only on the account console. SPEND_CAP_USD bounds them.

THE BATCHES. The pre-registration asks for about 150 historical five-item
integration batches, weighted toward the later item of each cross-outlet pair.
The operator settled the mix on 2026-09-26:
  - PAIR_BATCHES anchored on the LATER item of a random cross-outlet pair
    captured <= PAIR_GAP_HOURS apart. These are probe_clustering's pairs, from
    its own function. Both items must be material.
  - RANDOM_BATCHES anchored on a random material item.
Either way the anchor goes with the up to BATCH_SIZE - 1 material items
captured just before it, within RT_WINDOW_HOURS. That is what a real-time
micro-batch would have held. Items are listed id DESC, as pending_integration
lists them.

AS OF THE BATCH, NOT THE ANCHOR (review B1, 2026-09-26). In real time a
micro-batch's members are integrated TOGETHER, so nothing they produce exists
when the call is made. The cut-off is therefore the EARLIEST member's capture
time, not the anchor's. With the anchor's, a member captured 30 minutes earlier
could be offered its own entity, and its own event (events.created_at is when
the backlog integrated it). That would inflate both agreements toward 100% and
bias the verdict toward "qualifies".
  - Entities are offered only if BORN before the cut-off. An entity's birth is
    the capture time of the earliest item asserting one of its events whose
    TEXT NAMES IT, capped at entities.created_at (review M1). probe_clustering's
    entity_births is not used, because it dates an entity from ANY item
    asserting a tagged event. write_extraction tags a MATCHED older event with
    the matching item's new entities, so that rule dates them before they
    existed.
  - Events come through candidate_events(as_of=cut-off), via the production
    function comprehend.integration_candidates. There is no copy of the
    ranking here. A copy is how probe_corroboration once reported a retired
    ranking as live.
The request is built ONCE per batch by build_integration_request and sent
three times, differing only in `model`: Sonnet (A), Sonnet again (A'), and
Haiku 4.5 (H). parse_integration_response reads every response.

THE METRIC, fixed before any run:
  - An item's DECISION in one run is the SET of things it linked to:
      - existing events (candidate_id);
      - in-request clusters, meaning the set of items sharing one NEW-labelled
        event, the declarer included. A cluster is order-free: which item
        declared and which referenced does not change it (review m3).
    A NEW label declared ON an existing event resolves to that event. The
    empty set means "everything is new".
  - Two runs AGREE on an item when the sets are equal.
  - Agreement is measured over items that ALL THREE runs extracted, so A-A'
    and A-H share one denominator. The operator chose "all items both kept"
    over a match-only subset. The match-only subset (items where any run
    linked something) is printed beside it and not ruled on.
  - VALIDATION DROP is the share of offered items a run failed to extract,
    for any reason: a rejected row, an item the model never returned, a
    whole-response parse failure, or a neighbour fault.

RULE (operator, 2026-09-26): Haiku QUALIFIES iff
    agreement(A,H) >= agreement(A,A') - TOLERANCE      and
    drop(H)        <= drop(A)         + TOLERANCE
over the combined batches, with at least MIN_BATCHES complete batches.
GUESSES (pre-registered): A-vs-A' 80-90%, A-vs-H 65-80%. Together these predict
that Haiku fails narrowly. A disagreement with the guesses is REPORTED, not
reconciled.

A STATED BIAS, not corrected (review m4): pair batches are selected by
production's own past links. Historical Sonnet asserted both items onto one
event, which favours A-A' over A-H on that stratum. The random stratum is the
unconfounded one, and both strata are printed.

Run on the host (there is no checkout there; this runs from the image):

    docker compose run --rm --entrypoint python newsbrief \\
        scripts/replay_haiku.py --dry-run      # free: batches and a projection
    docker compose run --rm --entrypoint python newsbrief \\
        scripts/replay_haiku.py --limit 2      # smoke, ~$0.10, no verdict
    docker compose run --rm --entrypoint python newsbrief \\
        scripts/replay_haiku.py                # the measurement

The seed is fixed, so all three select the same batches while the corpus is
frozen.
"""

import argparse
import datetime as dt
import json
import random
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import comprehend  # noqa: E402  (path shim above must run first)
import db  # noqa: E402
from scripts.probe_clustering import (  # noqa: E402
    PAIR_GAP_HOURS,
    RT_WINDOW_HOURS,
    _pair_range,
    cross_outlet_pairs,
    material_items,
    paired_ci,
)

HAIKU = "claude-haiku-4-5"
PAIR_BATCHES = 100
RANDOM_BATCHES = 50
BATCH_SIZE = 5  # the pre-registration's; also COMPREHEND_INTEGRATE_BATCH's default
TOLERANCE = 0.05
MIN_BATCHES = 30  # probe_clustering's MIN_PAIRS, for the same reason
SPEND_CAP_USD = 10.0
SEED = 20260926
NARROW_SAMPLE = 20
# Worst-case input sizing for the spend guard ONLY: 3 chars per token
# overestimates English prose, which is the safe direction for a cap.
CHARS_PER_TOKEN_WORST = 3
ATTEMPTS = 2  # call_one retries a transient fault once
RUNS = ("A", "A'", "H")

_WORD = re.compile(r"\w+")


class Abort(RuntimeError):
    """A failure that says the REQUEST or ACCOUNT is refused, so every later
    call would fail the same way. Stop spending."""


@dataclass
class Batch:
    stratum: str  # "pair" | "random"
    anchor_id: int
    items: list[dict]  # id DESC, as pending_integration lists them
    earlier_id: int | None = None  # the pair's other item, pair batches only

    @property
    def cutoff(self) -> dt.datetime:
        """The earliest member's capture: nothing a member produced exists
        before it (review B1)."""
        return min(it["created_at"] for it in self.items)


@dataclass
class RunResult:
    kept: set[int] = field(default_factory=set)
    decisions: dict[int, frozenset] = field(default_factory=dict)
    usd: float = 0.0
    failed_attempts: int = 0  # transient failures, possibly billed
    parse_error: str | None = None
    transport_error: str | None = None


# --- Pure: batch shapes, decisions, agreement, the rule.


def _order_key(it: dict) -> tuple:
    return (it["created_at"], it["id"])


def batch_around(materials: list[dict], pos: int) -> list[dict]:
    """The anchor at `materials[pos]` plus up to BATCH_SIZE - 1 items captured
    just before it, within RT_WINDOW_HOURS. `materials` is sorted by
    (created_at, id). Returned id DESC, the order production lists them in."""
    anchor = materials[pos]
    floor = anchor["created_at"] - dt.timedelta(hours=RT_WINDOW_HOURS)
    out = [anchor]
    k = pos - 1
    while k >= 0 and len(out) < BATCH_SIZE and materials[k]["created_at"] >= floor:
        out.append(materials[k])
        k -= 1
    return sorted(out, key=lambda it: it["id"], reverse=True)


def select_batches(materials, pairs, rng: random.Random) -> list[Batch]:
    """PAIR_BATCHES pair-anchored plus RANDOM_BATCHES random-anchored batches,
    shuffled together so a run stopped early by the cap or --limit still
    holds both strata."""
    materials = sorted(materials, key=_order_key)
    pos = {it["id"]: i for i, it in enumerate(materials)}
    later_of: dict[int, int] = {}
    # Sorted: cross_outlet_pairs has no ORDER BY, and a later item paired with
    # several earlier ones must keep the SAME partner between the dry run and
    # the paid run (review m5).
    for p in sorted(pairs, key=lambda p: (p.a_id, p.b_id)):
        if p.a_id not in pos or p.b_id not in pos:
            continue
        a, b = materials[pos[p.a_id]], materials[pos[p.b_id]]
        later, earlier = (a, b) if _order_key(a) > _order_key(b) else (b, a)
        later_of.setdefault(later["id"], earlier["id"])
    pair_anchors = rng.sample(sorted(later_of), min(PAIR_BATCHES, len(later_of)))
    rest = [it["id"] for it in materials if it["id"] not in later_of]
    random_anchors = rng.sample(rest, min(RANDOM_BATCHES, len(rest)))

    batches = []
    for stratum, anchors in (("pair", pair_anchors), ("random", random_anchors)):
        for aid in anchors:
            batches.append(
                Batch(
                    stratum=stratum,
                    anchor_id=aid,
                    items=batch_around(materials, pos[aid]),
                    earlier_id=later_of.get(aid) if stratum == "pair" else None,
                )
            )
    rng.shuffle(batches)
    return batches


def decisions(extractions: list[dict]) -> dict[int, frozenset]:
    """{item id: the set of things it linked to}. See the module docstring.

    A NEW label becomes the frozenset of every item sharing its event,
    declarer included, so the encoding does not depend on which item the
    model chose to declare. A label declared ON an existing event resolves to
    that event: both items are then attached to it, and that is how
    write_extraction records it."""
    declared = {}  # label -> (declaring item, candidate id or None)
    for x in extractions:
        for ev in x["events"]:
            if "new_label" in ev:
                declared[ev["new_label"]] = (x["item_id"], ev.get("candidate_id"))
    members = defaultdict(set)
    for label, (item, _) in declared.items():
        members[label].add(item)
    for x in extractions:
        for ev in x["events"]:
            if ev.get("new_ref") in declared:
                members[ev["new_ref"]].add(x["item_id"])

    def target(label):
        cid = declared[label][1]
        if cid is not None:
            return ("EVT", cid)
        return ("CLUSTER", frozenset(members[label]))

    out = {}
    for x in extractions:
        d = set()
        for ev in x["events"]:
            if "candidate_id" in ev:
                d.add(("EVT", ev["candidate_id"]))
            elif ev.get("new_ref") in declared:
                d.add(target(ev["new_ref"]))
            elif "new_label" in ev and len(members[ev["new_label"]]) > 1:
                d.add(target(ev["new_label"]))
        out[x["item_id"]] = frozenset(d)
    return out


def agreement_rows(results: dict[str, RunResult], item_ids) -> list[tuple]:
    """(A==A', A==H, any-run-linked) for each item ALL THREE runs kept."""
    a, a2, h = (results[r] for r in RUNS)
    rows = []
    for i in item_ids:
        if i in a.kept and i in a2.kept and i in h.kept:
            da, da2, dh = a.decisions[i], a2.decisions[i], h.decisions[i]
            rows.append((da == da2, da == dh, bool(da or da2 or dh)))
    return rows


def decide(agree_aa, agree_ah, drop_a, drop_h, n_batches) -> str:
    if n_batches < MIN_BATCHES:
        return f"NOT MEASURABLE: {n_batches} complete batches < {MIN_BATCHES}"
    # Differences rounded before comparing: the boundary is INCLUSIVE, and
    # 0.85 - 0.05 is 0.7999999999999999 in binary, which would let exactly
    # 5 points pass or fail depending on the operands rather than the rule.
    agree_ok = round(agree_ah - agree_aa, 9) >= -TOLERANCE
    drop_ok = round(drop_h - drop_a, 9) <= TOLERANCE
    if agree_ok and drop_ok:
        return "HAIKU QUALIFIES"
    failed = [
        name
        for name, ok in (("agreement", agree_ok), ("validation drop", drop_ok))
        if not ok
    ]
    return "HAIKU DOES NOT QUALIFY: " + " and ".join(failed)


def worst_case_usd(model: str, request: dict) -> float:
    """An UPPER bound on ONE attempt, for the spend guard: every input
    character a third of a token, and the whole output budget used."""
    pin, pout = comprehend.price_of(model)
    tokens_in = len(json.dumps(request)) / CHARS_PER_TOKEN_WORST
    return (tokens_in * pin + comprehend.INTEGRATE_MAX_TOKENS * pout) / 1_000_000


def batch_reserve(models: dict, request: dict) -> float:
    """What the guard must hold back before a batch: every run's worst case,
    for every attempt call_one may make (review m1)."""
    return ATTEMPTS * sum(worst_case_usd(models[r], request) for r in RUNS)


# --- Entity hits and births.


def _first_token(form: str) -> str | None:
    toks = _WORD.findall((form or "").lower())
    return toks[0] if toks else None


def forms_by_token(index: "comprehend.SurfaceIndex") -> dict:
    by_token = defaultdict(list)
    for sf in index._forms:
        if sf.entity_id is not None:
            by_token[_first_token(sf.form)].append(sf)
    return by_token


def forms_by_entity(index: "comprehend.SurfaceIndex") -> dict[int, list[str]]:
    out = defaultdict(list)
    for sf in index._forms:
        if sf.entity_id is not None:
            out[sf.entity_id].append(sf.form)
    return out


def _text(it: dict) -> str:
    return f"{comprehend.clean(it['title'])}\n{comprehend.clean(it['body'])}"


def narrowed_forms(by_token, texts) -> list:
    """Every entity form whose first word appears in `texts`. A SUPERSET of
    what can match: that argument is in probe_clustering.item_entity_hits, and
    main() checks it against the full index on a sample."""
    toks = set()
    for text in texts:
        toks |= set(_WORD.findall(text.lower()))
    forms = list(by_token.get(None, []))
    for t in toks:
        forms.extend(by_token.get(t, ()))
    return forms


def item_hits(by_token, it: dict) -> set[int]:
    text = _text(it)
    narrow = comprehend.SurfaceIndex(narrowed_forms(by_token, [text]))
    return {sf.entity_id for sf in narrow.match(text)}


def narrowing_disagreements(full, by_token, batches) -> int:
    bad = 0
    for b in batches:
        for it in b.items:
            want = {sf.entity_id for sf in full.match(_text(it)) if sf.entity_id}
            bad += want != item_hits(by_token, it)
    return bad


def naming_births(conn, entity_ids, forms) -> dict[int, dt.datetime]:
    """{entity: when it was born}: the capture time of the earliest item that
    asserts one of its events AND whose text names it, capped at
    entities.created_at. Only an item that names an entity can have created
    it (review M1)."""
    births = {}
    for eid in sorted(entity_ids):
        created = conn.execute(
            "SELECT created_at FROM entities WHERE id = %s", (eid,)
        ).fetchone()
        if created is None:
            continue
        born = created[0]
        cur = conn.execute(
            "SELECT i.created_at, i.title, i.body FROM event_entities ee "
            "JOIN assertions a ON a.event_id = ee.event_id "
            "JOIN items i ON i.id = a.item_id "
            "WHERE ee.entity_id = %s AND i.created_at < %s "
            "ORDER BY i.created_at, i.id",
            (eid, born),
        )
        for at, title, body in cur:
            text = _text({"title": title, "body": body or ""})
            if any(comprehend.form_matches(f, text) for f in forms.get(eid, ())):
                born = at
                break
        births[eid] = born
    return births


def index_for(batch: Batch, hits, births, forms) -> "comprehend.SurfaceIndex":
    """The entity index as the batch would have seen it: only entities the
    batch's text names, born strictly before the batch's cut-off."""
    born = {
        e
        for it in batch.items
        for e in hits[it["id"]]
        if e in births and births[e] < batch.cutoff
    }
    return comprehend.SurfaceIndex(
        [
            comprehend.SurfaceForm(f, "tracked_entity", e)
            for e in sorted(born)
            for f in forms.get(e, ())
        ]
    )


# --- Calls.


def call_one(request: dict, model: str, offered, ent_labels, ev_labels) -> RunResult:
    """One run of one batch. A refusal of the request or the account ABORTS.
    A transient fault is retried once, then recorded, and the batch is left
    out of every comparison. A response that will not parse is a DROP of the
    whole batch for this run, which is what production would charge."""
    out = RunResult()
    resp = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = comprehend.call_integration(dict(request, model=model))
            break
        except Exception as exc:
            kind = comprehend._account_failure(exc)
            if kind:
                raise Abort(f"{model}: account failure ({kind}): {exc}") from exc
            if not comprehend._is_transient(exc):
                body = getattr(getattr(exc, "response", None), "text", "")
                raise Abort(
                    f"{model}: request refused: {exc} body={body[:500]}"
                ) from exc
            out.failed_attempts += 1
            if attempt == ATTEMPTS:
                out.transport_error = f"{type(exc).__name__}: {exc}"
                return out
    out.usd = comprehend.cost_usd(model, resp.get("usage") or {})
    try:
        extractions = comprehend.parse_integration_response(
            resp,
            offered,
            ent_labels,
            ev_labels,
            comprehend.Tally(enabled=True),
            neighbour_faults=set(),
        )
    except Exception as exc:
        out.parse_error = f"{type(exc).__name__}: {exc}"
        return out
    out.decisions = decisions(extractions)
    out.kept = set(out.decisions)
    return out


def run_batch(request, models, offered, ent_labels, ev_labels):
    """(results, abort). Every call is waited for BEFORE an abort surfaces, so
    siblings that finished, and billed, are still counted (review m1)."""
    with ThreadPoolExecutor(max_workers=len(RUNS)) as ex:
        futures = {
            r: ex.submit(call_one, request, models[r], offered, ent_labels, ev_labels)
            for r in RUNS
        }
    results, abort = {}, None
    for r, f in futures.items():
        try:
            results[r] = f.result()
        except Abort as exc:
            abort = abort or exc
    return results, abort


# --- Report.


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def summarize(done: list[tuple[Batch, dict]]) -> dict:
    offered = {r: 0 for r in RUNS}
    kept = {r: 0 for r in RUNS}
    rows = []
    for b, results in done:
        ids = [it["id"] for it in b.items]
        for r in RUNS:
            offered[r] += len(ids)
            kept[r] += len(results[r].kept & set(ids))
        rows.extend(agreement_rows(results, ids))
    drop = {r: (1 - kept[r] / offered[r]) if offered[r] else 0.0 for r in RUNS}
    n = len(rows)
    agree_aa = sum(x for x, _, _ in rows) / n if n else 0.0
    agree_ah = sum(y for _, y, _ in rows) / n if n else 0.0
    ci = paired_ci([y for _, y, _ in rows], [x for x, _, _ in rows])
    matched = [row for row in rows if row[2]]
    m = len(matched)
    return {
        "batches": len(done),
        "items": n,
        "agree_aa": agree_aa,
        "agree_ah": agree_ah,
        "ci": ci,
        "drop": drop,
        "matched_n": m,
        "matched_aa": sum(x for x, _, _ in matched) / m if m else None,
        "matched_ah": sum(y for _, y, _ in matched) / m if m else None,
    }


def print_summary(label: str, s: dict) -> None:
    print(f"\n--- {label}: {s['batches']} batches, {s['items']} items all three kept")
    if not s["items"]:
        print("  nothing to compare")
        return
    diff, lo, hi = s["ci"]
    print(f"  agreement A-A'  {_pct(s['agree_aa'])}")
    print(
        f"  agreement A-H   {_pct(s['agree_ah'])}   "
        f"A-H minus A-A' {diff * 100:+.1f} pts (95% CI {lo * 100:+.1f}, {hi * 100:+.1f})"
    )
    d = s["drop"]
    d_a2 = d["A'"]
    print(f"  validation drop A {_pct(d['A'])}  A' {_pct(d_a2)}  H {_pct(d['H'])}")
    if s["matched_n"]:
        print(
            f"  link-only subset (not ruled): n={s['matched_n']} "
            f"A-A' {_pct(s['matched_aa'])}  A-H {_pct(s['matched_ah'])}"
        )
    else:
        print("  link-only subset (not ruled): no item linked anything")


# --- The run.


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="no model calls")
    parser.add_argument(
        "--limit", type=int, help="run only the first N batches (smoke; no verdict)"
    )
    args = parser.parse_args(argv)

    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    sonnet = comprehend._integrate_model()
    models = {"A": sonnet, "A'": sonnet, "H": HAIKU}
    if not sonnet.startswith("claude-sonnet"):
        print(f"REFUSED: the live integration model is {sonnet!r}, not a Sonnet.")
        print("A and A' must be production's Sonnet for M2 to mean anything.")
        return 2
    for model in set(models.values()):
        comprehend.price_of(model)  # raises UnpricedModel before any spend

    with db.connect() as conn:
        try:
            return _run(conn, args, models)
        finally:
            conn.rollback()  # nothing writes; this makes it a property


def _run(conn, args, models) -> int:
    rng = random.Random(SEED)
    pairs = cross_outlet_pairs(conn, PAIR_GAP_HOURS)
    if not pairs:
        print("NOT MEASURABLE: no cross-outlet pairs in the KB.")
        return 2
    start, end = _pair_range(pairs)
    materials = material_items(conn, start, end)
    batches = select_batches(materials, pairs, rng)
    full = comprehend.SurfaceIndex.build(conn)
    by_token = forms_by_token(full)
    forms = forms_by_entity(full)
    outlets = dict(conn.execute("SELECT id, name FROM outlets").fetchall())

    print(f"models: A = A' = {models['A']}, H = {models['H']}")
    print(
        f"batches: {sum(b.stratum == 'pair' for b in batches)} pair, "
        f"{sum(b.stratum == 'random' for b in batches)} random "
        f"(seed {SEED}; pairs available {len(pairs)}, material items {len(materials)})"
    )
    sizes = [len(b.items) for b in batches]
    print(f"batch sizes: {dict(sorted({s: sizes.count(s) for s in sizes}.items()))}")

    bad = narrowing_disagreements(
        full, by_token, batches[: min(NARROW_SAMPLE, len(batches))]
    )
    print(f"Narrowing control: {bad} of <= {NARROW_SAMPLE} batches' items disagree")
    if bad:
        print(
            "REFUSED: the narrowed index is not exact, so the offer is not production's."
        )
        return 2

    hits = {it["id"]: item_hits(by_token, it) for b in batches for it in b.items}
    births = naming_births(conn, set().union(*hits.values()), forms)

    built = []
    for b in batches:
        tally = comprehend.Tally(enabled=True)
        ents, evs = comprehend.integration_candidates(
            conn, index_for(b, hits, births, forms), b.items, tally, as_of=b.cutoff
        )
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in b.items]
        request = comprehend.build_integration_request(
            payload, ents, evs, model=models["A"]
        )
        built.append((b, request, ents, evs))

    pair_offered = pair_inside = 0
    for b, _, _, evs in built:
        if b.stratum != "pair":
            continue
        if b.earlier_id in {it["id"] for it in b.items}:
            pair_inside += 1
        earlier_events = {
            r[0]
            for r in conn.execute(
                "SELECT event_id FROM assertions WHERE item_id = %s", (b.earlier_id,)
            ).fetchall()
        }
        pair_offered += bool(earlier_events & {e["id"] for e in evs})
    n_pair = sum(b.stratum == "pair" for b in batches)
    print(
        f"pair batches: earlier item's event OFFERED as of the batch in {pair_offered} "
        f"of {n_pair}; earlier item INSIDE the batch in {pair_inside} of {n_pair}"
    )

    worst = sum(batch_reserve(models, req) for _, req, _, _ in built)
    print(
        f"worst-case spend for every batch, retries included: ${worst:.2f} "
        f"(cap ${SPEND_CAP_USD:.2f})"
    )

    if args.dry_run:
        hist = conn.execute(
            "SELECT count(*), avg(input_tokens), avg(output_tokens) "
            "FROM comprehend_spend WHERE stage = 'integration'"
        ).fetchone()
        if hist[0]:
            mean_in, mean_out = float(hist[1]), float(hist[2])
            chars = sum(len(json.dumps(req)) for _, req, _, _ in built)
            print(
                f"history: {hist[0]} integration calls, mean in={mean_in:.0f} "
                f"out={mean_out:.0f} tokens"
            )
            projected = 0.0
            for r in RUNS:
                pin, pout = comprehend.price_of(models[r])
                projected += len(built) * (mean_in * pin + mean_out * pout) / 1_000_000
            print(
                f"projected spend at the historical mean: ${projected:.2f} "
                f"({chars} request chars over {len(built)} batches)"
            )
        else:
            print("history: no integration spend rows; only the worst case is known")
        print("\nDRY RUN: no model was called.")
        return 0

    todo = built[: args.limit] if args.limit else built
    spent = 0.0
    # Failed transient attempts may still have been generated and billed
    # server-side; their cost is unknown, so the cap assumes the worst.
    unknown = 0.0
    done: list[tuple[Batch, dict]] = []
    incomplete = 0
    stopped = None
    parse_failures = {r: 0 for r in RUNS}
    for k, (b, request, ents, evs) in enumerate(todo, 1):
        reserve = batch_reserve(models, request)
        if spent + unknown + reserve > SPEND_CAP_USD:
            stopped = (
                f"spend cap: ${spent:.2f} spent + ${unknown:.2f} unknown, "
                f"next batch reserves ${reserve:.2f}"
            )
            break
        results, abort = run_batch(
            request,
            models,
            {it["id"] for it in b.items},
            comprehend.label_map(comprehend._ENTITY_LABEL, ents),
            comprehend.label_map(comprehend._EVENT_LABEL, evs),
        )
        for r, res in results.items():
            spent += res.usd
            unknown += res.failed_attempts * worst_case_usd(models[r], request)
        if abort is not None:
            stopped = f"ABORTED: {abort}"
            break
        if any(res.transport_error for res in results.values()):
            incomplete += 1
            status = "incomplete (transport)"
        else:
            done.append((b, results))
            for r in RUNS:
                parse_failures[r] += results[r].parse_error is not None
            status = " ".join(f"{r}={len(results[r].kept)}" for r in RUNS)
        print(
            f"[{k}/{len(todo)}] {b.stratum} anchor={b.anchor_id} {status} ${spent:.2f}",
            flush=True,
        )

    print("\n=== M2: Haiku replay (news-brief-y1x) ===")
    print(
        f"spend: ${spent:.2f} billed + up to ${unknown:.2f} for failed attempts "
        f"(cap ${SPEND_CAP_USD:.2f})"
    )
    print(f"complete batches {len(done)}, incomplete (transport) {incomplete}")
    print(
        "whole-response parse failures in complete batches: "
        + ", ".join(f"{r}={parse_failures[r]}" for r in RUNS)
    )
    for stratum in ("pair", "random"):
        print_summary(stratum, summarize([d for d in done if d[0].stratum == stratum]))
    print(
        "  (pair batches are selected by production's own past links, which "
        "favours A-A'; random is the unconfounded stratum)"
    )
    combined = summarize(done)
    print_summary("combined (the rule's population)", combined)

    print(
        "\nPRE-REGISTERED guesses: A-A' 80-90%, A-H 65-80% (predicts: fails narrowly)."
    )
    print(
        f"Rule: A-H >= A-A' - {TOLERANCE * 100:.0f} pts AND drop(H) <= drop(A) + "
        f"{TOLERANCE * 100:.0f} pts, over >= {MIN_BATCHES} complete batches."
    )
    if stopped:
        print(f"STOPPED EARLY: {stopped}")
    if args.limit:
        print("SMOKE RUN (--limit): no verdict.")
        print("REPLAY COMPLETE")
        return 0
    verdict = decide(
        combined["agree_aa"],
        combined["agree_ah"],
        combined["drop"]["A"],
        combined["drop"]["H"],
        combined["batches"],
    )
    print(f"VERDICT: {verdict}")
    print("REPLAY COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())

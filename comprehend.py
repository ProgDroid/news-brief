"""Comprehension: read captured items, decide which are material, extract.

Spec: docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md

A supervisor job child, deliberately SEPARATE from capture. Capture must stay
continuous and cheap because feeds are windows and a missed item is gone
forever; comprehension can be lazy and batched because a captured item can be
reprocessed indefinitely. Concretely, an unbounded model call inside capture's
600s bound would trip the supervisor's overlap alert 48 times a day.

Nothing reads what this writes. entities/events/event_entities/assertions ship
QUARANTINED, per the rule Epic 1 converged on: quarantine is the default for an
unmeasured field, and measurement is what lifts it. The only thing that can say
whether this works is scripts/score_comprehension.py.
"""

import html
import re
import time
from dataclasses import dataclass, field

import requests

import common
from common import log

# Bump on any material change to the triage prompt. A prompt change that is not
# versioned is indistinguishable from a change in the world.
TRIAGE_PROMPT_VERSION = 1
INTEGRATE_PROMPT_VERSION = 2

# A pass must not outlive its own fire time: supervisor's _due_jobs loop alerts
# on any job still running at its next fire. Hourly schedule, 40-minute bound.
DEADLINE_SECONDS = 2400


@dataclass
class Tally:
    """What one pass did. Returned AND logged, because a bare count is
    unattributable: 0 rows written is ambiguous across "nothing was captured",
    "everything was immaterial" and "every model call failed"."""

    enabled: bool = False
    items_seen: int = 0
    triaged_by_rules: int = 0
    triaged_by_model: int = 0
    sampled: int = 0
    material: int = 0
    immaterial: int = 0
    failed_triage: int = 0
    failed_integration: int = 0
    # Items whose batch died on the network rather than on its contents. Held
    # APART from failed_integration on purpose: no verdict about these items was
    # ever obtained, so counting them as extraction failures misattributes a
    # host fault as a weak extractor -- the same confound that made
    # news-brief-uer a gate-validity bug rather than deferrable polish.
    deferred_transport: int = 0
    gave_up_triage: int = 0
    gave_up_integration: int = 0
    entities_created: int = 0
    entities_resolved: int = 0
    events_created: int = 0
    events_matched: int = 0
    assertions_written: int = 0
    # Distinguishes "one item failed" from "four items were collateral" -- the
    # exact confusion a batch-wide transaction would have produced.
    items_lost_to_savepoint: int = 0
    # Well-formed extractions that carried nothing. NOT a failure: spec 12.2
    # measured real stock-quote and market-summary junk in the corpus, so an
    # empty sample arm is a finding about the corpus, not about the pipeline.
    # Counted separately because folding it into failed_integration makes the
    # failure counters unattributable (news-brief-uer).
    empty_extraction: int = 0
    instrument_entity_refused: int = 0
    candidate_cap_hit: int = 0
    # A `candidate` the model supplied that matched no offered label. Not a
    # failure -- the row falls back to the new-entity/new-event path -- but it
    # is model NON-COMPLIANCE, and a fallback that left no trace would repeat
    # exactly what made failed_integration=172 unattributable.
    unmapped_candidate: int = 0
    failures: dict = field(default_factory=dict)


class NoEntitySurvived(ValueError):
    """Every entity the model returned was refused by `_resolve_entity`.

    A named subclass rather than a bare ValueError so `failures` can name it.
    This class fails the SAME item on every pass and is therefore certain to be
    retired at the ceiling, which is a different operational fact from an item
    that failed once and would succeed on a retry.
    """


def _note(tally, cause: str, n: int = 1) -> None:
    """Attribute a failure by cause, so `failures` can say WHICH one fired.

    Tolerates a None tally: `parse_integration_response` is called without one
    by `scripts/inspect_integration.py` and by the pure-function tests.
    """
    if tally is None:
        return
    tally.failures[cause] = tally.failures.get(cause, 0) + n


def _is_transient(exc: BaseException) -> bool:
    """True when the failure obtained no verdict AND is plausibly temporary.

    Only these may be retried without charging `integrate_attempts`, which is a
    ONE-WAY DOOR at 3. A ~90s DNS fault on 2026-09-08 failed three whole batches
    and spent an attempt on every item in them, for a fault that said nothing
    about any of those items; because the integration SELECT is `ORDER BY i.id`
    that loss lands on the OLDEST corpus rather than at random. 429 and 5xx sit
    here with the connection faults: they are the server declining to answer,
    which is likewise not a statement about what was asked.

    The set is deliberately narrow, because the failure mode of being too
    liberal is quieter than the bug it fixes. A 4xx other than 429 means the
    request itself was refused and will be refused identically next hour, so
    deferring it forever would re-pay an 8192-token generation every pass while
    nothing ever retired it.
    """
    if not isinstance(exc, requests.RequestException):
        return False
    resp = getattr(exc, "response", None)
    if resp is None:
        return True
    return resp.status_code == 429 or resp.status_code >= 500


def _timed_post(request: dict, label: str, timeout: int, max_attempts: int) -> dict:
    """Post via brief's HTTP path, logging how long it actually took.

    The elapsed time is the point. Both timeouts below are opening guesses, and
    a guess is only defensible while something is measuring it -- this is what
    lets the host retune the knobs from real durations instead of from a
    token-rate estimate. Logged on failure too: a call that timed out is
    precisely the one whose duration you want.
    """
    import brief

    started = time.monotonic()
    try:
        resp = brief._post_messages(request, timeout=timeout, max_attempts=max_attempts)
    except Exception:
        log.warning(
            f"Comprehend: {label} call failed after "
            f"{time.monotonic() - started:.1f}s (timeout={timeout}s)"
        )
        raise
    usage = resp.get("usage") or {}
    log.info(
        f"Comprehend: {label} call took {time.monotonic() - started:.1f}s "
        f"(timeout={timeout}s) stop_reason={resp.get('stop_reason')} "
        f"out={usage.get('output_tokens')}"
    )
    return resp


def call_triage(request: dict) -> dict:
    """The network seam for triage. Tests monkeypatch this."""
    return _timed_post(
        request, "triage", int(common.COMPREHEND_TRIAGE_TIMEOUT), max_attempts=2
    )


def call_integration(request: dict) -> dict:
    """The network seam for integration. Tests monkeypatch this.

    ONE HTTP attempt, deliberately. item_triage.integrate_attempts < 3 already
    gates the integration SELECT and the failure paths increment it, so a
    failed item is retried on the next hourly pass. Retrying here as well
    multiplies against that ceiling -- up to six charged 8192-token
    generations for one persistently-bad item -- and, unlike the item-level
    retry, it spends this pass's DEADLINE_SECONDS, shrinking how many other
    items get processed at all (news-brief-wvt).
    """
    return _timed_post(
        request,
        "integration",
        int(common.COMPREHEND_INTEGRATE_TIMEOUT),
        max_attempts=1,
    )


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run(conn) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS.

    Commit boundaries are load-bearing: one transaction per micro-batch, one
    savepoint per item inside it (spec section 6.4).
    """
    tally = Tally(enabled=bool(common.COMPREHEND_ENABLED))
    if not tally.enabled:
        log.info("Comprehend: disabled by COMPREHEND_ENABLED; nothing read")
        return tally

    deadline = time.monotonic() + DEADLINE_SECONDS
    index = SurfaceIndex.build(conn)
    outlets = dict(conn.execute("SELECT id, name FROM outlets").fetchall())

    pending = pending_triage(
        conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS)
    )
    tally.items_seen = len(pending)

    # --- Triage: rules half first, so the model never sees what a lookup answered.
    undecided = []
    for item in pending:
        hit = triage_by_rules(item, index)
        if hit:
            record_triage(
                conn, item["id"], "material", hit.reason, None, TRIAGE_PROMPT_VERSION
            )
            tally.triaged_by_rules += 1
            tally.material += 1
        else:
            undecided.append(item)
    conn.commit()

    # --- Triage: model half, on the remainder only.
    for batch in _chunk(undecided, int(common.COMPREHEND_TRIAGE_BATCH)):
        if time.monotonic() >= deadline:
            break
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        try:
            verdicts = parse_triage_response(
                call_triage(build_triage_request(payload)), {it["id"] for it in batch}
            )
        except Exception:
            tally.failed_triage += len(batch)
            log.warning("Comprehend: triage batch failed", exc_info=True)
            for it in batch:
                record_triage(
                    conn,
                    it["id"],
                    "failed",
                    "error",
                    _triage_model(),
                    TRIAGE_PROMPT_VERSION,
                )
            conn.commit()
            continue
        for it in batch:
            material = verdicts.get(it["id"], False)
            record_triage(
                conn,
                it["id"],
                "material" if material else "immaterial",
                "topical" if material else "none",
                _triage_model(),
                TRIAGE_PROMPT_VERSION,
            )
            tally.triaged_by_model += 1
            tally.material += int(material)
            tally.immaterial += int(not material)
        conn.commit()

    # --- The unconfounded control arm.
    for item_id in select_sampled(
        conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_SAMPLE_PER_DAY)
    ):
        record_triage(conn, item_id, "material", "sampled", None, TRIAGE_PROMPT_VERSION)
        conn.execute(
            "UPDATE item_triage SET sampled_at = now() "
            "WHERE item_id = %s AND triage_prompt_version = %s",
            (item_id, TRIAGE_PROMPT_VERSION),
        )
        tally.sampled += 1
    conn.commit()

    # --- Integration.
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.verdict = 'material' AND t.integrate_attempts < 3 "
        "  AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s) "
        "ORDER BY i.id LIMIT %s",
        (
            TRIAGE_PROMPT_VERSION,
            INTEGRATE_PROMPT_VERSION,
            int(common.COMPREHEND_MAX_ITEMS),
        ),
    ).fetchall()
    # published_at is carried because write_extraction needs it for
    # events.occurred_at. Without it every created event is invisible to
    # candidate_events and corroboration is impossible.
    material_items = [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
        }
        for r in rows
    ]
    published = {it["id"]: it["published_at"] for it in material_items}

    for batch in _chunk(material_items, int(common.COMPREHEND_INTEGRATE_BATCH)):
        if time.monotonic() >= deadline:
            break
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        hits = [
            sf
            for it in batch
            for sf in index.match(f"{clean(it['title'])}\n{clean(it['body'])}")
            if sf.entity_id is not None
        ]
        # Most-recent-first (spec §6), same as the event cap's ORDER BY DESC:
        # truncation should drop the least likely candidates. Entity ids are
        # monotonically increasing, so `id DESC` IS newest-first, and it is
        # unambiguous -- unlike index insertion order, which is index-build
        # order (SurfaceIndex.build's SELECT has no ORDER BY, so DB order is
        # arbitrary) followed by this-run's add_entity appends. Keeping the
        # FIRST N of insertion order means an entity THIS RUN just created is
        # the first one dropped, defeating the mid-run index refresh that
        # exists specifically to surface it as a candidate.
        ranked = sorted(dict.fromkeys(sf.entity_id for sf in hits), reverse=True)
        if len(ranked) > CANDIDATE_ENTITY_CAP:
            tally.candidate_cap_hit += 1
        entity_ids = ranked[:CANDIDATE_ENTITY_CAP]
        cand_entities = (
            [
                {"id": r[0], "name": r[1], "type": r[2]}
                for r in conn.execute(
                    "SELECT id, name, type FROM entities WHERE id = ANY(%s)",
                    (entity_ids,),
                ).fetchall()
            ]
            if entity_ids
            else []
        )
        cand_events = candidate_events(conn, entity_ids, tally)

        try:
            extractions = parse_integration_response(
                call_integration(
                    build_integration_request(payload, cand_entities, cand_events)
                ),
                {it["id"] for it in batch},
                label_map(_ENTITY_LABEL, cand_entities),
                label_map(_EVENT_LABEL, cand_events),
                tally,
            )
        except Exception as exc:
            # A network fault is a statement about the host, not about the news.
            # Charging it would let three unrelated outages retire an item that
            # was never once judged -- see _is_transient (news-brief-bqa.13).
            if _is_transient(exc):
                tally.deferred_transport += len(batch)
                _note(tally, "transport", len(batch))
                log.warning(
                    f"Comprehend: integration batch deferred, no attempt charged: "
                    f"{type(exc).__name__}: {exc}"
                )
                conn.commit()
                continue
            tally.failed_integration += len(batch)
            _note(tally, f"batch:{type(exc).__name__}", len(batch))
            log.warning("Comprehend: integration batch failed", exc_info=True)
            conn.execute(
                "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
                "WHERE item_id = ANY(%s)",
                ([it["id"] for it in batch],),
            )
            conn.commit()
            continue

        # A row _validate_item rejected never reaches write_batch, so nothing
        # else advances it: it satisfies the integration SELECT forever and
        # `ORDER BY i.id` puts it at the FRONT of every future batch, re-paying
        # its share of the call each pass with no operator-visible signal.
        # Charging it an attempt lets the ceiling of 3 retire it, which is the
        # same treatment a whole-batch failure already gets.
        dropped = [
            it["id"]
            for it in batch
            if it["id"] not in {e["item_id"] for e in extractions}
        ]
        if dropped:
            tally.failed_integration += len(dropped)
            # Counts EVERY absent item, while the `validate:*` keys attribute
            # only the subset _validate_item actually saw and rejected. The
            # difference is therefore items the model never returned at all --
            # a distinct defect, and one no single counter could have named.
            _note(tally, "dropped", len(dropped))
            conn.execute(
                "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
                "WHERE item_id = ANY(%s)",
                (dropped,),
            )

        for e in extractions:
            e["published_at"] = published.get(e["item_id"])
        write_batch(conn, extractions, index, tally)
        conn.commit()

    tally.gave_up_integration = conn.execute(
        "SELECT count(*) FROM item_triage WHERE integrate_attempts >= 3"
    ).fetchone()[0]
    tally.gave_up_triage = conn.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'failed' AND attempts >= 3"
    ).fetchone()[0]
    log.info(f"Comprehend: {tally}")
    return tally


def pending_triage(conn, version: int, limit: int) -> list[dict]:
    """Items with no verdict at this version, or a retryable failure.

    Oldest first: nothing reads this layer yet, so completeness beats recency
    and no item may starve. Newest-first would permanently skip the tail of any
    backlog.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at "
        "FROM items i "
        "LEFT JOIN item_triage t "
        "  ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.id IS NULL OR (t.verdict = 'failed' AND t.attempts < 3) "
        "ORDER BY i.id "
        "LIMIT %s",
        (version, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
        }
        for r in rows
    ]


# Surface forms that are also ordinary English words. A form on this list never
# matches, whatever entity claims it. Short and hand-maintained on purpose: a
# large stop-list hides a matcher that is too loose.
STOP_FORMS = frozenset(
    {
        "will",
        "may",
        "can",
        "us",
        "it",
        "he",
        "she",
        "they",
        "the",
        "and",
        "for",
        "was",
        "are",
        "has",
        "had",
        "new",
        "one",
        "two",
        "all",
        "any",
    }
)

# Below this length a form must match case-sensitively. Acronyms are real
# entities (US, EU, UN, IMF); lowercased they collide with common words.
_CASE_SENSITIVE_BELOW = 4


def clean(text: str | None) -> str:
    """Decode HTML entities and collapse whitespace.

    Measured on production 2026-09-05: item bodies carry literal `&nbsp;`.
    Reuters items read `...facing 10 years in jail&nbsp;&nbsp;Reuters`. Without
    unescaping, a surface form spanning an entity boundary silently fails to
    match -- and a silent miss in the tracked half presents as "the KB did not
    find that interesting", not as an error.

    Used by BOTH the matcher and the prompt builders, so the model never sees
    entity noise either.
    """
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def form_matches(form: str, text: str) -> bool:
    """Word-boundary match, never substring.

    Substring matching is the recorded PolyGram failure: `MU` matched "Musk".
    """
    form = (form or "").strip()
    if not form or not text:
        return False
    # The stop-list check is CASE-SENSITIVE for short forms, and that is not a
    # detail. STOP_FORMS holds lowercase words, and a short form already relies
    # on case to disambiguate -- so lowercasing before the lookup would stop
    # `US` (the country) because `us` (the pronoun) is on the list, and the same
    # for EU, UN and every other acronym that is also a common word. The short
    # form `us` is still stopped; the entity `US` is not.
    if (form if len(form) < _CASE_SENSITIVE_BELOW else form.lower()) in STOP_FORMS:
        return False
    flags = 0 if len(form) < _CASE_SENSITIVE_BELOW else re.IGNORECASE
    return re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text, flags) is not None


@dataclass(frozen=True)
class SurfaceForm:
    form: str
    reason: str  # tracked_entity | tracked_claim | tracked_story
    entity_id: int | None = None


class SurfaceIndex:
    """Every trackable surface form, held in memory for one run.

    A full scan of `entities` per run, deliberately: the word-boundary and
    case rules above do not express well in SQL, and one scan per run is
    cheaper than a per-mention query. This is why there is NO GIN index on
    entities.aliases -- an index with no reader is dead weight.
    """

    def __init__(self, forms: list[SurfaceForm]) -> None:
        self._forms = list(forms)

    @classmethod
    def build(cls, conn) -> "SurfaceIndex":
        forms: list[SurfaceForm] = []
        for eid, name, aliases in conn.execute(
            "SELECT id, name, aliases FROM entities"
        ).fetchall():
            for f in [name, *(aliases or [])]:
                forms.append(SurfaceForm(f, "tracked_entity", eid))
        for (topic,) in conn.execute(
            "SELECT DISTINCT topic FROM claims "
            "WHERE topic IS NOT NULL AND status IN ('standing', 'challenged') "
            "AND retired_on IS NULL"
        ).fetchall():
            forms.append(SurfaceForm(topic, "tracked_claim", None))
        for (name,) in conn.execute(
            "SELECT name FROM stories WHERE state <> 'closed'"
        ).fetchall():
            forms.append(SurfaceForm(name, "tracked_story", None))
        return cls(forms)

    def add_entity(self, entity_id: int, name: str, aliases: list[str]) -> None:
        """Called after each batch's writes. Built once per run, an entity
        created in batch 1 is invisible to batch 3, so events attached to it
        cannot be offered as candidates -- which depresses events_matched, the
        numerator of the corroboration gate. The gate would then fail for a
        caching reason while the matcher worked."""
        for f in [name, *(aliases or [])]:
            self._forms.append(SurfaceForm(f, "tracked_entity", entity_id))

    def match(self, text: str) -> list[SurfaceForm]:
        """Every distinct hit, deduplicated by entity (or by form where there
        is no entity). Order is stable: entity hits first, in insertion order."""
        seen: set = set()
        out: list[SurfaceForm] = []
        for sf in self._forms:
            key = ("e", sf.entity_id) if sf.entity_id is not None else ("f", sf.form)
            if key in seen:
                continue
            if form_matches(sf.form, text):
                seen.add(key)
                out.append(sf)
        return out


def triage_by_rules(item: dict, index: SurfaceIndex) -> SurfaceForm | None:
    """The tracked half. A database lookup, no model call.

    Reads title AND body. The body is the RSS blurb -- capture.py stores
    entry.get("summary"), not article text -- so it is short and there is no
    window to choose.
    """
    text = f"{clean(item.get('title'))} {clean(item.get('body'))}"
    hits = index.match(text)
    return hits[0] if hits else None


# Sized from the batch: 25 items x ~25 output tokens plus schema overhead. A
# tight max_tokens is what truncated the signals call twice; leave headroom and
# check stop_reason regardless.
TRIAGE_MAX_TOKENS = 2048

_TRIAGE_TOOL = {
    "name": "emit_triage",
    "description": "Return one materiality verdict per input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "material": {"type": "boolean"},
                    },
                    "required": ["id", "material"],
                },
            }
        },
        "required": ["items"],
    },
}

_TRIAGE_SYSTEM = """You are triaging news items for a geopolitics and macro \
knowledge base. For each item, answer one question: does it belong to that \
domain -- international relations, conflict, statecraft, energy, trade, \
central banks, sovereign risk, or the markets those move?

Judge the SUBJECT, not the importance. A minor development in the domain is \
material; a major story outside it is not. Sport, entertainment, crime and \
consumer news are not material unless they carry a stated geopolitical or \
macro consequence.

Return a verdict for every id you were given, and no others."""


def _triage_model() -> str:
    """An unset NEWSBRIEF_TRIAGE_MODEL means "follow MODEL"."""
    return common.TRIAGE_MODEL or common.MODEL


def build_triage_request(items: list[dict]) -> dict:
    lines = []
    for it in items:
        # clean() so the model never sees `&nbsp;` noise either. Measured
        # 2026-09-05: 41% of captured volume is Google News proxy items whose
        # body is the headline restated with entity separators.
        body = clean(it.get("body"))
        lines.append(
            f"- id={it['id']} outlet={it.get('outlet', '?')} "
            f"title={clean(it['title'])!r} lead={body[:300]!r}"
        )
    return {
        "model": _triage_model(),
        "max_tokens": TRIAGE_MAX_TOKENS,
        # Forced-tool extraction on a tight budget: thinking OFF. Omitting the
        # field runs ADAPTIVE thinking on Sonnet 5, which spends max_tokens.
        "thinking": {"type": "disabled"},
        "system": _TRIAGE_SYSTEM,
        "tools": [_TRIAGE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_triage"},
        "messages": [{"role": "user", "content": "ITEMS:\n" + "\n".join(lines)}],
    }


def _is_id(x) -> bool:
    """A real integer id, excluding the values Python quietly counts as one.

    `bool` subclasses `int`, so `isinstance(True, int)` is True and `True == 1`;
    and `1.0 == 1`, so a float passes a set-membership test against integer ids.
    Either would bind a hallucinated value onto a real row -- and because the
    resulting id IS real, nothing downstream errors.
    """
    return isinstance(x, int) and not isinstance(x, bool)


def parse_triage_response(resp: dict, offered_ids: set[int]) -> dict[int, bool]:
    """id -> material. Drops ids that were never offered.

    stop_reason is checked FIRST: a truncated reply produces a parse error that
    reads as a broken parser, and this repo has misdiagnosed that four times.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("triage response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_triage":
            rows = block.get("input", {}).get("items")
            if not isinstance(rows, list):
                raise ValueError("emit_triage input missing 'items' list")
            out: dict[int, bool] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rid, mat = r.get("id"), r.get("material")
                if _is_id(rid) and isinstance(mat, bool) and rid in offered_ids:
                    out[rid] = mat
            return out
    raise ValueError("no emit_triage tool_use block in response")


def record_triage(conn, item_id, verdict, reason, triage_model, version) -> None:
    """Insert a verdict, or bump `attempts` on a retry at the same version.

    ON CONFLICT rather than a plain INSERT because a failed row is retried, and
    the unique key would otherwise make the first failure permanent until
    someone bumped the prompt version.
    """
    conn.execute(
        "INSERT INTO item_triage "
        "  (item_id, verdict, reason, triage_model, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, triage_prompt_version) DO UPDATE SET "
        "  verdict = EXCLUDED.verdict, reason = EXCLUDED.reason, "
        "  triage_model = EXCLUDED.triage_model, "
        "  attempts = item_triage.attempts + 1",
        (item_id, verdict, reason, triage_model, version),
    )


def select_sampled(conn, version: int, per_day: int) -> list[int]:
    """Items BOTH halves rejected, promoted to material as the §5.3 control.

    Why this exists, and it is not coverage: a model-judged `topical` set is
    confounded with the triage model's own view of the domain. Measure enum
    variance over topical rows alone and there is no way to separate "the
    extractor works" from "triage picked items its sibling finds easy". A
    recency sample is not confounded, so these rows are the control the other
    arms are read against.

    Cheap because it is capped: removing the model triage half entirely and
    integrating by recency was considered and rejected -- it moves integration
    from ~120/day to ~1,200/day in the expensive tier.

    PER DAY, NOT PER RUN. The schedule is hourly, so a bare LIMIT would draw
    the cap on every fire: 20 becomes 480/day in the EXPENSIVE tier, turning a
    ~17% control-arm overhead into ~400% -- the same order of cost error as the
    proposal this arm was chosen over.

    The cap counts PROMOTIONS today via `sampled_at`, not `created_at`:
    `created_at` records when the row was TRIAGED, and `record_triage`'s
    ON CONFLICT never rewrites it on promotion. A row triaged yesterday and
    promoted today would be invisible to today's budget under `created_at`,
    letting the cap silently unbind across every UTC day boundary.
    """
    used = conn.execute(
        "SELECT count(*) FROM item_triage "
        "WHERE reason = 'sampled' AND sampled_at >= date_trunc('day', now())"
    ).fetchone()[0]
    remaining = max(0, per_day - used)
    if remaining == 0:
        return []
    rows = conn.execute(
        "SELECT item_id FROM item_triage "
        "WHERE triage_prompt_version = %s AND verdict = 'immaterial' "
        "ORDER BY item_id DESC LIMIT %s",
        (version, remaining),
    ).fetchall()
    return [r[0] for r in rows]


# Prompt-budget choices, not measurements. Ordered most-recent-first so
# truncation drops the least likely candidates, and a cap reached is counted.
CANDIDATE_ENTITY_CAP = 40
CANDIDATE_EVENT_CAP = 30
CANDIDATE_WINDOW_DAYS = 14

INTEGRATE_MAX_TOKENS = 8192


def candidate_events(conn, entity_ids: list[int], tally: Tally) -> list[dict]:
    """Recent events sharing an entity with this batch.

    Returns id and summary and NOTHING ELSE. events.type and
    commitment_state are scored by the pre-registered gate; sending them here
    would make every attached assertion inherit the framing by echo, and the
    gate would measure the prompt rather than the model.
    """
    if not entity_ids:
        return []
    rows = conn.execute(
        "SELECT DISTINCT e.id, e.summary, e.occurred_at FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id "
        "WHERE ee.entity_id = ANY(%s) "
        "  AND e.occurred_at >= now() - make_interval(days => %s) "
        "ORDER BY e.occurred_at DESC "
        "LIMIT %s",
        (list(entity_ids), CANDIDATE_WINDOW_DAYS, CANDIDATE_EVENT_CAP + 1),
    ).fetchall()
    if len(rows) > CANDIDATE_EVENT_CAP:
        tally.candidate_cap_hit += 1
        rows = rows[:CANDIDATE_EVENT_CAP]
    return [{"id": r[0], "summary": r[1]} for r in rows]


_INTEGRATE_SYSTEM = """You extract structured knowledge from news items for a \
geopolitics and macro knowledge base.

For each item, return:
  entities   -- the actors involved. Prefer an id from CANDIDATE ENTITIES when \
one refers to the same real-world actor; otherwise propose a new entity.
  events     -- what happened. Prefer an id from CANDIDATE EVENTS when the item \
reports the SAME event another outlet already reported; otherwise propose a new \
one. Matching an existing event is how corroboration is recorded, so match \
whenever the underlying occurrence is the same, even if the wording differs.
  assertion  -- how this outlet stands behind each event.

ONE ENTITY PER REAL-WORLD ACTOR. A company and its equity line are the SAME \
entity: use type 'company' and put the ticker in aliases. Never create a \
separate entity of type 'instrument' for a company's shares.

Distinguish what was DONE from what was SAID. "Trump declared the ceasefire \
over" is a statement; whether the ceasefire is over is a separate matter.

CANDIDATE LABELS. Candidates are listed with labels like ENT1 and EVT1. Set \
`candidate` to one of those labels ONLY to refer to that exact listed item. \
OMIT `candidate` entirely for anything new -- never invent or number a label \
yourself. Entities are deduplicated by name automatically, so you do not need \
an id to link two mentions of the same actor across items."""

_INTEGRATE_TOOL = {
    "name": "emit_extraction",
    "description": "Structured extraction for each input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer"},
                        "entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate": {
                                        "type": "string",
                                        "description": (
                                            "A label from CANDIDATE ENTITIES, "
                                            "e.g. 'ENT1'. Omit entirely for a "
                                            "new entity; never invent a label."
                                        ),
                                    },
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "country",
                                            "institution",
                                            "company",
                                            "person",
                                            "instrument",
                                        ],
                                    },
                                    "aliases": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        },
                        "events": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate": {
                                        "type": "string",
                                        "description": (
                                            "A label from CANDIDATE EVENTS, "
                                            "e.g. 'EVT1'. Omit entirely for a "
                                            "new event; never invent a label."
                                        ),
                                    },
                                    "summary": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": ["action", "statement", "disclosure"],
                                    },
                                    "commitment_state": {
                                        "type": "string",
                                        "enum": [
                                            "in_force",
                                            "committed",
                                            "intended",
                                            "proposed",
                                        ],
                                    },
                                    "standing": {
                                        "type": "string",
                                        "enum": [
                                            "verified",
                                            "official",
                                            "reported",
                                            "attributed",
                                            "alleged",
                                        ],
                                    },
                                },
                                "required": ["standing"],
                            },
                        },
                    },
                    "required": ["item_id", "entities", "events"],
                },
            }
        },
        "required": ["items"],
    },
}


def _integrate_model() -> str:
    return common.INTEGRATE_MODEL or common.MODEL


_ENTITY_LABEL = "ENT"
_EVENT_LABEL = "EVT"


def label_map(prefix: str, candidates: list[dict]) -> dict[str, int]:
    """Opaque label -> real id. The ONE definition of the label scheme.

    Raw database ids used to be sent to the model as `candidate_id`. `entities.id`
    is BIGSERIAL from 1, and a model numbering its own extractions 1, 2, 3
    produces values indistinguishable from real ids the moment the KB holds any
    rows -- so a parser liberal enough to survive a cold start would bind an
    assertion to an unrelated entity, silently and permanently. A label space
    the model cannot accidentally land in removes that ambiguity structurally
    rather than by instruction (news-brief-bqa.11).
    """
    return {f"{prefix}{i}": c["id"] for i, c in enumerate(candidates, 1)}


def _resolve_label(raw, labels: dict[str, int], tally) -> int | None:
    """The real id an offered label names, or None meaning "not a reference".

    None covers three cases that must NOT be distinguished by the caller: the
    field was omitted (compliance), it held a non-string, or it held a string
    naming nothing offered. All three fall through to the new-entity path.
    Rejecting instead would deadlock the cold start -- with an empty KB nothing
    can map, so no entity is ever created, so there are never any candidates.

    Only a supplied-but-unmatched value is counted: an omitted field is the
    documented way to say "this is new" and is not non-compliance.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and raw in labels:
        return labels[raw]
    if tally is not None:
        tally.unmapped_candidate += 1
    return None


def build_integration_request(
    items: list[dict], entities: list[dict], events: list[dict]
) -> dict:
    # Labels, never raw ids. Rendered from the SAME label_map the parser accepts
    # against, so what is offered and what is understood cannot drift apart.
    ent_lines = (
        "\n".join(
            f"- {label} {e['name']} ({e['type']})"
            for label, e in zip(label_map(_ENTITY_LABEL, entities), entities)
        )
        or "(none)"
    )
    # label and summary ONLY. See candidate_events.
    ev_lines = (
        "\n".join(
            f"- {label} {e['summary']}"
            for label, e in zip(label_map(_EVENT_LABEL, events), events)
        )
        or "(none)"
    )
    item_lines = "\n".join(
        f"- item_id={it['id']} outlet={it.get('outlet', '?')} "
        f"title={clean(it['title'])!r}\n  body={clean(it.get('body'))!r}"
        for it in items
    )
    return {
        "model": _integrate_model(),
        "max_tokens": INTEGRATE_MAX_TOKENS,
        "thinking": {"type": "disabled"},
        "system": _INTEGRATE_SYSTEM,
        "tools": [_INTEGRATE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_extraction"},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"CANDIDATE ENTITIES:\n{ent_lines}\n\n"
                    f"CANDIDATE EVENTS:\n{ev_lines}\n\n"
                    f"ITEMS:\n{item_lines}"
                ),
            }
        ],
    }


_ENTITY_TYPES = {"country", "institution", "company", "person", "instrument"}
_EVENT_TYPES = {"action", "statement", "disclosure"}
_COMMITMENT = {"in_force", "committed", "intended", "proposed"}
_STANDING = {"verified", "official", "reported", "attributed", "alleged"}


def parse_integration_response(
    resp: dict,
    offered_item_ids: set[int],
    entity_labels: dict[str, int],
    event_labels: dict[str, int],
    tally=None,
) -> list[dict]:
    """Validated extractions, one per item. Drops an item WHOLE on any defect.

    Dropping the whole item rather than the bad part is deliberate: a
    half-written extraction is a claim about the world that no source made, and
    the item can be retried.

    An unrecognised `candidate` is NOT such a defect. It is treated as "this is
    new" rather than as a reference, because a label the model invented cannot
    collide with a real id -- see `label_map`. It is counted, not silently
    absorbed.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("integration response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_extraction":
            rows = block.get("input", {}).get("items")
            if not isinstance(rows, list):
                raise ValueError("emit_extraction input missing 'items' list")
            return [
                p
                for r in rows
                if isinstance(r, dict)
                and (
                    p := _validate_item(
                        r, offered_item_ids, entity_labels, event_labels, tally
                    )
                )
            ]
    raise ValueError("no emit_extraction tool_use block in response")


def _validate_item(
    row, item_ids, entity_labels, event_labels, tally=None
) -> dict | None:
    item_id = row.get("item_id")
    if not _is_id(item_id) or item_id not in item_ids:
        _note(tally, "validate:item_id")
        return None

    entities = []
    for e in row.get("entities") or []:
        if not isinstance(e, dict):
            _note(tally, "validate:entity_shape")
            return None
        cid = _resolve_label(e.get("candidate"), entity_labels, tally)
        if cid is not None:
            entities.append({"candidate_id": cid})
            continue
        name, etype = e.get("name"), e.get("type")
        if not (isinstance(name, str) and name.strip() and etype in _ENTITY_TYPES):
            _note(tally, "validate:entity_fields")
            return None
        aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
        entities.append({"name": name.strip(), "type": etype, "aliases": aliases})

    events = []
    for ev in row.get("events") or []:
        if not isinstance(ev, dict) or ev.get("standing") not in _STANDING:
            _note(tally, "validate:event_shape")
            return None
        cid = _resolve_label(ev.get("candidate"), event_labels, tally)
        if cid is not None:
            events.append({"candidate_id": cid, "standing": ev["standing"]})
            continue
        summary, etype = ev.get("summary"), ev.get("type")
        commitment = ev.get("commitment_state")
        if not (isinstance(summary, str) and summary.strip()):
            _note(tally, "validate:event_summary")
            return None
        if etype not in _EVENT_TYPES or commitment not in _COMMITMENT:
            _note(tally, "validate:event_enums")
            return None
        events.append(
            {
                "summary": summary.strip(),
                "type": etype,
                "commitment_state": commitment,
                "standing": ev["standing"],
            }
        )

    # An empty list here is NOT a defect: every member that survived the loops
    # above was well-formed, so reaching this point with nothing means the item
    # genuinely had nothing to extract. Returning None would collapse that into
    # the malformed case, and run() would charge it three integration calls
    # before the ceiling retired it (news-brief-uer). write_extraction owns the
    # empty branch, because marking the item done is the only thing that stops
    # it being re-offered.
    return {"item_id": item_id, "entities": entities, "events": events}


def _resolve_entity(conn, spec: dict, tally: Tally) -> int | None:
    """A candidate id, or an upserted new entity. None means refused."""
    if "candidate_id" in spec:
        tally.entities_resolved += 1
        return spec["candidate_id"]

    name, etype = spec["name"], spec["type"]
    # bqa.9 item 5, enforced where the code can see it. The prompt states the
    # rule too, but jx9.5 showed a guard on a model-supplied field gets walked
    # around by the model choosing the other value.
    if etype == "instrument":
        shadowed = conn.execute(
            "SELECT 1 FROM entity_instruments ei JOIN entities e ON e.id = ei.entity_id "
            "WHERE lower(ei.symbol) = lower(%s) AND e.type = 'company' LIMIT 1",
            (name,),
        ).fetchone()
        if shadowed:
            tally.instrument_entity_refused += 1
            log.warning(
                f"Comprehend: refused instrument entity {name!r}; a company "
                f"already maps that symbol (one entity per company)"
            )
            return None

    row = conn.execute(
        "INSERT INTO entities (name, type, aliases, extractor_model, prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (lower(name), type) DO NOTHING RETURNING id",
        (
            name,
            etype,
            spec.get("aliases") or [],
            _integrate_model(),
            INTEGRATE_PROMPT_VERSION,
        ),
    ).fetchone()
    if row:
        tally.entities_created += 1
        return row[0]
    tally.entities_resolved += 1
    return conn.execute(
        "SELECT id FROM entities WHERE lower(name) = lower(%s) AND type = %s",
        (name, etype),
    ).fetchone()[0]


def write_extraction(conn, extraction: dict, index: SurfaceIndex, tally: Tally) -> bool:
    """Write one item's extraction inside its OWN savepoint.

    capture.store_items already established this pattern and documented why:
    each entry gets its own savepoint so neither a duplicate nor a rejected
    entry can lose the entries around it. Here the payload is far more
    expensive to re-derive, so the argument is stronger, not weaker.

    Returns True if the item was integrated.
    """
    item_id = extraction["item_id"]
    # A new event has no unique key to ON CONFLICT against, so re-running the
    # same extraction (a retry, a re-queued item) would otherwise mint a
    # second event and a second assertion every time. integrated_at alone is
    # NOT the right guard, though: spec 4.2's pending predicate is
    # `integrated_at IS NULL OR integrate_prompt_version < :current`, and
    # 0009's comment names exactly the failure a version-blind guard would
    # reintroduce -- "bumping the integration prompt left integrated_at set
    # so nothing could re-extract". Scope the guard to the CURRENT version:
    # bumping INTEGRATE_PROMPT_VERSION must still re-integrate. A NULL
    # integrate_prompt_version makes `NULL >= n` NULL (never true), so a row
    # that was never integrated always falls through and (re-)writes.
    already = conn.execute(
        "SELECT 1 FROM item_triage WHERE item_id = %s "
        "AND triage_prompt_version = %s AND verdict = 'material' "
        "AND integrated_at IS NOT NULL AND integrate_prompt_version >= %s",
        (item_id, TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION),
    ).fetchone()
    if already:
        return True

    # Nothing to extract. Mark it done and charge it nothing: it is neither a
    # model failure nor a parser failure, and leaving integrated_at NULL is the
    # shape this repo has hit three times -- a row a predicate keeps
    # re-selecting that nothing advances. `ORDER BY i.id` would put it at the
    # FRONT of every future batch, re-paying its share of the call each pass.
    # Note this is NOT the same as "every entity was refused": that path went
    # through resolution and had work rejected, so it keeps its attempt.
    if not extraction["entities"] and not extraction["events"]:
        conn.execute(
            "UPDATE item_triage SET integrated_at = now(), "
            "  integrate_prompt_version = %s "
            "WHERE item_id = %s AND triage_prompt_version = %s "
            "  AND verdict = 'material'",
            (INTEGRATE_PROMPT_VERSION, item_id, TRIAGE_PROMPT_VERSION),
        )
        tally.empty_extraction += 1
        return True

    try:
        with conn.transaction():
            entity_ids = []
            for spec in extraction["entities"]:
                eid = _resolve_entity(conn, spec, tally)
                if eid is not None:
                    entity_ids.append(eid)
                    if "name" in spec:
                        index.add_entity(eid, spec["name"], spec.get("aliases") or [])
            if not entity_ids:
                raise NoEntitySurvived("no entity survived resolution")

            for ev in extraction["events"]:
                if "candidate_id" in ev:
                    event_id = ev["candidate_id"]
                    tally.events_matched += 1
                else:
                    # occurred_at is NOT optional here, and omitting it is fatal
                    # rather than untidy. candidate_events filters
                    # `occurred_at >= now() - interval`, which is FALSE for
                    # NULL -- so an event created without one can never be
                    # offered as a candidate, events_matched stays 0, and the
                    # corroboration floor fails BY CONSTRUCTION while the
                    # matcher is working perfectly.
                    #
                    # It is taken from the item's published_at, an observed fact
                    # capture already stores, rather than extracted: a model
                    # guess here would be one more unmeasured field, and article
                    # publication is a good enough proxy for a 14-day window.
                    event_id = conn.execute(
                        "INSERT INTO events (summary, type, commitment_state, "
                        "  occurred_at, extractor_model, prompt_version) "
                        "VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s) RETURNING id",
                        (
                            ev["summary"],
                            ev["type"],
                            ev["commitment_state"],
                            extraction.get("published_at"),
                            _integrate_model(),
                            INTEGRATE_PROMPT_VERSION,
                        ),
                    ).fetchone()[0]
                    tally.events_created += 1

                for eid in entity_ids:
                    conn.execute(
                        "INSERT INTO event_entities (event_id, entity_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (event_id, eid),
                    )
                # source_relationship is deliberately NOT written: no rubric
                # exists for it, and bqa.8 owns its fate.
                written = conn.execute(
                    "INSERT INTO assertions (item_id, event_id, standing, "
                    "  extractor_model, prompt_version) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (item_id, event_id) DO NOTHING RETURNING id",
                    (
                        item_id,
                        event_id,
                        ev["standing"],
                        _integrate_model(),
                        INTEGRATE_PROMPT_VERSION,
                    ),
                ).fetchone()
                if written:
                    tally.assertions_written += 1

            conn.execute(
                "UPDATE item_triage SET integrated_at = now(), "
                "  integrate_prompt_version = %s "
                "WHERE item_id = %s AND triage_prompt_version = %s "
                "  AND verdict = 'material'",
                (INTEGRATE_PROMPT_VERSION, item_id, TRIAGE_PROMPT_VERSION),
            )
    except Exception as exc:
        tally.items_lost_to_savepoint += 1
        tally.failed_integration += 1
        _note(tally, f"savepoint:{type(exc).__name__}")
        log.warning(
            f"Comprehend: item {item_id} rolled back its savepoint", exc_info=True
        )
        conn.execute(
            "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
            "WHERE item_id = %s",
            (item_id,),
        )
        return False
    return True


def write_batch(conn, extractions, index: SurfaceIndex, tally: Tally) -> int:
    """One transaction for the batch, one savepoint per item inside it.

    The OUTER `conn.transaction()` is load-bearing and must not be removed as
    redundant. db.connect() sets autocommit=False, so psycopg's
    `conn.transaction()` is a real transaction when it is the outermost block
    and a SAVEPOINT only when one is already open. Without this wrapper the
    behaviour of write_extraction depends on whether the caller happens to have
    an open transaction -- which differs between a test that just committed and
    the run loop, which has an open SELECT. A test would then assert semantics
    production never uses.
    """
    with conn.transaction():
        return sum(1 for e in extractions if write_extraction(conn, e, index, tally))

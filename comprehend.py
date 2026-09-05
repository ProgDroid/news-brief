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

import common
from common import log

# Bump on any material change to the triage prompt. A prompt change that is not
# versioned is indistinguishable from a change in the world.
TRIAGE_PROMPT_VERSION = 1
INTEGRATE_PROMPT_VERSION = 1

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
    instrument_entity_refused: int = 0
    candidate_cap_hit: int = 0
    failures: dict = field(default_factory=dict)


def run(conn) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS.

    Commit boundaries are load-bearing: one transaction per micro-batch with a
    savepoint per item. See the module docstring and spec section 6.4.
    """
    tally = Tally(enabled=bool(common.COMPREHEND_ENABLED))
    if not tally.enabled:
        log.info("Comprehend: disabled by COMPREHEND_ENABLED; nothing read")
        return tally

    deadline = time.monotonic() + DEADLINE_SECONDS
    pending = pending_triage(
        conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS)
    )
    tally.items_seen = len(pending)
    _ = deadline  # stages are added in Tasks 4-10
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
                if (
                    isinstance(rid, int)
                    and isinstance(mat, bool)
                    and rid in offered_ids
                ):
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
    """
    used = conn.execute(
        "SELECT count(*) FROM item_triage "
        "WHERE reason = 'sampled' AND created_at >= date_trunc('day', now())"
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
over" is a statement; whether the ceasefire is over is a separate matter."""

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
                                    "candidate_id": {"type": "integer"},
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
                                    "candidate_id": {"type": "integer"},
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


def build_integration_request(
    items: list[dict], entities: list[dict], events: list[dict]
) -> dict:
    ent_lines = (
        "\n".join(f"- id={e['id']} {e['name']} ({e['type']})" for e in entities)
        or "(none)"
    )
    # id and summary ONLY. See candidate_events.
    ev_lines = "\n".join(f"- id={e['id']} {e['summary']}" for e in events) or "(none)"
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
    offered_entity_ids: set[int],
    offered_event_ids: set[int],
) -> list[dict]:
    """Validated extractions, one per item. Drops an item WHOLE on any defect.

    Dropping the whole item rather than the bad part is deliberate: a
    half-written extraction is a claim about the world that no source made, and
    the item can be retried. A hallucinated candidate id is the specific hazard
    -- the model may name an id that was never offered, and writing it would
    attach this item's assertion to an unrelated entity or event.
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
                        r, offered_item_ids, offered_entity_ids, offered_event_ids
                    )
                )
            ]
    raise ValueError("no emit_extraction tool_use block in response")


def _validate_item(row, item_ids, entity_ids, event_ids) -> dict | None:
    item_id = row.get("item_id")
    if not isinstance(item_id, int) or item_id not in item_ids:
        return None

    entities = []
    for e in row.get("entities") or []:
        if not isinstance(e, dict):
            return None
        cid = e.get("candidate_id")
        if cid is not None:
            if cid not in entity_ids:
                return None
            entities.append({"candidate_id": cid})
            continue
        name, etype = e.get("name"), e.get("type")
        if not (isinstance(name, str) and name.strip() and etype in _ENTITY_TYPES):
            return None
        aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
        entities.append({"name": name.strip(), "type": etype, "aliases": aliases})

    events = []
    for ev in row.get("events") or []:
        if not isinstance(ev, dict) or ev.get("standing") not in _STANDING:
            return None
        cid = ev.get("candidate_id")
        if cid is not None:
            if cid not in event_ids:
                return None
            events.append({"candidate_id": cid, "standing": ev["standing"]})
            continue
        summary, etype = ev.get("summary"), ev.get("type")
        commitment = ev.get("commitment_state")
        if not (isinstance(summary, str) and summary.strip()):
            return None
        if etype not in _EVENT_TYPES or commitment not in _COMMITMENT:
            return None
        events.append(
            {
                "summary": summary.strip(),
                "type": etype,
                "commitment_state": commitment,
                "standing": ev["standing"],
            }
        )

    if not entities or not events:
        return None
    return {"item_id": item_id, "entities": entities, "events": events}

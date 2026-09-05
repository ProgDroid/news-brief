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

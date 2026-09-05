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

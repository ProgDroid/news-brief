"""Inspect ONE real integration call: what the model returned, and what survives.

`comprehend` cannot currently say WHY integration rejected an item. `Tally.failures`
is declared and written by nothing, so it always prints as `{}` — which reads like
"no failure details" rather than "field nobody populates" — and `failed_integration`
is a bare count. `_validate_item` has six `return None` branches and no way to
report which one fired.

This rebuilds one integration call exactly as `comprehend.run()` builds it, dumps
the raw `emit_extraction` payload, and prints `_validate_item`'s verdict per row.
The raw payload is the point: read it against `_ENTITY_TYPES`, `_EVENT_TYPES`,
`_COMMITMENT` and `_STANDING` to see which field a rejected row is missing.

There is deliberately NO mirror of `_validate_item` that names the failing branch.
A hand-written copy agrees with itself by construction and cannot detect that it
has drifted from the function it claims to explain.

READ-ONLY. It SELECTs, calls the model once, prints, and rolls back — no
`item_triage` counter moves, so it burns no integration attempts.

Run (there is no checkout on the deploy host, so this runs from the image):

    docker compose run --rm --entrypoint python newsbrief \
        scripts/inspect_integration.py [batch_size]
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402  (path shim above must run first)
from comprehend import (  # noqa: E402
    _ENTITY_LABEL,
    _EVENT_LABEL,
    CANDIDATE_ENTITY_CAP,
    INTEGRATE_PROMPT_VERSION,
    TRIAGE_PROMPT_VERSION,
    SurfaceIndex,
    Tally,
    _validate_item,
    build_integration_request,
    call_integration,
    candidate_events,
    clean,
    label_map,
)

DEFAULT_BATCH = 5


def select_batch(conn, limit: int) -> list[dict]:
    """The SAME select `run()` uses, so this probes the rows that actually failed.

    `ORDER BY i.id` matters: it is what puts the same low-id items at the front of
    every pass, so a front-of-queue sample is the population that burns its retry
    budget first — not a convenient sample from somewhere in the middle.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.verdict = 'material' AND t.integrate_attempts < 3 "
        "  AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s) "
        "ORDER BY i.id LIMIT %s",
        (TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION, limit),
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


def build_candidates(conn, batch: list[dict]) -> tuple[list[dict], list[dict]]:
    """Candidate entities and events, assembled exactly as `run()` assembles them.

    Approximating this would measure a different layer than the one that failed:
    the candidate lists are part of the prompt, and an id the model was never
    offered is one of the things `_validate_item` rejects on.
    """
    index = SurfaceIndex.build(conn)
    hits = [
        sf
        for it in batch
        for sf in index.match(f"{clean(it['title'])}\n{clean(it['body'])}")
        if sf.entity_id is not None
    ]
    ranked = sorted(dict.fromkeys(sf.entity_id for sf in hits), reverse=True)
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
    return cand_entities, candidate_events(conn, entity_ids, Tally(enabled=True))


def report(conn, limit: int) -> int:
    batch = select_batch(conn, limit)
    if not batch:
        print("No material, un-integrated items available. Nothing to inspect.")
        return 2

    outlets = dict(conn.execute("SELECT id, name FROM outlets").fetchall())
    payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
    cand_entities, cand_events = build_candidates(conn, batch)

    offered_items = {it["id"] for it in batch}
    entity_labels = label_map(_ENTITY_LABEL, cand_entities)
    event_labels = label_map(_EVENT_LABEL, cand_events)

    print("=== Offered ===")
    print(f"items             : {sorted(offered_items)}")
    print(f"entity candidates : {entity_labels or '(none)'}")
    print(f"event candidates  : {event_labels or '(none)'}")

    resp = call_integration(
        build_integration_request(payload, cand_entities, cand_events)
    )

    print("\n=== Response envelope ===")
    print(f"stop_reason = {resp.get('stop_reason')}")
    print(f"usage       = {resp.get('usage')}")

    block = next(
        (
            b
            for b in resp.get("content", [])
            if b.get("type") == "tool_use" and b.get("name") == "emit_extraction"
        ),
        None,
    )
    if block is None:
        print("\nNo emit_extraction tool_use block. Raw content:")
        print(json.dumps(resp.get("content"), indent=2, default=str)[:4000])
        return 1

    print("\n=== Raw emit_extraction payload ===")
    print(json.dumps(block.get("input"), indent=2, default=str))

    items = block.get("input", {}).get("items")
    if not isinstance(items, list):
        print("\n'items' is not a list; parse_integration_response would raise here.")
        return 1

    print("\n=== _validate_item verdict per row ===")
    tally = Tally(enabled=True)
    accepted = 0
    for i, row in enumerate(items):
        if not isinstance(row, dict):
            print(f"[{i}] row is {type(row).__name__}, not an object")
            continue
        ok = (
            _validate_item(row, offered_items, entity_labels, event_labels, tally)
            is not None
        )
        accepted += int(ok)
        print(
            f"[{i}] item_id={row.get('item_id')} {'ACCEPT' if ok else 'REJECT'} "
            f"(entities={len(row.get('entities') or [])}, "
            f"events={len(row.get('events') or [])})"
        )

    print(
        f"\nrows={len(items)} offered={len(offered_items)} "
        f"accepted={accepted} rejected={len(items) - accepted} "
        f"unmapped_candidate={tally.unmapped_candidate}"
    )
    if tally.unmapped_candidate:
        print(
            "unmapped_candidate > 0 means the model supplied labels that match "
            "nothing offered. Those rows still resolve via name/type, but it is "
            "non-compliance with the prompt and worth reading the payload for."
        )
    return 0


def main() -> int:
    if not db.is_configured():
        print("No database configured. Export DATABASE_URL.")
        return 2

    limit = DEFAULT_BATCH
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            print(f"batch_size must be an integer, got {sys.argv[1]!r}")
            return 2

    with db.connect() as conn:
        try:
            return report(conn, limit)
        finally:
            # Nothing here writes, but rolling back makes that a property of the
            # script rather than a claim in its docstring.
            conn.rollback()


if __name__ == "__main__":
    sys.exit(main())

"""Event groups from embeddings, in capture order (storylines plan 1).

`embed_pending` turns items into vectors, `assign_pending` puts items into
groups, and `group_pass` runs both under the space's lock. SQL is in
`group_store`; this module commits, the store never does.

Two rules carry the design:

- Order. Items are assigned strictly by `(created_at, id)`, and assignment stops
  at the first item that has no vector yet. A provider outage in the middle of a
  backlog therefore delays grouping but never reorders it: no item is grouped
  against one captured after it.
- Nothing is retired by a failure that judged nothing. Only an empty text, or an
  input the provider refuses when sent on its own, gets a skip row; a transient,
  config or bad_response error leaves the item waiting.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Sequence

import db
import group_store as gs
from common import log
from embeddings import EmbedError, embed_text, text_hash, to_pg_vector

EMBED_CHUNK = 128  # provider inputs per call
LIVE_MAX_ITEMS = 500  # items per live pass
COMMIT_EVERY = 100  # assignments per transaction
EMPTY_TEXT = "empty_text"


@dataclass(frozen=True)
class Thresholds:
    join_sim: float
    pair_sim: float
    title_sim: float

    @classmethod
    def from_calibration(cls, cal: gs.Calibration) -> "Thresholds":
        return cls(cal.join_sim, cal.pair_sim, cal.title_sim)


def decide_group(cands: Sequence[gs.Candidate], th: Thresholds) -> gs.Candidate | None:
    """The passing candidate with the highest similarity; ties to the lower id.
    A candidate passes on similarity alone, or on a lower similarity backed by
    a similar title."""
    passing = [
        c
        for c in cands
        if c.similarity >= th.join_sim
        or (c.similarity >= th.pair_sim and c.title_similarity >= th.title_sim)
    ]
    if not passing:
        return None
    return min(passing, key=lambda c: (-c.similarity, c.group_id))


@contextmanager
def space_lock(conn, space: str):
    """The per-space interlock: the live pass, the backfill and calibration all
    hold it, so two writers never interleave in one space. Yields whether it was
    taken."""
    with db.advisory_lock(conn, f"group:{space}") as got:
        yield got


# ---- embedding ----


def _zero_embed_counts() -> dict:
    return {"embedded": 0, "reused": 0, "skipped": 0, "calls": 0}


def embed_pending(
    conn,
    embedder,
    *,
    space: str,
    dim: int,
    limit: int,
    purpose: str,
    item_ids: Sequence[int] | None = None,
) -> dict:
    """Embed up to `limit` items that have no vector in `space`, oldest first.

    Returns {"embedded", "reused", "skipped", "calls"}: `embedded` counts items
    whose vector came from a provider call this pass, `reused` items that took
    a vector already known for the same text (stored, or embedded earlier in
    this pass), `calls` the call records written. On a transient, config or
    bad_response error it raises that EmbedError after recording its calls and
    committing; the counts so far are on the exception as `.counts`.
    """
    counts = _zero_embed_counts()
    own = getattr(embedder, "space", space)
    if own != space:
        raise EmbedError("config", f"embedder is for space {own!r}, not {space!r}")
    items = gs.items_missing_vectors(conn, space=space, limit=limit, item_ids=item_ids)
    # End the read transaction before any HTTP call.
    conn.commit()
    if not items:
        return counts

    by_hash: dict[str, list[int]] = {}  # text hash -> item ids, in order
    texts: dict[str, str] = {}  # text hash -> text
    for it in items:
        text = embed_text(it.title, it.body, it.url)
        if not text:
            gs.record_skip(conn, space=space, item_id=it.id, reason=EMPTY_TEXT)
            counts["skipped"] += 1
            continue
        h = text_hash(text)
        texts.setdefault(h, text)
        by_hash.setdefault(h, []).append(it.id)
    conn.commit()

    known = gs.vectors_by_hash(conn, space=space, hashes=list(by_hash))
    rows = [(iid, h, known[h]) for h in by_hash if h in known for iid in by_hash[h]]
    gs.store_vectors(conn, space=space, dim=dim, rows=rows)
    counts["reused"] += len(rows)
    conn.commit()

    def record(calls) -> None:
        gs.record_calls(conn, calls, purpose=purpose)
        counts["calls"] += len(calls)
        # The spend is committed on its own, right after the call that made it.
        conn.commit()

    def stored(hashes: list[str], vectors) -> None:
        out = []
        for h, vec in zip(hashes, vectors):
            if len(vec) != dim:
                raise EmbedError(
                    "bad_response", f"vector has {len(vec)} components, expected {dim}"
                )
            lit = to_pg_vector(vec)
            first, *rest = by_hash[h]
            out.append((first, h, lit))
            out.extend((iid, h, lit) for iid in rest)
            counts["embedded"] += 1
            counts["reused"] += len(rest)
        gs.store_vectors(conn, space=space, dim=dim, rows=out)
        conn.commit()

    def refused(h: str, message: str) -> None:
        for iid in by_hash[h]:
            gs.record_skip(conn, space=space, item_id=iid, reason=message)
            counts["skipped"] += 1
        conn.commit()

    def embed(hashes: list[str]) -> None:
        """One call; on `input`, the two halves, down to single texts."""
        try:
            vectors, calls = embedder.embed([texts[h] for h in hashes], kind="document")
        except EmbedError as exc:
            record(exc.calls)
            if exc.kind != "input":
                raise
            if len(hashes) == 1:
                # The provider's message can echo the input: it goes in the
                # skip row, never in the log.
                log.warning(
                    f"Group: {len(by_hash[hashes[0]])} item(s) refused alone in {space}"
                )
                refused(hashes[0], str(exc) or "input refused")
                return
            mid = len(hashes) // 2
            embed(hashes[:mid])
            embed(hashes[mid:])
            return
        record(calls)
        if len(vectors) != len(hashes):
            raise EmbedError(
                "bad_response",
                f"{len(vectors)} vectors for {len(hashes)} texts",
            )
        stored(hashes, vectors)

    todo = [h for h in by_hash if h not in known]
    try:
        for start in range(0, len(todo), EMBED_CHUNK):
            embed(todo[start : start + EMBED_CHUNK])
    except EmbedError as exc:
        exc.counts = dict(counts)
        raise
    return counts


# ---- assignment ----


def assign_pending(
    conn, *, space: str, thresholds: Thresholds, window_hours: int, limit: int
) -> dict:
    """Assign up to `limit` ungrouped items, oldest first, stopping at the first
    item without a vector. Commits every COMMIT_EVERY items and at the end."""
    counts = {
        "grouped": 0,
        "opened": 0,
        "joined": 0,
        "stopped_at_missing_vector": False,
    }
    for item, vector in gs.next_ungrouped(conn, space=space, limit=limit):
        if vector is None:
            counts["stopped_at_missing_vector"] = True
            break
        cands = gs.candidate_groups(
            conn,
            space=space,
            vector=vector,
            title=item.title,
            created_at=item.created_at,
            window_hours=window_hours,
        )
        choice = decide_group(cands, thresholds)
        if choice is None:
            gs.open_group(conn, space=space, item=item)
            counts["opened"] += 1
        else:
            gs.join_group(
                conn, space=space, group_id=choice.group_id, item=item, candidate=choice
            )
            counts["joined"] += 1
        counts["grouped"] += 1
        if counts["grouped"] % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()
    return counts


# ---- the pass ----


def group_pass(
    conn,
    embedder,
    *,
    space: str,
    dim: int,
    window_hours: int,
    max_items: int,
    purpose: str = "items",
) -> dict:
    """Embed, then (in a calibrated space) assign, holding the space's lock.

    Status: ok, nothing_to_do, busy, uncalibrated, or the EmbedError kind
    (transient, config, bad_response). An embed error still lets the items that
    already have vectors be assigned: the stop rule keeps the order. When both
    apply, the embed error is the status and the space is still unassigned.
    """
    out = {
        "status": None,
        "error": None,
        **_zero_embed_counts(),
        "grouped": 0,
        "opened": 0,
        "joined": 0,
        "stopped_at_missing_vector": False,
    }
    with space_lock(conn, space) as got:
        if not got:
            out.update(status="busy", error=f"another pass holds {space}")
            return out
        cal = gs.latest_calibration(conn, space)
        conn.commit()
        failure: EmbedError | None = None
        try:
            out.update(
                embed_pending(
                    conn,
                    embedder,
                    space=space,
                    dim=dim,
                    limit=max_items,
                    purpose=purpose,
                )
            )
        except EmbedError as exc:
            conn.rollback()
            out.update(getattr(exc, "counts", {}))
            failure = exc
        if cal is not None:
            out.update(
                assign_pending(
                    conn,
                    space=space,
                    thresholds=Thresholds.from_calibration(cal),
                    window_hours=window_hours,
                    limit=max_items,
                )
            )
    if failure is not None:
        out.update(status=failure.kind, error=f"{space}: {failure}")
    elif cal is None:
        out.update(
            status="uncalibrated",
            error=f"no calibration for space {space}: vectors stored, nothing grouped",
        )
    elif any(out[k] for k in ("embedded", "reused", "skipped", "grouped")):
        out["status"] = "ok"
    else:
        out["status"] = "nothing_to_do"
    return out

"""Storyline store (plan 2a): storylines, proposals and attach decisions.

This module owns ALL SQL for the 0019 tables; callers pass a connection and
never write that SQL themselves. Nothing here commits: the caller does. There
is no DELETE from any of these tables, by design.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

PROBLEM_MAX_CHARS = 300


@dataclass(frozen=True)
class Storyline:
    id: int
    name: str
    description: str
    status: str
    pinned: bool
    last_attached_at: datetime | None


class Outcome(NamedTuple):
    storyline_id: int | None
    # "bound" | "duplicate" | "unknown" | "already accepted" | "already rejected" | None
    problem: str | None


def live_storylines(conn) -> list[Storyline]:
    rows = conn.execute(
        "SELECT id, name, description, status, pinned, last_attached_at"
        " FROM storylines WHERE status IN ('active', 'dormant') ORDER BY id"
    ).fetchall()
    return [Storyline(*r) for r in rows]


def count_live(conn) -> int:
    return conn.execute(
        "SELECT count(*) FROM storylines WHERE status IN ('active', 'dormant')"
    ).fetchone()[0]


def add_storyline(
    conn,
    name: str,
    description: str,
    *,
    origin: str,
    pinned: bool = False,
    max_live: int,
) -> Outcome:
    """Insert a live storyline. The bound is checked first, then the duplicate."""
    if count_live(conn) >= max_live:
        return Outcome(None, "bound")
    dup = conn.execute(
        "SELECT 1 FROM storylines WHERE lower(name) = lower(%s) AND status <> 'retired'",
        (name,),
    ).fetchone()
    if dup:
        return Outcome(None, "duplicate")
    sid = conn.execute(
        "INSERT INTO storylines (name, description, origin, pinned)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (name, description, origin, pinned),
    ).fetchone()[0]
    return Outcome(sid, None)


def retire_storyline(conn, storyline_id: int, *, on_date: str) -> bool:
    """Retire a live storyline; False when it is not live."""
    row = conn.execute(
        "UPDATE storylines SET status = 'retired', retired_on = %s"
        " WHERE id = %s AND status <> 'retired' RETURNING id",
        (on_date, storyline_id),
    ).fetchone()
    return row is not None


def add_proposal(
    conn, name: str, description: str, *, origin: str, pinned: bool = False
) -> int:
    return conn.execute(
        "INSERT INTO storyline_proposals (name, description, origin, pinned)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (name, description, origin, pinned),
    ).fetchone()[0]


def pending_proposals(conn, *, origin: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, description, pinned, origin FROM storyline_proposals"
        " WHERE outcome IS NULL AND (%s::text IS NULL OR origin = %s::text)"
        " ORDER BY id",
        (origin, origin),
    ).fetchall()
    keys = ("id", "name", "description", "pinned", "origin")
    return [dict(zip(keys, r)) for r in rows]


def get_proposal(conn, proposal_id: int) -> dict | None:
    """One proposal by id (decided or not), or None."""
    row = conn.execute(
        "SELECT id, name, description, pinned, origin, outcome"
        " FROM storyline_proposals WHERE id = %s",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "name", "description", "pinned", "origin", "outcome")
    return dict(zip(keys, row))


def decide_proposal(conn, proposal_id: int, accept: bool, *, max_live: int) -> Outcome:
    """Accept or reject a proposal. On `bound` or `duplicate` it stays pending."""
    row = conn.execute(
        "SELECT name, description, pinned, origin, outcome FROM storyline_proposals"
        " WHERE id = %s FOR UPDATE",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return Outcome(None, "unknown")
    name, description, pinned, origin, outcome = row
    if outcome is not None:
        return Outcome(None, f"already {outcome}")
    if not accept:
        conn.execute(
            "UPDATE storyline_proposals SET outcome = 'rejected', decided_at = now()"
            " WHERE id = %s",
            (proposal_id,),
        )
        return Outcome(None, None)
    added = add_storyline(
        conn,
        name,
        description,
        origin="seed" if origin == "seed" else "proposal",
        pinned=pinned,
        max_live=max_live,
    )
    if added.problem is not None:
        return added
    conn.execute(
        "UPDATE storyline_proposals SET outcome = 'accepted', decided_at = now(),"
        " storyline_id = %s WHERE id = %s",
        (added.storyline_id, proposal_id),
    )
    return added


def undecided_items(
    conn,
    *,
    now: datetime,
    lookback_hours: int,
    max_attempts: int,
    max_defers: int,
    limit: int,
) -> list[dict]:
    """Recent items still to attach: no decision yet, or a retryable failure/deferral."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.body FROM items i"
        " LEFT JOIN item_attach_decisions d ON d.item_id = i.id"
        " WHERE i.created_at > %s - make_interval(hours => %s)"
        "   AND (d.item_id IS NULL"
        "        OR (d.outcome IN ('failed', 'deferred')"
        "            AND d.attempts < %s AND d.defers < %s))"
        " ORDER BY i.created_at, i.id LIMIT %s",
        (now, lookback_hours, max_attempts, max_defers, limit),
    ).fetchall()
    return [{"id": r[0], "title": r[1], "body": r[2]} for r in rows]


def record_decision(
    conn, item_id: int, storyline_ids: list[int], *, model: str
) -> None:
    """Record `attached` (with its rows) or `none` for an item.

    Overwrites only a `failed` or `deferred` row, keeping its counters. A row
    that is already final is left alone: no decision and no attachment rows.
    """
    outcome = "attached" if storyline_ids else "none"
    written = conn.execute(
        "INSERT INTO item_attach_decisions (item_id, outcome, model)"
        " VALUES (%s, %s, %s)"
        " ON CONFLICT (item_id) DO UPDATE"
        "   SET outcome = EXCLUDED.outcome, model = EXCLUDED.model, decided_at = now()"
        "   WHERE item_attach_decisions.outcome IN ('failed', 'deferred')"
        " RETURNING item_id",
        (item_id, outcome, model),
    ).fetchone()
    if written is None:
        return
    for sid in storyline_ids:
        conn.execute(
            "INSERT INTO item_attachments (item_id, storyline_id) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (item_id, sid),
        )
    if storyline_ids:
        conn.execute(
            "UPDATE storylines SET last_attached_at = now() WHERE id = ANY(%s)",
            (list(storyline_ids),),
        )


def record_failure(conn, item_id: int, *, model: str, problem: str) -> None:
    """A judged-bad answer for the item: upsert `failed`, `attempts + 1`."""
    conn.execute(
        "INSERT INTO item_attach_decisions (item_id, outcome, model, attempts, last_problem)"
        " VALUES (%s, 'failed', %s, 1, %s)"
        " ON CONFLICT (item_id) DO UPDATE"
        "   SET outcome = 'failed', model = EXCLUDED.model,"
        "       attempts = item_attach_decisions.attempts + 1,"
        "       last_problem = EXCLUDED.last_problem, decided_at = now()"
        "   WHERE item_attach_decisions.outcome IN ('failed', 'deferred')",
        (item_id, model, problem[:PROBLEM_MAX_CHARS]),
    )


def record_deferral(conn, item_id: int, *, model: str, problem: str) -> None:
    """A response that judged nothing: upsert `deferred`, `defers + 1`."""
    conn.execute(
        "INSERT INTO item_attach_decisions (item_id, outcome, model, defers, last_problem)"
        " VALUES (%s, 'deferred', %s, 1, %s)"
        " ON CONFLICT (item_id) DO UPDATE"
        "   SET outcome = 'deferred', model = EXCLUDED.model,"
        "       defers = item_attach_decisions.defers + 1,"
        "       last_problem = EXCLUDED.last_problem, decided_at = now()"
        "   WHERE item_attach_decisions.outcome IN ('failed', 'deferred')",
        (item_id, model, problem[:PROBLEM_MAX_CHARS]),
    )


def attached_since(conn, *, since: datetime) -> dict[int, int]:
    rows = conn.execute(
        "SELECT storyline_id, count(*) FROM item_attachments"
        " WHERE attached_at >= %s GROUP BY storyline_id",
        (since,),
    ).fetchall()
    return {sid: n for sid, n in rows}


def items_between(conn, *, start: datetime, end: datetime) -> list[dict]:
    """Items created in [start, end), oldest first: the offline check's input."""
    rows = conn.execute(
        "SELECT id, title, body FROM items"
        " WHERE created_at >= %s AND created_at < %s ORDER BY created_at, id",
        (start, end),
    ).fetchall()
    return [{"id": r[0], "title": r[1], "body": r[2]} for r in rows]

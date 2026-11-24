"""Group store (storylines plan 1): vectors, embedding calls, calibrations and
event groups.

This module owns ALL SQL for the 0020 tables; callers pass a connection and
never write that SQL themselves. Nothing here commits: the caller does. There
is no DELETE from any of these tables, by design.

Order is `(items.created_at, items.id)` everywhere, and a group's history is
read through its members' `joined_at` (the item's own capture time), never wall
clock: an item is never compared with a member captured after it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from embeddings import PROBLEM_MAX_CHARS, CallRecord, clean_title

SKIP_REASON_MAX_CHARS = 300  # item_embed_skips.reason is limited to this


@dataclass(frozen=True)
class PendingItem:
    id: int
    title: str
    body: str | None
    url: str
    outlet: str
    outlet_kind: str
    created_at: datetime


@dataclass(frozen=True)
class Candidate:
    group_id: int
    similarity: float
    title_similarity: float


@dataclass(frozen=True)
class Calibration:
    space: str
    join_sim: float
    pair_sim: float
    title_sim: float
    retrieval_min_sim: float | None
    weighted_precision: float
    weighted_recall: float
    pairs_labelled: int
    prompt_version: int
    judge_model: str
    week_ending: date


_PENDING_COLS = (
    "i.id, i.title, i.body, i.url, o.name, o.kind, i.created_at"
    " FROM items i JOIN outlets o ON o.id = i.outlet_id"
)
_NOT_SKIPPED = (
    "NOT EXISTS (SELECT 1 FROM item_embed_skips s"
    " WHERE s.item_id = i.id AND s.space = %(space)s)"
)


# ---- calls ----


def record_calls(conn, calls: Sequence[CallRecord], *, purpose: str) -> None:
    for c in calls:
        conn.execute(
            "INSERT INTO embedding_calls (provider, space, purpose, inputs, tokens,"
            " runtime_s, boot_s, cold_start, batch, usd, openai_equivalent_usd,"
            " outcome, problem)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                c.provider,
                c.space,
                purpose,
                c.inputs,
                c.tokens,
                c.runtime_s,
                c.boot_s,
                c.cold_start,
                c.batch,
                c.usd,
                c.openai_equivalent_usd,
                c.outcome,
                None if c.problem is None else c.problem[:PROBLEM_MAX_CHARS],
            ),
        )


# ---- vectors ----


def items_missing_vectors(
    conn, *, space: str, limit: int, item_ids: Sequence[int] | None = None
) -> list[PendingItem]:
    """Items with neither a vector nor a skip row in `space`, oldest first. With
    `item_ids`, only those items are considered."""
    only = " AND i.id = ANY(%(ids)s)" if item_ids is not None else ""
    rows = conn.execute(
        f"SELECT {_PENDING_COLS}"
        " WHERE NOT EXISTS (SELECT 1 FROM item_embeddings e"
        " WHERE e.item_id = i.id AND e.space = %(space)s)"
        f" AND {_NOT_SKIPPED}{only}"
        " ORDER BY i.created_at, i.id LIMIT %(limit)s",
        {"space": space, "limit": limit, "ids": list(item_ids or [])},
    ).fetchall()
    return [PendingItem(*r) for r in rows]


def items_with_vectors(conn, *, space: str, item_ids: Sequence[int]) -> set[int]:
    """Of `item_ids`, those that already have a vector in `space`."""
    if not item_ids:
        return set()
    rows = conn.execute(
        "SELECT item_id FROM item_embeddings WHERE space = %s AND item_id = ANY(%s)",
        (space, list(item_ids)),
    ).fetchall()
    return {r[0] for r in rows}


def record_skip(conn, *, space: str, item_id: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO item_embed_skips (item_id, space, reason) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (item_id, space, reason[:SKIP_REASON_MAX_CHARS]),
    )


def vectors_by_hash(conn, *, space: str, hashes: Sequence[str]) -> dict[str, str]:
    """text_hash -> a stored vector literal for that text, for the hashes known."""
    if not hashes:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (text_hash) text_hash, embedding::text"
        " FROM item_embeddings WHERE space = %s AND text_hash = ANY(%s)"
        " ORDER BY text_hash, item_id",
        (space, list(hashes)),
    ).fetchall()
    return dict(rows)


def store_vectors(
    conn, *, space: str, dim: int, rows: Sequence[tuple[int, str, str]]
) -> None:
    """rows: (item_id, text_hash, vector literal)."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO item_embeddings (item_id, space, text_hash, dim, embedding)"
            " VALUES (%s, %s, %s, %s, %s::vector) ON CONFLICT DO NOTHING",
            [(item_id, space, h, dim, vec) for item_id, h, vec in rows],
        )


# ---- groups ----


def next_ungrouped(
    conn, *, space: str, limit: int
) -> list[tuple[PendingItem, str | None]]:
    """Items not yet in a group of `space` and not skipped there, oldest first,
    each with its vector literal (None when it has none yet)."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.url, o.name, o.kind, i.created_at,"
        " e.embedding::text"
        " FROM items i JOIN outlets o ON o.id = i.outlet_id"
        " LEFT JOIN item_embeddings e ON e.item_id = i.id AND e.space = %(space)s"
        " WHERE NOT EXISTS (SELECT 1 FROM item_group_members m"
        " WHERE m.item_id = i.id AND m.space = %(space)s)"
        f" AND {_NOT_SKIPPED}"
        " ORDER BY i.created_at, i.id LIMIT %(limit)s",
        {"space": space, "limit": limit},
    ).fetchall()
    return [(PendingItem(*r[:7]), r[7]) for r in rows]


def candidate_groups(
    conn,
    *,
    space: str,
    vector: str,
    title: str,
    created_at: datetime,
    window_hours: int,
    min_similarity: float,
    limit: int = 50,
) -> list[Candidate]:
    """Groups opened within `window_hours` before `created_at`, seen only through
    members that joined at or before `created_at`. Per group: the best cosine
    similarity and the best title trigram similarity over those members.

    `title` is the item's CLEANED title (`embeddings.clean_title`), compared with
    the members' cleaned titles. Groups whose best similarity is under
    `min_similarity` (the caller passes the pair floor, below which no rule can
    pass) are dropped BEFORE the limit, so a pair-rule match ranked below a
    crowd of cosine-only near misses is still returned."""
    rows = conn.execute(
        "SELECT m.group_id,"
        " max(1 - (e.embedding <=> %(vec)s::vector)) AS sim,"
        " max(similarity(m.title, %(title)s)) AS tsim"
        " FROM item_groups g"
        " JOIN item_group_members m ON m.group_id = g.id AND m.space = g.space"
        " JOIN item_embeddings e ON e.item_id = m.item_id AND e.space = m.space"
        " WHERE g.space = %(space)s"
        " AND g.opened_at >= %(at)s - %(hours)s * interval '1 hour'"
        " AND m.joined_at <= %(at)s"
        " GROUP BY m.group_id"
        " HAVING max(1 - (e.embedding <=> %(vec)s::vector)) >= %(floor)s"
        " ORDER BY sim DESC, m.group_id"
        " LIMIT %(limit)s",
        {
            "vec": vector,
            "title": title,
            "space": space,
            "at": created_at,
            "hours": window_hours,
            "floor": min_similarity,
            "limit": limit,
        },
    ).fetchall()
    return [Candidate(gid, float(sim), float(tsim)) for gid, sim, tsim in rows]


def _add_member(conn, *, space, group_id, item, sim, tsim) -> None:
    conn.execute(
        "INSERT INTO item_group_members"
        " (item_id, space, group_id, joined_at, title, similarity, title_similarity)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            item.id,
            space,
            group_id,
            item.created_at,
            clean_title(item.title, item.url),
            sim,
            tsim,
        ),
    )


def _refresh(conn, group_id: int) -> None:
    """outlet_count (distinct outlets), last_seen_at and has_primary, from the
    members: recomputed, never incremented, so they cannot drift."""
    conn.execute(
        "UPDATE item_groups g SET outlet_count = s.n, last_seen_at = s.last,"
        " has_primary = s.prim"
        " FROM (SELECT count(DISTINCT i.outlet_id) AS n, max(m.joined_at) AS last,"
        " bool_or(o.kind = 'primary') AS prim"
        " FROM item_group_members m JOIN items i ON i.id = m.item_id"
        " JOIN outlets o ON o.id = i.outlet_id WHERE m.group_id = %(g)s) s"
        " WHERE g.id = %(g)s",
        {"g": group_id},
    )


def open_group(conn, *, space: str, item: PendingItem) -> int:
    """A new group founded by `item`; opened_at is the item's capture time."""
    gid = conn.execute(
        "INSERT INTO item_groups"
        " (space, opened_at, last_seen_at, title, outlet_count, has_primary)"
        " VALUES (%s, %s, %s, %s, 1, %s) RETURNING id",
        (
            space,
            item.created_at,
            item.created_at,
            clean_title(item.title, item.url),
            item.outlet_kind == "primary",
        ),
    ).fetchone()[0]
    _add_member(conn, space=space, group_id=gid, item=item, sim=None, tsim=None)
    return gid


def join_group(
    conn, *, space: str, group_id: int, item: PendingItem, candidate: Candidate
) -> None:
    _add_member(
        conn,
        space=space,
        group_id=group_id,
        item=item,
        sim=candidate.similarity,
        tsim=candidate.title_similarity,
    )
    _refresh(conn, group_id)


def oldest_ungrouped_created_at(conn, *, space: str) -> datetime | None:
    """Capture time of the oldest item neither grouped nor skipped in `space`,
    with or without a vector: an item that cannot be embedded still ages."""
    return conn.execute(
        "SELECT min(i.created_at) FROM items i"
        " WHERE NOT EXISTS (SELECT 1 FROM item_group_members m"
        " WHERE m.item_id = i.id AND m.space = %(space)s)"
        f" AND {_NOT_SKIPPED}",
        {"space": space},
    ).fetchone()[0]


def embedding_month_to_date(conn, *, now: datetime) -> dict[str, dict]:
    """Spend per provider for the calendar month of `now`, in UTC.

    Every recorded call counts, a failed one included: it may have been billed.
    Providers with no calls this month are absent."""
    rows = conn.execute(
        "SELECT provider, sum(usd), sum(openai_equivalent_usd), count(*),"
        " count(*) FILTER (WHERE cold_start), sum(tokens)"
        " FROM embedding_calls"
        " WHERE created_at >= date_trunc('month', %(now)s AT TIME ZONE 'UTC')"
        "   AT TIME ZONE 'UTC'"
        " AND created_at < (date_trunc('month', %(now)s AT TIME ZONE 'UTC')"
        "   + interval '1 month') AT TIME ZONE 'UTC'"
        " GROUP BY provider",
        {"now": now},
    ).fetchall()
    return {
        provider: {
            "usd": float(usd),
            "openai_equivalent_usd": float(equiv),
            "calls": int(calls),
            "cold_starts": int(cold),
            "tokens": int(tokens),
        }
        for provider, usd, equiv, calls, cold, tokens in rows
    }


# ---- calibrations ----

_CAL_COLS = (
    "space, join_sim, pair_sim, title_sim, retrieval_min_sim, weighted_precision,"
    " weighted_recall, pairs_labelled, prompt_version, judge_model, week_ending"
)


def latest_calibration(conn, space: str) -> Calibration | None:
    """The row in force for `space`: its newest."""
    row = conn.execute(
        f"SELECT {_CAL_COLS} FROM embedding_calibrations"
        " WHERE space = %s ORDER BY id DESC LIMIT 1",
        (space,),
    ).fetchone()
    return Calibration(*row) if row else None


def write_calibration(conn, cal: Calibration) -> None:
    conn.execute(
        f"INSERT INTO embedding_calibrations ({_CAL_COLS})"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            cal.space,
            cal.join_sim,
            cal.pair_sim,
            cal.title_sim,
            cal.retrieval_min_sim,
            cal.weighted_precision,
            cal.weighted_recall,
            cal.pairs_labelled,
            cal.prompt_version,
            cal.judge_model,
            cal.week_ending,
        ),
    )


# ---- retrieval ----


@dataclass(frozen=True)
class GroupView:
    """A group as it stood at `as_of`: every field comes from the members that
    had joined by then, never from the stored (current) group row."""

    group_id: int
    title: str
    outlets: tuple[str, ...]
    outlet_count: int
    perspectives: tuple[str, ...]
    has_primary: bool
    first_seen: datetime
    last_seen: datetime
    item_ids: tuple[int, ...]


def query_vector(conn, *, space: str, text_hash: str) -> str | None:
    row = conn.execute(
        "SELECT embedding::text FROM query_embeddings"
        " WHERE space = %s AND text_hash = %s",
        (space, text_hash),
    ).fetchone()
    return row[0] if row else None


def store_query_vector(
    conn, *, space: str, dim: int, text_hash: str, vector: str
) -> None:
    conn.execute(
        "INSERT INTO query_embeddings (text_hash, space, dim, embedding)"
        " VALUES (%s, %s, %s, %s::vector) ON CONFLICT DO NOTHING",
        (text_hash, space, dim, vector),
    )


def vector_hits(
    conn,
    *,
    space: str,
    vector: str,
    since: datetime,
    as_of: datetime,
    min_similarity: float | None,
    limit: int,
) -> list[tuple[int, float]]:
    """(group_id, best cosine similarity), best first. A group qualifies through
    any member with `since < joined_at <= as_of`. `min_similarity` None means no
    floor: only rank counts."""
    rows = conn.execute(
        "SELECT m.group_id, max(1 - (e.embedding <=> %(vec)s::vector)) AS sim"
        " FROM item_group_members m"
        " JOIN item_embeddings e ON e.item_id = m.item_id AND e.space = m.space"
        " WHERE m.space = %(space)s AND m.joined_at > %(since)s"
        " AND m.joined_at <= %(as_of)s"
        " GROUP BY m.group_id"
        " HAVING %(floor)s::float8 IS NULL"
        " OR max(1 - (e.embedding <=> %(vec)s::vector)) >= %(floor)s::float8"
        " ORDER BY sim DESC, m.group_id LIMIT %(limit)s",
        {
            "vec": vector,
            "space": space,
            "since": since,
            "as_of": as_of,
            "floor": min_similarity,
            "limit": limit,
        },
    ).fetchall()
    return [(gid, float(sim)) for gid, sim in rows]


def lexical_hits(
    conn,
    *,
    space: str,
    query: str,
    since: datetime,
    as_of: datetime,
    min_score: float,
    limit: int,
) -> list[tuple[int, float]]:
    """(group_id, best word_similarity of a member's cleaned title inside the
    query), best first. The title is the first argument, so a short title found
    inside a long query scores high."""
    rows = conn.execute(
        "SELECT m.group_id, max(word_similarity(m.title, %(q)s)) AS score"
        " FROM item_group_members m"
        " WHERE m.space = %(space)s AND m.joined_at > %(since)s"
        " AND m.joined_at <= %(as_of)s"
        " GROUP BY m.group_id"
        " HAVING max(word_similarity(m.title, %(q)s)) >= %(min)s"
        " ORDER BY score DESC, m.group_id LIMIT %(limit)s",
        {
            "q": query,
            "space": space,
            "since": since,
            "as_of": as_of,
            "min": min_score,
            "limit": limit,
        },
    ).fetchall()
    return [(gid, float(score)) for gid, score in rows]


def groups_as_of(
    conn, *, group_ids: Sequence[int], as_of: datetime
) -> dict[int, GroupView]:
    """Each group as it stood at `as_of`, from members with `joined_at <= as_of`
    only. A group with no such member is absent. The stored outlet_count,
    last_seen_at and has_primary are current, not as-of, and are never read."""
    if not group_ids:
        return {}
    rows = conn.execute(
        "SELECT m.group_id,"
        " (array_agg(m.title ORDER BY m.joined_at, m.item_id))[1],"
        " array_agg(DISTINCT o.name ORDER BY o.name),"
        " count(DISTINCT o.id),"
        " array_agg(DISTINCT o.perspective ORDER BY o.perspective)"
        " FILTER (WHERE o.perspective IS NOT NULL),"
        " bool_or(o.kind = 'primary'),"
        " min(m.joined_at), max(m.joined_at),"
        " array_agg(m.item_id ORDER BY m.joined_at, m.item_id)"
        " FROM item_group_members m"
        " JOIN items i ON i.id = m.item_id JOIN outlets o ON o.id = i.outlet_id"
        " WHERE m.group_id = ANY(%s) AND m.joined_at <= %s"
        " GROUP BY m.group_id",
        (list(group_ids), as_of),
    ).fetchall()
    return {
        r[0]: GroupView(
            group_id=r[0],
            title=r[1],
            outlets=tuple(r[2]),
            outlet_count=r[3],
            perspectives=tuple(r[4] or ()),
            has_primary=r[5],
            first_seen=r[6],
            last_seen=r[7],
            item_ids=tuple(r[8]),
        )
        for r in rows
    }


# ---- calibration reads (scripts/embed_calibrate.py) ----


def items_in_window(conn, *, since: datetime, until: datetime) -> list[PendingItem]:
    """Items created in [since, until), oldest first."""
    rows = conn.execute(
        f"SELECT {_PENDING_COLS}"
        " WHERE i.created_at >= %s AND i.created_at < %s"
        " ORDER BY i.created_at, i.id",
        (since, until),
    ).fetchall()
    return [PendingItem(*r) for r in rows]


def item_vectors(conn, *, space: str, item_ids: Sequence[int]) -> dict[int, str]:
    """item id -> its vector literal in `space`, for the items that have one."""
    if not item_ids:
        return {}
    rows = conn.execute(
        "SELECT item_id, embedding::text FROM item_embeddings"
        " WHERE space = %s AND item_id = ANY(%s)",
        (space, list(item_ids)),
    ).fetchall()
    return dict(rows)


def nearest_items(
    conn,
    *,
    space: str,
    vector: str,
    since: datetime,
    until: datetime,
    limit: int,
    exclude_ids: Sequence[int] = (),
) -> list[tuple[int, float]]:
    """(item id, cosine similarity), best first, ties to the lower id: item-level
    neighbours of `vector` among items created in [since, until) that have a
    vector in `space`, without `exclude_ids`."""
    rows = conn.execute(
        "SELECT e.item_id, 1 - (e.embedding <=> %(vec)s::vector) AS sim"
        " FROM item_embeddings e JOIN items i ON i.id = e.item_id"
        " WHERE e.space = %(space)s AND i.created_at >= %(since)s"
        " AND i.created_at < %(until)s AND NOT (e.item_id = ANY(%(ex)s))"
        " ORDER BY e.embedding <=> %(vec)s::vector, e.item_id LIMIT %(limit)s",
        {
            "vec": vector,
            "space": space,
            "since": since,
            "until": until,
            "ex": list(exclude_ids),
            "limit": limit,
        },
    ).fetchall()
    return [(iid, float(sim)) for iid, sim in rows]


def pair_similarities(
    conn, *, space: str, pairs: Sequence[tuple[int, int]]
) -> list[float | None]:
    """Cosine similarity of each (item, item) pair in `space`, in order; None
    where either item has no vector there."""
    if not pairs:
        return []
    rows = conn.execute(
        "SELECT 1 - (a.embedding <=> b.embedding)"
        " FROM unnest(%(x)s::bigint[], %(y)s::bigint[]) WITH ORDINALITY AS p(x, y, n)"
        " LEFT JOIN item_embeddings a ON a.item_id = p.x AND a.space = %(space)s"
        " LEFT JOIN item_embeddings b ON b.item_id = p.y AND b.space = %(space)s"
        " ORDER BY p.n",
        {"x": [a for a, _ in pairs], "y": [b for _, b in pairs], "space": space},
    ).fetchall()
    return [None if r[0] is None else float(r[0]) for r in rows]


def trigram_similarities(
    conn, pairs: Sequence[tuple[str, str]], *, word: bool = False
) -> list[float]:
    """pg_trgm `similarity(a, b)` per pair, in order, or `word_similarity(a, b)`
    (a found inside b) with `word`: the two functions grouping and retrieval use."""
    if not pairs:
        return []
    fn = "word_similarity" if word else "similarity"
    rows = conn.execute(
        f"SELECT {fn}(p.a, p.b)"
        " FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS p(a, b, n)"
        " ORDER BY p.n",
        ([a for a, _ in pairs], [b for _, b in pairs]),
    ).fetchall()
    return [float(r[0]) for r in rows]

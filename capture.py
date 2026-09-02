"""Continuous capture: polling feeds into the knowledge base (news-brief-b42.1).

The ONLY module that knows capture SQL, in the way claim_store.py owns claim
SQL. It writes `outlets` and `items` -- what the world published -- plus three
telemetry tables recording which of this reader's feeds showed it and when.

Nothing reads these rows yet. That boundary is why a broken capture costs a log
line rather than a brief; it is ALSO why the schema had to be checked against
b42.2's question directly, since no consumer exists to fail if it cannot answer.

Spec: docs/superpowers/specs/2026-09-02-continuous-capture-design.md
"""

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import brief
from common import log

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _normalize_url(url: str) -> str:
    """Strip tracking parameters and the fragment. Redirects are NOT followed:
    resolving Google News redirect URLs would double the request count and make
    dedup depend on a network call."""
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith(_TRACKING_PREFIXES) and k not in _TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def content_hash(entry: dict) -> str:
    """The item's identity: the publisher's own guid when offered, else the URL.

    Title is deliberately excluded, so a headline correction updates an item
    rather than duplicating it -- the common case on wire copy, and one this
    feed set demonstrably produces (6 of 100 entries in one sampled fetch shared
    a title with another entry while being different pages).
    """
    basis = entry.get("guid") or _normalize_url(entry.get("url", ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def resolve_outlet(conn, feed: dict, *, strict: bool = False) -> int | None:
    """The outlet id for a feed, inserting it once on first sight.

    An existing row is never rewritten: outlets are shared across readers, so a
    later feed must not silently restate another's editorial metadata. With
    `strict`, a disagreement returns None instead -- the caller drops that source
    and counts it, the contract load_temp_sources already sets for bad input.
    """
    name = brief.outlet_for(feed)
    shape = (
        feed.get("kind", "regional"),
        feed.get("perspective"),
        bool(feed.get("state_funded", False)),
    )
    row = conn.execute(
        "SELECT id, kind, perspective, state_funded FROM outlets "
        "WHERE lower(name) = lower(%s)",
        (name,),
    ).fetchone()
    if row:
        if strict and (row[1], row[2], row[3]) != shape:
            log.warning(
                f"Capture: source {feed['name']!r} disagrees with outlet {name!r} "
                f"metadata {(row[1], row[2], row[3])} vs {shape}; source dropped"
            )
            return None
        return row[0]
    return conn.execute(
        "INSERT INTO outlets (name, kind, perspective, state_funded) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (name, *shape),
    ).fetchone()[0]


def store_items(conn, outlet_id: int, entries: list[dict]) -> tuple[int, int, int]:
    """Write entries for one outlet. Returns (written, already_present, failed).

    Three outcomes, not two: written is a new row, already_present is a
    duplicate hash resolved via `ON CONFLICT DO NOTHING`, and failed is an
    entry the database itself rejected -- a NUL byte in the title, say, past
    whatever the Python guard above catches. Folding `failed` into
    `already_present` would make that bucket mean two different things, and an
    operator reading "N already held" would have no way to tell some of those
    N actually failed.

    Each entry gets its own savepoint (`conn.transaction()`), so neither a
    duplicate nor a rejected entry can lose the entries around it: a duplicate
    just returns no row, and a rejection is caught here rather than left to
    propagate, so it costs one failed entry rather than the whole pass, and
    rolls back only its own savepoint rather than poisoning the transaction
    the caller is still using.
    """
    written = already = failed = 0
    for entry in entries:
        if not entry.get("title") or not entry.get("url"):
            log.warning(
                f"Capture: entry with no title or url skipped: {str(entry)[:120]}"
            )
            continue
        try:
            with conn.transaction():
                row = conn.execute(
                    "INSERT INTO items (outlet_id, url, title, body, published_at, "
                    "content_hash) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (outlet_id, content_hash) DO NOTHING RETURNING id",
                    (
                        outlet_id,
                        entry["url"],
                        entry["title"],
                        entry.get("summary") or None,
                        entry.get("published_at"),
                        content_hash(entry),
                    ),
                ).fetchone()
        except Exception:
            log.warning(
                f"Capture: entry rejected by the database, skipped: {str(entry)[:120]}",
                exc_info=True,
            )
            failed += 1
            continue
        if row:
            written += 1
        else:
            already += 1
    return written, already, failed


@dataclass
class Tally:
    """What one pass did. Returned AND persisted, because a bare count is
    unattributable: 0 new items is ambiguous across "nothing published", "every
    fetch failed" and "the store refused everything"."""

    feeds_total: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    items_new: int = 0
    sources_dropped: int = 0
    failures: dict | None = None

    def __post_init__(self):
        if self.failures is None:
            self.failures = {}


def start_run(conn, enabled: bool) -> int:
    return conn.execute(
        "INSERT INTO capture_runs (enabled) VALUES (%s) RETURNING id", (enabled,)
    ).fetchone()[0]


def finish_run(conn, run_id: int, tally: "Tally") -> None:
    conn.execute(
        "UPDATE capture_runs SET finished_at = now(), feeds_total = %s, "
        "feeds_ok = %s, feeds_failed = %s, items_seen = %s, items_new = %s, "
        "sources_dropped = %s WHERE id = %s",
        (
            tally.feeds_total,
            tally.feeds_ok,
            tally.feeds_failed,
            tally.items_seen,
            tally.items_new,
            tally.sources_dropped,
            run_id,
        ),
    )


def record_poll(conn, run_id: int, source_name: str, failure, entries_seen: int):
    conn.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, failure, entries_seen) "
        "VALUES (%s, %s, %s, %s)",
        (run_id, source_name, failure, entries_seen),
    )


def record_sightings(conn, source_name: str, entries: list[dict], item_ids: dict):
    """Advance last_seen_at for everything this feed showed.

    first_seen_at is never touched on conflict: it is the left edge of dwell
    time. A failed poll calls this with nothing, so no timestamp moves --
    nothing was observed, so nothing is asserted.
    """
    for position, entry in enumerate(entries, start=1):
        digest = content_hash(entry)
        conn.execute(
            "INSERT INTO feed_sightings (source_name, content_hash, item_id, position) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (source_name, content_hash) DO UPDATE "
            "SET last_seen_at = now(), position = EXCLUDED.position",
            (source_name, digest, item_ids.get(digest), position),
        )


def rolled_off(conn, source_name: str) -> list[str]:
    """Hashes this feed has stopped serving, judged ONLY against polls that ran.

    The predicate must name `failure IS NULL` explicitly. A query that omits it
    counts a 403 as evidence of absence and reports a large, clean, entirely
    fictitious roll-off.
    """
    rows = conn.execute(
        "SELECT s.content_hash FROM feed_sightings s "
        "WHERE s.source_name = %s AND EXISTS ("
        "  SELECT 1 FROM feed_polls p WHERE p.source_name = s.source_name "
        "  AND p.failure IS NULL AND p.polled_at > s.last_seen_at)",
        (source_name,),
    ).fetchall()
    return [r[0] for r in rows]

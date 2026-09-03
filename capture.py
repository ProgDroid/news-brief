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
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import brief
import common
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
        # Divergent from brief.py's render-path default ("wire") on purpose:
        # this one matches the `outlets.kind` column default, and every
        # RSS_FEEDS/load_temp_sources entry sets `kind` explicitly, so the
        # difference is unreachable today.
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


def _lookup_item_ids(conn, outlet_id: int, entries: list[dict]) -> dict[str, int]:
    """Map content_hash -> items.id for this outlet, one batched query.

    Called after store_items so a just-written row is already visible in the
    same transaction. store_items cannot supply this itself: `ON CONFLICT DO
    NOTHING RETURNING id` returns no row for the already-present case, which is
    the majority, so item_id would otherwise be NULL by construction on every
    row feed_sightings writes.
    """
    hashes = [content_hash(entry) for entry in entries]
    if not hashes:
        return {}
    rows = conn.execute(
        "SELECT content_hash, id FROM items WHERE outlet_id = %s "
        "AND content_hash = ANY(%s)",
        (outlet_id, hashes),
    ).fetchall()
    return dict(rows)


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
    # Not persisted: capture_runs has no items_already/items_failed columns
    # (migration 0008 is already applied), so these live in the log line only.
    items_already: int = 0
    items_failed: int = 0
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


DEADLINE_SECONDS = 600
HOST_GAP_SECONDS = 5


def capture_sources() -> list[dict]:
    """Feeds only. `brief.all_sources()` also returns source_type='page' entries,
    which are scraped pages with no entry list. RSS_FEEDS carries no source_type
    key, so its ABSENCE means feed."""
    temp = [
        s for s in brief.load_temp_sources() if s.get("source_type", "feed") == "feed"
    ]
    return list(brief.RSS_FEEDS) + temp


def _host(feed: dict) -> str:
    return urlsplit(feed["url"]).netloc


def order_by_host(feeds: list[dict]) -> list[dict]:
    """Interleave so no two consecutive fetches hit one host.

    Round-robins across per-host queues, longest queue first, which spreads the
    heaviest host as widely as the list allows.
    """
    queues: dict[str, list[dict]] = {}
    for feed in feeds:
        queues.setdefault(_host(feed), []).append(feed)
    ordered: list[dict] = []
    while any(queues.values()):
        candidates = sorted(
            (h for h, q in queues.items() if q),
            key=lambda h: (-len(queues[h]), h),
        )
        placed = next(
            (h for h in candidates if not ordered or _host(ordered[-1]) != h),
            candidates[0],
        )
        ordered.append(queues[placed].pop(0))
    return ordered


def run(conn) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS so it cannot outlive its own
    fire time and trip the supervisor's overlap alert.

    COMMIT BOUNDARIES ARE LOAD-BEARING. db.connect() is autocommit=False, so a
    pass wrapped in one transaction rolls the capture_runs row back on a crash --
    and a crashed pass then looks exactly like one that never fired, which is the
    ambiguity that row exists to remove. We commit the run row immediately, then
    once per feed, then at the end. A crash costs at most one feed's work, which
    is what "capture is cheap and irreversible" has to mean in practice.
    """
    enabled = bool(common.CAPTURE_ENABLED)
    tally = Tally()
    run_id = start_run(conn, enabled)
    conn.commit()
    if not enabled:
        log.info("Capture: disabled by CAPTURE_ENABLED; no feeds polled")
        finish_run(conn, run_id, tally)
        conn.commit()
        return tally

    feeds = order_by_host(capture_sources())
    tally.feeds_total = len(feeds)
    deadline = time.monotonic() + DEADLINE_SECONDS
    last_fetch_at: dict[str, float] = {}

    for feed in feeds:
        if time.monotonic() >= deadline:
            record_poll(conn, run_id, feed["name"], "deadline", 0)
            conn.commit()
            tally.feeds_failed += 1
            tally.failures["deadline"] = tally.failures.get("deadline", 0) + 1
            continue
        host = _host(feed)
        since = time.monotonic() - last_fetch_at.get(host, 0.0)
        if host in last_fetch_at and since < HOST_GAP_SECONDS:
            time.sleep(HOST_GAP_SECONDS - since)
        last_fetch_at[host] = time.monotonic()

        got = brief.fetch_feed_entries(feed)
        if got.failure:
            record_poll(conn, run_id, feed["name"], got.failure, 0)
            conn.commit()
            tally.feeds_failed += 1
            tally.failures[got.failure] = tally.failures.get(got.failure, 0) + 1
            continue

        outlet_id = resolve_outlet(conn, feed, strict=True)
        if outlet_id is None:
            tally.sources_dropped += 1
            record_poll(conn, run_id, feed["name"], "outlet_conflict", 0)
            conn.commit()
            tally.feeds_failed += 1
            tally.failures["outlet_conflict"] = (
                tally.failures.get("outlet_conflict", 0) + 1
            )
            continue

        written, already, failed = store_items(conn, outlet_id, got.entries)
        item_ids = _lookup_item_ids(conn, outlet_id, got.entries)
        record_sightings(conn, feed["name"], got.entries, item_ids)
        record_poll(conn, run_id, feed["name"], None, len(got.entries))
        # INVARIANT: record_sightings and this feed's record_poll must commit
        # in the SAME transaction as each other. rolled_off's whole predicate
        # rests on Postgres's now() (== transaction_timestamp()) being
        # identical for a sighting and the poll that produced it, so
        # `polled_at > last_seen_at` is false for a same-pass poll. Splitting
        # the commit between them, or swapping now() for clock_timestamp(),
        # makes every feed's entire window read as rolled off on every pass.
        conn.commit()
        tally.feeds_ok += 1
        tally.items_seen += len(got.entries)
        tally.items_new += written
        tally.items_already += already
        tally.items_failed += failed

    finish_run(conn, run_id, tally)
    conn.commit()
    kinds = ", ".join(f"{k} x{v}" for k, v in sorted(tally.failures.items()))
    log.info(
        f"Capture: {tally.feeds_total} feeds, {tally.feeds_ok} ok, "
        f"{tally.feeds_failed} failed ({kinds or 'none'}), "
        f"{tally.items_seen} items seen, {tally.items_new} new, "
        f"{tally.items_already} already held, {tally.items_failed} failed, "
        f"{tally.sources_dropped} sources dropped"
    )
    return tally

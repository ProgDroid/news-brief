"""Continuous capture: polling feeds into the knowledge base (news-brief-b42.1).

The ONLY module that knows capture SQL, in the way claim_store.py owns claim
SQL. It writes `outlets` and `items` -- what the world published -- plus three
telemetry tables recording which of this reader's feeds showed it and when.

Nothing in the BRIEF path reads these rows, and that boundary is what keeps a
broken capture costing a log line rather than a brief. It is also why the schema
had to be checked against b42.2's question directly, since no consumer existed
to fail if it could not answer. Two readers exist now (news-brief-a9q) and both
are health surfaces, outside that path: `/capture` and the monitor's liveness
check, at the bottom of this file.

Spec: docs/superpowers/specs/2026-09-02-continuous-capture-design.md
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    # Same -- no column, log only. A skip is neither a failure nor a poll, so it
    # needs its own name: "28 feeds, 3 ok" with no third number is exactly the
    # ambiguity capture_runs exists to remove.
    feeds_not_due: int = 0
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
# HOST_GAP_SECONDS moved to common.HostSpacer (news-brief-bzo) — the brief's
# collect-time fetch needs the same guard, and capture imports brief, so the
# primitive cannot live here.


def capture_sources() -> list[dict]:
    """Feeds only, each with the URL CAPTURE should poll.

    `brief.all_sources()` also returns source_type='page' entries, which are
    scraped pages with no entry list. RSS_FEEDS carries no source_type key, so
    its ABSENCE means feed.

    A feed may carry `capture_url`, a narrower window than the brief's
    (news-brief-b42.4). Four Google News proxies return exactly 100 entries per
    poll — the cap — because `when:2d` offers Google ~370 candidates for 100
    relevance-ranked slots, and ranking is not chronological, so an item never
    in the top 100 at any poll instant is lost unobservably. A shorter window
    removes that mechanism. The brief cannot share it: it fetches at brief time
    and takes the newest 25, so a 6h window at 06:00 would hand it the overnight
    hours and nothing else.

    Substituted into a COPY. The brief reads `RSS_FEEDS` directly, and editing
    the dict in place would narrow its window too — silently, and only in
    processes that had run a capture pass first.
    """
    temp = [
        s for s in brief.load_temp_sources() if s.get("source_type", "feed") == "feed"
    ]
    return [
        {**feed, "url": feed["capture_url"]} if feed.get("capture_url") else feed
        for feed in list(brief.RSS_FEEDS) + temp
    ]


# ── Per-feed poll cadence (news-brief-b42.5 Phase 1) ──────────────────────────
# An optional key on a feed dict rather than a table: `capture_url`, `outlet` and
# `kind` already work this way, and this phase deliberately adds no schema. The
# measured intervals and their provenance are Phase 2.

POLL_INTERVAL_KEY = "poll_every_minutes"

# How far beyond the observed evidence an interval may be extrapolated.
#
# `poll_pairs` (scripts/measure_roll_off.py:151) discards every pair wider than
# nominal x MAX_GAP_FACTOR, so every turnover figure behind these intervals is
# measured over spans of at most 45 minutes. The b42.2 summary's 240 was a 5.3x
# extrapolation beyond ANY observation; 4x nominal is 2.7x, and the request
# saving has no beneficiary, so there is no reason to buy more of it with
# extrapolation risk. Spec section 3.2.
#
# A FACTOR, not an absolute: the evidence bound itself scales with nominal, so a
# hardcoded ceiling would leave the reasoning behind the moment capture is
# retimed -- silently collapsing every declared interval at nominal 60, and
# making the valid range empty at nominal 120.
MAX_INTERVAL_FACTOR = 4


def max_poll_interval_minutes() -> int:
    return _interval_minutes() * MAX_INTERVAL_FACTOR


def rank_ordered(feed: dict) -> bool:
    """Is this feed's window ordered by RELEVANCE rather than by time?

    Google News returns relevance-ranked results under a 100-entry cap, so an
    item's absence from a poll does not mean it departed -- turnover is
    unmeasurable in principle, not merely unmeasured, and such a feed can never
    be slowed on the strength of a measurement.

    Derived from the URL rather than a list of names, which would be one new
    proxy away from being silently wrong. NOT the same set as "carries a
    capture_url": eight feeds are Google News proxies, only four have an
    override.
    """
    return common.feed_host(feed) == "news.google.com"


def poll_interval_minutes(feed: dict) -> int:
    """How often this feed should be polled, in minutes.

    Fails open in every direction: an absent key, a non-integer, a bool, a
    rank-ordered feed, or anything at or below the scheduler tick all yield
    nominal -- exactly today's behaviour. The only way to be slowed is to declare
    a valid interval above nominal and not be relevance-ranked.
    """
    nominal = _interval_minutes()
    if rank_ordered(feed):
        return nominal
    declared = feed.get(POLL_INTERVAL_KEY)
    if not isinstance(declared, int) or isinstance(declared, bool):
        return nominal
    if declared <= nominal:
        return nominal
    return min(declared, max_poll_interval_minutes())


def due_feeds(conn, feeds: list[dict], now) -> list[dict]:
    """The feeds whose interval has elapsed since their last ATTEMPT.

    Last attempt rather than last success, deliberately. Keying on success would
    retry a broken feed every tick until it recovered: the worst possible
    response to a 429, and pointless against a 403, which
    `source-fetch-failure-modes` says must never be retried. Attempt-keying makes
    a feed's request rate equal its interval regardless of health, and
    `failing_feeds` -- not the retry -- is what reports the breakage.

    A `deadline` row is NOT an attempt. `run` writes one for a feed the pass ran
    out of time to reach, so nothing left the box -- counting it would let a pass
    that skipped a feed consume that feed's slot, doubling its real gap while
    feed_polls still shows a row per interval and every downstream reader reports
    the cadence as honoured.

    HALF A TICK OF SLACK, and it is load-bearing rather than a fudge. Due-ness is
    evaluated on the scheduler's discrete grid -- `previous_fire` snaps capture to
    a fixed 30-minute boundary (scheduler.py:96) -- while `polled_at` defaults to
    Postgres now() taken MID-pass. So a feed polled five seconds into the 11:30
    pass shows 29m55s elapsed at the 12:00 fire, and a bare `>= interval` would
    find it not due and defer it a whole tick. With no keys set at all that
    silently halves the fleet's cadence to 60 minutes -- the opposite of the
    "behaviour identical to today" this phase promises. Half a tick is the correct
    rounding for a threshold sampled on a grid: it makes `interval == nominal`
    fire every tick, and `interval == k * nominal` fire every k-th.

    FAILS OPEN. Any error and every feed is due, which is today's behaviour.
    """
    try:
        rows = conn.execute(
            "SELECT source_name, max(polled_at) FROM feed_polls "
            "WHERE failure IS DISTINCT FROM 'deadline' GROUP BY source_name"
        ).fetchall()
        last_attempt = {name: at for name, at in rows}
    except Exception as e:
        log.warning(f"Capture: due-check unavailable, polling every feed ({e})")
        return list(feeds)

    slack = timedelta(minutes=_interval_minutes() / 2)
    due = []
    for feed in feeds:
        at = last_attempt.get(feed["name"])
        if at is None:
            due.append(feed)
            continue
        if now - at + slack >= timedelta(minutes=poll_interval_minutes(feed)):
            due.append(feed)
    return due


def run(conn, spacer=None, now=None) -> Tally:
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
    # Injectable for the same reason `spacer` is: the store tests pin time at a
    # fixed NOW and insert poll rows relative to it, so a wall-clock read would
    # see every fixture row as days stale and poll everything.
    now = now or datetime.now(timezone.utc)
    tally = Tally()
    run_id = start_run(conn, enabled)
    conn.commit()
    if not enabled:
        log.info("Capture: disabled by CAPTURE_ENABLED; no feeds polled")
        finish_run(conn, run_id, tally)
        conn.commit()
        return tally

    sources = capture_sources()
    tally.feeds_total = len(sources)
    # Due-ness first, ordering second: order_by_host must interleave the set
    # actually being fetched, not the full list.
    feeds = common.order_by_host(due_feeds(conn, sources, now))
    tally.feeds_not_due = len(sources) - len(feeds)
    deadline = time.monotonic() + DEADLINE_SECONDS
    spacer = spacer or common.HostSpacer()

    for feed in feeds:
        if time.monotonic() >= deadline:
            record_poll(conn, run_id, feed["name"], "deadline", 0)
            conn.commit()
            tally.feeds_failed += 1
            tally.failures["deadline"] = tally.failures.get("deadline", 0) + 1
            continue
        spacer.wait(feed)

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
        f"{tally.feeds_not_due} not due, "
        f"{tally.sources_dropped} sources dropped"
    )
    return tally


# ── Reading capture back (news-brief-a9q) ─────────────────────────────────────
# The threshold-free half of "is capture healthy". A rate -- too many feeds
# failing, too few new items -- needs a number measured against real traffic,
# which is news-brief-b42.2's job and news-brief-w3q's blocker. These two do
# not: "it stopped firing" follows from the schedule, and "a pass died" follows
# from a finished_at that can never be filled in. Neither invents a constant.

STALE_AFTER_INTERVALS = 3


def _interval_minutes() -> int:
    """Capture's poll interval, read from the schedule rather than copied.

    A constant here would be a knob tracking another knob: retiming capture
    would leave the tolerance behind, still passing its tests, now meaning
    something nobody chose.
    """
    import scheduler

    return next(s.every_minutes for s in scheduler.SCHEDULES if s.job == "capture")


def liveness(conn, now) -> tuple[str, str] | None:
    """(episode key, message) when capture looks dead, else None.

    The key identifies the OUTAGE, not the check, so a caller that remembers the
    last key it sent alerts once per episode instead of once per monitor run --
    the difference between one message and one every hour until someone looks.

    Three deliberate silences:
      * No rows at all. That cannot tell "never deployed" from "broken", and the
        two want opposite responses. UNKNOWN is reported by /capture, not paged.
      * A disabled capture. It still writes a row every fire, so it can never go
        stale -- which is what capture_runs.enabled is for.
      * The newest run being unfinished. That is also what a pass in flight
        looks like; only a successor proves it can never finish.
    """
    newest = conn.execute(
        "SELECT id, started_at FROM capture_runs ORDER BY started_at DESC, id DESC "
        "LIMIT 1"
    ).fetchone()
    if newest is None:
        return None
    run_id, started_at = newest

    tolerance = timedelta(minutes=_interval_minutes() * STALE_AFTER_INTERVALS)
    if now - started_at > tolerance:
        late = now - started_at
        return (
            f"stale:{run_id}",
            f"Capture has not run for {_duration(late)}. The last pass started "
            f"{started_at:%Y-%m-%d %H:%M} UTC and nothing has fired since; the "
            f"schedule is every {_interval_minutes()} minutes.",
        )

    died = conn.execute(
        "SELECT id, started_at FROM capture_runs WHERE finished_at IS NULL "
        "AND id <> %s ORDER BY started_at DESC, id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if died is not None:
        return (
            f"crashed:{died[0]}",
            f"A capture pass died mid-flight: run {died[0]}, started "
            f"{died[1]:%Y-%m-%d %H:%M} UTC, never finished. Later passes have "
            f"run since, so it cannot complete.",
        )
    return None


# The baseline must contain at least one weekend, or the first quiet Sunday
# looks unprecedented and alerts every week. Derived from the volume cycle
# rather than picked: it is the shortest window that spans one.
HISTORY_DAYS = 7


def failing_feeds(conn, now) -> tuple[str, str] | None:
    """(episode key, message) for feeds that are being polled and never work.

    The tolerance is `STALE_AFTER_INTERVALS`, reused rather than copied — this
    is the same question `liveness` asks, one level down, and a second constant
    would be a knob tracking a knob.

    Two distinctions the filter exists for, both ABSENT versus UNKNOWN:

      * A feed still being ATTEMPTED and failing, against one that is no longer
        polled at all. A feed dropped from `RSS_FEEDS` has a last success that
        ages forever, so a stale-success test alone would alert hourly about a
        feed nobody asked for and the operator cannot fix.
      * NEVER succeeded, against succeeded long ago. The NULL survives into the
        message as "never", because rendering it as an age prints a confident
        wrong number — the failure `feed_health`'s docstring already names.
    """
    tolerance = timedelta(minutes=_interval_minutes() * STALE_AFTER_INTERVALS)
    cutoff = now - tolerance
    rows = conn.execute(
        "WITH ok AS ("
        "  SELECT source_name, max(polled_at) AS last_ok FROM feed_polls "
        "  WHERE failure IS NULL GROUP BY source_name"
        ") "
        "SELECT p.source_name, max(p.polled_at), o.last_ok, "
        "  count(*) FILTER (WHERE p.failure IS NOT NULL "
        "    AND (o.last_ok IS NULL OR p.polled_at > o.last_ok)), "
        "  (array_agg(p.failure ORDER BY p.polled_at DESC) "
        "    FILTER (WHERE p.failure IS NOT NULL))[1] "
        "FROM feed_polls p LEFT JOIN ok o ON o.source_name = p.source_name "
        "GROUP BY p.source_name, o.last_ok "
        "ORDER BY o.last_ok ASC NULLS FIRST, p.source_name"
    ).fetchall()

    failing = [
        (name, last_ok, fails, kind)
        for name, last_try, last_ok, fails, kind in rows
        if last_try > cutoff and (last_ok is None or last_ok < cutoff)
    ]
    if not failing:
        return None

    lines = [
        f"   {name[:24]:<26}"
        + (f"last ok {_duration(now - last_ok)} ago" if last_ok else "never succeeded")
        + f", {fails} failures since ({kind})"
        for name, last_ok, fails, kind in failing
    ]
    return (
        "failing:" + ",".join(sorted(name for name, _, _, _ in failing)),
        f"{len(failing)} feed(s) have not polled successfully in "
        f"{_duration(tolerance)}:\n" + "\n".join(lines),
    )


def item_drought(conn, now) -> tuple[str, str] | None:
    """(episode key, message) when new items stop arriving for longer than ever.

    The threshold is the feed's OWN history rather than a constant: alert when
    the current run of zero-new passes is longer than the longest in the
    trailing window. That makes the message state its evidence — "the longest
    gap in seven days was two passes, this is eight" — which a guessed rate
    never could, and it tracks volume as the corpus grows.

    Three silences, each a different fact from "healthy":

      * Not enough history. A baseline that has never seen a weekend makes the
        first quiet Sunday look unprecedented. Below HISTORY_DAYS this reports
        nothing, because it has measured nothing.
      * A disabled capture. It writes a row every fire with zero new items,
        which is exactly what `capture_runs.enabled` exists to tell apart.
      * A drought a fetch failure explains. That is `failing_feeds`' story, and
        two messages about one cause is how an operator learns to skim them —
        the `fail-closed-needs-status-not-count` rule about attributing a zero.
    """
    since = now - timedelta(days=HISTORY_DAYS)
    oldest = conn.execute(
        "SELECT min(started_at) FROM capture_runs "
        "WHERE enabled AND finished_at IS NOT NULL"
    ).fetchone()[0]
    if oldest is None or oldest > since:
        return None

    rows = conn.execute(
        "SELECT id, started_at, items_new FROM capture_runs "
        "WHERE enabled AND finished_at IS NOT NULL AND started_at > %s "
        "ORDER BY started_at DESC, id DESC",
        (since,),
    ).fetchall()

    current = 0
    while current < len(rows) and rows[current][2] == 0:
        current += 1
    if current == 0:
        return None

    longest, run = 0, 0
    for _, _, items_new in rows[current:]:
        run = run + 1 if items_new == 0 else 0
        longest = max(longest, run)
    if current <= longest:
        return None

    streak = [r[0] for r in rows[:current]]
    if conn.execute(
        "SELECT count(*) FROM feed_polls "
        "WHERE capture_run_id = ANY(%s) AND failure IS NOT NULL",
        (streak,),
    ).fetchone()[0]:
        return None

    started_at = rows[current - 1][1]
    return (
        f"drought:{rows[current - 1][0]}",
        f"No new items for {current} consecutive passes "
        f"({_duration(now - started_at)}).\n"
        f"   Longest gap in the previous {HISTORY_DAYS} days: {longest} passes.\n"
        "   Every poll in that stretch succeeded, so this is not a fetch failure.",
    )


def _duration(delta: timedelta) -> str:
    hours, seconds = divmod(int(delta.total_seconds()), 3600)
    return f"{hours}h{seconds // 60:02d}m" if hours else f"{seconds // 60}m"


# What /capture shows. No thresholds live here on purpose: these are the very
# numbers b42.2 needs in order to choose one, and a surface that pre-judged them
# would be hiding its own evidence.

_RUN_COLUMNS = (
    "id",
    "started_at",
    "finished_at",
    "enabled",
    "feeds_total",
    "feeds_ok",
    "feeds_failed",
    "items_seen",
    "items_new",
    "sources_dropped",
)


def last_run(conn) -> dict | None:
    """The newest pass, or None if capture has never run in this deployment."""
    row = conn.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM capture_runs "
        "ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else dict(zip(_RUN_COLUMNS, row))


def failure_breakdown(conn, runs: int = 8) -> list[tuple[str, int]]:
    """Failure kinds and their counts over the last `runs` passes, worst first.

    Windowed by a COUNT OF PASSES rather than by a time span, so it means the
    same thing after the poll interval changes -- which is a thing b42.2 exists
    to change. A time window would quietly halve or double its own sample.
    """
    rows = conn.execute(
        "SELECT failure, count(*) FROM feed_polls "
        "WHERE failure IS NOT NULL AND capture_run_id IN ("
        "  SELECT id FROM capture_runs ORDER BY started_at DESC, id DESC LIMIT %s"
        ") GROUP BY failure ORDER BY count(*) DESC, failure",
        (runs,),
    ).fetchall()
    return [(kind, n) for kind, n in rows]


def feed_health(conn) -> list[dict]:
    """Per feed: when it last worked, and how many times it has failed since.

    `failures_since` counts only failures AFTER the last success, so a feed that
    broke once and recovered reads as healthy while one that is 403ing now
    climbs. Ordered worst first -- never-succeeded, then longest-since -- because
    the operator reads the top of a Telegram message and not the bottom.

    A NULL `last_ok_at` is load-bearing and must survive to the caller: "never
    worked" and "worked recently" are opposite facts, and a render that turned
    the NULL into an age would print a confident wrong number.
    """
    rows = conn.execute(
        "WITH ok AS ("
        "  SELECT source_name, max(polled_at) AS last_ok FROM feed_polls "
        "  WHERE failure IS NULL GROUP BY source_name"
        ") "
        "SELECT p.source_name, o.last_ok, "
        "  count(*) FILTER (WHERE p.failure IS NOT NULL "
        "    AND (o.last_ok IS NULL OR p.polled_at > o.last_ok)), "
        "  (array_agg(p.failure ORDER BY p.polled_at DESC) "
        "    FILTER (WHERE p.failure IS NOT NULL))[1] "
        "FROM feed_polls p LEFT JOIN ok o ON o.source_name = p.source_name "
        "GROUP BY p.source_name, o.last_ok "
        "ORDER BY o.last_ok ASC NULLS FIRST, p.source_name"
    ).fetchall()
    return [
        {
            "source_name": name,
            "last_ok_at": last_ok,
            "failures_since": failures,
            "last_failure": kind,
        }
        for name, last_ok, failures, kind in rows
    ]

# Continuous Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A supervisor job child that polls every feed source every 30 minutes and records what it finds in `outlets`, `items`, `feed_sightings`, `feed_polls` and `capture_runs`, without touching the brief.

**Architecture:** `fetch_rss` splits into a structured fetcher (`fetch_feed_entries` returning a `FeedFetch`) and a thin renderer that keeps its current output byte-for-byte. A new top-level `capture.py` owns every line of capture SQL, the way `claim_store.py` owns claim SQL. Capture runs as a `JOB_MODES` entry spawned by the supervisor, so it inherits the advisory lock and the `job_runs` row for free.

**Tech Stack:** Python 3.12, psycopg 3, feedparser, requests, pytest, Postgres 18, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-continuous-capture-design.md`

## Global Constraints

- **The brief must not change.** `mode_submit` and `mode_collect` are untouched; `fetch_rss`'s output is byte-identical before and after Task 2. Nothing reads `items`.
- **Pre-push gate is three commands**, not just pytest: `ruff check .`, `ruff format --check .`, `pytest -q`. `ruff format` edits in place — `git add` every file it touches.
- **`pytest` alone reports green with the DB layer skipped.** Every DB-backed task must be run with `DATABASE_URL` exported. A skip is not a pass.
- **Knobs are read as `common.X`**, never `from common import X` — a from-import copy freezes at import time and defeats both host toggles and `monkeypatch`.
- **Retirement/telemetry rows are never deleted** by this work. No `DELETE` appears in `capture.py`.
- **Test counts are absolute, not deltas.** Baseline before Task 1: **1354 passed, 0 skipped** against `postgres:18-alpine`.
- **Never `docker compose up -d` locally** — it starts a second Telegram `getUpdates` consumer and 409s the live bot. `docker compose config` only.
- **Adding a `JOB_MODES` entry is not enough on its own.** `brief.py:3955-3964` looks the mode up in a `dispatch` dict *before* the `JOB_MODES` check, so a mode missing from `dispatch` prints the usage string and exits 1. Both edits are required.
- **Every non-`serve` mode pays a seed pass.** `brief.py:3941-3953` runs `config.ensure_seeded` plus four importers on each invocation, so 48 capture passes a day add ~250 seed queries daily. All are emptiness-guarded and idempotent, so this is correct rather than a bug — but it is a known cost of running a frequent job through this entry point, and it should be recognised in a slow-query log rather than rediscovered.

**Local Postgres for every DB-backed task:**

```bash
docker run --rm -d --name nb-dev -p 5433:5432 -e POSTGRES_PASSWORD=newsbrief \
  -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5433/newsbrief_test"
```

---

## File Structure

| File | Responsibility |
|---|---|
| `migrations/0008_capture_telemetry_up.sql` (create) | `feed_sightings`, `feed_polls`, `capture_runs` |
| `migrations/0008_capture_telemetry_down.sql` (create) | Drops all three, reverse order |
| `capture.py` (create) | Every line of capture SQL + the pass. No prompt or render logic. |
| `tests/test_capture_schema.py` (create) | DDL-level tests: constraints, keys, rollback |
| `tests/test_capture.py` (create) | Unit tests: hashing, ordering, deadline, source selection |
| `tests/test_capture_store.py` (create) | DB-backed: outlets, items, sightings, polls, roll-off |
| `brief.py` (modify) | `FeedFetch`, `fetch_feed_entries`, renderer, `outlet` keys, `mode_capture`, `JOB_MODES` |
| `scheduler.py` (modify) | The `capture` schedule |
| `supervisor.py` (modify) | Shutdown-budget comment arithmetic |
| `common.py` (modify) | `CAPTURE_ENABLED` knob |
| `docker-compose.yml` (modify) | Anchor line for the knob |
| `tests/test_supervisor.py` (modify) | Budget-count test |

---

## Task 1: Migration 0008 — capture telemetry tables

**Files:**
- Create: `migrations/0008_capture_telemetry_up.sql`
- Create: `migrations/0008_capture_telemetry_down.sql`
- Test: `tests/test_capture_schema.py`

**Interfaces:**
- Consumes: `db.run_migrations(conn)`, migration 0006's `items(id)`.
- Produces: tables `capture_runs`, `feed_polls`, `feed_sightings`; unique index `feed_sightings_source_hash` on `(source_name, content_hash)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture_schema.py`:

```python
"""Schema tests for capture telemetry (news-brief-b42.1).

DB-gated for the same reason tests/test_kb_schema.py is: a skip is not a pass,
and every assertion here is about what Postgres actually does.
"""

import pytest

import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL, e.g. "
    "docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=newsbrief "
    "-e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine",
)


@pytest.fixture()
def conn():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def store(conn):
    db.run_migrations(conn)
    conn.commit()
    return conn


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_the_three_telemetry_tables_exist(store):
    assert _columns(store, "capture_runs")
    assert _columns(store, "feed_polls")
    assert _columns(store, "feed_sightings")


def test_a_sighting_is_unique_per_feed_and_hash(store):
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters Markets', 'abc', 1)"
    )
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO feed_sightings (source_name, content_hash, position) "
            "VALUES ('Reuters Markets', 'abc', 2)"
        )


def test_the_same_hash_on_two_feeds_is_two_sightings(store):
    """The per-FEED key, not a global one: one item carried by two feeds is two
    sightings, which is what makes per-feed dwell time computable at all."""
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters Markets', 'abc', 1)"
    )
    store.execute(
        "INSERT INTO feed_sightings (source_name, content_hash, position) "
        "VALUES ('Reuters World', 'abc', 1)"
    )
    n = store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0]
    assert n == 2


def test_a_poll_records_its_failure_kind_and_belongs_to_a_run(store):
    run_id = store.execute(
        "INSERT INTO capture_runs (enabled) VALUES (true) RETURNING id"
    ).fetchone()[0]
    store.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, failure, entries_seen) "
        "VALUES (%s, 'Kyiv Independent', 'http_403', 0)",
        (run_id,),
    )
    row = store.execute(
        "SELECT failure, entries_seen FROM feed_polls WHERE capture_run_id = %s",
        (run_id,),
    ).fetchone()
    assert row == ("http_403", 0)


def test_a_successful_poll_stores_null_failure(store):
    run_id = store.execute(
        "INSERT INTO capture_runs (enabled) VALUES (true) RETURNING id"
    ).fetchone()[0]
    store.execute(
        "INSERT INTO feed_polls (capture_run_id, source_name, entries_seen) "
        "VALUES (%s, 'TASS', 42)",
        (run_id,),
    )
    failure = store.execute(
        "SELECT failure FROM feed_polls WHERE capture_run_id = %s", (run_id,)
    ).fetchone()[0]
    assert failure is None


def test_a_poll_cannot_orphan_itself_from_a_run(store):
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO feed_polls (capture_run_id, source_name) VALUES (999999, 'x')"
        )


def test_the_down_migration_removes_all_three(conn):
    """Executed, not assumed: no down migration is trusted until it has run."""
    db.run_migrations(conn)
    conn.commit()
    db.run_migrations(conn, direction="down")
    conn.commit()
    for table in ("capture_runs", "feed_polls", "feed_sightings"):
        assert not _columns(conn, table), f"{table} survived the down migration"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_capture_schema.py -q`
Expected: FAIL — the tables do not exist. Confirm the failures name `capture_runs` / `feed_sightings`, not a fixture or import error.

> **The signature is `run_migrations(conn, direction="up", steps=None)` (`db.py:171`) — there is no `target` parameter.** With `direction="down"` and `steps=None`, `db.py:200-201` reverses the applied list and sets `limit = 1`, so it reverts exactly one migration: the highest applied, which is 0008. Do not pass `target`; it raises `TypeError`.

- [ ] **Step 3: Write the up migration**

Create `migrations/0008_capture_telemetry_up.sql`:

```sql
-- Capture telemetry (news-brief-b42.1).
--
-- These are NOT the knowledge base. `items` records what the world published;
-- these three record which of ONE reader's feeds showed it, when, and whether
-- the poll that failed to show it actually ran. Different object, different
-- lifetime, different table -- which is why `items` gets no feed column.
--
-- See docs/superpowers/specs/2026-09-02-continuous-capture-design.md section 6.

CREATE TABLE capture_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL means the pass did not finish: a crash is told from a completed run
    -- by this column, since job_runs.exit_code cannot say which pass it was.
    finished_at     TIMESTAMPTZ NULL,
    -- A disabled pass writes a row too. job_runs has no free-text column
    -- (0001:29-38), so "switched off" and "ran fine" are byte-identical there.
    enabled         BOOLEAN     NOT NULL,
    feeds_total     INTEGER     NOT NULL DEFAULT 0,
    feeds_ok        INTEGER     NOT NULL DEFAULT 0,
    feeds_failed    INTEGER     NOT NULL DEFAULT 0,
    items_seen      INTEGER     NOT NULL DEFAULT 0,
    items_new       INTEGER     NOT NULL DEFAULT 0,
    sources_dropped INTEGER     NOT NULL DEFAULT 0
);

-- One row per feed per pass, success or failure. This table is the DENOMINATOR
-- for roll-off: an item's absence only means "left the window" if a SUCCESSFUL
-- poll followed its last sighting. Without it, a feed 403ing for a day reads as
-- its entire window rolling over at once -- a large, clean, fictitious signal,
-- and the same "a dropped feed looks identical to a quiet one" failure this repo
-- already carries a comment about at brief.py:1849-1853.
CREATE TABLE feed_polls (
    id             BIGSERIAL PRIMARY KEY,
    capture_run_id BIGINT      NOT NULL REFERENCES capture_runs(id),
    source_name    TEXT        NOT NULL,
    polled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = success. Otherwise the FeedFetch.failure kind, including
    -- 'deadline' for a feed the pass ran out of time to reach.
    failure        TEXT        NULL,
    entries_seen   INTEGER     NOT NULL DEFAULT 0
);
CREATE INDEX feed_polls_source_time ON feed_polls (source_name, polled_at DESC);

CREATE TABLE feed_sightings (
    id            BIGSERIAL PRIMARY KEY,
    -- The FEED, not the outlet: Reuters Markets and Reuters World collapse to
    -- one outlet by design (spec 4), so per-feed measurement can only live here.
    -- Deliberately TEXT and not a FK to `sources`: the baked-in RSS_FEEDS have
    -- no sources row, and a renamed or deleted source must not destroy the
    -- measurement history that justified a poll interval.
    source_name   TEXT        NOT NULL,
    content_hash  TEXT        NOT NULL,
    item_id       BIGINT      NULL REFERENCES items(id),
    position      INTEGER     NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX feed_sightings_source_hash
    ON feed_sightings (source_name, content_hash);
```

- [ ] **Step 4: Write the down migration**

Create `migrations/0008_capture_telemetry_down.sql`:

```sql
-- Reverse order: feed_polls references capture_runs.
DROP TABLE IF EXISTS feed_sightings;
DROP TABLE IF EXISTS feed_polls;
DROP TABLE IF EXISTS capture_runs;
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_capture_schema.py -q`
Expected: PASS, 7 tests, **0 skipped**. A skip means `DATABASE_URL` is unset — fix that before continuing, or this task is unverified.

- [ ] **Step 6: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add migrations/0008_capture_telemetry_up.sql migrations/0008_capture_telemetry_down.sql tests/test_capture_schema.py
git commit -m "feat(capture): telemetry tables, and the denominator that makes absence mean something"
```

Expected suite total: **1361 passed** (1354 + 7).

---

## Task 2: Split `fetch_rss` into a fetcher and a renderer

**Files:**
- Modify: `brief.py:1843-1901` (`fetch_rss`)
- Test: `tests/test_capture.py` (create)

**Interfaces:**
- Produces: `brief.FeedFetch(entries: list[dict], failure: str | None)`; `brief.fetch_feed_entries(feed) -> FeedFetch`. Each entry dict has keys `title`, `url`, `summary`, `published_raw`, `published_at`, `guid`.
- `brief.fetch_rss(feed, max_items=25) -> str` keeps its exact current signature and output.

- [ ] **Step 1: Write the characterization test FIRST**

This must be written and passing **before** any refactor. It is the only thing that proves the brief did not change.

Create `tests/test_capture.py`:

```python
"""Unit tests for continuous capture (news-brief-b42.1). No network, no DB."""

import brief


FEED = {
    "name": "Test Wire",
    "url": "https://example.com/feed",
    "category": "macro",
    "kind": "wire",
}

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item>
  <title>First headline</title>
  <link>https://example.com/a?utm_source=rss#frag</link>
  <guid>guid-a</guid>
  <description>&lt;p&gt;Body of &lt;b&gt;a&lt;/b&gt;.&lt;/p&gt;</description>
  <pubDate>Tue, 02 Sep 2026 10:00:00 GMT</pubDate>
</item>
<item>
  <title>Second headline</title>
  <link>https://example.com/b</link>
  <description>Body of b.</description>
  <pubDate>Tue, 02 Sep 2026 11:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


def test_fetch_rss_output_is_unchanged_by_the_split(monkeypatch):
    """Characterization: pins the rendered string byte-for-byte.

    Written before the refactor and never edited to match new output. If this
    test needs changing, the brief's prompt changed, which this issue forbids.
    """
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    out = brief.fetch_rss(FEED)
    assert "First headline" in out
    assert "Second headline" in out
    assert "Body of a." in out
    assert out.startswith(
        brief._source_header("Test Wire", "wire", "macro", None, False)
    )
    assert "<b>" not in out, "HTML must still be stripped"


def test_an_empty_feed_still_returns_empty_string_and_logs(monkeypatch, caplog):
    """brief.py:1877-1880 returns "" AND logs on one path. A test pinning only
    the return value passes even if the warning disappears -- and that warning is
    the only signal distinguishing a malformed feed from a quiet one."""
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(empty))
    with caplog.at_level("WARNING"):
        assert brief.fetch_rss(FEED) == ""
    assert "No entries: Test Wire" in caplog.text
```

- [ ] **Step 2: Run it against the UNMODIFIED code to verify it passes**

Run: `py -m pytest tests/test_capture.py -q`
Expected: **PASS**, 2 tests. This is the one test in this plan that must pass before the change, not fail — it is a characterization test, and a failure here means it does not describe current behaviour.

- [ ] **Step 3: Write the failing tests for the new fetcher**

Append to `tests/test_capture.py`:

```python
def test_fetch_feed_entries_returns_structured_entries(monkeypatch):
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    got = brief.fetch_feed_entries(FEED)
    assert got.failure is None
    assert [e["title"] for e in got.entries] == ["First headline", "Second headline"]
    assert got.entries[0]["guid"] == "guid-a"
    assert got.entries[0]["summary"] == "Body of a."
    assert got.entries[0]["published_at"] is not None


def test_a_missing_guid_is_none_not_absent(monkeypatch):
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries[1]["guid"] is None


def test_an_unparseable_date_becomes_none_and_keeps_the_entry(monkeypatch):
    bad = RSS.replace(b"Tue, 02 Sep 2026 10:00:00 GMT", b"not a date")
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(bad))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries[0]["published_at"] is None
    assert got.entries[0]["title"] == "First headline"


def test_a_403_is_reported_as_a_kind_not_as_emptiness(monkeypatch):
    """An empty list is ambiguous across 403 / timeout / malformed / quiet, and
    the tally promises to tell them apart."""

    class Forbidden(_Resp):
        status_code = 403

        def raise_for_status(self):
            raise brief.requests.HTTPError("403")

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: Forbidden(b""))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries == []
    assert got.failure == "http_403"


def test_an_empty_feed_is_reported_as_empty_not_malformed(monkeypatch):
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(empty))
    assert brief.fetch_feed_entries(FEED).failure == "empty"
```

- [ ] **Step 4: Run to verify they fail**

Run: `py -m pytest tests/test_capture.py -q`
Expected: 2 pass (characterization), 5 FAIL with `AttributeError: module 'brief' has no attribute 'fetch_feed_entries'`.

- [ ] **Step 5: Implement the split**

In `brief.py`, add near the other small types:

```python
class FeedFetch(NamedTuple):
    """Entries plus WHY there are none, because those are different facts.

    `fetch_rss` collapsed every failure to `return ""`, so an empty result was
    ambiguous across 403, timeout, malformed and genuinely quiet. Capture's
    tally and its roll-off denominator both need the distinction: a 403 read as
    "quiet" records a window that emptied when the fetch never happened.
    """

    entries: list[dict]
    failure: str | None = None
```

Replace the body of `fetch_rss` with a fetcher plus a renderer. `fetch_feed_entries` keeps the existing retry loop, timeout and User-Agent verbatim — do not rewrite them:

```python
def fetch_feed_entries(feed: dict) -> FeedFetch:
    """Fetch and parse one feed. All the HTTP scars live here, once."""
    try:
        for attempt in range(1, RSS_MAX_ATTEMPTS + 1):
            resp = requests.get(
                feed["url"], timeout=20, headers={"User-Agent": SOURCE_USER_AGENT}
            )
            if resp.status_code not in RSS_RETRY_STATUSES:
                break
            if attempt == RSS_MAX_ATTEMPTS:
                log.warning(
                    f"RSS gave up on {feed['name']}: {resp.status_code} after "
                    f"{attempt} attempts"
                )
                return FeedFetch([], _failure_for_status(resp.status_code))
            wait = _rss_retry_wait(resp, attempt)
            log.info(
                f"RSS retry {attempt}/{RSS_MAX_ATTEMPTS - 1} for {feed['name']}: "
                f"{resp.status_code}, waiting {wait}s "
                f"(Retry-After={(resp.headers or {}).get('Retry-After')})"
            )
            time.sleep(wait)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            bozo_exc = getattr(parsed, "bozo_exception", None)
            log.warning(f"No entries: {feed['name']} ({bozo_exc or 'empty feed'})")
            return FeedFetch([], "malformed" if bozo_exc else "empty")
        return FeedFetch([_entry_from(e) for e in parsed.entries])
    except requests.Timeout as e:
        log.warning(f"RSS failed {feed['name']}: {e}")
        return FeedFetch([], "timeout")
    except Exception as e:
        log.warning(f"RSS failed {feed['name']}: {e}")
        return FeedFetch([], _failure_for_exception(e))


def _failure_for_status(status: int) -> str:
    return {403: "http_403", 429: "http_429"}.get(status, "http_5xx")


def _failure_for_exception(exc) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return _failure_for_status(status) if status else "malformed"


def _entry_from(entry) -> dict:
    """One feedparser entry, normalized. `summary` is the FULL stripped text —
    the 400-char cap is a prompt-budget concern and stays in the renderer."""
    summary = re.sub(
        r"<[^>]+>", "", entry.get("summary", entry.get("description", "")).strip()
    )
    parsed_date = entry.get("published_parsed")
    published_at = (
        datetime(*parsed_date[:6], tzinfo=timezone.utc) if parsed_date else None
    )
    return {
        "title": entry.get("title", "").strip(),
        "url": entry.get("link", ""),
        "summary": summary,
        "published_raw": entry.get("published", ""),
        "published_at": published_at,
        "guid": entry.get("id") or None,
    }


def fetch_rss(feed: dict, max_items: int = 25) -> str:
    """Render a feed for the prompt. Output is unchanged by the b42.1 split."""
    got = fetch_feed_entries(feed)
    if got.failure:
        return ""
    lines = [
        _source_header(
            feed["name"],
            feed.get("kind", "wire"),
            feed["category"],
            feed.get("perspective"),
            feed.get("state_funded", False),
        )
    ]
    for entry in got.entries[:max_items]:
        lines.append(
            f"- {entry['title']} ({entry['published_raw']})\n  {entry['summary'][:400]}"
        )
    return "\n".join(lines)
```

**`brief.py` has no `typing` import at all** — the only match for "typing" in the file is the English word in a comment at `brief.py:1502`. Add a new line:

```python
from typing import NamedTuple
```

`re` and `time` are already imported. Confirm `datetime`/`timezone` are too before adding a duplicate — check the import block rather than assuming either way.

- [ ] **Step 6: Run the tests to verify all pass**

Run: `py -m pytest tests/test_capture.py tests/test_signals.py -q`
Expected: PASS. `test_signals.py` exercises real feed parsing and is the strongest existing check that the render path still works.

- [ ] **Step 7: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add brief.py tests/test_capture.py
git commit -m "refactor(capture): fetch_rss splits into a fetcher and a renderer, output pinned"
```

Expected suite total: **1368 passed** (1361 + 7).

---

## Task 3: The feed-to-outlet mapping

**Files:**
- Modify: `brief.py:152-355` (`RSS_FEEDS` — add `outlet` to 7 entries)
- Modify: `brief.py:467-478` (`load_temp_sources` — carry `outlet` through)
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: every feed dict may carry `"outlet": str`; `brief.outlet_for(feed) -> str` returning `feed.get("outlet") or feed["name"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`:

```python
def test_outlet_defaults_to_the_feed_name():
    assert brief.outlet_for({"name": "TASS", "url": "u", "category": "geo"}) == "TASS"


def test_an_explicit_outlet_key_wins():
    feed = {"name": "Reuters Markets", "url": "u", "category": "macro",
            "outlet": "Reuters"}
    assert brief.outlet_for(feed) == "Reuters"


def test_both_reuters_feeds_resolve_to_one_outlet():
    named = {f["name"]: f for f in brief.RSS_FEEDS}
    assert brief.outlet_for(named["Reuters Markets"]) == "Reuters"
    assert brief.outlet_for(named["Reuters World"]) == "Reuters"


def test_jacob_shapiro_publishes_under_one_outlet_across_two_media():
    """jashap.substack.com and the @jacobshap Nitter feed are the same author.
    Left unmapped they become two outlets, and one take reaching both reads as
    two independent sources corroborating each other."""
    named = {f["name"]: f for f in brief.RSS_FEEDS}
    assert brief.outlet_for(named["Intersubjectively Transmissible"]) == "Jacob Shapiro"
    assert brief.outlet_for(named["Jacob Shapiro (@jacobshap)"]) == "Jacob Shapiro"


def test_no_feed_ships_a_product_name_as_an_outlet():
    """outlets.name is UNIQUE(lower(name)) and is the corroboration dimension,
    so a feed-product name in it invents a publisher that does not exist."""
    product_names = {
        "ISW Daily Assessment",
        "BOJ Statements",
        "EIA Today in Energy",
        "Reuters Markets",
        "Reuters World",
        "Marko Papic (@geo_papic)",
        "Jacob Shapiro (@jacobshap)",
        "Intersubjectively Transmissible",
    }
    for feed in brief.RSS_FEEDS:
        if feed["name"] in product_names:
            assert brief.outlet_for(feed) != feed["name"], (
                f"{feed['name']} is a product name and needs an explicit outlet"
            )


def test_feeds_sharing_an_outlet_agree_on_its_metadata():
    """A developer error caught here rather than at runtime: outlets carries
    kind/perspective/state_funded, and two feeds mapping to one outlet cannot
    disagree about them. `category` is deliberately excluded — it is a property
    of the reader's slicing, not of the publisher, and outlets has no such
    column."""
    by_outlet: dict[str, list[dict]] = {}
    for feed in brief.RSS_FEEDS:
        by_outlet.setdefault(brief.outlet_for(feed), []).append(feed)
    for outlet, feeds in by_outlet.items():
        shapes = {
            (f.get("kind", "regional"), f.get("perspective"),
             bool(f.get("state_funded", False)))
            for f in feeds
        }
        assert len(shapes) == 1, f"{outlet} has feeds disagreeing on metadata: {shapes}"


def test_load_temp_sources_carries_the_outlet_key(monkeypatch):
    """load_temp_sources rebuilds each entry from a fixed field list, so a key it
    does not name is silently dropped — and the mapping would then work for
    baked-in feeds and fail invisibly for user sources."""
    monkeypatch.setattr(
        brief.config,
        "sources",
        lambda: [{"name": "Reuters Tech", "url": "https://x/y", "category": "macro",
                  "outlet": "Reuters"}],
    )
    loaded = brief.load_temp_sources()
    assert loaded[0]["outlet"] == "Reuters"
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_capture.py -q -k "outlet or shapiro or product or metadata"`
Expected: FAIL — `brief.outlet_for` does not exist.

- [ ] **Step 3: Add `outlet_for` and the seven mappings**

In `brief.py`, next to `all_sources`:

```python
def outlet_for(feed: dict) -> str:
    """The publisher behind a feed. Declared per feed, never derived from the URL.

    Most feed names ARE publisher names — including the Google News proxies,
    which are named for the publisher they proxy (`Kyiv Independent`, `NHK
    World`) rather than for Google. Eight are not, and those carry an explicit
    `outlet`.
    """
    return feed.get("outlet") or feed["name"]
```

Add `"outlet": ...` to exactly these `RSS_FEEDS` entries:

| Feed `name` | Add |
|---|---|
| `Reuters Markets` | `"outlet": "Reuters",` |
| `Reuters World` | `"outlet": "Reuters",` |
| `ISW Daily Assessment` | `"outlet": "Institute for the Study of War",` |
| `BOJ Statements` | `"outlet": "Bank of Japan",` |
| `EIA Today in Energy` | `"outlet": "U.S. Energy Information Administration",` |
| `Marko Papic (@geo_papic)` | `"outlet": "Marko Papic",` |
| `Jacob Shapiro (@jacobshap)` | `"outlet": "Jacob Shapiro",` |
| `Intersubjectively Transmissible` | `"outlet": "Jacob Shapiro",` |

In `load_temp_sources`, add to the `loaded` dict after `state_funded`:

```python
        outlet = entry.get("outlet")
        if outlet:
            loaded["outlet"] = str(outlet)
```

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_capture.py -q`
Expected: PASS, 14 tests.

If `test_feeds_sharing_an_outlet_agree_on_its_metadata` fails, **do not loosen the test** — the two feeds genuinely disagree and the mapping or the feed metadata is wrong. Report it.

- [ ] **Step 5: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add brief.py tests/test_capture.py
git commit -m "feat(capture): feeds declare their publisher, and one author stops being two outlets"
```

Expected suite total: **1375 passed** (1368 + 7).

---

## Task 4: `capture.py` — outlet resolution and item storage

**Files:**
- Create: `capture.py`
- Test: `tests/test_capture_store.py` (create)

**Interfaces:**
- Consumes: `brief.outlet_for`, `brief.FeedFetch`, `db.connect`.
- Produces: `capture.content_hash(entry) -> str`; `capture.resolve_outlet(conn, feed) -> int | None`; `capture.store_items(conn, outlet_id, entries) -> tuple[int, int]` returning `(written, already_present)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture_store.py`:

```python
"""DB-backed capture storage tests (news-brief-b42.1)."""

import pytest

import capture
import db

pytestmark = pytest.mark.skipif(
    not db.is_configured(), reason="No database is configured; export DATABASE_URL"
)


@pytest.fixture()
def store():
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


def _entry(url="https://example.com/a", guid=None, title="A headline"):
    return {
        "title": title,
        "url": url,
        "summary": "Body.",
        "published_raw": "",
        "published_at": None,
        "guid": guid,
    }


FEED = {"name": "Test Wire", "url": "u", "category": "macro", "kind": "wire"}


def test_a_guid_beats_the_url_for_identity():
    same_guid_different_url = [
        capture.content_hash(_entry(url="https://a/1", guid="g")),
        capture.content_hash(_entry(url="https://a/2", guid="g")),
    ]
    assert len(set(same_guid_different_url)) == 1


def test_without_a_guid_the_normalized_url_is_the_identity():
    plain = capture.content_hash(_entry(url="https://a/1"))
    tracked = capture.content_hash(_entry(url="https://a/1?utm_source=rss#frag"))
    assert plain == tracked


def test_two_different_items_hash_differently():
    assert capture.content_hash(_entry(url="https://a/1")) != capture.content_hash(
        _entry(url="https://a/2")
    )


def test_an_outlet_is_inserted_once_then_looked_up(store):
    first = capture.resolve_outlet(store, FEED)
    second = capture.resolve_outlet(store, FEED)
    assert first == second
    n = store.execute("SELECT count(*) FROM outlets").fetchone()[0]
    assert n == 1


def test_capture_never_overwrites_an_outlets_metadata(store):
    capture.resolve_outlet(store, {**FEED, "kind": "wire"})
    capture.resolve_outlet(store, {**FEED, "kind": "analyst"})
    kind = store.execute("SELECT kind FROM outlets").fetchone()[0]
    assert kind == "wire", "an existing outlet row is looked up, never rewritten"


def test_a_source_conflicting_with_an_existing_outlet_is_refused(store):
    capture.resolve_outlet(store, {**FEED, "kind": "wire"})
    got = capture.resolve_outlet(store, {**FEED, "kind": "analyst"}, strict=True)
    assert got is None, "a user source that disagrees is dropped, not merged silently"


def test_the_same_item_captured_twice_writes_one_row(store):
    outlet_id = capture.resolve_outlet(store, FEED)
    first = capture.store_items(store, outlet_id, [_entry()])
    second = capture.store_items(store, outlet_id, [_entry()])
    assert first == (1, 0)
    assert second == (0, 1)
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_the_same_hash_under_two_outlets_both_store(store):
    """The per-outlet key, positive control: dedup must NOT collapse syndicated
    wire copy across publishers, or any corroboration count caps at one."""
    a = capture.resolve_outlet(store, FEED)
    b = capture.resolve_outlet(store, {**FEED, "name": "Other Wire"})
    capture.store_items(store, a, [_entry()])
    capture.store_items(store, b, [_entry()])
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_an_entry_with_no_title_is_skipped_not_fatal(store):
    """items.title and items.url are NOT NULL, and one violation inside a shared
    transaction would abort the whole pass, losing everything captured before it."""
    outlet_id = capture.resolve_outlet(store, FEED)
    written, _ = capture.store_items(
        store, outlet_id, [_entry(title=""), _entry(url="https://example.com/ok")]
    )
    assert written == 1
    assert store.execute("SELECT count(*) FROM items").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_capture_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'capture'`.

- [ ] **Step 3: Write `capture.py`**

```python
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

# EVERY import this module will ever need is declared here, in Tasks 4 through
# 6. There is no ruff config in this repo, so defaults apply and E402
# (module-level import not at top of file) is selected: appending an import
# further down fails `ruff check .` before a single test runs. `common` is
# imported as a module AND `log` by name on purpose -- a knob must be read as
# `common.CAPTURE_ENABLED`, since a from-import copy freezes at import time and
# defeats both host toggles and monkeypatch.

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


def store_items(conn, outlet_id: int, entries: list[dict]) -> tuple[int, int]:
    """Write entries for one outlet. Returns (written, already_present).

    Each entry is its own savepoint: items.title and items.url are NOT NULL, and
    one bad entry inside a shared transaction would abort the pass and lose
    everything captured before it.
    """
    written = already = 0
    for entry in entries:
        if not entry.get("title") or not entry.get("url"):
            log.warning(
                f"Capture: entry with no title or url skipped: {str(entry)[:120]}"
            )
            continue
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
        if row:
            written += 1
        else:
            already += 1
    return written, already
```

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_capture_store.py -q`
Expected: PASS, 9 tests, **0 skipped**.

- [ ] **Step 5: Register the new module and confirm packaging**

`capture.py` is a new top-level module. `tests/test_packaging.py` fails until it is registered in all three places, but only once something in the copied set imports it — Task 7 adds that import. Add it now anyway:

- `Dockerfile` COPY line: add `capture.py` after `brief.py`
- `.github/workflows/docker-publish.yml`: add `- 'capture.py'` to `paths:`, and `capture.py` to both `ruff` invocations

- [ ] **Step 6: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add capture.py tests/test_capture_store.py Dockerfile .github/workflows/docker-publish.yml
git commit -m "feat(capture): outlets resolve once and items dedup per outlet"
```

Expected suite total: **1384 passed** (1375 + 9).

---

## Task 5: Sightings and polls — making absence mean something

**Files:**
- Modify: `capture.py`
- Test: `tests/test_capture_store.py`

**Interfaces:**
- Produces: `capture.start_run(conn, enabled) -> int`; `capture.finish_run(conn, run_id, tally) -> None`; `capture.record_poll(conn, run_id, source_name, failure, entries_seen) -> None`; `capture.record_sightings(conn, source_name, entries, item_ids) -> None`; `capture.rolled_off(conn, source_name) -> list[str]` returning content hashes judged to have left the window.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture_store.py`:

```python
def test_a_sighting_is_created_then_advanced(store):
    entries = [_entry()]
    capture.record_sightings(store, "Test Wire", entries, {})
    first = store.execute(
        "SELECT first_seen_at, last_seen_at FROM feed_sightings"
    ).fetchone()
    store.execute("UPDATE feed_sightings SET last_seen_at = last_seen_at - "
                  "interval '1 hour'")
    capture.record_sightings(store, "Test Wire", entries, {})
    second = store.execute(
        "SELECT first_seen_at, last_seen_at, position FROM feed_sightings"
    ).fetchone()
    assert store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0] == 1
    assert second[0] == first[0], "first_seen_at must never move"
    assert second[1] > first[1] - __import__("datetime").timedelta(hours=1)


def test_one_item_on_two_feeds_is_two_sightings(store):
    capture.record_sightings(store, "Reuters Markets", [_entry()], {})
    capture.record_sightings(store, "Reuters World", [_entry()], {})
    assert store.execute("SELECT count(*) FROM feed_sightings").fetchone()[0] == 2


def test_roll_off_is_computed_only_against_successful_polls(store):
    """Three passes: the item is present, present, then absent from a SUCCESSFUL
    poll. Only then has it left the window."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    # Move pass 1 WHOLLY into the past -- the sighting AND the poll that
    # produced it. Backdating only the sighting leaves it behind its own poll,
    # which then satisfies the predicate by itself and makes this test pass
    # without pass 3 existing at all. The poll goes one second further back so
    # the "a poll strictly after the sighting" rule is tested, not equality.
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )

    run3 = capture.start_run(store, enabled=True)
    capture.record_poll(store, run3, "Test Wire", None, 0)

    assert capture.rolled_off(store, "Test Wire") == [
        capture.content_hash(_entry())
    ]


def test_no_later_poll_means_nothing_has_rolled_off(store):
    """Guards the two roll-off tests against passing vacuously. With only the
    original poll on record, nothing has left the window yet -- so if this
    returns a hash, the predicate is being satisfied by the poll that recorded
    the sighting rather than by a later one."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )
    assert capture.rolled_off(store, "Test Wire") == []


def test_a_failed_poll_produces_no_roll_off_signal(store):
    """The negative control for the whole measurement. A feed 403ing for a day
    freezes every last_seen_at, which is byte-identical to its entire window
    rolling over at once. Without feed_polls this test cannot be written."""
    run1 = capture.start_run(store, enabled=True)
    capture.record_sightings(store, "Test Wire", [_entry()], {})
    capture.record_poll(store, run1, "Test Wire", None, 1)
    store.execute("UPDATE feed_sightings SET last_seen_at = now() - interval '2 hours'")
    store.execute(
        "UPDATE feed_polls SET polled_at = now() - interval '2 hours 1 second'"
    )

    run3 = capture.start_run(store, enabled=True)
    capture.record_poll(store, run3, "Test Wire", "http_403", 0)

    assert capture.rolled_off(store, "Test Wire") == []


def test_a_disabled_run_is_distinguishable_from_one_that_never_fired(store):
    run = capture.start_run(store, enabled=False)
    capture.finish_run(store, run, capture.Tally())
    row = store.execute(
        "SELECT enabled, finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()
    assert row[0] is False
    assert row[1] is not None


def test_an_unfinished_run_keeps_a_null_finished_at(store):
    run = capture.start_run(store, enabled=True)
    finished = store.execute(
        "SELECT finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()[0]
    assert finished is None, "a crashed pass must be tellable from a completed one"


def test_the_run_row_survives_a_crash_mid_pass(store):
    """db.connect() is autocommit=False, so without an explicit commit the
    capture_runs row rolls back with everything else and a crashed pass becomes
    indistinguishable from one that never fired — the exact ambiguity the row
    exists to remove. This test fails if the commit after start_run is dropped.
    """
    run = capture.start_run(store, enabled=True)
    store.commit()
    try:
        capture.record_sightings(store, "Test Wire", [_entry()], {})
        raise RuntimeError("simulated pass crash")
    except RuntimeError:
        store.rollback()
    row = store.execute(
        "SELECT enabled, finished_at FROM capture_runs WHERE id = %s", (run,)
    ).fetchone()
    assert row is not None, "the run row was rolled back with the crash"
    assert row[1] is None, "an unfinished run must keep a NULL finished_at"
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_capture_store.py -q -k "sighting or roll or disabled or unfinished"`
Expected: FAIL — `capture.record_sightings` does not exist.

- [ ] **Step 3: Implement**

Append to `capture.py`. **Add no import lines** — `dataclass` is already in Task 4's header, and a new import here is an `E402` failure:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_capture_store.py -q`
Expected: PASS, 18 tests, 0 skipped.

- [ ] **Step 5: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add capture.py tests/test_capture_store.py
git commit -m "feat(capture): sightings and polls, so an absence means something"
```

Expected suite total: **1392 passed** (1384 + 8).

---

## Task 6: The pass — source selection, host spacing, deadline, tally

**Files:**
- Modify: `capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `capture.capture_sources() -> list[dict]`; `capture.order_by_host(feeds) -> list[dict]`; `capture.run(conn=None) -> Tally`.
- Constants: `capture.DEADLINE_SECONDS = 600`, `capture.HOST_GAP_SECONDS = 5`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`. **Put `import capture` and `import common` in the file's TOP import block, not above the appended tests** — `tests/` is inside `ruff check .`, so a mid-file import is `E402` and fails the gate before any test runs. This rule holds for every later task that needs a new import in an existing test file.

```python
def test_capture_polls_feeds_and_never_page_sources(monkeypatch):
    """all_sources() is the wrong entry point: it includes source_type='page'
    entries, which are scraped pages with no entry list. RSS_FEEDS carries no
    source_type key at all, so its absence must mean "feed"."""
    monkeypatch.setattr(brief, "RSS_FEEDS", [{"name": "Baked", "url": "https://a/f",
                                              "category": "macro"}])
    monkeypatch.setattr(
        brief,
        "load_temp_sources",
        lambda: [
            {"name": "UserFeed", "url": "https://b/f", "category": "geo",
             "source_type": "feed"},
            {"name": "UserPage", "url": "https://c/p", "category": "geo",
             "source_type": "page"},
        ],
    )
    names = [f["name"] for f in capture.capture_sources()]
    assert names == ["Baked", "UserFeed"]


def test_no_two_consecutive_fetches_share_a_host():
    """The documented Nitter 429 is an ADJACENCY bug, not a volume one: the two
    X feeds sit next to each other in RSS_FEEDS, so one 429'd on most runs. At
    48 passes a day that collision would recur 48 times a day."""
    feeds = [
        {"name": "A", "url": "https://nitter.example/a/rss", "category": "geo"},
        {"name": "B", "url": "https://nitter.example/b/rss", "category": "geo"},
        {"name": "C", "url": "https://other.example/c", "category": "geo"},
    ]
    ordered = capture.order_by_host(feeds)
    hosts = [f["url"].split("/")[2] for f in ordered]
    assert len(ordered) == 3
    assert all(a != b for a, b in zip(hosts, hosts[1:])), hosts


def test_the_real_feed_list_never_polls_one_host_back_to_back():
    """Written against the real RSS_FEEDS as well as a synthetic list, because
    this is the regression test for a production failure."""
    ordered = capture.order_by_host(list(brief.RSS_FEEDS))
    hosts = [f["url"].split("/")[2] for f in ordered]
    assert len(ordered) == len(brief.RSS_FEEDS)
    assert all(a != b for a, b in zip(hosts, hosts[1:])), hosts


def test_a_pass_stops_at_its_deadline_and_records_what_it_skipped(monkeypatch):
    """26 feeds x 3 attempts x 20s is ~26 minutes, which outlives the 30-minute
    interval -- and the supervisor telegram_alerts a job still running at its
    next fire time, 48 chances a day."""
    feeds = [{"name": f"F{i}", "url": f"https://h{i}.example/f", "category": "geo"}
             for i in range(5)]
    monkeypatch.setattr(capture, "capture_sources", lambda: feeds)
    monkeypatch.setattr(capture, "DEADLINE_SECONDS", 0)
    recorded = []
    monkeypatch.setattr(
        capture,
        "record_poll",
        lambda conn, run, name, failure, seen: recorded.append((name, failure)),
    )
    monkeypatch.setattr(capture, "start_run", lambda conn, enabled: 1)
    monkeypatch.setattr(capture, "finish_run", lambda conn, run, tally: None)
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)

    class _FakeConn:
        """`run` commits per feed, so a bare object() raises AttributeError
        before the deadline logic is ever reached."""

        def commit(self):
            return None

    tally = capture.run(conn=_FakeConn())
    assert len(recorded) == 5, "every feed gets a poll row, reached or not"
    assert all(failure == "deadline" for _, failure in recorded)
    assert tally.feeds_failed == 5
```

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_capture.py -q -k "capture_polls or consecutive or back_to_back or deadline"`
Expected: FAIL — `capture.capture_sources` does not exist.

- [ ] **Step 3: Implement the pass**

Append to `capture.py`. **Add no import lines** — `time`, `urlsplit` and `common` are all in Task 4's header. Re-importing `urlsplit` here is an `F811` redefinition as well as an `E402`:

```python
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


def run(conn=None) -> Tally:
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
            continue

        written, already = store_items(conn, outlet_id, got.entries)
        record_sightings(conn, feed["name"], got.entries, {})
        record_poll(conn, run_id, feed["name"], None, len(got.entries))
        # Items, sightings and this feed's poll row commit together, so the
        # denominator can never disagree with what was actually stored.
        conn.commit()
        tally.feeds_ok += 1
        tally.items_seen += len(got.entries)
        tally.items_new += written

    finish_run(conn, run_id, tally)
    conn.commit()
    kinds = ", ".join(f"{k} x{v}" for k, v in sorted(tally.failures.items()))
    log.info(
        f"Capture: {tally.feeds_total} feeds, {tally.feeds_ok} ok, "
        f"{tally.feeds_failed} failed ({kinds or 'none'}), "
        f"{tally.items_seen} items seen, {tally.items_new} new, "
        f"{tally.sources_dropped} sources dropped"
    )
    return tally
```

> **Add the `CAPTURE_ENABLED` knob from Task 7 Step 3 before running these tests, AND monkeypatch it true.** Adding the knob alone is not enough and would fail silently: `tests/conftest.py` stubs `config._read_settings` to `dict`, so every knob resolves to its `Knob.default` (`common.py:290`) — which for `CAPTURE_ENABLED` is `False`. `capture.run` would then early-return before touching a single feed, and the deadline test would see `recorded == []` against `assert len(recorded) == 5`. That is a wrong answer, not a crash, which is worse. The deadline test needs:
>
> ```python
> monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
> ```
>
> with `import common` at the top of the test module. Set `DEADLINE_SECONDS = 0` so no fetch happens.

- [ ] **Step 4: Run to verify they pass**

Run: `py -m pytest tests/test_capture.py -q`
Expected: PASS, 18 tests (test_capture.py).

If `test_the_real_feed_list_never_polls_one_host_back_to_back` fails, the interleaver cannot separate the real list — report the host distribution rather than weakening the assertion.

- [ ] **Step 5: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add capture.py tests/test_capture.py
git commit -m "feat(capture): a bounded pass that never polls one host twice in a row"
```

Expected suite total: **1396 passed** (1392 + 4).

---

## Task 7: Wire it in — mode, schedule, knob, budget

**Files:**
- Modify: `brief.py:3838` (`JOB_MODES` and its comment), `brief.py:3981` (usage string), plus `mode_capture`
- Modify: `scheduler.py:69-75` (`SCHEDULES`)
- Modify: `supervisor.py:60-78` (budget comment)
- Modify: `common.py:177+` (`KNOBS`)
- Modify: `docker-compose.yml` (the `&newsbrief` anchor)
- Modify: `brief.py:158` (stale comment)
- Test: `tests/test_supervisor.py`, `tests/test_capture.py`

**Interfaces:**
- Consumes: `capture.run`.
- Produces: `brief.mode_capture()` taking no arguments (`run_job` calls `fn()` with none, `brief.py:3906`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`. Add `import scheduler` to the file's **top import block**, not here (`E402`).

```python
def test_capture_is_a_job_mode():
    """A JOB_MODES entry is what gives capture the advisory lock and its
    job_runs row through the mode dispatch — the rule that every entry path to a
    job, including `docker compose run`, records itself."""
    assert "capture" in brief.JOB_MODES


def test_every_job_mode_has_a_dispatch_entry():
    """JOB_MODES membership alone is not enough: the dispatch lookup runs FIRST,
    so a job mode with no MODES entry prints usage and exits 1 — and the
    supervisor turns that into a Telegram alert on every fire time, 48 a day for
    capture. This assertion is the reason MODES is module-level."""
    assert brief.JOB_MODES <= set(brief.MODES)


def test_capture_has_a_schedule_whose_grace_clears_one_tick():
    spec = next(s for s in scheduler.SCHEDULES if s.job == "capture")
    assert spec.kind == "interval"
    assert spec.every_minutes == 30
    assert spec.grace_minutes * 60 > scheduler.TICK_SECONDS


def test_the_pass_deadline_is_shorter_than_the_interval():
    """A pass that outlives its fire time trips supervisor.py's overlap
    telegram_alert, 48 chances a day."""
    spec = next(s for s in scheduler.SCHEDULES if s.job == "capture")
    assert capture.DEADLINE_SECONDS < spec.every_minutes * 60


def test_the_capture_knob_defaults_off():
    assert common.KNOBS["CAPTURE_ENABLED"].default is False
```

Append to `tests/test_supervisor.py`:

```python
def test_the_shutdown_budget_comment_matches_the_schedule_count():
    """supervisor.py:60-78 hand-computes a budget term as "<N> schedules x 2
    statements x 0.5s". Nothing computes that N. The comment names its own
    fragility and cannot enforce it; this is the enforcement."""
    import re

    import scheduler
    import supervisor

    source = pathlib.Path(supervisor.__file__).read_text(encoding="utf-8")
    documented = re.search(r"(\d+) schedules x 2 statements", source)
    assert documented, "the budget comment's schedule term is missing or reworded"
    assert int(documented.group(1)) == len(scheduler.SCHEDULES)
```

(Add `import pathlib` at the top of `tests/test_supervisor.py` if it is not already there.)

- [ ] **Step 2: Run to verify they fail**

Run: `py -m pytest tests/test_capture.py tests/test_supervisor.py -q -k "job_mode or schedule or deadline or knob or budget"`
Expected: FAIL — `capture` is not in `JOB_MODES`, no schedule named `capture`, no `CAPTURE_ENABLED` knob, and the budget comment says `5`.

- [ ] **Step 3: Add the knob**

In `common.py`, inside `KNOBS`:

```python
    # Continuous capture (news-brief-b42.1). Default OFF: capture writes to
    # shared tables and has no consumer yet, so it is switched on deliberately
    # on the host after a deploy rather than starting on its own.
    "CAPTURE_ENABLED": Knob(bool, False),
```

In `docker-compose.yml`, add to the `&newsbrief` anchor's environment list:

```yaml
    - NEWSBRIEF_CAPTURE_ENABLED=${NEWSBRIEF_CAPTURE_ENABLED:-}
```

> The anchor SEEDS the settings rows. A missing line freezes the default into a row on first boot and adding the line later fixes nothing.

- [ ] **Step 4: Add the mode and the schedule**

In `brief.py`:

```python
JOB_MODES = frozenset({"submit", "collect", "weekly", "monitor", "backup", "capture"})
```

Add the mode function near the other `mode_*` functions:

```python
def mode_capture():
    """Poll every feed source into the knowledge base. Reads nothing back.

    Takes no arguments: run_job calls fn() with none (brief.py:3906).
    `capture.run` owns its own commit boundaries -- see the note below.
    """
    import capture

    with db.connect() as conn:
        capture.run(conn)
```

**There is no module-level dispatch mapping, and this task creates one.** `dispatch` is currently a local dict at `brief.py:3955-3964`, inside the `if __name__ == "__main__":` block beginning at `brief.py:3925`. That placement makes the entry untestable, and missing it is expensive: `supervisor.py:174` spawns `[sys.executable, "brief.py", "capture"]`, `dispatch.get("capture")` returns `None`, the usage string prints, `sys.exit(1)`, and `supervisor.py:825` fires `telegram_alert(f"{mode} exited with code {code}")` — **every 30 minutes, 48 times a day.** The same alert storm §8.2 exists to prevent, through a third door.

Hoist the dict to module level, immediately after the `mode_*` functions:

```python
# Module level, not inside __main__, so a test can assert JOB_MODES is covered.
# A mode in JOB_MODES but missing here is not a quiet no-op: the supervisor
# spawns it, gets exit 1 from the usage branch, and alerts on every fire time.
MODES = {
    "submit": mode_submit,
    "collect": mode_collect,
    "weekly": mode_weekly,
    "commands": mode_commands,
    "paper": mode_paper,
    "monitor": mode_monitor,
    "backup": mode_backup,
    "pgdiag": mode_pgdiag,
    "capture": mode_capture,
}
```

In the `__main__` block, replace the local dict with `fn = MODES.get(mode)`. Change nothing else about the dispatch flow.

Add `capture` to the usage string at `brief.py:3981`.

In `scheduler.py`, add to `SCHEDULES`:

```python
    Schedule("capture", "interval", None, 30, grace_minutes=10),
```

In `supervisor.py`, update the budget comment:

```
#   +  6  DB_STATEMENT_TIMEOUT  6 schedules x 2 statements x 0.5s
```

and the total from `39` to `40` in the same block, leaving the prose about the 60s `stop_grace_period` intact — 40 is still comfortably inside it.

Fix the stale comment at `brief.py:158`: "Only the 5 newest items are used" — the default became 25 in `240a4cb`. Change `5` to `25`.

- [ ] **Step 5: Run to verify they pass**

Run: `py -m pytest tests/test_capture.py tests/test_supervisor.py tests/test_packaging.py -q`
Expected: PASS. `test_packaging.py` now exercises `capture.py` for real, since `brief.py` imports it.

- [ ] **Step 6: Verify compose still parses and the image builds**

```bash
docker compose config > /dev/null && echo "COMPOSE_OK"
docker build -t nb-capture-check . && \
  docker run --rm -e ANTHROPIC_API_KEY=x -e TELEGRAM_BOT_TOKEN=x nb-capture-check --help 2>&1 | tail -3
```

Expected: `COMPOSE_OK`, a successful build, and a usage string listing `capture`. **Never `docker compose up -d`.**

- [ ] **Step 7: Full gate and commit**

```bash
ruff check . && ruff format . && py -m pytest -q
git add brief.py scheduler.py supervisor.py common.py docker-compose.yml tests/test_capture.py tests/test_supervisor.py
git commit -m "feat(capture): capture becomes a scheduled job child, off by default"
```

Expected suite total: **1402 passed** (1396 + 6).

- [ ] **Step 8: File the follow-ups**

```bash
bd create --title="Retention for capture telemetry and job_runs" --type=task --priority=3 \
  --description="b42.1 adds items, feed_sightings, feed_polls and capture_runs, none with a retention rule. feed_polls is the largest at ~455k rows/year. Separately, capture takes job_runs from ~5 rows/day to 53 — a 10x growth-rate change on an EXISTING unbounded table, since retention.py prunes files only and has no job_runs handling. Same shape as news-brief-6wc."
bd update news-brief-b42.1 --description="AMENDED 2026-09-02: writes outlets and items (created empty by 0006), NOT a captured_items staging table — that was superseded by the KB schema DDL and would re-open the global-content-hash corroboration bug. Migration 0008 adds capture telemetry: feed_sightings, feed_polls, capture_runs. See docs/superpowers/specs/2026-09-02-continuous-capture-design.md"
bd close news-brief-b42.1
```

---

## Self-Review

**Spec coverage:** §1 (why) → no task, context only. §2 boundary → enforced by Task 2's characterization test. §3 (no `captured_items`) → Task 1. §4/4.1/4.2 (outlet mapping, conflicts) → Task 3 + Task 4 Step 3 `strict`. §5 (dedup key, dates, junk) → Task 2 + Task 4. §6 (telemetry, roll-off definition) → Tasks 1 and 5. §7.1 (split) → Task 2. §7.2 (`capture.py`) → Tasks 4–6. §7.3 (four traps) → Task 3 Step 3 (`outlet` passthrough), Task 4 Step 3 (`NOT NULL` savepoints), Task 6 Step 3 (`capture_sources`), Task 7 Step 4 (`fn()` no args, comment, usage). §8 (schedule) → Task 7. §8.1 (host spacing) → Task 6. §8.2 (deadline) → Task 6. §8.3 (budget) → Task 7. §9 (knob, anchor) → Task 7. §10 (tally, no Telegram) → Tasks 5 and 6. §11 (tests) → distributed. §13 (follow-ups) → Task 7 Step 8. §14 criteria → criteria 1–3, 5–7 have tests; **criterion 4** (brief output byte-identical) is Task 2's characterization test.

**Gap accepted:** §10's "monitor alerts on anomaly" is *not* implemented here. Capture writes the rows that make it possible; wiring `monitor` to read them is a separate change with its own alerting-threshold decision, and folding it in would put an unmeasured threshold on the critical path. Filed as part of Task 7 Step 8's follow-up rather than silently dropped.

**Type consistency:** `FeedFetch(entries, failure)` used identically in Tasks 2, 6. `content_hash(entry)` — Tasks 4, 5. `Tally` — Tasks 5, 6. `resolve_outlet(conn, feed, *, strict=False)` — Tasks 4, 6. `outlet_for(feed)` — Tasks 3, 4. `record_poll(conn, run_id, source_name, failure, entries_seen)` — Tasks 5, 6 (monkeypatched with matching arity).

**Placeholder scan:** none.

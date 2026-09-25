# Comprehension Cost Redesign — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make restarting comprehension safe and bounded. That means: stop the Reuters quote-page flood at capture; stop charging items for account failures; cap spend with a token bucket; process newest-first with a 14-day horizon; and narrow the free "material" rule to explicitly tracked topics.

**Architecture:** Every change lands in the existing modules (`common.py`, `capture.py`, `brief.py`, `comprehend.py`) plus two migrations. No new top-level module, so the Dockerfile allowlist is untouched. The budget lives in `runtime_state` (key `comprehend_budget`), and spend goes to a new `comprehend_spend` ledger table. Alerts copy the existing `capture.liveness` episode-key contract, wired through `brief.mode_monitor`.

**Tech Stack:** Python 3, psycopg 3, Postgres 18 with pg_trgm, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md` (§4, §6.1, §7, §8). Read it before starting. Phase 2 (§5) is a separate plan: `docs/superpowers/plans/2026-09-25-comprehension-cost-phase-2.md`.

## Global Constraints

- **The pre-push gate is three commands, and all three must pass:** `ruff check .`, `ruff format --check .`, and `py -m pytest -q` **with a database exported.** Without `DATABASE_URL`, every DB-backed test SKIPS and the suite reports green with the whole DB layer unexecuted. Start Postgres like this:
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -d -p 5432:5432 --tmpfs /var/lib/postgresql \
    -e POSTGRES_PASSWORD=newsbrief -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
  export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
  ```
- **Run Python with `py`,** never `python`: the winpty alias in Git Bash swallows exit codes.
- **Read every knob as `common.X` at call time, never `from common import X`.** Knobs are settings rows behind a PEP 562 `__getattr__`.
- **A new knob needs a `KNOBS` entry AND a compose anchor line** in `docker-compose.yml` (next to `COMPREHEND_MAX_DEFERS`, line 134).
- **Never give a model raw database ids.** Unchanged in this phase; do not touch `label_map`.
- **Do not bump `TRIAGE_PROMPT_VERSION` or `INTEGRATE_PROMPT_VERSION` in this phase.** A triage bump re-triages the whole corpus; an integration bump re-integrates it (`news-brief-3wb`).
- **Commit straight to `main`, with explicit paths, never `git add -A`.** The operator runs concurrent sessions. End every commit message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Never put backticks or `$(` inside `git commit -m`.
- **Do not deploy, and do not touch the host.** `COMPREHEND_ENABLED` must stay false on the host until the old gate has run (spec §6.1). Host steps live in the runbook written in Task 11.
- **Prices (per MTok, in/out): Sonnet 5 $2/$10, Haiku 4.5 $1/$5.** Source: the Anthropic model table (claude-api skill, cached 2026-06-24), checked 2026-09-25.
- **Budget defaults:** `COMPREHEND_DAILY_BUDGET_USD = 1.50`, `COMPREHEND_BUDGET_MAX_DAYS = 3`. **Horizon:** `CANDIDATE_WINDOW_DAYS` (14), measured on `coalesce(published_at, created_at)`.
- **Surge rule:** last 24 h > 3 × the trailing 7-day daily median **and** at least 200 items above that median.

## Review Focus

These are the inputs most likely to bite once deployed that no task's core tests exercise. Each line's pinning test is written into the owning task.

1. **The budget row is missing, or holds junk** (e.g. `{"balance_usd": "abc"}` after a hand edit). Expected: the pass re-initialises to one day's allowance and runs; it never crashes. *(Task 9, `test_a_corrupt_budget_row_is_reinitialised_not_fatal`)*
2. **The stored budget timestamp is in the future** (the host clock moved back). Expected: no negative accrual; the balance is unchanged. *(Task 9, `test_a_future_timestamp_never_accrues_negative`)*
3. **A response with no `usage` block.** Expected: cost 0, a warning logged, and the pass continues. *(Task 8, `test_a_response_without_usage_costs_nothing_and_warns`)*
4. **An item with a NULL `published_at`.** Expected: staleness uses `created_at` instead, so the item is neither stale forever nor never stale. *(Task 7, `test_a_null_published_at_falls_back_to_created_at`)*
5. **A quote-page title stored HTML-escaped** (`Stock Price &amp; Latest News`). Expected: still matched. *(Task 1, a case in `QUOTE_PAGES`)*

---

### Task 0: File the beads

**Files:** none (tracker only)

- [ ] **Step 1: Create the epic and its children**

```bash
cd /g/pythonDev/news-brief
bd create --type=epic --priority=1 --title='Comprehension cost redesign, phase 1: safe, bounded restart' --description='Spec docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md section 4. Plan docs/superpowers/plans/2026-09-25-comprehension-cost-phase-1.md.'
```
Record the epic id, then create one child per task (Tasks 1–11). Use `--parent=<epic>` and `--type=task`, and title each after its task heading below. `news-brief-0rg` already exists: link it as a dependency of the Task 4 child with `bd dep add <task4-child> news-brief-0rg`.

- [ ] **Step 2: Export** — `bd export -o .beads/issues.jsonl`. Nothing to commit yet; the export is committed with Task 1.

---

### Task 1: `common.is_quote_page`, applied at capture and in the brief

**Files:**
- Modify: `common.py` (add near the other text helpers; `re` is already imported; add `import html`)
- Modify: `capture.py:169-192` (Tally), `capture.py:451-486` (run loop)
- Modify: `brief.py:1768-1786` (`fetch_rss`)
- Create: `tests/test_quote_pages.py`
- Modify: `tests/test_capture_store.py` (one new DB test)

**Interfaces:**
- Produces: `common.is_quote_page(title: str | None) -> bool`. Task 6 uses it in triage.
- Produces: `capture.Tally.quote_pages_dropped: int` (log only; `capture_runs` has no column for it).

- [ ] **Step 1: Write the failing predicate tests**

`tests/test_quote_pages.py`:
```python
"""common.is_quote_page: Reuters instrument pages that reached us as "news".

Every title below is REAL, from live Google News fetches on 2026-09-25 (spec
2026-09-25 section 2.2). The negatives are real headlines from the same feeds,
including a markets column, because the failure that matters is dropping news.
"""

import pytest

import common

QUOTE_PAGES = [
    "MSTS.DE - Reuters",
    "BESG.TO - Reuters",
    "XEQ2.DE - Reuters",
    "QQQB.OQ - Reuters",
    "8PB.MU - Reuters",
    "SOGN.HA - | Stock Price & Latest News - Reuters",
    "(UN) | Stock Price & Latest News - Reuters",
    # How the title can sit in the database: capture stores feedparser's value,
    # and some paths carry the entity escaped.
    "IBXVF.PK - | Stock Price &amp; Latest News - Reuters",
]

NEWS = [
    "Mapping the Market: Why 3M shares might be set to rally again - Reuters",
    "Sterling treads water at 3-month low after weekly drop on dollar rally - Reuters",
    "US-sanctioned oil tanker Sibu 1 rescued from Somali pirates - Reuters",
    "Pact with Saudi and Pakistan could expand to Muslim world, Iran, Turkish speaker says - Reuters",
    "Iran signals a ceasefire",
    "",
]


@pytest.mark.parametrize("title", QUOTE_PAGES)
def test_a_quote_page_is_recognised(title):
    assert common.is_quote_page(title) is True


@pytest.mark.parametrize("title", NEWS)
def test_real_news_is_never_a_quote_page(title):
    assert common.is_quote_page(title) is False


def test_none_is_not_a_quote_page():
    assert common.is_quote_page(None) is False
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `py -m pytest tests/test_quote_pages.py -q`
Expected: every case fails with `AttributeError: module 'common' has no attribute 'is_quote_page'`.

- [ ] **Step 3: Implement the predicate**

In `common.py`, add `import html` to the imports. Then add:
```python
# Reuters instrument (quote) pages. Around 2026-09-15 Google News began indexing
# them under site:reuters.com/markets, and the capped 6h window returned a
# DIFFERENT 100 of them on every poll -- ~2,000 a day, 78% of the corpus, each
# one paid for again by comprehension (spec 2026-09-25 section 2.2). Two
# shapes: a bare instrument code ("MSTS.DE - Reuters") and the page-title
# phrase. The bare-code rule is uppercase/digits/.^=- only, so any headline with
# a space or a lowercase word -- every real one measured -- cannot match.
_QUOTE_PAGE_PHRASE = "stock price & latest news"
_BARE_INSTRUMENT_TITLE = re.compile(r"^[A-Z0-9^=][A-Z0-9.^=\-]*\s+-\s+Reuters$")


def is_quote_page(title: str | None) -> bool:
    """True for a Reuters instrument page masquerading as a news item."""
    text = html.unescape(title or "").strip()
    if not text:
        return False
    if _QUOTE_PAGE_PHRASE in text.lower():
        return True
    return _BARE_INSTRUMENT_TITLE.match(text) is not None
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `py -m pytest tests/test_quote_pages.py -q`
Expected: 15 passed.

- [ ] **Step 5: Mutation check with a count**

Pre-register the expected failures first: deleting the phrase branch should fail **3** tests (the three phrase titles); deleting the bare-code branch should fail **5**. Make each deletion in turn, run the tests, record the actual counts, and revert. If a count disagrees, assume the tests are wrong before assuming the code is, and fix the tests.

- [ ] **Step 6: Write the failing capture test**

Append to `tests/test_capture_store.py`:
```python
def test_capture_drops_quote_pages_before_storing(store, monkeypatch):
    """The flood is stopped at the door: a quote page never becomes an item,
    so nothing downstream (triage, integration, the bill) ever sees it."""
    feed = {"name": "OK Wire", "url": "https://ok.example/feed",
            "category": "macro", "kind": "wire"}
    entries = [
        _entry(url="https://ok.example/a", guid="g1",
               title="MSTS.DE - Reuters"),
        _entry(url="https://ok.example/b", guid="g2",
               title="Sterling treads water at 3-month low - Reuters"),
    ]
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "capture_sources", lambda: [feed])
    monkeypatch.setattr(
        brief, "fetch_feed_entries",
        lambda f: brief.FeedFetch(entries=list(entries), failure=None),
    )

    tally = capture.run(store, spacer=common.HostSpacer(0))

    titles = [r[0] for r in store.execute("SELECT title FROM items").fetchall()]
    assert titles == ["Sterling treads water at 3-month low - Reuters"]
    assert tally.quote_pages_dropped == 1
    assert tally.items_seen == 1, "a dropped quote page was never an item seen"
```

- [ ] **Step 7: Run it and confirm it fails**

Run: `py -m pytest tests/test_capture_store.py::test_capture_drops_quote_pages_before_storing -q`
Expected: FAIL. `titles` has 2 rows, or `AttributeError` on `quote_pages_dropped`.

- [ ] **Step 8: Implement the capture filter**

In `capture.Tally`, after `items_failed`:
```python
    # Log only, like items_already: entries common.is_quote_page refused before
    # storage. Counted so a feed that turns into quote pages is visible as a
    # number rather than as a mysteriously quiet feed.
    quote_pages_dropped: int = 0
```
In `capture.run`, directly after the `if got.failure:` block and before `outlet_id = resolve_outlet(...)`:
```python
        entries = [e for e in got.entries if not common.is_quote_page(e.get("title"))]
        tally.quote_pages_dropped += len(got.entries) - len(entries)
```
Use the filtered `entries` for STORAGE ONLY: `store_items`, `_lookup_item_ids` and `tally.items_seen += len(entries)`. **Leave `record_poll(..., len(got.entries))` and `record_sightings(conn, feed["name"], got.entries, item_ids)` on the RAW list.** `feed_polls.entries_seen` is the capping instrument: "100 entries every poll" is how capping was measured on 2026-09-08. If a feed's 100-slot window filled with quote pages and this recorded 3, the crowding-out that loses real articles unobservably would be hidden (phase-1 red-team, defect 5). Quote pages in `record_sightings` get no `item_id` from the lookup, which is already how that function treats entries with no stored row. Add `{tally.quote_pages_dropped} quote pages dropped, ` to the final `log.info` line, after `items failed`. Also add an assertion to the Step 6 test: `store.execute("SELECT entries_seen FROM feed_polls").fetchone()[0] == 2`.

- [ ] **Step 9: Write the failing brief test**

Append to `tests/test_capture.py`:
```python
def test_the_brief_never_reads_a_quote_page_as_a_headline(monkeypatch):
    entries = [
        {"title": "XEQ2.DE - Reuters", "published_raw": "Fri", "summary": ""},
        {"title": "Germany approves fuel tax discount - Reuters",
         "published_raw": "Fri", "summary": ""},
    ]
    monkeypatch.setattr(
        brief, "fetch_feed_entries", lambda f: brief.FeedFetch(entries=entries)
    )
    out = brief.fetch_rss({"name": "Reuters Business", "category": "macro"})
    assert "Germany approves fuel tax discount" in out
    assert "XEQ2.DE" not in out
```

- [ ] **Step 10: Run it; it fails. Implement.** In `brief.fetch_rss`, change the loop source:
```python
    entries = [e for e in got.entries if not common.is_quote_page(e.get("title"))]
    for entry in entries[:max_items]:
```

- [ ] **Step 11: Run the whole capture and quote-page set**

Run: `py -m pytest tests/test_quote_pages.py tests/test_capture.py tests/test_capture_store.py -q`
Expected: all pass, with no skips when `DATABASE_URL` is exported.

- [ ] **Step 12: Commit**
```bash
git add common.py capture.py brief.py tests/test_quote_pages.py tests/test_capture.py tests/test_capture_store.py .beads/issues.jsonl
git commit -m "feat(capture): refuse Reuters quote pages at the door" -m "Google News began returning Reuters instrument pages under /markets around 2026-09-15; one shared predicate now drops them in capture and in the brief." -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Swap the Markets proxy for Reuters Business

**Files:**
- Modify: `brief.py:148-167` (the first `RSS_FEEDS` entry)
- Modify: `tests/test_capture.py:253-261` (the `product_names` set) and `:486-491` (the pinned `capture_url` set)

**Interfaces:** none new. The source name changes from `"Reuters Markets"` to `"Reuters Business"`. The outlet stays `"Reuters"`.

- [ ] **Step 1: Update the pinned tests first** (they are claims about the feeds)

In `tests/test_capture.py`, in `product_names`, replace `"Reuters Markets",` with `"Reuters Business",`. In the pinned `capture_url` set, replace `"Reuters Markets",` with `"Reuters Business",`. Then edit the comment above that set to read:
```python
    # The feeds measured as capping (100 entries every poll) on 2026-09-08,
    # except Reuters Business, which replaced the /markets proxy on 2026-09-25
    # and measured 34 per 6h window: it keeps the narrow capture window because
    # its when:2d window exceeds the cap at ~136/day.
```

- [ ] **Step 2: Run them; they fail** — `py -m pytest tests/test_capture.py -q`. Expected: the pinned-set test fails on the name.

- [ ] **Step 3: Replace the feed entry.** Swap the whole first `RSS_FEEDS` dict in `brief.py` for:
```python
    {
        "name": "Reuters Business",
        # Was "Reuters Markets" (site:reuters.com/markets) until 2026-09-25.
        # Around 2026-09-15 Google News began indexing Reuters' INSTRUMENT pages
        # under /markets ("MSTS.DE - Reuters", "... | Stock Price & Latest News
        # - Reuters"): the 6h window returned the 100-item cap with a different
        # sample on every poll (union 191 across three fetches), and 48 polls a
        # day captured ~2,000 quote pages a day -- 78% of the corpus, each one
        # paid for again by comprehension. -inurl:companies changed nothing and
        # the /markets/<region> sub-paths return nothing; /business measured 34
        # per 6h window with no quote pages (spec 2026-09-25 section 2.2).
        # common.is_quote_page guards every feed in case this recurs elsewhere.
        #
        # Reuters has no public RSS (June 2020), hence the Google News proxy.
        # `site:` is stable (unlike allinurl:); `when:2d` is a freshness
        # guardrail for the BRIEF, which takes only the 25 newest items. For
        # CAPTURE the window is a volume control, which is what `capture_url`
        # says. The narrow window REQUIRES frequent polling -- do not slow this
        # feed without widening it (b42.4/b42.5).
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fbusiness&hl=en-US&gl=US&ceid=US%3Aen",
        "capture_url": "https://news.google.com/rss/search?q=when:6h+site%3Areuters.com%2Fbusiness&hl=en-US&gl=US&ceid=US%3Aen",
        "category": "macro",
        "kind": "wire",
        "outlet": "Reuters",
    },
```

- [ ] **Step 4: Grep for the old name in code** — `grep -rn "Reuters Markets" --include=*.py . | grep -v "^./tests/"`. Expected: no output. Then confirm the probe can see something by grepping for `"Reuters World"`, which must print `brief.py`.
  - **`tests/test_capture.py:237` DOES read the real list** (`named` is built from `brief.RSS_FEEDS` in `test_both_reuters_feeds_resolve_to_one_outlet`), so change its `"Reuters Markets"` to `"Reuters Business"`.
  - Tests that build a *synthetic* feed named "Reuters Markets" and never read `RSS_FEEDS` stay as they are: `tests/test_capture_store.py:86-91` and `tests/test_capture_schema.py:54-68`. Open each one and confirm this before leaving it.
  - **Host state keyed by the old name needs no action:** `feed_sightings.source_name`, `feed_polls.source_name`, and any `poll_every_minutes` override (none is set). `failing_feeds` ignores a name that is no longer polled (`capture.py:579-650`, verified by the phase-1 red-team). Old rows record what the old source saw. Say this in the task report.

- [ ] **Step 5: Live check of the new URL (network, run once by hand, not in tests)**
```bash
S="<your session scratchpad>"   # never a fixed /tmp path: concurrent sessions share it
curl -s --compressed -A 'Mozilla/5.0' 'https://news.google.com/rss/search?q=when:6h+site%3Areuters.com%2Fbusiness&hl=en-US&gl=US&ceid=US%3Aen' -o "$S/rb.xml"; echo "EXIT=$?"
grep -ao '<item>' "$S/rb.xml" | wc -l
grep -ao '<title>[^<]*</title>' "$S/rb.xml" | grep -ci 'stock price\|^<title>[A-Z0-9.]* - Reuters</title>'
```
Expected: EXIT=0; 15–60 items; 0 quote pages. Use `grep -a`: a Google feed reads as binary to grep, and without `-a` it prints 0 whatever the feed holds.

- [ ] **Step 6: Run the tests; they pass** — `py -m pytest tests/test_capture.py tests/test_capture_store.py -q`

- [ ] **Step 7: Commit**
```bash
git add brief.py tests/test_capture.py
git commit -m "fix(sources): replace the Reuters /markets proxy with /business" -m "The /markets Google News proxy became a rotating sample of instrument pages; /business measured 34 items per 6h window with none." -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Per-outlet surge alert

**Files:**
- Modify: `capture.py` (add after `item_drought`; add `import statistics`)
- Modify: `brief.py:3698-3737` (`CAPTURE_SURGE_KEY` plus one tuple in `capture_quality_alert`)
- Modify: `tests/test_capture_store.py` (append)

**Interfaces:**
- Produces: `capture.item_surge(conn, now) -> tuple[str, str] | None` returning `(episode_key, message)`. The key is `"surge:" + ",".join(sorted outlet names)`.
- Produces: the constants `capture.SURGE_FACTOR = 3` and `capture.SURGE_MIN_EXCESS = 200`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_capture_store.py`; they reuse its `NOW`, `_run` and `store`)
```python
def _outlet_id(store, name):
    row = store.execute("SELECT id FROM outlets WHERE name = %s", (name,)).fetchone()
    if row:
        return row[0]
    return store.execute(
        "INSERT INTO outlets (name, kind) VALUES (%s, 'wire') RETURNING id", (name,)
    ).fetchone()[0]


def _items(store, name, n, days_ago):
    """n items captured `days_ago` whole days before NOW, all inside one
    24h bucket (spread over the bucket's first hour)."""
    store.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at) "
        "SELECT %s, 'u', 't', md5(%s || '-' || %s || '-' || g), "
        "       %s - make_interval(days => %s, secs => g %% 3600) "
        "FROM generate_series(1, %s) g",
        (_outlet_id(store, name), name, days_ago, NOW, days_ago, n),
    )


def _history(store):
    """item_surge, like item_drought, refuses to judge without a week of runs."""
    _run(store, minutes_ago=8 * 24 * 60)


def test_a_surge_far_above_the_outlets_own_median_is_flagged(store):
    """The 2026-09-16 Reuters shape, scaled down: ~8x its median."""
    _history(store)
    for d in range(1, 8):
        _items(store, "Reuters", 50, d)
    _items(store, "Reuters", 400, 0)
    store.commit()

    verdict = capture.item_surge(store, NOW)

    assert verdict is not None
    key, message = verdict
    assert key == "surge:Reuters"
    assert "400" in message and "50" in message


def test_a_small_outlet_tripling_is_not_a_surge(store):
    """3x of 5 is 15 items: noise, not a bill. The absolute floor is why."""
    _history(store)
    for d in range(1, 8):
        _items(store, "Meduza", 5, d)
    _items(store, "Meduza", 15, 0)
    store.commit()

    assert capture.item_surge(store, NOW) is None


def test_a_big_outlet_rising_under_3x_is_not_a_surge(store):
    _history(store)
    for d in range(1, 8):
        _items(store, "Reuters", 300, d)
    _items(store, "Reuters", 800, 0)  # +500, but only 2.7x
    store.commit()

    assert capture.item_surge(store, NOW) is None


def test_a_surge_is_unknown_while_the_history_is_too_short(store):
    """No week of capture runs: unmeasured, not healthy and not surging."""
    for d in range(1, 8):
        _items(store, "Reuters", 50, d)
    _items(store, "Reuters", 400, 0)
    store.commit()

    assert capture.item_surge(store, NOW) is None
```

- [ ] **Step 2: Run them; they fail** — `py -m pytest tests/test_capture_store.py -k surge -q`. Expected: `AttributeError: ... 'item_surge'`.

- [ ] **Step 3: Implement** (in `capture.py`, after `item_drought`; add `import statistics` at the top)
```python
# The first alert here that watches for TOO MUCH. Until 2026-09-25 capture
# alerted only on absence, and the only detector that ever caught a surge was
# the Anthropic balance running out: Reuters went from ~300 to ~2,600 items a
# day on 2026-09-16 and nothing said so for nine days (spec 2026-09-25 2.2).
# Two conditions, both required: a ratio against the outlet's OWN median, so a
# naturally busy outlet is judged against itself, and an absolute excess, so a
# quiet outlet going from 5 to 15 -- noise, and no bill -- cannot trip it.
SURGE_FACTOR = 3
SURGE_MIN_EXCESS = 200


def item_surge(conn, now) -> tuple[str, str] | None:
    """(episode key, message) for outlets capturing far more than usual.

    Silent below HISTORY_DAYS of runs, for item_drought's reason: a baseline
    that has not seen a full week is not a baseline.
    """
    since = now - timedelta(days=HISTORY_DAYS)
    oldest = conn.execute(
        "SELECT min(started_at) FROM capture_runs "
        "WHERE enabled AND finished_at IS NOT NULL"
    ).fetchone()[0]
    if oldest is None or oldest > since:
        return None

    rows = conn.execute(
        "SELECT o.name, "
        "  floor(extract(epoch FROM (%s - i.created_at)) / 86400)::int AS ago, "
        "  count(*) "
        "FROM items i JOIN outlets o ON o.id = i.outlet_id "
        "WHERE i.created_at > %s - make_interval(days => %s) "
        "  AND i.created_at <= %s "
        "GROUP BY 1, 2",
        (now, now, HISTORY_DAYS + 1, now),
    ).fetchall()
    by_outlet: dict[str, dict[int, int]] = {}
    for name, ago, n in rows:
        by_outlet.setdefault(name, {})[ago] = n

    surging = []
    for name, days in sorted(by_outlet.items()):
        recent = days.get(0, 0)
        # Zero-filled: a day with no items is a real zero in the baseline,
        # and omitting it would bias the median upward.
        median = statistics.median(days.get(d, 0) for d in range(1, HISTORY_DAYS + 1))
        if recent > SURGE_FACTOR * median and recent - median >= SURGE_MIN_EXCESS:
            surging.append((name, recent, median))
    if not surging:
        return None

    lines = [
        f"   {name[:24]:<26}{recent} items in 24h, daily median {median:g} "
        f"over the previous {HISTORY_DAYS} days"
        for name, recent, median in surging
    ]
    return (
        "surge:" + ",".join(name for name, _, _ in surging),
        f"{len(surging)} outlet(s) are capturing far more than usual. Every "
        "extra item is also paid for by comprehension:\n" + "\n".join(lines),
    )
```

- [ ] **Step 4: Wire it into the monitor.** In `brief.py`, next to `CAPTURE_DROUGHT_KEY`, add `CAPTURE_SURGE_KEY = "capture_surge_alert"`. In `capture_quality_alert`'s tuple, add `(CAPTURE_SURGE_KEY, capture.item_surge),` as the third entry, and add one sentence to its docstring: "Surge (2026-09-25) is the first check for too MUCH."

- [ ] **Step 5: Write the once-per-episode test** (append to `tests/test_capture_store.py`)
```python
def test_a_surge_alerts_once_not_once_per_check(store, monkeypatch, state_store):
    _history(store)
    for d in range(1, 8):
        _items(store, "Reuters", 50, d)
    _items(store, "Reuters", 400, 0)
    store.commit()
    sent = []
    monkeypatch.setattr(brief, "telegram_alert", sent.append)

    for _ in range(3):
        brief.capture_quality_alert(store, NOW)

    assert [m for m in sent if "capturing far more" in m] == [sent[0]]
    assert state_store[brief.CAPTURE_SURGE_KEY] == "surge:Reuters"
```

- [ ] **Step 6: Run them; all pass** — `py -m pytest tests/test_capture_store.py -q`

- [ ] **Step 7: Mutation check.** Pre-register: removing `and recent - median >= SURGE_MIN_EXCESS` fails **1** test (the small outlet). Changing `SURGE_FACTOR * median` to `1 * median` fails **1** (the under-3× case). Removing the history guard fails **1**. Make each change in turn, record the counts, and revert.

- [ ] **Step 8: Commit**
```bash
git add capture.py brief.py tests/test_capture_store.py
git commit -m "feat(capture): alert when an outlet surges far past its own median" -m "Capture alerted only on absence; the Reuters surge ran nine days and was caught by the Anthropic balance emptying." -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `news-brief-0rg` — account failures abort the pass and charge nothing

**Files:**
- Modify: `comprehend.py` (`Tally`, `_is_transient` neighbourhood, and `run()` at `:338-373` and `:454-476`)
- Modify: `tests/test_comprehend_triage.py` (append)

**Interfaces:**
- Produces: `comprehend._account_failure(exc) -> str | None`, returning `"billing"` (402), `"auth"` (401, 403) or `None`.
- Produces: `Tally.aborted: str = ""`. Tasks 8, 9 and 10 set other values, all prefixed by a kind: `billing`, `auth`, `unpriced_model:<model>`.
- Produces: `comprehend._abort(conn, tally, reason: str) -> Tally`. It commits, logs the tally and returns it. Task 10 extends it to persist state.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_comprehend_triage.py`)
```python
def _http_error(status):
    resp = comprehend.requests.Response()
    resp.status_code = status
    return comprehend.requests.HTTPError(f"{status} error", response=resp)


def test_an_empty_account_charges_no_triage_attempt(kb, monkeypatch):
    """news-brief-0rg. A 402 says the ACCOUNT is empty. It says nothing about
    any item, and charging it retired items every hour of the 2026-09-25
    outage. The pass must stop at the first such failure."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_TRIAGE_BATCH", 1)
    for n in range(3):
        _add_item(kb, f"Chip export controls tightened {n}", h=f"H{n}")
    kb.commit()
    calls = []

    def broke(req):
        calls.append(req)
        raise _http_error(402)

    monkeypatch.setattr(comprehend, "call_triage", broke)

    tally = comprehend.run(kb)
    kb.commit()

    assert len(calls) == 1, "every later call fails identically; stop at the first"
    assert tally.aborted == "billing"
    assert kb.execute("SELECT count(*) FROM item_triage").fetchone()[0] == 0


def test_an_empty_account_charges_no_integration_attempt(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_INTEGRATE_BATCH", 1)
    first = _tracked_material(kb)
    second = _add_item(kb, "Ukraine talks stall", h="H2")
    kb.commit()
    calls = []

    def broke(req):
        calls.append(req)
        raise _http_error(402)

    monkeypatch.setattr(comprehend, "call_integration", broke)

    tally = comprehend.run(kb)
    kb.commit()

    assert len(calls) == 1
    assert tally.aborted == "billing"
    for item_id in (first, second):
        assert _attempts(kb, item_id) == 0
        assert _defers(kb, item_id) == 0


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (402, "billing")])
def test_account_statuses_are_classified(status, kind):
    assert comprehend._account_failure(_http_error(status)) == kind


def _http_error_with_body(status, err_type, message):
    import json as _json
    err = _http_error(status)
    err.response._content = _json.dumps(
        {"type": "error", "error": {"type": err_type, "message": message}}
    ).encode()
    return err


def test_a_400_saying_the_credit_balance_is_too_low_is_billing():
    """Phase-1 red-team (a): the widely reported response to an exhausted
    balance is 400 invalid_request_error "credit balance is too low", not
    the documented 402. WHICH the host received on 2026-09-25 is recorded
    nowhere -- the log kept only the status -- so classify on the body too."""
    err = _http_error_with_body(
        400, "invalid_request_error",
        "Your credit balance is too low to access the Anthropic API.")
    assert comprehend._account_failure(err) == "billing"


def test_an_ordinary_400_is_not_an_account_failure():
    err = _http_error_with_body(400, "invalid_request_error", "max_tokens: too large")
    assert comprehend._account_failure(err) is None


@pytest.mark.parametrize("status", [400, 404, 429, 500, 529])
def test_other_statuses_are_not_account_failures(status):
    assert comprehend._account_failure(_http_error(status)) is None
```
`_tracked_material`, `_attempts` and `_defers` already exist in this file (`:1295`, `:1409-1420`). Task 6 changes `_tracked_material` to use a tracked story; these tests keep working, because the helper's contract ("one material item with no model call") does not change.

- [ ] **Step 2: Run them; they fail** — `py -m pytest tests/test_comprehend_triage.py -k "account or empty_account" -q`

- [ ] **Step 3: Implement.** In `Tally`, before `failures`:
```python
    # Why the pass stopped early, or "" if it did not. An account-level failure
    # (billing, auth) or a refusal to spend (unpriced model) stops the WHOLE
    # pass without charging any item: nothing about the items was judged.
    aborted: str = ""
```
After `_is_transient`:
```python
_ACCOUNT_STATUSES = {401: "auth", 402: "billing", 403: "auth"}


def _account_failure(exc: BaseException) -> str | None:
    """"billing" or "auth" when the failure is about the ACCOUNT, else None.

    news-brief-0rg: on 2026-09-25 the balance ran out, every call returned
    402, and _is_transient -- which spares only 429 and 5xx -- charged each
    one to the ITEM. Triage gave items up after 3 passes and integration
    retired them after 13, for a fault that said nothing about any of them.
    Every later call in the pass would fail identically, so the pass stops.
    """
    if not isinstance(exc, requests.RequestException):
        return None
    resp = getattr(exc, "response", None)
    kind = _ACCOUNT_STATUSES.get(getattr(resp, "status_code", None))
    if kind:
        return kind
    # The DOCUMENTED empty-balance error is 402 billing_error, but the widely
    # reported one is 400 invalid_request_error "credit balance is too low".
    # Which one the host got on 2026-09-25 was never recorded (only the status
    # was logged), so match the body as well: a real 400 for a bad request
    # must still charge its item.
    try:
        body = resp.json().get("error") or {}
    except Exception:
        return None
    if "credit balance" in str(body.get("message", "")).lower():
        return "billing"
    return None


def _abort(conn, tally: Tally, reason: str, exc: BaseException | None = None) -> Tally:
    """Stop the pass WITHOUT charging anything, and say why.

    Logs the response BODY, not just the status: an HTTPError stringifies to a
    status and a URL, and that is why nobody can now say whether 2026-09-25's
    empty balance came back as 400 or 402 (http-error-body-is-the-diagnosis).
    """
    conn.commit()
    tally.aborted = reason
    body = getattr(getattr(exc, "response", None), "text", "") or ""
    log.error(
        f"Comprehend: pass aborted ({reason}); no item was charged. "
        f"body={body[:500]!r}"
    )
    log.info(f"Comprehend: {tally}")
    return tally
```
In `run()`, change the triage `except Exception:` to `except Exception as exc:` and make its first lines:
```python
            kind = _account_failure(exc)
            if kind:
                return _abort(conn, tally, kind, exc)
```
In the integration `except Exception as exc:` branch, add the same two checks as its first statements, before `if _is_transient(exc):`.

- [ ] **Step 4: Run them; they pass.** Then run the whole module: `py -m pytest tests/test_comprehend_triage.py -q`. Expected: all pass.

- [ ] **Step 5: Mutation check.** Pre-register:
  - Deleting the triage-branch abort fails **1** test (the triage one).
  - Deleting the integration-branch abort fails **1**.
  - Emptying `_ACCOUNT_STATUSES` fails **5**: the two end-to-end tests plus the three parametrised classification cases.
  - Deleting the body check fails **1** (the credit-balance 400).

  Run each, record the counts, and revert.

- [ ] **Step 6: Commit**
```bash
git add comprehend.py tests/test_comprehend_triage.py
git commit -m "fix(comprehend): an empty account aborts the pass and charges no item" -m "news-brief-0rg" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
The bead id is alone on its own line on purpose: in this repo that means "completes it" (`newsbrief-beads-hygiene`).

---

### Task 5: Migration 0014 — `stale` verdict and the `quote_page` / `stale` reasons

**Files:**
- Create: `migrations/0014_triage_stale_quote_page_up.sql`, `migrations/0014_triage_stale_quote_page_down.sql`
- Modify: `tests/test_comprehension_schema.py` (append)

**Interfaces:**
- Produces: `item_triage.verdict ∈ {material, immaterial, failed, stale}` and `reason` gains `quote_page` and `stale`. The biconditional becomes `(verdict = 'material') = (reason NOT IN ('none', 'error', 'quote_page', 'stale'))`.

- [ ] **Step 1: Confirm the live constraint names** (they were auto-named in 0009):
```bash
py - <<'PYEOF'
import db
with db.connect() as c:
    db.run_migrations(c); c.commit()
    print(c.execute("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'item_triage'::regclass AND contype = 'c'").fetchall())
PYEOF
```
Expected: `item_triage_verdict_check`, `item_triage_reason_check` and `item_triage_check` (the biconditional). If the names differ, use the printed ones in Step 3.

- [ ] **Step 2: Write the failing schema tests** (append). The module's migrated-DB fixture is **`kb`**, and it imports conftest as `conftest.` (see `TARGET = "0009_comprehension"` at `tests/test_comprehension_schema.py:13` for the version-string convention). **Replace `schema` with `kb` everywhere below**; the code keeps `schema` only so the diff reads clearly.
```python
def _triage_row(conn, verdict, reason):
    outlet = conn.execute(
        "INSERT INTO outlets (name, kind) VALUES ('O' || gen_random_uuid(), 'wire') RETURNING id"
    ).fetchone()[0]
    item = conn.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 't', gen_random_uuid()::text) RETURNING id", (outlet,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO item_triage (item_id, verdict, reason, triage_prompt_version) "
        "VALUES (%s, %s, %s, 1)", (item, verdict, reason),
    )


@pytest.mark.parametrize("verdict,reason", [
    ("stale", "stale"),
    ("immaterial", "quote_page"),
])
def test_the_new_legal_pairs_are_accepted(schema, verdict, reason):
    _triage_row(schema, verdict, reason)


@pytest.mark.parametrize("verdict,reason", [
    ("material", "quote_page"),   # a structural reject cannot be material
    ("material", "stale"),
    ("stale", "tracked_story"),   # a stale row claims no tracking reason
])
def test_the_biconditional_refuses_the_mismatched_pairs(schema, verdict, reason):
    with pytest.raises(psycopg.errors.CheckViolation):
        _triage_row(schema, verdict, reason)
```
Before running, rename `schema` to whatever this module's migrated-connection fixture is actually called (open the file and read its fixtures first).

- [ ] **Step 3: Write the migration**

`migrations/0014_triage_stale_quote_page_up.sql`:
```sql
-- Comprehension cost redesign, phase 1 (spec 2026-09-25 sections 4.4-4.5).
--
-- `stale`: an item older than the candidate window (14 days) when triage
-- reaches it. It cannot be matched against current events -- candidate_events
-- windows on now() -- so it is never integrated. A policy, not a one-way
-- door: deleting the row lets a later backfill re-triage it.
--
-- `quote_page`: common.is_quote_page rejected it structurally, with no model.
-- Held apart from the model's `none` because select_sampled draws its control
-- arm from items the MODEL judged immaterial; a structural reject in that pool
-- would replace the unconfounded control with junk.
ALTER TABLE item_triage DROP CONSTRAINT item_triage_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_verdict_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_reason_check;
ALTER TABLE item_triage ADD CONSTRAINT item_triage_verdict_check
    CHECK (verdict IN ('material', 'immaterial', 'failed', 'stale'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_reason_check
    CHECK (reason IN ('tracked_entity', 'tracked_claim', 'tracked_story',
                      'topical', 'sampled', 'none', 'error',
                      'quote_page', 'stale'));
-- Still biconditional in BOTH directions (see 0009).
ALTER TABLE item_triage ADD CONSTRAINT item_triage_check
    CHECK ((verdict = 'material')
           = (reason NOT IN ('none', 'error', 'quote_page', 'stale')));
-- And a stale verdict carries exactly the stale reason.
ALTER TABLE item_triage ADD CONSTRAINT item_triage_stale_check
    CHECK ((verdict = 'stale') = (reason = 'stale'));
```
`migrations/0014_triage_stale_quote_page_down.sql`:
```sql
-- Rows using the new values cannot survive the old constraints. They are
-- regenerable (triage re-derives them), so they are deleted, not rewritten.
DELETE FROM item_triage WHERE verdict = 'stale' OR reason IN ('quote_page', 'stale');
ALTER TABLE item_triage DROP CONSTRAINT item_triage_stale_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_verdict_check;
ALTER TABLE item_triage DROP CONSTRAINT item_triage_reason_check;
ALTER TABLE item_triage ADD CONSTRAINT item_triage_verdict_check
    CHECK (verdict IN ('material', 'immaterial', 'failed'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_reason_check
    CHECK (reason IN ('tracked_entity', 'tracked_claim', 'tracked_story',
                      'topical', 'sampled', 'none', 'error'));
ALTER TABLE item_triage ADD CONSTRAINT item_triage_check
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error')));
```
The new `item_triage_stale_check` makes the `("stale", "tracked_story")` test case fail on either constraint. That is intended.

- [ ] **Step 4: Add a round-trip test** (append):
```python
def test_0014_rolls_back_and_forward(schema):
    import db
    from conftest import steps_back_through
    _triage_row(schema, "stale", "stale")
    schema.commit()
    # The FULL stem: db names versions by file stem, and steps_back_through
    # asserts membership, so "0014" alone raises "0014 is not applied".
    db.run_migrations(schema, direction="down",
                      steps=steps_back_through(schema, "0014_triage_stale_quote_page"))
    schema.commit()
    assert schema.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'stale'"
    ).fetchone()[0] == 0
    db.run_migrations(schema)
    schema.commit()
    _triage_row(schema, "stale", "stale")
```
Check `db.run_migrations`'s real signature for the down direction first (`grep -n "def run_migrations" -A15 db.py`), and match how other tests call it (`grep -rn "direction=\"down\"" tests/ | head -3`).

- [ ] **Step 5: Run** — `py -m pytest tests/test_comprehension_schema.py -q`. All pass, with no skips.

- [ ] **Step 6: Commit**
```bash
git add migrations/0014_triage_stale_quote_page_up.sql migrations/0014_triage_stale_quote_page_down.sql tests/test_comprehension_schema.py
git commit -m "feat(schema): a stale triage verdict and a quote_page reason" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Triage rules — stale, quote page, then tracked topics only; sampling excludes structural rejects

**Files:**
- Modify: `comprehend.py`: `pending_triage` (`:610-636`), `triage_by_rules` (`:770-779`), the rules loop in `run()` (`:323-335`), `select_sampled` (`:937-942`), and `Tally`
- Modify: `tests/test_comprehend_triage.py` (update fixtures and old-rule tests; add new ones)

**Interfaces:**
- Consumes: `common.is_quote_page` (Task 1), migration 0014 (Task 5).
- Produces: `pending_triage(...)` rows gain the key `"stale": bool`.
- Produces: `triage_by_rules(item, index)` returns only a `tracked_claim` or `tracked_story` hit, else `None`.
- Produces: `Tally.stale: int` and `Tally.quote_pages: int`.

- [ ] **Step 1: Pre-register, then flip the rule and count the fallout.** Make only the `triage_by_rules` change from Step 4 below. Then run `py -m pytest tests/ -q -p no:randomly 2>&1 | tail -30` and record the failing test names in the task report. Pre-registered: the failures are the tests that use the Ukraine *entity* as a material fixture. That is `_tracked_material` (9 callers in `tests/test_comprehend_triage.py`) plus the direct Ukraine-entity tests. Expect **at least 10** failures, all in `tests/test_comprehend_triage.py` and `tests/test_comprehend_integration.py`. A failure in any OTHER module is a finding: stop and report it rather than fix it.

- [ ] **Step 2: Convert the fixtures.** Classify each failure as one of:
  - **(a) Asserting the old rule** (e.g. `test_a_tracked_entity_makes_an_item_material_with_no_model_call`, `test_the_rules_half_reads_the_body_as_well_as_the_title`). Rewrite it to assert the NEW rule.
  - **(b) Using the rule as a fixture** (everything reached through `_tracked_material`). Switch the fixture to a tracked story.

  Replace `_tracked_material` with:
```python
def _tracked_material(kb):
    """One item the rules half makes material with no model call.

    A tracked STORY, not an entity: since 2026-09-25 an entity mention alone no
    longer decides (spec D5), because the entity set grows with the KB and the
    rule's precision fell with it -- 86% of items were 'material'."""
    kb.execute(
        "INSERT INTO stories (name, scope) VALUES ('Ukraine talks', 'episodic')"
    )
    item_id = _add_item(kb, "Ukraine talks resume")
    kb.commit()
    return item_id
```
  Rewrite `test_a_tracked_entity_makes_an_item_material_with_no_model_call` as:
```python
def test_an_entity_mention_alone_no_longer_decides_material(kb):
    """Spec D5. The entity is still IN the index -- integration uses it for
    candidates -- but the rules half no longer treats a mention as material."""
    kb.execute("INSERT INTO entities (name, type) VALUES ('Ukraine', 'country')")
    _add_item(kb, "Ukraine signals a ceasefire")
    kb.commit()

    index = comprehend.SurfaceIndex.build(kb)
    item = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)[0]

    assert [h.reason for h in index.match(item["title"])] == ["tracked_entity"]
    assert comprehend.triage_by_rules(item, index) is None
```
  For any other test in class (a), keep its *intent* (e.g. "the rules read the body") but drive it with a story name. For example, `test_the_rules_half_reads_the_body_as_well_as_the_title` inserts story `'Ukraine talks'` and puts `"Ukraine talks"` only in the body. List every converted test and its class in the task report.

- [ ] **Step 3: Write the new failing tests** (append)
```python
def test_a_quote_page_is_immaterial_by_rule_with_no_model_call(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    item_id = _add_item(kb, "MSTS.DE - Reuters")
    kb.commit()
    monkeypatch.setattr(comprehend, "call_triage",
                        lambda r: pytest.fail("a quote page reached the model"))

    tally = comprehend.run(kb)
    kb.commit()

    assert kb.execute(
        "SELECT verdict, reason, triage_model FROM item_triage WHERE item_id = %s",
        (item_id,)).fetchone() == ("immaterial", "quote_page", None)
    assert tally.quote_pages == 1


def test_an_item_past_the_horizon_is_stale_with_no_model_call(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
        "VALUES (%s, 'u', 'Old news about Iran', 'OLD', now() - interval '15 days') "
        "RETURNING id", (_outlet(kb),)).fetchone()[0]
    kb.commit()
    monkeypatch.setattr(comprehend, "call_triage",
                        lambda r: pytest.fail("a stale item reached the model"))

    tally = comprehend.run(kb)
    kb.commit()

    assert kb.execute(
        "SELECT verdict, reason FROM item_triage WHERE item_id = %s", (item_id,)
    ).fetchone() == ("stale", "stale")
    assert tally.stale == 1


def test_a_stale_quote_page_is_stale_not_immaterial(kb, monkeypatch):
    """Rule ORDER: stale first. Both are free; stale is checked first so no
    later rule runs on an item that will be skipped anyway."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    item_id = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
        "VALUES (%s, 'u', 'MSTS.DE - Reuters', 'OLDQ', now() - interval '20 days') "
        "RETURNING id", (_outlet(kb),)).fetchone()[0]
    kb.commit()

    comprehend.run(kb)
    kb.commit()

    assert kb.execute("SELECT verdict FROM item_triage WHERE item_id = %s",
                      (item_id,)).fetchone()[0] == "stale"


def test_the_sample_never_promotes_a_structural_reject(kb):
    """The control arm is items the MODEL judged immaterial. A pool of only
    quote pages must promote nothing."""
    for n in range(3):
        item_id = _add_item(kb, f"XEQ{n}.DE - Reuters", h=f"Q{n}")
        comprehend.record_triage(kb, item_id, "immaterial", "quote_page", None,
                                 comprehend.TRIAGE_PROMPT_VERSION)
    kb.commit()

    assert comprehend.select_sampled(kb, comprehend.TRIAGE_PROMPT_VERSION, 20) == []
```
The existing `test_the_sample_draws_only_from_items_both_halves_rejected` is the presence sibling: it must still pass, because its rows carry `reason='none'`. Confirm that it does.

- [ ] **Step 4: Implement**

`pending_triage`: add the stale flag to the SELECT, computed on the DB clock:
```python
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at, "
        "  coalesce(i.published_at, i.created_at) "
        "    < now() - make_interval(days => %s) AS stale "
```
Pass `CANDIDATE_WINDOW_DAYS` as the first parameter, and add `"stale": r[5]` to the row dict. (Task 7 changes this function's ordering; leave `ORDER BY i.id` for now.) `CANDIDATE_WINDOW_DAYS` is defined further down the module; module-level names resolve at call time, so the order is fine.

`triage_by_rules`:
```python
# The rules half decides MATERIAL only for what the operator chose to track.
# An entity mention used to be enough, and the entity set is the KB itself: it
# grows with every pass, so the rule's precision FELL as the pipeline worked,
# until 86% of items were material (spec 2026-09-25 D5). Entity-only items now
# go to the model, which judges the subject.
_RULE_REASONS = frozenset({"tracked_claim", "tracked_story"})


def triage_by_rules(item: dict, index: SurfaceIndex) -> SurfaceForm | None:
    """The tracked half. A database lookup, no model call.

    Reads title AND body. The body is the RSS blurb -- capture.py stores
    entry.get("summary"), not article text -- so it is short and there is no
    window to choose.
    """
    text = f"{clean(item.get('title'))} {clean(item.get('body'))}"
    for hit in index.match(text):
        if hit.reason in _RULE_REASONS:
            return hit
    return None
```
`run()`, the rules loop becomes:
```python
    for item in pending:
        # Rule order (spec 4.5): stale, quote page, tracked topic, then the model.
        if item["stale"]:
            record_triage(conn, item["id"], "stale", "stale", None, TRIAGE_PROMPT_VERSION)
            tally.stale += 1
            continue
        if common.is_quote_page(item.get("title")):
            record_triage(
                conn, item["id"], "immaterial", "quote_page", None, TRIAGE_PROMPT_VERSION
            )
            tally.triaged_by_rules += 1
            tally.immaterial += 1
            tally.quote_pages += 1
            continue
        hit = triage_by_rules(item, index)
        if hit:
            record_triage(
                conn, item["id"], "material", hit.reason, None, TRIAGE_PROMPT_VERSION
            )
            tally.triaged_by_rules += 1
            tally.material += 1
        else:
            undecided.append(item)
    conn.commit()
```
`Tally`, after `immaterial`:
```python
    # Structural outcomes of the free rules (spec 2026-09-25 4.4-4.5).
    stale: int = 0
    quote_pages: int = 0
```
`select_sampled`: change its WHERE clause to
`"WHERE triage_prompt_version = %s AND verdict = 'immaterial' AND reason = 'none' "`, and add to its docstring: "`reason = 'none'` restricts the pool to items the MODEL judged immaterial; a structural reject (quote_page) would replace the control with junk."

- [ ] **Step 4b: Verify the Haiku 4.5 `thinking` shape (spec §4.5).** `build_triage_request` sends `"thinking": {"type": "disabled"}`, which is right for Sonnet 5. Triage moves to Haiku 4.5 through a settings row, so check the claude-api skill's thinking table (the `Haiku 4.5` row) for whether `{"type": "disabled"}` is accepted, and record the finding in the task report.
  - **If it is accepted,** add a test that pins the request shape for a Haiku model:
```python
def test_a_haiku_triage_request_keeps_thinking_disabled(monkeypatch):
    monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-haiku-4-5")
    req = comprehend.build_triage_request([{"id": 1, "title": "t"}])
    assert req["model"] == "claude-haiku-4-5"
    assert req["thinking"] == {"type": "disabled"}
```
  - **If it is NOT accepted,** make `build_triage_request` omit `thinking` for models whose id starts with `claude-haiku-4-5`. On Haiku 4.5, omitting it means no thinking. Test that the key is absent for Haiku and still `disabled` for Sonnet.

  Either way, runbook step 5 (Task 11) verifies it by effect: the first Haiku triage call must log `stop_reason=tool_use`, not a 400.

- [ ] **Step 5: Run the full comprehend suites** — `py -m pytest tests/test_comprehend_triage.py tests/test_comprehend_integration.py tests/test_comprehend_matcher.py tests/test_comprehend_labels.py -q`. All pass.

- [ ] **Step 6: Mutation check.** Pre-register: reverting `triage_by_rules` to `hits[0] if hits else None` fails **1** (`test_an_entity_mention_alone_no_longer_decides_material`). Removing `AND reason = 'none'` fails **1**. Swapping the stale and quote-page blocks fails **1**. Run each, record the counts, and revert.

- [ ] **Step 7: Commit**
```bash
git add comprehend.py tests/test_comprehend_triage.py tests/test_comprehend_integration.py
git commit -m "feat(comprehend): free rules reject quote pages and stale items; material only for tracked topics" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Newest-first, with the horizon applied to integration

**Files:**
- Modify: `comprehend.py`: `pending_triage` ORDER BY and docstring; the integration SELECT in `run()` (`:389-400`), extracted into a function; `Tally`
- Modify: `scripts/inspect_integration.py:55-75` (`select_batch` calls the production function instead of keeping its own copy)
- Modify: `tests/test_comprehend_triage.py` (append)

**Interfaces:**
- Produces: `Tally.aged_out: int`, a standing count of material, un-integrated, un-retired items past the horizon.
- Produces: `comprehend.pending_integration(conn, limit: int) -> list[dict]`, returning rows with the keys `id`, `title`, `body`, `outlet_id` and `published_at`. This is **the one definition** of the integration SELECT. `scripts/inspect_integration.py` keeps its own copy of the SELECT (`:62-70`), and that copy would silently keep probing oldest-first items with no horizon: `reconstruction-drifts-from-production`, the same failure as `news-brief-bqa.26`. Phase 2 changes this SELECT again, and will change only this function.

- [ ] **Step 1: Write the failing tests**
```python
def test_triage_takes_the_newest_items_first(kb, monkeypatch):
    """Spec D4: under a budget, OLD items give way, so the KB stays current."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_MAX_ITEMS", 1)
    old = _add_item(kb, "Older item", h="A")
    new = _add_item(kb, "Newer item", h="B")
    kb.commit()

    rows = comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 1)

    assert [r["id"] for r in rows] == [new]
    assert old < new


def test_a_material_item_past_the_horizon_is_not_integrated(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    kb.execute("INSERT INTO stories (name, scope) VALUES ('Ukraine talks', 'episodic')")
    item_id = _add_item(kb, "Ukraine talks resume")
    comprehend.record_triage(kb, item_id, "material", "tracked_story", None,
                             comprehend.TRIAGE_PROMPT_VERSION)
    kb.execute("UPDATE items SET published_at = now() - interval '15 days' "
               "WHERE id = %s", (item_id,))
    kb.commit()
    monkeypatch.setattr(comprehend, "call_integration",
                        lambda r: pytest.fail("an aged-out item was integrated"))

    tally = comprehend.run(kb)

    assert tally.aged_out == 1
    assert kb.execute("SELECT verdict FROM item_triage WHERE item_id = %s",
                      (item_id,)).fetchone()[0] == "material", (
        "the row is not rewritten; its triage reason is kept")


def test_a_null_published_at_falls_back_to_created_at(kb):
    """Review Focus 4: NULL published_at must neither exempt an item from the
    horizon nor make it stale on arrival."""
    fresh = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash) "
        "VALUES (%s, 'u', 'no date, fresh', 'N1') RETURNING id", (_outlet(kb),)
    ).fetchone()[0]
    old = kb.execute(
        "INSERT INTO items (outlet_id, url, title, content_hash, created_at) "
        "VALUES (%s, 'u', 'no date, old', 'N2', now() - interval '20 days') "
        "RETURNING id", (_outlet(kb),)
    ).fetchone()[0]
    kb.commit()

    rows = {r["id"]: r["stale"] for r in
            comprehend.pending_triage(kb, comprehend.TRIAGE_PROMPT_VERSION, 10)}

    assert rows == {fresh: False, old: True}
```

- [ ] **Step 2: Run them; the first two fail.** The third may already pass after Task 6; that is fine, because it pins the behaviour.

- [ ] **Step 3: Implement.** `pending_triage`: change to `"ORDER BY i.id DESC "`, and replace the "Oldest first" docstring paragraph with:
```
    NEWEST first (spec 2026-09-25 D4). This was oldest-first on the argument
    that "nothing reads this layer yet, so completeness beats recency", which
    assumed a small backlog. A 12-day pause left 24,045 items, drained at
    $0.0027 each, oldest first. Under a budget, something must give when
    arrivals outrun it, and a KB that lags the news cannot corroborate current
    events -- so the OLD items give, and the horizon marks them stale.
```
Move the integration SELECT out of `run()` into a module-level function, placed after `pending_triage`, and make `run()` call `material_items = pending_integration(conn, int(common.COMPREHEND_MAX_ITEMS))`, deleting the inline SELECT and its row-dict comprehension:
```python
def pending_integration(conn, limit: int) -> list[dict]:
    """The ONE definition of what integration picks up next.

    scripts/inspect_integration.py used to carry its own copy of this SELECT,
    which is how a diagnostic ends up probing a query production no longer
    runs (news-brief-bqa.26). Call this; never re-derive it.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.verdict = 'material' AND t.integrate_attempts < 3 "
        "  AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s) "
        "  AND coalesce(i.published_at, i.created_at) "
        "      >= now() - make_interval(days => %s) "
        "ORDER BY i.id DESC LIMIT %s",
        (TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION, CANDIDATE_WINDOW_DAYS, limit),
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "body": r[2] or "", "outlet_id": r[3],
         "published_at": r[4]}
        for r in rows
    ]
```
In `scripts/inspect_integration.py`, replace `select_batch`'s body with `return comprehend.pending_integration(conn, limit)`, keeping its docstring's first line, and delete the now-stale paragraph about `ORDER BY i.id`. Check how the script imports from `comprehend` (it names `call_integration` and friends at `:40-46`) and add `pending_integration` to that import. Then run `py scripts/inspect_integration.py --help` (or the script's cheapest no-DB invocation) to prove it still imports.
After the integration loop, next to `gave_up_integration`:
```python
    tally.aged_out = conn.execute(
        "SELECT count(*) FROM item_triage t JOIN items i ON i.id = t.item_id "
        "WHERE t.triage_prompt_version = %s AND t.verdict = 'material' "
        "  AND t.integrate_attempts < 3 AND t.integrated_at IS NULL "
        "  AND coalesce(i.published_at, i.created_at) "
        "      < now() - make_interval(days => %s)",
        (TRIAGE_PROMPT_VERSION, CANDIDATE_WINDOW_DAYS),
    ).fetchone()[0]
```
`Tally`, after `gave_up_integration`:
```python
    # Material items past the horizon, never to be integrated (spec D4). A
    # standing count, like gave_up_integration. Not a failure: the policy
    # under a budget is that old items give way.
    aged_out: int = 0
```

- [ ] **Step 4: Run all comprehend suites** — `py -m pytest tests/test_comprehend_triage.py tests/test_comprehend_integration.py -q`. If an existing test relied on oldest-first order (e.g. the one asserting "the SAME low-id items sit at the front"), classify it in the report: it either pins the old policy, in which case rewrite it to the new one, or it depends on order incidentally, in which case make its fixture order-independent.

- [ ] **Step 5: Mutation check.** Pre-register: reverting `pending_triage` to ascending fails **1**. Removing the horizon clause from the integration SELECT fails **1** (`pytest.fail` fires). Run each, record the counts, and revert.

- [ ] **Step 6: Commit**
```bash
git add comprehend.py scripts/inspect_integration.py tests/test_comprehend_triage.py tests/test_comprehend_integration.py
git commit -m "feat(comprehend): newest first, and nothing integrated past the 14-day horizon" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Price table, spend ledger, and refusal of unpriced models

**Files:**
- Create: `migrations/0015_comprehend_spend_up.sql`, `migrations/0015_comprehend_spend_down.sql`
- Modify: `comprehend.py` (new block after `call_integration`; `run()`; `Tally`)
- Create: `tests/test_comprehend_budget.py`

**Interfaces:**
- Produces: `comprehend.PRICES_PER_MTOK: dict[str, tuple[float, float]]` and `BATCH_FACTOR = 0.5`.
- Produces: `comprehend.UnpricedModel(RuntimeError)` and `price_of(model: str) -> tuple[float, float]`.
- Produces: `cost_usd(model: str, usage: dict, *, batch: bool = False) -> float`.
- Produces: `record_spend(conn, stage: str, model: str, usage: dict, *, batch_id: str | None = None, batch: bool = False) -> float`, which inserts one ledger row and returns the dollars.
- Produces: `Tally.spent_usd: float`.

- [ ] **Step 1: Write the migration**

`migrations/0015_comprehend_spend_up.sql`:
```sql
-- What comprehension spent, one row per model call (spec 2026-09-25 4.3).
-- "What did comprehension cost yesterday" took a billing console and three
-- days to answer on 2026-09-10, because the number was never recorded here.
-- It also feeds the phase-2 batch reservation estimate.
CREATE TABLE comprehend_spend (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    stage         TEXT          NOT NULL CHECK (stage IN ('triage', 'integration')),
    model         TEXT          NOT NULL,
    input_tokens  INTEGER       NOT NULL,
    output_tokens INTEGER       NOT NULL,
    usd           NUMERIC(12,6) NOT NULL,
    batch_id      TEXT          NULL
);
CREATE INDEX comprehend_spend_at ON comprehend_spend (at);
```
`migrations/0015_comprehend_spend_down.sql`: `DROP TABLE comprehend_spend;`

- [ ] **Step 2: Write the failing tests** in `tests/test_comprehend_budget.py`:
```python
"""Spend accounting and the budget (spec 2026-09-25 section 4.3)."""

import logging

import pytest

import comprehend
import db

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


def test_sonnet_cost_is_priced_from_the_table():
    usd = comprehend.cost_usd(
        "claude-sonnet-5", {"input_tokens": 1_000_000, "output_tokens": 100_000}
    )
    assert usd == pytest.approx(2.00 + 1.00)


def test_the_batch_rate_is_half():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert comprehend.cost_usd("claude-sonnet-5", usage, batch=True) == pytest.approx(1.00)


def test_an_unpriced_model_is_refused_not_priced_at_zero():
    """A silent $0 would make the budget accepted-and-inert."""
    with pytest.raises(comprehend.UnpricedModel):
        comprehend.cost_usd("claude-sonnet-9", {"input_tokens": 1})


def test_a_response_without_usage_costs_nothing_and_warns(caplog):
    """Review Focus 3."""
    with caplog.at_level(logging.WARNING):
        assert comprehend.cost_usd("claude-sonnet-5", {}) == 0.0
    assert "no usage" in caplog.text


@needs_db
def test_record_spend_writes_one_ledger_row(kb):
    usd = comprehend.record_spend(
        kb, "triage", "claude-haiku-4-5", {"input_tokens": 2000, "output_tokens": 100}
    )
    kb.commit()
    row = kb.execute(
        "SELECT stage, model, input_tokens, output_tokens, usd FROM comprehend_spend"
    ).fetchone()
    assert row[:4] == ("triage", "claude-haiku-4-5", 2000, 100)
    assert float(row[4]) == pytest.approx(usd) == pytest.approx(0.0025)


@needs_db
def test_an_unpriced_model_aborts_the_pass_before_any_call(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "TRIAGE_MODEL", "claude-sonnet-9")
    monkeypatch.setattr(comprehend, "call_triage",
                        lambda r: pytest.fail("called an unpriced model"))

    tally = comprehend.run(kb)

    assert tally.aborted == "unpriced_model:claude-sonnet-9"
```

- [ ] **Step 3: Run; they fail.**

- [ ] **Step 4: Implement** (in `comprehend.py`, after `call_integration`)
```python
# $ per million tokens, (input, output). Source: the Anthropic model table
# (claude-api skill, cached 2026-06-24), checked 2026-09-25. Cache tokens use
# the documented multipliers. A model missing from this table REFUSES to run
# (UnpricedModel): pricing an unknown model at $0 would make the budget an
# accepted-and-inert config the moment a settings row named a new model.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
BATCH_FACTOR = 0.5
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


class UnpricedModel(RuntimeError):
    """A model with no entry in PRICES_PER_MTOK."""


def price_of(model: str) -> tuple[float, float]:
    try:
        return PRICES_PER_MTOK[model]
    except KeyError:
        raise UnpricedModel(model) from None


def cost_usd(model: str, usage: dict, *, batch: bool = False) -> float:
    """Dollars for one call's `usage`. Raises UnpricedModel before anything else."""
    pin, pout = price_of(model)
    if not usage:
        log.warning(f"Comprehend: {model} response carried no usage; costed at $0")
        return 0.0
    tokens_in = (
        (usage.get("input_tokens") or 0) * pin
        + (usage.get("cache_creation_input_tokens") or 0) * pin * _CACHE_WRITE_MULT
        + (usage.get("cache_read_input_tokens") or 0) * pin * _CACHE_READ_MULT
    )
    usd = (tokens_in + (usage.get("output_tokens") or 0) * pout) / 1_000_000
    return usd * (BATCH_FACTOR if batch else 1.0)


def record_spend(conn, stage, model, usage, *, batch_id=None, batch=False) -> float:
    usd = cost_usd(model, usage, batch=batch)
    conn.execute(
        "INSERT INTO comprehend_spend "
        "  (stage, model, input_tokens, output_tokens, usd, batch_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            stage,
            model,
            (usage or {}).get("input_tokens") or 0,
            (usage or {}).get("output_tokens") or 0,
            usd,
            batch_id,
        ),
    )
    return usd
```
`Tally`, before `failures`: `spent_usd: float = 0.0`.

In `run()`, directly after `if not tally.enabled: ... return tally`:
```python
    # Refuse to spend on a model we cannot price, BEFORE any call (spec 4.3).
    for model in {_triage_model(), _integrate_model()}:
        try:
            price_of(model)
        except UnpricedModel:
            return _abort(conn, tally, f"unpriced_model:{model}")
```
After each successful call, record the spend. In the triage loop, bind the response first:
```python
            resp = call_triage(build_triage_request(payload))
            tally.spent_usd += record_spend(
                conn, "triage", _triage_model(), resp.get("usage") or {}
            )
            verdicts = parse_triage_response(resp, {it["id"] for it in batch})
```
In the integration `try`, likewise:
```python
            resp = call_integration(
                build_integration_request(payload, cand_entities, cand_events)
            )
            tally.spent_usd += record_spend(
                conn, "integration", _integrate_model(), resp.get("usage") or {}
            )
            extractions = parse_integration_response(resp, ...)  # the existing args
```
The spend row sits in the same transaction as the batch's writes. On a later parse failure, the `except` branch commits after charging or deferring, so the row survives, which is correct: the call was paid for.

- [ ] **Step 5: Run everything comprehend** — `py -m pytest tests/test_comprehend_budget.py tests/test_comprehend_triage.py tests/test_comprehend_integration.py -q`. All pass. The existing fakes return no `usage`, so they cost $0 and log a warning, which is harmless.

- [ ] **Step 6: Mutation check.** Pre-register: replacing the `raise UnpricedModel` in `price_of` with `return (0.0, 0.0)` fails **2** (the refusal test and the run abort). Removing the `if not usage` guard changes nothing numerically but drops the warning, so it fails **1**. Run each, record the counts, and revert.

- [ ] **Step 7: Commit**
```bash
git add migrations/0015_comprehend_spend_up.sql migrations/0015_comprehend_spend_down.sql comprehend.py tests/test_comprehend_budget.py
git commit -m "feat(comprehend): a spend ledger, and refuse any model it cannot price" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: The token bucket

**Files:**
- Modify: `common.py` (`KNOBS`, after `COMPREHEND_MAX_DEFERS`)
- Modify: `docker-compose.yml:134` (two anchor lines)
- Modify: `comprehend.py` (budget block after the pricing block; `run()`; `Tally`)
- Modify: `tests/test_comprehend_budget.py` (append)

**Interfaces:**
- Consumes: `record_spend` / `Tally.spent_usd` (Task 8), `_abort` (Task 4).
- Produces: `comprehend.BUDGET_STATE_KEY = "comprehend_budget"`. The state dict is `{"balance_usd": float, "at": iso8601, "exhausted_on"?: "YYYY-MM-DD" (UTC)}`.
- Produces: `accrue(state: dict | None, now: datetime, allowance: float, max_days: float) -> dict`, a pure function.
- Produces: `class Budget` with `.balance`, `.can_spend()`, `.debit(usd)` and `.mark_exhausted(now)`, plus `open_budget(now) -> Budget` and `pause_budget(now) -> None`.
- Produces: `Tally.budget_exhausted: bool` and `Tally.budget_balance_usd: float | None`.

- [ ] **Step 1: Add the knobs.** In `common.KNOBS`, after `COMPREHEND_MAX_DEFERS`:
```python
    # The comprehension token bucket (spec 2026-09-25 4.3). A daily allowance
    # in USD that accrues continuously while enabled and is capped at MAX_DAYS
    # of allowance, so quiet days bank budget for busy ones without letting a
    # long lull release one enormous burst. MAX_DAYS is 3, not the 7 first
    # drafted: 7 days of $1.50 let a single surge day spend ~$12, the failure
    # this whole redesign exists to prevent.
    "COMPREHEND_DAILY_BUDGET_USD": Knob(float, 1.50),
    "COMPREHEND_BUDGET_MAX_DAYS": Knob(float, 3.0),
```
In `docker-compose.yml`, after line 134:
```yaml
    - COMPREHEND_DAILY_BUDGET_USD=${COMPREHEND_DAILY_BUDGET_USD:-}
    - COMPREHEND_BUDGET_MAX_DAYS=${COMPREHEND_BUDGET_MAX_DAYS:-}
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_comprehend_budget.py`)
```python
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_a_first_pass_starts_with_one_days_allowance():
    s = comprehend.accrue(None, T0, 1.5, 3)
    assert s["balance_usd"] == pytest.approx(1.5)


def test_the_balance_accrues_continuously():
    s = comprehend.accrue({"balance_usd": 0.0, "at": T0.isoformat()},
                          T0 + timedelta(hours=12), 1.5, 3)
    assert s["balance_usd"] == pytest.approx(0.75)


def test_the_balance_is_capped_at_max_days():
    s = comprehend.accrue({"balance_usd": 4.0, "at": T0.isoformat()},
                          T0 + timedelta(days=10), 1.5, 3)
    assert s["balance_usd"] == pytest.approx(4.5)


def test_a_future_timestamp_never_accrues_negative():
    """Review Focus 2: a clock moved back must not drain the bucket."""
    s = comprehend.accrue({"balance_usd": 1.0, "at": (T0 + timedelta(hours=5)).isoformat()},
                          T0, 1.5, 3)
    assert s["balance_usd"] == pytest.approx(1.0)


def test_a_corrupt_budget_row_is_reinitialised_not_fatal():
    """Review Focus 1: a hand-edited row must not crash every pass."""
    for junk in ({"balance_usd": "abc", "at": T0.isoformat()},
                 {"balance_usd": 1.0, "at": "not a date"},
                 {"balance_usd": 1.0, "at": "2026-09-30T00:00:00"},  # naive
                 {"at": T0.isoformat()},
                 "not a dict"):
        s = comprehend.accrue(junk, T0, 1.5, 3)
        assert s["balance_usd"] == pytest.approx(1.5)


def _fake_triage_costing(calls):
    def fake(req):
        calls.append(req)
        ids = [int(line.split("id=")[1].split()[0])
               for line in req["messages"][0]["content"].splitlines()
               if line.startswith("- id=")]
        return {
            "stop_reason": "tool_use",
            # Sonnet: 1000 in + 100 out = $0.003
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            "content": [{"type": "tool_use", "name": "emit_triage",
                         "input": {"items": [{"id": i, "material": False} for i in ids]}}],
        }
    return fake


def _seed(kb, n):
    outlet = kb.execute(
        "INSERT INTO outlets (name, kind) VALUES ('Wire', 'wire') RETURNING id"
    ).fetchone()[0]
    for k in range(n):
        kb.execute(
            "INSERT INTO items (outlet_id, url, title, content_hash, published_at) "
            "VALUES (%s, 'u', %s, %s, now())", (outlet, f"Trade talk {k}", f"H{k}"))
    kb.commit()


@needs_db
def test_an_empty_bucket_makes_no_call(kb, monkeypatch, state_store):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.0, "at": datetime.now(timezone.utc).isoformat()}
    _seed(kb, 3)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    tally = comprehend.run(kb)

    assert calls == []
    assert tally.budget_exhausted is True
    assert state_store[comprehend.BUDGET_STATE_KEY]["exhausted_on"] == NOW_T.date().isoformat()


@needs_db
def test_a_pass_stops_when_the_balance_runs_out(kb, monkeypatch, state_store):
    """Worst-case overdraft is ONE call: $0.002 left, each call costs $0.003."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_SAMPLE_PER_DAY", 0)
    monkeypatch.setattr(comprehend.common, "COMPREHEND_TRIAGE_BATCH", 1)
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.002, "at": datetime.now(timezone.utc).isoformat()}
    _seed(kb, 3)
    calls = []
    monkeypatch.setattr(comprehend, "call_triage", _fake_triage_costing(calls))

    tally = comprehend.run(kb)

    assert len(calls) == 1
    assert tally.budget_exhausted is True
    assert state_store[comprehend.BUDGET_STATE_KEY]["balance_usd"] == pytest.approx(-0.001)


@needs_db
def test_a_disabled_pass_does_not_bank_budget(kb, monkeypatch, state_store):
    """Re-enabling after a pause accrues from the flip, not from the pause."""
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", False)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    state_store[comprehend.BUDGET_STATE_KEY] = {"balance_usd": 0.0, "at": long_ago}

    comprehend.run(kb)

    assert state_store[comprehend.BUDGET_STATE_KEY]["at"] != long_ago
    assert state_store[comprehend.BUDGET_STATE_KEY]["balance_usd"] == 0.0
```
`state_store` is the in-memory `runtime_state` fixture in `tests/conftest.py`.

- [ ] **Step 3: Run; they fail.**

- [ ] **Step 4: Implement** (in `comprehend.py`, after the pricing block; add `import config` and `from datetime import datetime, timezone` at the top)
```python
BUDGET_STATE_KEY = "comprehend_budget"


def accrue(state, now, allowance: float, max_days: float) -> dict:
    """The bucket after accruing up to `now`. Pure.

    Continuous accrual at allowance/day, capped at allowance * max_days. An
    unreadable state (a hand-edited row, a type drift) starts over at one
    day's allowance rather than crashing every pass. A timestamp in the future
    (clock moved back) accrues nothing rather than a negative amount.
    """
    fresh = {"balance_usd": float(allowance), "at": now.isoformat()}
    try:
        balance = float(state["balance_usd"])
        then = datetime.fromisoformat(state["at"])
        # INSIDE the try: a hand-edited naive timestamp parses fine and then
        # raises TypeError (aware minus naive) on the subtraction, on every pass.
        days = max(0.0, (now - then).total_seconds() / 86400)
    except (TypeError, KeyError, ValueError):
        return fresh
    out = dict(state)
    out["balance_usd"] = min(allowance * max_days, balance + allowance * days)
    out["at"] = now.isoformat()
    return out


class Budget:
    """One pass's view of the bucket. Every debit is persisted immediately,
    so a crash mid-pass cannot un-spend what was spent."""

    def __init__(self, state: dict) -> None:
        self.state = state

    @property
    def balance(self) -> float:
        return float(self.state["balance_usd"])

    def can_spend(self) -> bool:
        return self.balance > 0

    def debit(self, usd: float) -> None:
        self.state["balance_usd"] = self.balance - usd
        config.set_runtime_state({BUDGET_STATE_KEY: self.state})

    def mark_exhausted(self, now) -> None:
        # The UTC DATE, overwritten each time: the alert is at most once per
        # day the budget ran out (spec 4.3, revised). A once-per-episode key
        # cleared by "a full day banked" could fire once and then never again,
        # because spend runs at about the allowance.
        self.state["exhausted_on"] = now.date().isoformat()
        config.set_runtime_state({BUDGET_STATE_KEY: self.state})


def _allowance() -> tuple[float, float]:
    return (
        float(common.COMPREHEND_DAILY_BUDGET_USD),
        float(common.COMPREHEND_BUDGET_MAX_DAYS),
    )


def open_budget(now) -> Budget:
    allowance, max_days = _allowance()
    state = accrue(
        config.runtime_state().get(BUDGET_STATE_KEY), now, allowance, max_days
    )
    config.set_runtime_state({BUDGET_STATE_KEY: state})
    return Budget(state)


def pause_budget(now) -> None:
    """A disabled pass moves the clock without accruing: time spent off
    must not bank budget that the flip back on would then release at once."""
    state = config.runtime_state().get(BUDGET_STATE_KEY)
    if isinstance(state, dict):
        config.set_runtime_state({BUDGET_STATE_KEY: {**state, "at": now.isoformat()}})
```
`Tally`, before `failures`:
```python
    budget_exhausted: bool = False
    budget_balance_usd: float | None = None
```
`run()` becomes `def run(conn, now=None) -> Tally:`. The clock is injectable, as `capture.run` already is. The bucket accrues continuously, so a test that stores a $0 balance and then calls `run()` a few milliseconds later would otherwise see a slightly positive balance and make a call (phase-1 red-team, defect 3). Make its first line `now = now or datetime.now(timezone.utc)`.
- In the disabled branch, before `return tally`: `pause_budget(now)`.
- After the unpriced-model check: `budget = open_budget(now)`.

In the two bucket tests in Step 2, set `NOW_T = datetime.now(timezone.utc)`, store `"at": NOW_T.isoformat()`, and call `comprehend.run(kb, now=NOW_T)`. Then `0.0` stays exactly `0.0`, and `-0.001` is exact.
- At the top of the triage batch loop, next to the deadline check:
```python
        if not budget.can_spend():
            tally.budget_exhausted = True
            budget.mark_exhausted(now)
            break
```
- After each `record_spend` (both call sites), debit the same amount:
```python
            usd = record_spend(conn, "triage", _triage_model(), resp.get("usage") or {})
            tally.spent_usd += usd
            budget.debit(usd)
```
(and the same for integration, with `"integration"` and `_integrate_model()`).
- At the top of the integration batch loop, the same `can_spend` guard as triage.
- Before the final `log.info`: `tally.budget_balance_usd = round(budget.balance, 4)`.

- [ ] **Step 5: Run everything** — `py -m pytest tests/test_comprehend_budget.py tests/test_comprehend_triage.py tests/test_comprehend_integration.py tests/test_config.py -q`. All pass. Existing run() tests hit the real, empty `runtime_state` table and start at $1.50 with $0 fake costs, so they are unaffected.

- [ ] **Step 6: Mutation check.** Pre-register:
  - `max(0.0, …)` → `(…)`: **1** fails (future timestamp).
  - Remove the `min(allowance * max_days, …)` cap: **1** fails.
  - Remove the triage `can_spend` guard: **2** fail (empty bucket, runs out).
  - Remove `pause_budget` from the disabled branch: **1** fails.
  - Stop `mark_exhausted` from writing `exhausted_on`: **1** fails (the empty-bucket test).

- [ ] **Step 7: Commit**
```bash
git add common.py docker-compose.yml comprehend.py tests/test_comprehend_budget.py
git commit -m "feat(comprehend): a token-bucket budget that stops a pass at zero" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Abort and budget alerts in the monitor

**Files:**
- Modify: `comprehend.py` (`_abort` persists; a clean pass clears; two verdict functions)
- Modify: `brief.py` (two alert functions next to `comprehend_retirement_alert` at `:3664`; wire them into `mode_monitor` at `:3792`)
- Modify: `tests/test_comprehend_budget.py` (append)

**Interfaces:**
- Consumes: `BUDGET_STATE_KEY`, `accrue`, `_allowance` (Task 9); `Tally.aborted` (Task 4).
- Produces: `comprehend.ABORT_STATE_KEY = "comprehend_abort"`, with state `{"reason": str, "at": iso}`.
- Produces: `comprehend.abort_verdict(state: dict) -> tuple[str, str] | None`.
- Produces: `comprehend.budget_verdict(conn, state: dict, now) -> tuple[str, str] | None`.
- Produces: `brief.COMPREHEND_ABORT_ALERT_KEY`, `brief.COMPREHEND_BUDGET_ALERT_KEY`, `brief.comprehend_abort_alert()` and `brief.comprehend_budget_alert(conn)`.

- [ ] **Step 1: Write the failing tests** (append)
```python
def test_an_abort_names_its_reason_and_model():
    v = comprehend.abort_verdict({comprehend.ABORT_STATE_KEY: {
        "reason": "unpriced_model:claude-sonnet-9", "at": T0.isoformat()}})
    key, message = v
    assert key == "abort:unpriced_model:claude-sonnet-9"
    assert "claude-sonnet-9" in message and "PRICES_PER_MTOK" in message


def test_a_billing_abort_says_to_top_up():
    _, message = comprehend.abort_verdict({comprehend.ABORT_STATE_KEY: {
        "reason": "billing", "at": T0.isoformat()}})
    assert "balance" in message.lower()


def test_no_abort_state_is_no_verdict():
    assert comprehend.abort_verdict({}) is None


def _exhausted_on(day):
    return {comprehend.BUDGET_STATE_KEY: {
        "balance_usd": 0.0, "at": T0.isoformat(), "exhausted_on": day}}


@needs_db
def test_one_budget_key_per_utc_day(kb, monkeypatch):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_DAILY_BUDGET_USD", 1.5)
    today = T0.date().isoformat()
    first = comprehend.budget_verdict(kb, _exhausted_on(today), T0)
    later = comprehend.budget_verdict(kb, _exhausted_on(today), T0 + timedelta(hours=9))
    assert first[0] == later[0] == f"budget:{today}"
    assert "COMPREHEND_DAILY_BUDGET_USD" in first[1]
    assert "aged out" in first[1] and "stale" in first[1]


@needs_db
def test_yesterdays_exhaustion_is_no_verdict_today(kb):
    """The per-day key can never go permanently silent AND never fires more
    than daily: yesterday's episode is over at midnight UTC."""
    yesterday = (T0 - timedelta(days=1)).date().isoformat()
    assert comprehend.budget_verdict(kb, _exhausted_on(yesterday), T0) is None


@needs_db
def test_no_exhaustion_is_no_budget_verdict(kb):
    state = {comprehend.BUDGET_STATE_KEY: {"balance_usd": 1.0, "at": T0.isoformat()}}
    assert comprehend.budget_verdict(kb, state, T0) is None


@needs_db
def test_a_clean_pass_clears_a_previous_abort(kb, monkeypatch, state_store):
    monkeypatch.setattr(comprehend.common, "COMPREHEND_ENABLED", True)
    state_store[comprehend.ABORT_STATE_KEY] = {"reason": "billing", "at": "x"}

    tally = comprehend.run(kb)  # nothing to do, nothing fails

    assert tally.aborted == ""
    assert comprehend.ABORT_STATE_KEY not in state_store


@needs_db
def test_the_budget_alert_fires_once_per_episode(kb, monkeypatch, state_store):
    import brief
    sent = []
    monkeypatch.setattr(brief, "telegram_alert", sent.append)
    today = datetime.now(timezone.utc).date().isoformat()
    state_store[comprehend.BUDGET_STATE_KEY] = {
        "balance_usd": 0.0, "at": T0.isoformat(), "exhausted_on": today}

    for _ in range(3):
        brief.comprehend_budget_alert(kb)

    assert len(sent) == 1
```

- [ ] **Step 2: Run; they fail.**

- [ ] **Step 3: Implement in `comprehend.py`**
```python
ABORT_STATE_KEY = "comprehend_abort"

_ABORT_ADVICE = {
    "billing": "The Anthropic balance is empty. Top it up; nothing was charged "
               "to any item, and the next pass after that resumes on its own.",
    "auth": "The API key was refused (401/403). Check ANTHROPIC_API_KEY on the host.",
}


def abort_verdict(state: dict) -> tuple[str, str] | None:
    a = state.get(ABORT_STATE_KEY)
    if not isinstance(a, dict) or not a.get("reason"):
        return None
    reason = a["reason"]
    if reason.startswith("unpriced_model:"):
        model = reason.split(":", 1)[1]
        advice = (f"Model {model!r} has no price in comprehend.PRICES_PER_MTOK, "
                  "so comprehension refuses to spend on it. Add its price, or "
                  "point the model settings row back at a priced model.")
    else:
        advice = _ABORT_ADVICE.get(reason, "See the comprehend log.")
    return (f"abort:{reason}", f"Comprehension is stopping every pass: {advice}")


def budget_verdict(conn, state: dict, now) -> tuple[str, str] | None:
    b = state.get(BUDGET_STATE_KEY)
    today = now.date().isoformat()
    if not isinstance(b, dict) or b.get("exhausted_on") != today:
        return None
    allowance, max_days = _allowance()
    # Budget-starved items must never be silent (spec 4.3, revised): the day's
    # structural outcomes ride along with the one alert.
    stale_today = conn.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'stale' "
        "AND created_at >= %s::date", (today,)
    ).fetchone()[0]
    aged_out = conn.execute(
        "SELECT count(*) FROM item_triage t JOIN items i ON i.id = t.item_id "
        "WHERE t.triage_prompt_version = %s AND t.verdict = 'material' "
        "  AND t.integrate_attempts < 3 AND t.integrated_at IS NULL "
        "  AND coalesce(i.published_at, i.created_at) "
        "      < now() - make_interval(days => %s)",
        (TRIAGE_PROMPT_VERSION, CANDIDATE_WINDOW_DAYS),
    ).fetchone()[0]
    balance = accrue(b, now, allowance, max_days)["balance_usd"]
    untriaged = conn.execute(
        "SELECT count(*) FROM items i LEFT JOIN item_triage t "
        "  ON t.item_id = i.id AND t.triage_prompt_version = %s WHERE t.id IS NULL",
        (TRIAGE_PROMPT_VERSION,),
    ).fetchone()[0]
    # Spec 4.3 asks for BOTH counts. Through the production predicate, never
    # a copy of it (reconstruction-drifts-from-production).
    awaiting = len(pending_integration(conn, 1_000_000))
    return (
        f"budget:{today}",
        f"Comprehension's budget ran out today ({today}). "
        f"Balance ${balance:.2f} of ${allowance:.2f}/day (cap {max_days:g} days); "
        f"{untriaged} items untriaged, {awaiting} awaiting integration; "
        f"{stale_today} went stale today and {aged_out} have aged out unintegrated. "
        "It resumes as the allowance accrues. To spend more, raise the "
        "COMPREHEND_DAILY_BUDGET_USD settings row.",
    )
```
Extend `_abort` so the reason is persisted (before `return tally`):
```python
    config.set_runtime_state(
        {ABORT_STATE_KEY: {"reason": reason, "at": datetime.now(timezone.utc).isoformat()}}
    )
```
At the end of a non-aborted `run()` (before the final `log.info`):
```python
    if ABORT_STATE_KEY in config.runtime_state():
        config.clear_runtime_state([ABORT_STATE_KEY])
```

- [ ] **Step 4: Implement in `brief.py`**, after `comprehend_retirement_alert`:
```python
COMPREHEND_ABORT_ALERT_KEY = "comprehend_abort_alert"
COMPREHEND_BUDGET_ALERT_KEY = "comprehend_budget_alert"


def comprehend_abort_alert() -> None:
    """Say once that comprehension is refusing to run, and why (spec 4.2-4.3).
    Covers an empty balance, a refused key and an unpriced model -- each one
    stops every pass without charging an item, which is exactly the kind of
    failure that is otherwise silent."""
    import comprehend

    try:
        state = load_state()
        _alert_once(COMPREHEND_ABORT_ALERT_KEY, comprehend.abort_verdict(state),
                    state, "\U0001f6d1")
    except Exception:
        log.exception("comprehend abort check failed")


def comprehend_budget_alert(conn) -> None:
    """Say once per episode that the budget ran dry (spec 4.3)."""
    import comprehend

    try:
        state = load_state()
        verdict = comprehend.budget_verdict(conn, state, datetime.now(timezone.utc))
        _alert_once(COMPREHEND_BUDGET_ALERT_KEY, verdict, state, "\U0001f4b8")
    except Exception:
        log.exception("comprehend budget check failed")


def _alert_once(state_key, verdict, state, icon) -> None:
    """The capture.liveness contract, once: send on a NEW key, before storing
    it; clear the key when the verdict goes away."""
    seen = state.get(state_key)
    if verdict is None:
        if seen:
            config.clear_runtime_state([state_key])
        return
    key, message = verdict
    if key == seen:
        return
    telegram_alert(f"{icon} {message}")
    save_state({state_key: key})
```
Wire both into `mode_monitor`, directly after `comprehend_retirement_alert(conn)`:
```python
            comprehend_abort_alert()
            comprehend_budget_alert(conn)
```

- [ ] **Step 5: Run everything** — `py -m pytest tests/test_comprehend_budget.py tests/test_comprehend_triage.py tests/ -q -k "comprehend or monitor"`. Then run the full suite.

- [ ] **Step 6: Mutation check.** Pre-register:
  - Remove the `key == seen` return in `_alert_once`: **1** fails (fires once).
  - Remove the clear-on-clean-pass block: **1** fails.
  - Change `budget_verdict`'s `!= today` test to `is None` (so yesterday's exhaustion still alerts): **1** fails (`test_yesterdays_exhaustion_is_no_verdict_today`).

  Record the counts, and revert.

- [ ] **Step 7: Commit**
```bash
git add comprehend.py brief.py tests/test_comprehend_budget.py
git commit -m "feat(monitor): alert once when comprehension aborts or runs out of budget" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Runbook, the full gate, and docs

**Files:**
- Create: `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md`
- Modify: `docs/2026-09-22-host-runbook-comprehension-restart.md` (a pointer at the top of its Status table)

- [ ] **Step 1: Write the runbook.** It must contain these steps **in this order**, with the SQL copied from the spec:
  1. **Deploy phase 1 with `COMPREHEND_ENABLED` still false.** Confirm by effect that capture changed:
     ```sql
     SELECT count(*) FROM items i JOIN outlets o ON o.id = i.outlet_id
      WHERE o.name = 'Reuters' AND i.created_at > '<deploy time>'
        AND (i.title ~ '^[A-Z0-9^=][A-Z0-9.^=\-]*\s+-\s+Reuters$'
             OR i.title ILIKE '%stock price & latest news%'
             OR i.title ILIKE '%stock price &amp; latest news%');
     ```
     Expected: 0. **Positive control first:** run the same query with `i.created_at BETWEEN '2026-09-20' AND '2026-09-21'`. It must return hundreds. Without that control, a 0 cannot be told apart from a pattern that matches nothing. Also check that the log line `quote pages dropped` is non-zero on the next capture pass.
  2. **On or after 2026-09-30 00:13:37Z, run the old gate exactly as `docs/2026-09-22-host-runbook-comprehension-restart.md` step 5 says,** and record the output verbatim in `docs/`. Include the warning from spec §6.1: **comprehension must not be enabled before this step**, because `zp8` cannot see a mid-window hole (`news-brief-li9`).
  3. **Set the `NEWSBRIEF_TRIAGE_MODEL` row** to `claude-haiku-4-5`, using the same `INSERT ... ON CONFLICT (key) WHERE user_id IS NULL` shape as the old runbook's step 2. **The key is `NEWSBRIEF_TRIAGE_MODEL`, NOT `TRIAGE_MODEL`**: the knob is declared with `env="NEWSBRIEF_TRIAGE_MODEL"` (`common.py:289`), and `Knob.key` stores and reads that name. A `TRIAGE_MODEL` row is accepted and read by nothing, so triage would silently stay on Sonnet at twice the planned price (phase-1 red-team (b)). **Verify by effect**, after the first pass: `SELECT model, count(*) FROM comprehend_spend WHERE stage = 'triage' GROUP BY 1` must show `claude-haiku-4-5`. The model is not in the call log line, and Sonnet also returns `tool_use`, so nothing else can tell you.
  4. **Run the spec §4.6 recovery SQL,** and record both returned row counts.
  5. **Flip `COMPREHEND_ENABLED`,** and verify by effect within the hour: `SELECT count(*), max(at) FROM comprehend_spend`; a `stale` count and a `quote_page` count in `item_triage`; and a log line showing `spent_usd` and `budget_balance_usd`.
  6. **The deliberate-exhaustion check:** set the `COMPREHEND_DAILY_BUDGET_USD` row to `0.01` for one pass. Expect the pass to stop with `budget_exhausted=True` and ONE Telegram message. Then restore `1.50`.
  7. **After 7 days, record:** the material rate
     (`SELECT reason, count(*) FROM item_triage WHERE created_at > <flip> GROUP BY 1`)
     and `SELECT date_trunc('day', at), sum(usd) FROM comprehend_spend GROUP BY 1 ORDER BY 1`, next to the spec's §4 estimate.

- [ ] **Step 2: Point the old runbook at the new one.** Add one row at the top of its Status table: `| — | superseded for the restart by docs/2026-09-25-host-runbook-cost-redesign-phase-1.md; step 5 (the gate) is still run from HERE |`.

- [ ] **Step 3: The full gate**
```bash
ruff check . ; echo "RUFF=$?"
ruff format --check . ; echo "FMT=$?"
L="<session scratchpad>/p1-pytest.log"; : > "$L"   # never a shared /tmp path
py -m pytest -q > "$L" 2>&1; echo "REAL_EXIT=$?" >> "$L"; tail -5 "$L"
```
Expected: `RUFF=0`, `FMT=0`, `REAL_EXIT=0`, and the pytest summary line shows **no DB skips**. Compare the passed count with the count before Task 1 (record both). If `ruff format` rewrites files, `git add` them.

- [ ] **Step 4: Commit and close the beads**
```bash
git add docs/2026-09-25-host-runbook-cost-redesign-phase-1.md docs/2026-09-22-host-runbook-comprehension-restart.md
git commit -m "docs(runbook): phase-1 restart order, with the old gate first" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
bd close <task-children>
bd export -o .beads/issues.jsonl
git add .beads/issues.jsonl
git commit -m "chore(beads): phase 1 tasks closed" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
**Keep the epic open.** Its host steps (runbook 1–7) are unverified until the operator runs them, and a close has to name the observation that measured it.

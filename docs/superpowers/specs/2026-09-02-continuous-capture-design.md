# Continuous Capture — Design

**Date:** 2026-09-02
**Issue:** `news-brief-b42.1` (Epic 2), unblocks `news-brief-b42.2` and `news-brief-bqa.4`
**Status:** Design, revised after red-team review, awaiting approval
**Amends:** `2026-08-31-process-architecture-and-storage-design.md` §7.2

---

## 1. Why

`b42` states the defect: RSS feeds are windows, not archives, and an item that appears and rolls
off between polls is never seen at all. The evidence it cites is that chips appear in 58 of 90
briefs while the ledger holds zero chip claims.

Two things measured while designing this narrow that claim, and both are recorded here rather
than left to be rediscovered.

**The `max_items` half of the defect is already fixed.** `fetch_rss` defaults to `max_items=25`,
not 5. Minimal-repair item #8 (§12.3 of the KB architecture design) was applied at some point
after that spec was written. The remaining exposure is polling *frequency*, not per-poll depth.

**Depth still exceeds what one poll consumes, on at least one feed.** A live fetch of the Reuters
markets feed returned **100 entries** against a `when:2d` window; the brief reads 25. So on that
feed the loss is not roll-off between polls at all — it is truncation inside a single poll, and
capture recovers it immediately by storing everything the feed returns.

**The Google News proxies are the WORST exposure, not the best — measured, after a draft claimed
the opposite.** An earlier draft reasoned that the 8 `site:` proxies carry explicit `when:2d` /
`when:7d` windows, so one daily poll sees everything inside them, making their roll-off risk near
zero. That is wrong. The same feed fetched three ways:

| Query | Entries |
|---|---|
| `when:2d` | **100** |
| `when:7d` | **100** |
| no window at all | **100** |

**100 is Google News RSS's response cap, not a count.** `brief.py:158` records the same figure
without drawing the conclusion. A capped response means the server truncates *inside* the window,
so the `when:` parameter does not bound what is lost — anything past position 100 is invisible,
and a wider window makes that strictly worse. These 8 feeds are therefore the most truncation-
exposed in the set, and more frequent polling is the direct remedy: at 30-minute passes, far
fewer items pass position 100 between sightings.

The tell was in the first probe and was missed: two independent feeds returning *exactly* 100 is
a pile-up on a round number, which is the signature of a limit rather than a measurement.

**Where exposure actually sits is still unmeasured.** The 5 weekly Substacks publish far slower
than any poll interval under discussion. Whether the native wires or the capped proxies lose more
is exactly what `b42.2` answers — and why this design sets one uniform interval rather than
guessing per-feed ones.

### 1.1 A claim withdrawn

An earlier draft argued that `Reuters World` and `Reuters Markets` must collapse to one outlet
because a story that is both macro and geo would otherwise be double-counted. A live fetch of both
feeds found **0 of ~194 titles present in both**. The two `site:` paths are disjoint in practice.

The decision to collapse them is unchanged — Reuters is one newsroom, and `outlets_name` is
`UNIQUE (lower(name))` regardless — but the *reason* given for it was not supported, and is
withdrawn rather than quietly retained.

---

## 2. What this is and is not

**In scope.** A supervisor job child that polls every feed source on a fixed interval, writes what
it finds into `outlets` and `items`, and records per-feed sighting telemetry.

**Explicitly out of scope.** `mode_submit` and `mode_collect` are untouched. The brief keeps
fetching its own feeds, renders from its own prompt assembly, and is unaffected by capture
succeeding, failing, or being disabled. Switching the brief's input to the knowledge base is
`bqa.4` and Epic 4.

That boundary means capture can run for days at zero risk to the morning delivery: a broken
capture child costs a log line and a `job_runs` row, never a brief.

**The boundary also has a cost, and the first draft paid it.** Because nothing reads these rows,
nothing forced the schema to be checked against a consumer's question — and the first draft
shipped a §13 that *asserted* `b42.2` could measure roll-off from `items` when it provably could
not (§6). A "nothing reads it yet" boundary removes the forcing function that would otherwise
catch a schema that answers no question. Section 6 exists because of that.

---

## 3. Amendment: `captured_items` is superseded

`b42.1` was amended on 2026-08-31 to write "a `captured_items` table directly rather than a JSONL
that phase 3 migrates." The KB schema DDL landed afterwards, on 2026-09-02, and migration 0006
created `outlets` and `items` — empty, with `items_outlet_hash` unique on `(outlet_id,
content_hash)`. `captured_items` exists in two specs and zero SQL.

Building it would also re-open a defect 0006 records closing. From `0006_knowledge_base_up.sql`:

> Per-outlet, NOT global. 12.3 #15 specified a content-hash dedup key for a capture JSONL with no
> outlet dimension; promoted unchanged to a shared, outlet-attributed table it would collapse
> syndicated wire copy and cap any corroboration count at one.

**Captured content goes to `outlets` and `items`; there is no staging table.** A migration 0008
does exist, but for capture *telemetry* (§6) — a different object with a different lifetime, not
a staging area that anything later drains.

The `b42.1` issue text needs updating to match.

---

## 4. The feed-to-outlet mapping

`items.outlet_id` is `NOT NULL`, so every captured item must resolve to an outlet. The mapping is
declared, not derived.

**A feed definition gains an optional `outlet` key, defaulting to its `name`.**

A draft claimed only the two Reuters feeds need one, because "every other feed's name already *is*
its publisher name". That is false for **7 of 26**, and `outlets.name` is `UNIQUE (lower(name))`
and is the KB's corroboration dimension — so a feed-product name shipped into it becomes a
publisher that does not exist.

| Feed | `outlet` | Why |
|---|---|---|
| `Reuters Markets`, `Reuters World` | `Reuters` | Two sections, one newsroom |
| `ISW Daily Assessment` | `Institute for the Study of War` | Product name, not publisher |
| `BOJ Statements` | `Bank of Japan` | Product name |
| `EIA Today in Energy` | `U.S. Energy Information Administration` | Product name |
| `Marko Papic (@geo_papic)` | `Marko Papic` | Person plus handle |
| `Jacob Shapiro (@jacobshap)`, `Intersubjectively Transmissible` | `Jacob Shapiro` | **One author, two media** |

The last row is the case the draft's rule would have missed entirely, and it is the Reuters
problem in a less obvious costume: `jashap.substack.com` and the `@jacobshap` Nitter feed are the
same author publishing twice. Left unmapped they become two outlets, and the same take reaching
both would read as two independent sources corroborating each other. Cross-**medium** duplication
is harder to spot than cross-**section** duplication, which is exactly why the mapping is declared
per feed rather than inferred from names.

What *is* a reliable convention, and worth stating so it is not "tidied" later: the 8 Google News
proxies are named for the publisher they proxy — `Kyiv Independent`, `NHK World`,
`Yonhap (English)` — never `Google News`. Those need no `outlet` key.

**A test asserts every feed resolves to an outlet whose name is not merely the feed name** for
these 7, so a future feed added with a product name fails rather than quietly minting an outlet.

URL-derived outlet resolution was rejected: it makes the Google News query format load-bearing for
data integrity, and still needs an editorial domain-to-display-name table underneath.

### 4.1 Which fields belong to the outlet

`outlets` carries `kind`, `perspective`, `state_funded`. `sources` carries those **plus**
`category`. That difference is the schema already drawing the line correctly: `category` is how
one reader slices their reading, while `kind`/`perspective`/`state_funded` are properties of the
publisher. The two Reuters feeds differ only in `category` (`macro` vs `geo`) and agree on all
three outlet fields, so collapsing them is safe.

### 4.2 Conflicting metadata

Two feeds mapping to one outlet must agree on the three outlet fields. Disagreement is handled by
origin, because the origins have different blast radii:

- **Two baked-in `RSS_FEEDS` entries disagree** — a developer error, caught by a unit test over
  the static feed list. It never reaches runtime.
- **A user source disagrees with an existing outlet** — an operator error. The source is dropped
  with a warning and capture continues, matching the contract `load_temp_sources` already states:
  *"one bad hand-edit must not take down the morning brief."*

Capture never overwrites an existing outlet row's metadata. An outlet is inserted once, on first
sight, and thereafter only looked up.

---

## 5. The dedup key

`content_hash` is `SHA-256` over **the feed entry's `id`/`guid` when the feed supplies one, and
the normalized item URL otherwise**. `title`, `body` (the entry's summary text) and `published_at`
are stored as columns and are *not* part of the key.

`items.published_at` is `TIMESTAMPTZ NULL`, while a feed supplies a date string. Capture marshals
`published_parsed` to an aware UTC timestamp and stores `NULL` when the feed omits it or the value
does not parse — an unparseable date must not drop the item, because the URL is the identity and
the date is metadata.

Two probes decided the key:

| Probe | Result |
|---|---|
| Same feed polled twice, 3s apart | **100/100 links identical** — Google News item URLs are not per-request tokens, so URL-keyed dedup works |
| Duplicate titles within one poll | **6 of 100**, each with 2 distinct URLs and 2 distinct pubdates — so title cannot be part of the key |

The first probe's claim is deliberately narrow: three seconds apart is weak evidence about
long-term URL stability, but it decisively rules out the failure that would have made URL-keyed
dedup a no-op. A guid, where offered, is the publisher's own answer to "is this the same item",
which is why it wins when present.

Excluding title from the key means a headline correction updates an item rather than duplicating
it — the common case on wire copy.

**Normalization** strips `utm_*` and other tracking parameters and the URL fragment. It does not
follow redirects: resolving Google News redirect URLs would turn one capture into two HTTP
requests per item and make dedup depend on a network call.

### 5.1 Capture stores everything, including the junk

A single fetch of one feed — Reuters markets — contained **6 non-article entries out of 100**:
**ticker quote pages**, not journalism —
`SUNTF.PK - Reuters`, `COPY.N - | Stock Price & Latest News - Reuters`. `site:reuters.com/markets`
reads like "Reuters markets journalism" and silently also matches `/markets/companies/*`.

These are stored. Capture is cheap and irreversible; comprehension is expensive and retryable, and
a filter that silently drops items is invisible exactly when it is wrong. The quote pages become
*measurable input* for `b42.2` rather than a rule guessed from one feed's sample.

---

## 6. Capture telemetry — migration 0008

`items` cannot answer `b42.2`, and the first draft wrongly claimed it could. Two independent
reasons, both verified against `0006_knowledge_base_up.sql:27-38`:

- **No feed dimension.** `items` is `id, outlet_id, url, title, body, published_at, content_hash,
  created_at`. There is no feed or source column — and §4 deliberately collapses the two
  highest-volume feeds into one `outlet_id`, so per-feed attribution is destroyed *by design*.
- **First sight only.** `ON CONFLICT DO NOTHING` records that an item was seen once. Roll-off is
  *when an item leaves the feed window*; nothing records that.

**These are not defects in `items`. Forcing a feed column into it would be the defect.** `items`
is a shared knowledge-base object — what the world published. "Which of my feeds showed it, when,
and for how long" is capture telemetry about one reader's plumbing. Different object, different
lifetime, different table.

```sql
CREATE TABLE feed_sightings (
    id            BIGSERIAL PRIMARY KEY,
    source_name   TEXT        NOT NULL,   -- the FEED, not the outlet
    content_hash  TEXT        NOT NULL,
    item_id       BIGINT      NULL REFERENCES items(id),
    position      INTEGER     NOT NULL,   -- rank within the poll's entry list
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX feed_sightings_source_hash ON feed_sightings (source_name, content_hash);

CREATE TABLE capture_runs (
    id             BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ NULL,
    enabled        BOOLEAN     NOT NULL,
    feeds_total    INTEGER     NOT NULL DEFAULT 0,
    feeds_ok       INTEGER     NOT NULL DEFAULT 0,
    feeds_failed   INTEGER     NOT NULL DEFAULT 0,
    items_seen     INTEGER     NOT NULL DEFAULT 0,
    items_new      INTEGER     NOT NULL DEFAULT 0,
    sources_dropped INTEGER    NOT NULL DEFAULT 0
);
```

```sql
CREATE TABLE feed_polls (
    id             BIGSERIAL PRIMARY KEY,
    capture_run_id BIGINT      NOT NULL REFERENCES capture_runs(id),
    source_name    TEXT        NOT NULL,
    polled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    failure        TEXT        NULL,      -- NULL = success; else the FeedFetch kind
    entries_seen   INTEGER     NOT NULL DEFAULT 0
);
CREATE INDEX feed_polls_source_time ON feed_polls (source_name, polled_at DESC);
```

Every pass upserts one `feed_sightings` row per entry: insert on first sight, otherwise advance
`last_seen_at` and `position`. It also writes one `feed_polls` row per feed, success or failure.

**`feed_polls` is what makes absence interpretable, and without it the measurement is wrong.** An
item's disappearance only means "left the window" if a *successful* poll of that feed happened
afterwards and did not contain it. Absence during a 403 means nothing at all. With `feed_sightings`
alone, a feed 403ing for a day would read as every one of its items rolling off simultaneously —
a large, clean, entirely fictitious roll-off signal, and precisely the "a dropped feed looks
identical to a quiet one" failure this repo has already been bitten by once.

So roll-off is defined against successful polls only:

> An item has left the window at time *T* if a `feed_polls` row for its `source_name` with
> `failure IS NULL` exists at *T*, and the item's `last_seen_at` is older than *T*.

Dwell time is `last_seen_at - first_seen_at`, **per feed**, keeping `Reuters Markets` and
`Reuters World` distinct even though they share an outlet.

A failed poll never advances any `last_seen_at`: nothing was observed, so nothing is asserted.

`source_name` is deliberately a plain `TEXT`, not a FK to `sources`: the baked-in `RSS_FEEDS` have
no `sources` row, and a renamed or deleted user source must not destroy the measurement history
that justified an interval.

**Volume.** ~26 feeds × ~40 entries = ~1,000 `feed_sightings` upserts per pass, ~50k/day, of which
only ~1–2k are inserts. `feed_polls` grows 26 × 48 = **1,248 rows/day**; `capture_runs`, 48. All
three are unbounded-growth tables with no retention rule — a real cost stated rather than hidden,
and filed as a follow-up alongside `news-brief-6wc` rather than absorbed here.

`feed_polls` is the one to watch: at ~455k rows/year it is the largest thing this design creates,
and it exists purely as the denominator for a measurement `b42.2` takes once. A retention rule
that keeps it for a bounded window is likely correct and is deliberately not guessed here.

---

## 7. Module structure

### 7.1 Splitting `fetch_rss`

`fetch_rss` currently does fetch, retry, parse **and render-to-prompt-string** in one function,
returning a formatted string. Capture needs structured entries.

It splits in place, in `brief.py`:

```
fetch_feed_entries(feed) -> FeedFetch      # fetch + retry + parse. New.
fetch_rss(feed, max_items=25) -> str       # renders a FeedFetch. Same signature, same output.

FeedFetch = NamedTuple(entries: list[dict], failure: str | None)
# failure ∈ {None, "http_403", "http_429", "http_5xx", "timeout", "malformed", "empty"}
```

**`FeedFetch` rather than a bare list, because a bare list is the bug this design elsewhere
warns about.** `fetch_rss` today collapses every failure to `return ""` (`brief.py:1900-1902`), so
an empty result is ambiguous across 403, timeout, malformed feed and genuinely quiet. §10 promises
a tally reading `2 failed (403 x1, timeout x1)`, which is *not derivable* from `list[dict]`.

An earlier draft specified the bare list while citing `fail-closed-needs-status-not-count` two
sections later — the rule and its violation in one document. The distinction is load-bearing twice
over: "a dropped feed looks identical to a quiet one downstream" is a documented production
failure here, and a 403 silently read as "quiet" would corrupt `b42.2`'s roll-off measurement by
recording a window that emptied when in fact the fetch never happened.

`fetch_rss` keeps its existing external contract: it renders `.entries` and returns `""` when
`failure` is set, so the brief's behaviour and output are unchanged.

Duplicating the fetcher into `capture.py` was rejected. What is buried in that function is not 30
lines of HTTP — it is a 429/5xx retry loop honouring `Retry-After`, an explicit socket timeout
(feedparser's own fetch has none, so one wedged Nitter would hang an entire submit run), a shared
`SOURCE_USER_AGENT` for 403 avoidance, and `bozo_exception` handling. Each is a scar from a
specific production failure. The cost of a second fetcher is not writing it; it is that the *next*
scar gets applied to only one of them.

A new fetch module was rejected for this increment: moving fetch out of `brief.py` is a larger diff
than the feature warrants and can be done later without changing this design.

**The refactor is guarded by a characterization test** pinning `fetch_rss`'s current output
byte-for-byte, written and passing before the split.

### 7.2 `capture.py`

A new top-level module owning all capture SQL, in the way `claim_store.py` owns all claim SQL.

```
run(conn) -> CaptureResult                                  # one full pass
_resolve_outlet(conn, feed) -> int                          # insert-once, then look up
_store_items(conn, outlet_id, entries) -> tuple[int, int]   # (written, already_present)
_record_sightings(conn, source_name, entries) -> None
```

Item writes use `INSERT ... ON CONFLICT (outlet_id, content_hash) DO NOTHING`, so re-capturing is
a no-op and the returned rowcount is the honest "new items" number. Sighting writes use
`ON CONFLICT (source_name, content_hash) DO UPDATE` to advance `last_seen_at`.

Being a new top-level module, `capture.py` needs the Dockerfile `COPY` line, the workflow `paths:`
filter and both ruff file lists. As of `51e72de` those are enforced by `tests/test_packaging.py`
rather than remembered.

### 7.3 Which sources capture iterates, and four ways it would not work as written

Four integration details that the design assumed and the code does not currently provide. Each is
small; each silently produces the wrong behaviour if missed.

**`all_sources()` is the wrong entry point.** It returns `RSS_FEEDS + load_temp_sources()`, and
temp sources include `source_type='page'` entries, which are scraped pages with no entry list.
Capture iterates `RSS_FEEDS` plus only those temp sources with `source_type == "feed"`. Note
`RSS_FEEDS` entries carry no `source_type` key at all, so the filter must treat its absence as
`"feed"` rather than testing the key's presence.

**`load_temp_sources` would silently discard the `outlet` key.** It rebuilds each entry as a new
dict from a fixed field list (`brief.py:456-470`), so any key it does not name is dropped. §4's
mapping would therefore work for baked-in feeds and fail, without error, for user sources. It must
carry `outlet` through — and a test must assert it, because the failure is invisible: the source
still captures, just under the wrong outlet.

**`items.title` and `items.url` are `NOT NULL`.** A feed entry missing either would raise, and
inside one transaction that aborts the *whole pass*, losing every item captured before it. Items
are written per feed in their own transaction, and an entry lacking a title or link is skipped
with a warning and counted, never allowed to take down the pass. This mirrors the contract
`load_temp_sources` already sets: one bad input degrades itself, not the run.

**`run_job` calls `fn()` with no arguments** (`brief.py:3874`), so `mode_capture` takes none and
opens its own connection. Adding the mode also means updating the `JOB_MODES` comment at
`brief.py:3838` and the usage string at `brief.py:3982`, neither of which is exercised by any test
and both of which will otherwise go stale immediately.

---

## 8. Scheduling

One schedule, added to `scheduler.SCHEDULES`:

```python
Schedule("capture", "interval", None, 30, grace_minutes=10)
```

**Thirty minutes, uniformly, across every feed.** 26 feeds × 48 passes = **1,248 requests/day**.
In aggregate that is trivial load.

The interval is 30 rather than 60 because **the poll interval bounds the resolution of what
`b42.2` can measure**: sub-hourly roll-off is indistinguishable from exactly-hourly roll-off if you
poll hourly. Per-feed intervals are deliberately not built here — every one would be guessed, and
§2 of the KB architecture design says to set them empirically.

`grace_minutes=10` satisfies the floor (`grace_minutes * 60 > TICK_SECONDS`, 30s) and reflects that
a very late capture is worth little when the next one is minutes away.

`"capture"` joins `brief.JOB_MODES`, which is what gives it the `pg_advisory_lock` on the job name
and its `job_runs` row through the mode dispatch — the §4.4a rule that every entry path to a job,
including `docker compose run`, records itself. `mode_capture` is a thin wrapper in `brief.py`
delegating to `capture.run`; the dispatch table lives there and the supervisor spawns
`python brief.py capture`.

### 8.1 Aggregate load is the wrong measure

An earlier draft claimed 30-minute polling sits "far below the cadence that produced the documented
Nitter 429s". That misreads the cause. `fetch_rss`'s own comment records it: *"the two X feeds sit
next to each other in `RSS_FEEDS`, so one of them 429'd on most runs and silently never reached the
brief."* The 429 is produced by **two requests to one host arriving back-to-back inside a single
pass** — not by requests per day. Polling 48 times a day therefore reproduces that collision 48
times a day rather than diluting it. The two feeds are `Marko Papic (@geo_papic)` and
`Jacob Shapiro (@jacobshap)`, both resolving `NITTER_BASE_URL`.

**So capture spaces requests per host.** Feeds are ordered so no two consecutive fetches share a
host, and a minimum delay is enforced between two requests to the same host within a pass. This is
a property of the pass, asserted by a test that captures request order, not a `sleep` sprinkled
between calls.

This matters more than the interval choice: a 429'd feed returns empty, and *"a dropped feed looks
identical to a quiet one downstream"* — which would silently corrupt the very roll-off measurement
`b42.2` is meant to take.

### 8.2 A pass must be bounded, or it alerts 48 times a day

**The worst case exceeds the interval.** 26 feeds × `RSS_MAX_ATTEMPTS` 3 × `timeout=20` is ~26
minutes of pure socket timeout before any `Retry-After` sleeps. A pathological pass therefore
outlives its own 30-minute fire time.

That is not merely slow. `supervisor.py:834-851` handles a job still running at its next fire time
by logging *and* `telegram_alert`ing, once per skipped fire time — deliberately, because "a wedged
collect looks exactly like a quiet news day". Correct for a daily job; at 48 fire times a day it
is an alert storm, and §10 goes to some trouble to keep capture off Telegram precisely so the
channel stays meaningful. The alert would arrive through a door §10 never looked at.

**So a pass carries a wall-clock deadline of 10 minutes.** On expiry, remaining feeds are not
fetched and are recorded in `feed_polls` with `failure = 'deadline'`. This makes "a pass finishes
inside its interval" a property of the code rather than a hope about network latency, and it keeps
the skipped-fire-time path unreachable for capture.

A deadline-skipped feed is explicitly *not* a successful poll, so §6's roll-off rule ignores it —
the same reason a 403 does. A capture that keeps hitting the deadline shows up as a rising
`failure = 'deadline'` count, which is the anomaly signal §10 already routes through `monitor`.

**Asserted by test**, not by arithmetic in a comment: with every feed stubbed to hang, the pass
returns inside the deadline and every unfetched feed has a `feed_polls` row.

### 8.3 The shutdown budget must be updated with it

`supervisor.py:60-78` carries a hand-computed shutdown budget including:

```
+  5  DB_STATEMENT_TIMEOUT  5 schedules x 2 statements x 0.5s
```

Nothing computes that 5 — `SHUTDOWN_DB_STATEMENT_TIMEOUT_MS = 500` is per-statement, and the
schedule count is arithmetic done by hand in a comment. A sixth schedule makes it 6, and the
audited sum 39s → 40s, still inside the 60s `stop_grace_period`. The comment names its own
fragility (*"the next person reconciles a budget that does not add up and concludes the wrong
thing"*) but cannot enforce it.

**The comment is updated and a test asserts the documented count equals `len(scheduler.SCHEDULES)`.**
A guarantee stated in prose is intent; the question is which test fails when it stops being true.

---

## 9. Configuration

One knob, `CAPTURE_ENABLED: Knob(bool, False)`, added to `common.KNOBS` and to the `&newsbrief`
compose anchor as `NEWSBRIEF_CAPTURE_ENABLED=${NEWSBRIEF_CAPTURE_ENABLED:-}`.

Default **off**. Capture is new, writes to shared tables, and has no consumer yet, so it is enabled
deliberately on the host after a deploy rather than starting on its own.

The anchor line is not optional and not cosmetic: the anchor *seeds the settings rows*, so a
missing line freezes the default into a row on first boot, and adding the line later fixes nothing.
The knob is read as `common.CAPTURE_ENABLED`, never `from common import CAPTURE_ENABLED` — a
from-import copy freezes at import time and defeats both host toggles and test monkeypatching.

A disabled pass writes a `capture_runs` row with `enabled = false` and exits 0. `job_runs` cannot
express this — it is `id, job_name, scheduled_for, trigger, status, started_at, finished_at,
exit_code` (`0001_runtime_foundation_up.sql:29-38`), with no free-text column, so a disabled run
and a successful one are byte-identical rows there. An earlier draft promised the distinction
anyway. `capture_runs.enabled` is where it actually lives.

---

## 10. Error handling and observability

**A feed failure is per-feed.** One 403, 429 or malformed feed produces a warning and the pass
continues. The existing `fetch_rss` contract already degrades a failed feed to empty; capture keeps
that and counts it.

**Each pass emits one log line and writes one `capture_runs` row:**

```
Capture: 26 feeds, 24 ok, 2 failed (403 x1, timeout x1), 312 items seen,
         47 new, 265 already held, 1 source dropped (outlet metadata conflict)
```

`fail-closed-needs-status-not-count` is the rule: a run writing 0 new items is ambiguous between
"nothing published", "every fetch failed" and "the store refused everything", and those need
different responses.

**Nothing is sent to Telegram per pass.** 48 notifications a day would train the operator to ignore
the channel, which is worse than silence. The existing hourly `monitor` job raises capture health
only on anomaly — failed feeds above a threshold, or zero new items across several consecutive
passes. Routine success is queryable, never pushed.

**A swallowed exception is a failed job, not a quiet success.** If the pass itself raises, the mode
exits non-zero so `job_runs.exit_code` records it, and `capture_runs.finished_at` stays `NULL` —
which is how a crashed pass is told apart from a completed one.

---

## 11. Testing

TDD throughout; each test observed failing first.

**Unit, no network:**
- `fetch_rss` characterization test — output unchanged across the split (written *before* it).
  **It pins the log as well as the return value.** `brief.py:1877-1880` returns `""` and emits
  `No entries: <feed> (<bozo_exception or 'empty feed'>)` on the same path, so a test asserting
  only the return value passes even if that warning disappears — and the warning is the operator's
  sole signal that a feed was malformed rather than merely quiet. It is also the distinction
  `FeedFetch.failure` must preserve when the path splits into `"malformed"` and `"empty"`: the
  existing log already draws the line, so the taxonomy must not blur it.
- `fetch_feed_entries` parses title, link, summary, published, guid from a fixture feed.
- Hash: guid wins when present; falls back to URL; tracking params and fragments do not change it.
- An unparseable or absent date stores `NULL` and does not drop the item.
- Every baked-in `RSS_FEEDS` entry mapping to a shared outlet agrees on `kind`/`perspective`/
  `state_funded` — the §4.2 developer-error case.
- The documented schedule count in the `supervisor.py` budget equals `len(scheduler.SCHEDULES)`.
- **Per-host spacing (§8.1):** given a source list whose two Nitter feeds are adjacent, the
  recorded fetch order never places two same-host requests consecutively and respects the enforced
  gap. Written against the real `RSS_FEEDS` ordering as well as a synthetic list, since it is the
  regression test for a documented production 429.

**Against Postgres** (the layer that skips silently when unconfigured):
- An outlet is inserted once and looked up thereafter; metadata is never overwritten.
- The same item captured twice writes one row; `written` counts 1 then 0.
- Two outlets with the same `content_hash` both store — the per-outlet key, positive control.
- A user source conflicting with an existing outlet is dropped, capture continues, tally reports it.
- Capture disabled: a `capture_runs` row exists with `enabled = false`, exit 0, no `items` written.
- **Roll-off is computable:** an item present in pass 1 and pass 2 but absent from a *successful*
  pass 3 has a `last_seen_at` that stopped advancing, and dwell time is derivable from the two
  timestamps. Criterion 6 tested directly rather than asserted — the failure the first draft shipped.
- **A failed poll produces no roll-off signal:** the same three passes with pass 3 recorded as
  `failure = 'http_403'` yield zero items judged to have left the window, and no `last_seen_at`
  moves. The negative control for the whole measurement; without `feed_polls` this test cannot
  even be written.
- A renamed source does not orphan or destroy its prior sightings.
- A feed returning a `FeedFetch` with `failure` set is counted in the tally by kind, not folded
  into "quiet" — the §7.1 distinction, asserted where it is consumed rather than where it is
  produced.

**Pre-registered numbers.** Before the first live run: 26 feeds; `items` goes from 0 to non-zero;
`outlets` at or below the number of distinct outlet names; `feed_sightings` ≥ `items` (a shared
item sighted by two feeds makes two sightings). A disagreement is assumed to be the system's fault,
not the prediction's.

---

## 12. Rejected

- **`captured_items` staging table** — superseded by 0006 (§3), and it re-opens the global-hash
  corroboration bug.
- **A feed column on `items`** (§6) — `items` is a shared KB object; feed provenance is telemetry
  about one reader's plumbing and belongs in its own table.
- **Filtering junk at capture time** (§5.1) — unrecoverable, and guessed from one sample.
- **Per-feed poll intervals** (§8) — every value would be guessed; that is `b42.2`'s job.
- **A second fetcher in `capture.py`** (§7.1) — guarantees the next HTTP scar lands once.
- **Telegram per pass** (§10) — 48/day destroys the channel's signal value.
- **Page sources** (`source_type='page'`) — a second code path with different failure modes, and
  "the page changed" is a far noisier signal than "a feed published an item". Revisit when the BCA
  dashboards are re-added (`news-brief-jh5`).
- **Resolving Google News redirect URLs** (§5) — doubles request count and makes dedup depend on a
  network call.

---

## 13. Consequences for other issues

- **`b42.1`** — issue text still says `captured_items`; update to `outlets`/`items` plus the
  telemetry tables.
- **`b42.2`** — unblocked once capture has run for several days, and now actually answerable from
  stored rows. It also owns the quote-page noise quantified in §5.1.
- **`bqa.4`** — reads `items` that have no `assertions` row yet. This design commits to that
  interface and to nothing else.
- **`bqa.9` items 2-5** — untouched. They constrain the migration `bqa.4` has not written, and
  capture writes no claims.
- **New follow-up** — retention for `items`, `feed_sightings`, `feed_polls`, `capture_runs` **and
  `job_runs`**. `retention.py` prunes files only and has no `job_runs` handling, so capture taking
  the table from ~5 rows/day to 53 is a **10x growth rate change on an existing unbounded table** —
  a consequence for something already shipped, not just for the new tables. Filed, not absorbed.
- **Stale comment, fixed in passing** — `brief.py:158` still reads "Only the 5 newest items are
  used (fetch_rss max_items)". The default became 25 in `240a4cb`; the comment did not follow. It
  is adjacent to the code this issue edits and is the kind of note that makes a future reader
  re-derive the wrong exposure, which is how this design's own §1 went wrong once.

---

## 14. Success criteria

1. Dedup demonstrably works: re-running a pass against an unchanged feed writes **zero** new
   `items` and advances every matching `last_seen_at`. (An earlier draft used "`already_present`
   far exceeds `new`", which is non-discriminating — it is equally true of a working system and of
   one where every fetch fails and nothing is written at all.)
2. Both Reuters feeds resolve to one `Reuters` outlet row, while remaining distinct in
   `feed_sightings`.
3. A feed failing does not reduce what other feeds capture, and shows in the tally.
4. The brief's output is byte-identical across the `fetch_rss` split.
5. A disabled pass is distinguishable from one that never fired, via `capture_runs.enabled`.
6. **Roll-off per feed is computable from `feed_sightings` alone, with no new instrumentation** —
   demonstrated by a test over three simulated passes before any live run, not asserted.
7. The two Nitter feeds do not 429 each other at 48 passes/day.

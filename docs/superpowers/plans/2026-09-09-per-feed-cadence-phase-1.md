# Per-Feed Capture Cadence, Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll each RSS feed on its own interval instead of polling all 28 every 30 minutes, and rework `failing_feeds` so slowed feeds are still monitored correctly.

**Architecture:** An optional `poll_every_minutes` key on `RSS_FEEDS` entries. `capture.run` asks `due_feeds` which feeds have gone longer than their interval since their last *attempt*, and polls only those. Every read fails open to nominal cadence, so any failure in this subsystem degrades to today's behaviour. `failing_feeds` swaps its single global tolerance for a per-feed one derived from the same interval.

**Tech Stack:** Python 3.14, psycopg 3, pytest. No new dependencies, no migration, no new top-level module.

**Spec:** `docs/superpowers/specs/2026-09-09-per-feed-cadence-design.md` — §2.1 (phasing), §3A (Phase 1), §3.2 (the ceiling), §7 (due-check), §8 (alerting), §9 (testing), §10.1 (rollout).

> **REVISED TWICE after red-team review** (`…-phase-1-redteam.md`, `…-phase-1-redteam-2.md`).
>
> Round 1 found two tests that could not pass, a false claim about which file tests `failing_feeds`, a hardcoded ceiling violating this plan's own Global Constraints, hand-rolled fixtures duplicating existing helpers, and a Self-Review claiming coverage of a spec section no task implemented.
>
> **Round 2 found the most serious defect in the work: as first written, this plan would have halved capture's cadence in production with zero keys set** — the opposite of the "behaviour is identical to today" that makes the rollout safe. `previous_fire` snaps capture to a fixed 30-minute grid (`scheduler.py:96`) while `polled_at` defaults to Postgres `now()` taken *mid-pass*, so a bare `now - polled_at >= interval` is false for every untuned feed at every tick. **None of the planned tests could have caught it** — they all inject `now` and construct elapsed times directly, so none of them ever meets the grid. §Task 2 now carries the half-tick slack and the regression test for it. Round 2 also found `run` reading the wall clock against a fixture pinned at `NOW`, and `deadline` rows being counted as attempts.

## Global Constraints

- **The ceiling is derived from nominal, never hardcoded.** Spec §3.2: every turnover figure behind these intervals comes from spans of at most `nominal × MAX_GAP_FACTOR` = 45 minutes (`scripts/measure_roll_off.py:151`). What the evidence fixes is the **extrapolation factor** (~2.7×), not an absolute number — so the ceiling is `_interval_minutes() * 4`, and retiming capture carries the evidence with it.
- **Nothing in this phase creates a table, a migration, a scheduler job, a knob, or a top-level module.** All new code lives in `capture.py`. Phase 2 moves it to `feed_cadence.py`.
- **Every read fails open.** A missing key, a wrong type, an unreadable table, or any exception means poll at nominal. Spec §4.4, §7.3.
- **Google News feeds are never slowed**, whatever they declare — relevance ranking under a 100-entry cap makes turnover unmeasurable in principle. Spec §4.3.
- **`_interval_minutes()` is the only source of "nominal".** Never hardcode 30.
- **Reuse the fixtures that exist.** `tests/test_capture_store.py` already defines `NOW` (line 392), `INTERVAL` (393), `_run` (396), `_poll` (567) and `TOLERANCE` (692). Do not write new ones.
- **Knobs are read as `common.X`**, never `from common import X`.
- **Do not set any `poll_every_minutes` values in this plan.** The code ships with no feed declaring one, so behaviour is identical to today. Populating them is rollout step 3, an operator action. Spec §10.1.
- **Pre-push gate is three commands**, and `ruff format` edits in place — `git add` every file it touches:
  ```
  ruff check .
  ruff format --check .
  pytest -q
  ```
- **`pytest` alone is not a pass.** DB-backed tests skip without a database and report green. Before the final commit run against a real Postgres and confirm the skip count is 0:
  ```
  docker run --rm -d --name nb-test-pg -p 5432:5432 -e POSTGRES_PASSWORD=newsbrief \
    -e POSTGRES_USER=newsbrief -e POSTGRES_DB=newsbrief_test postgres:18-alpine
  export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
  ```
  Never `docker compose up -d` in this repo — it starts a second Telegram `getUpdates` consumer and 409s the live bot.
- **Commit straight to `main`.** Solo repo, no branch. Stage explicit paths, never `git add -A`.
- **Run Python as `py`**, not `python`.

---

## File Structure

| file | responsibility in this phase |
|---|---|
| `capture.py` | everything new: `rank_ordered`, `max_poll_interval_minutes`, `poll_interval_minutes`, `due_feeds`, the `run` skip, the `failing_feeds` tolerance |
| `brief.py` | nothing — `poll_every_minutes` keys are added at rollout, not here |
| `tests/test_capture.py` | **no module-level skipmark.** Every test needing no database: the interval accessor, the pinned-vs-native control, the real-list invariants, the fail-open path, `feeds_total` |
| `tests/test_capture_store.py` | **carries `pytestmark = skipif(not db.is_configured())` at line 13.** Only tests that genuinely need a database |

**Placement rule, and it has already bitten this plan once:** put a test where its *dependencies* are, not where its subject is. A test needing no database, dropped into `test_capture_store.py`, sits behind that skipmark. CI runs a `postgres:18-alpine` service and exports `DATABASE_URL` so it would still run there — but it vanishes from every local `pytest -q`, which is the loop where the fail-open safety net most wants covering.

---

## Task 1: The interval accessor, and the guard that a pinned feed is never slowed

**Files:**
- Modify: `capture.py` (add after `capture_sources`, before `run`)
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `common.feed_host`, `capture._interval_minutes()` (`capture.py:387`)
- Produces: `capture.rank_ordered(feed: dict) -> bool`, `capture.max_poll_interval_minutes() -> int`, `capture.poll_interval_minutes(feed: dict) -> int`, `capture.POLL_INTERVAL_KEY: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`:

```python
# --- Per-feed poll intervals (news-brief-b42.5 Phase 1).


def _native(**extra):
    return {"name": "N", "url": "https://example.com/rss", **extra}


def _google_news(**extra):
    return {
        "name": "GN",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aexample.com",
        **extra,
    }


def test_a_feed_with_no_declared_interval_polls_at_nominal():
    assert capture.poll_interval_minutes(_native()) == capture._interval_minutes()


def test_a_declared_interval_is_honoured():
    assert capture.poll_interval_minutes(_native(poll_every_minutes=90)) == 90


def test_a_declared_interval_above_the_ceiling_is_clamped():
    assert (
        capture.poll_interval_minutes(_native(poll_every_minutes=9999))
        == capture.max_poll_interval_minutes()
    )


def test_the_ceiling_is_derived_from_nominal_rather_than_hardcoded():
    """Spec section 3.2. The evidence bound is nominal x MAX_GAP_FACTOR = 45
    minutes, so what the measurement fixes is the EXTRAPOLATION FACTOR, not an
    absolute number. A hardcoded 120 silently collapses every declared interval
    if capture is ever retimed to 60, and makes the valid range empty at 120."""
    assert capture.max_poll_interval_minutes() == capture._interval_minutes() * 4
    assert capture.max_poll_interval_minutes() > capture._interval_minutes()


def test_a_declared_interval_below_nominal_falls_back_to_nominal():
    """The scheduler tick is the floor: a smaller number is a promise the
    schedule cannot keep."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes=5)) == (
        capture._interval_minutes()
    )


def test_a_non_integer_declared_interval_falls_back_to_nominal():
    """Fail open: a bad value means today's behaviour, never a crash in the poll
    loop. `True` is included because bool is an int subclass and would otherwise
    be read as a one-minute interval."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes="fast")) == (
        capture._interval_minutes()
    )
    assert capture.poll_interval_minutes(_native(poll_every_minutes=True)) == (
        capture._interval_minutes()
    )


def test_a_rank_ordered_feed_is_never_slowed_even_when_it_declares_an_interval():
    """Google News returns RELEVANCE-ranked results under a 100-entry cap, so an
    item missing from a poll may never have departed: turnover is unmeasurable
    in principle, and such a feed stays at nominal whatever it declares."""
    assert capture.poll_interval_minutes(_google_news(poll_every_minutes=120)) == (
        capture._interval_minutes()
    )


def test_a_native_feed_declaring_THE_SAME_interval_is_slowed():
    """The control for the test above. Without it, an implementation that never
    slows anything passes the pinned-feed assertion."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes=120)) == 120


def test_no_rank_ordered_feed_in_the_real_list_declares_an_interval():
    """Self-maintaining: a ninth Google News proxy tests itself. The two control
    assertions keep it honest -- without them it passes on a predicate that is
    constantly False."""
    assert any(capture.rank_ordered(f) for f in brief.RSS_FEEDS), "control failed"
    assert any(not capture.rank_ordered(f) for f in brief.RSS_FEEDS), "control failed"
    for f in brief.RSS_FEEDS:
        if capture.rank_ordered(f):
            assert capture.POLL_INTERVAL_KEY not in f, (
                f"{f['name']} is relevance-ranked and cannot carry an interval"
            )


def test_every_declared_interval_in_the_real_list_is_within_range():
    """VACUOUS TODAY, and kept knowingly: no feed declares an interval until
    rollout. It is the assertion that fires when someone pastes a 240 back in."""
    for f in brief.RSS_FEEDS:
        declared = f.get(capture.POLL_INTERVAL_KEY)
        if declared is not None:
            assert isinstance(declared, int) and not isinstance(declared, bool), f
            assert (
                capture._interval_minutes()
                < declared
                <= capture.max_poll_interval_minutes()
            ), f["name"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```
py -m pytest tests/test_capture.py -q -k "interval or rank_ordered or ceiling" 2>&1 | tail -5
```

Expected: FAIL with `AttributeError: module 'capture' has no attribute 'poll_interval_minutes'`. If any test *passes* here, stop — it is testing something that already exists.

- [ ] **Step 3: Write the minimal implementation**

Add to `capture.py` after `capture_sources()` and before `run()`:

```python
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
# A FACTOR, not an absolute: the evidence bound itself scales with nominal, so
# a hardcoded ceiling would leave the reasoning behind the moment capture is
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
    nominal -- exactly today's behaviour. The only way to be slowed is to
    declare a valid interval above nominal and not be relevance-ranked.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```
py -m pytest tests/test_capture.py -q 2>&1 | tail -3
```

Expected: all pass, no new skips.

- [ ] **Step 5: Commit**

```bash
ruff check . && ruff format .
git add capture.py tests/test_capture.py
git commit -F - <<'MSGEOF'
feat(capture): a per-feed poll interval, with a ceiling tied to its evidence

An optional poll_every_minutes on a feed dict -- the seam capture_url, outlet
and kind already use. Nothing declares one yet, so behaviour is unchanged.

The ceiling is 4x nominal, not the 240 the b42.2 summary quotes. poll_pairs
discards every pair wider than nominal x MAX_GAP_FACTOR = 45 minutes, so every
turnover figure behind these intervals is measured over spans of at most 45
minutes and 240 was a 5.3x extrapolation. It is expressed as a FACTOR because
the evidence bound scales with nominal too: a hardcoded 120 would silently
collapse every declared interval if capture were retimed to 60, and make the
valid range empty at 120.

Relevance-ranked feeds are never slowed: Google News caps at 100 entries and
orders by relevance, so an item missing from a poll may never have departed.
Derived from the URL, not a name list -- and deliberately not "has a
capture_url", which is four feeds where this is eight.

Refs news-brief-b42.5
MSGEOF
```

---

## Task 2: `due_feeds` — the read, keyed on last attempt, failing open

**Files:**
- Modify: `capture.py` (after `poll_interval_minutes`)
- Test: `tests/test_capture_store.py` (DB-backed), `tests/test_capture.py` (fail-open)

**Interfaces:**
- Consumes: `capture.poll_interval_minutes` (Task 1)
- Produces: `capture.due_feeds(conn, feeds: list[dict], now) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture_store.py`, reusing the existing `NOW`, `INTERVAL`, `_run` and `_poll` helpers:

```python
# --- due_feeds (news-brief-b42.5 Phase 1).

SLOW = {"name": "Slow", "url": "https://slow.example/rss", "poll_every_minutes": 120}
DEFAULT = {"name": "Default", "url": "https://default.example/rss"}


def test_a_feed_polled_inside_its_interval_is_not_due(store):
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", minutes_ago=10)
    store.commit()
    assert capture.due_feeds(store, [SLOW], NOW) == []


def test_a_feed_polled_longer_ago_than_its_interval_is_due(store):
    """The presence sibling. Without it, the test above passes against a
    due_feeds that returns nothing at all."""
    run = _run(store, minutes_ago=200)
    _poll(store, run, "Slow", minutes_ago=130)
    store.commit()
    assert capture.due_feeds(store, [SLOW], NOW) == [SLOW]


def test_a_feed_polled_exactly_one_interval_ago_is_due(store):
    """The boundary, pre-registered in spec section 9.5 as the mutation that
    would otherwise fail zero tests: flipping >= to > is invisible without it."""
    run = _run(store, minutes_ago=200)
    _poll(store, run, "Slow", minutes_ago=120)
    store.commit()
    assert capture.due_feeds(store, [SLOW], NOW) == [SLOW]


def test_a_feed_that_has_never_been_polled_is_due(store):
    assert capture.due_feeds(store, [SLOW], NOW) == [SLOW]


def test_a_feed_with_no_declared_interval_is_due_after_nominal(store):
    """A feed nobody has tuned behaves exactly as it does today."""
    run = _run(store, minutes_ago=INTERVAL + 5)
    _poll(store, run, "Default", minutes_ago=INTERVAL + 1)
    store.commit()
    assert capture.due_feeds(store, [DEFAULT], NOW) == [DEFAULT]


def test_a_failed_poll_still_counts_as_an_attempt(store):
    """Due-ness is keyed on the last ATTEMPT, not the last success. Keying on
    success would retry a broken feed every tick until it recovered -- the worst
    response to a 429, and pointless against a 403, which must never be
    retried."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", failure="http_403", minutes_ago=10)
    store.commit()
    assert capture.due_feeds(store, [SLOW], NOW) == []


def test_a_deadline_row_does_NOT_count_as_an_attempt(store):
    """`run` writes a deadline row for a feed the pass ran out of time to reach.
    Nothing left the box, so it is not an attempt. Counting it would let a
    skipped pass consume the feed's slot -- doubling its real gap while
    feed_polls still shows a row per interval and every downstream reader
    reports the cadence as honoured. Silent by construction."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", failure="deadline", minutes_ago=10)
    store.commit()
    assert capture.due_feeds(store, [SLOW], NOW) == [SLOW]


def test_with_no_keys_set_every_feed_is_due_on_every_scheduler_tick(store):
    """THE REGRESSION TEST FOR SPEC 10.1's SAFETY PROPERTY: with no feed
    declaring an interval, behaviour must be identical to today.

    It is not automatic. previous_fire snaps capture to a fixed 30-minute grid
    (scheduler.py:96) while polled_at defaults to Postgres now() taken MID-pass,
    so a feed polled five seconds into the previous pass shows 29m55s elapsed at
    the next fire. A bare `>= interval` finds it NOT due and defers it a whole
    tick, halving the fleet's cadence to 60 minutes with zero keys set.

    The seconds here are the point -- do not round them away."""
    run = _run(store, minutes_ago=INTERVAL)
    _poll(store, run, "Default", minutes_ago=INTERVAL)
    # The previous pass reached this feed five seconds after it started.
    store.execute(
        "UPDATE feed_polls SET polled_at = %s WHERE source_name = 'Default'",
        (NOW - timedelta(minutes=INTERVAL) + timedelta(seconds=5),),
    )
    store.commit()

    assert capture.due_feeds(store, [DEFAULT], NOW) == [DEFAULT], (
        "an untuned feed missed its tick: the fleet just halved its cadence"
    )


def test_a_slowed_feed_fires_every_kth_tick_and_not_more_often(store):
    """The control for the slack: it must not be so generous that a 120-minute
    feed fires early. At four ticks of 30 minutes, it is due at the fourth and
    at no earlier one."""
    run = _run(store, minutes_ago=200)
    for ticks, expected in ((1, []), (2, []), (3, []), (4, [SLOW])):
        store.execute("DELETE FROM feed_polls WHERE source_name = 'Slow'")
        _poll(store, run, "Slow", minutes_ago=INTERVAL * ticks)
        store.execute(
            "UPDATE feed_polls SET polled_at = polled_at + interval '5 seconds' "
            "WHERE source_name = 'Slow'"
        )
        store.commit()
        assert capture.due_feeds(store, [SLOW], NOW) == expected, f"{ticks} ticks"
```

Append to `tests/test_capture.py` — **not** the store file, because it needs no database:

```python
def test_the_due_check_fails_open_when_the_table_cannot_be_read():
    """Every failure in this subsystem degrades to today's behaviour. The worst
    case spends requests, the only resource it was ever optimising.

    Lives here rather than in test_capture_store.py: it needs no database, and
    that module's skipmark would hide it from every local pytest run."""

    class _BrokenConn:
        def execute(self, *a, **kw):
            raise RuntimeError("relation feed_polls does not exist")

    feeds = [
        {"name": "A", "url": "https://a.example/rss", "poll_every_minutes": 120},
        {"name": "B", "url": "https://b.example/rss"},
    ]
    assert capture.due_feeds(_BrokenConn(), feeds, datetime.now(timezone.utc)) == feeds
```

`tests/test_capture.py` must import `datetime` and `timezone` if it does not already — check its import block and add only what is missing.

- [ ] **Step 2: Run the tests to verify they fail**

```
export DATABASE_URL="postgresql://newsbrief:newsbrief@localhost:5432/newsbrief_test"
py -m pytest tests/test_capture.py tests/test_capture_store.py -q -k due 2>&1 | tail -5
```

Expected: FAIL with `AttributeError: module 'capture' has no attribute 'due_feeds'`.
If the store tests report **skipped**, the database is not configured — a skip is not a pass.

- [ ] **Step 3: Write the minimal implementation**

Add to `capture.py` after `poll_interval_minutes`:

```python
def due_feeds(conn, feeds: list[dict], now) -> list[dict]:
    """The feeds whose interval has elapsed since their last ATTEMPT.

    Last attempt rather than last success, deliberately. Keying on success would
    retry a broken feed every tick until it recovered: the worst possible
    response to a 429, and pointless against a 403, which
    `source-fetch-failure-modes` says must never be retried. Attempt-keying
    makes a feed's request rate equal its interval regardless of health, and
    `failing_feeds` -- not the retry -- is what reports the breakage.

    A `deadline` row is NOT an attempt. `run` writes one for a feed the pass ran
    out of time to reach, so nothing left the box -- counting it would let a
    pass that skipped a feed consume that feed's slot, doubling its real gap
    while feed_polls still shows a row per interval and every downstream reader
    reports the cadence as honoured.

    HALF A TICK OF SLACK, and it is load-bearing rather than a fudge. Due-ness is
    evaluated on the scheduler's discrete grid -- `previous_fire` snaps capture
    to a fixed 30-minute boundary (scheduler.py:96) -- while `polled_at` defaults
    to Postgres now() taken MID-pass. So a feed polled five seconds into the
    11:30 pass shows 29m55s elapsed at the 12:00 fire, and a bare `>= interval`
    would find it not due and defer it a whole tick. With no keys set at all that
    silently halves the fleet's cadence to 60 minutes -- the opposite of the
    "behaviour identical to today" this phase promises. Half a tick is the
    correct rounding for a threshold sampled on a grid: it makes `interval ==
    nominal` fire every tick, and `interval == k * nominal` fire every k-th.

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
```

`timedelta` is already imported at `capture.py:20`.

- [ ] **Step 4: Run the tests to verify they pass**

```
py -m pytest tests/test_capture.py tests/test_capture_store.py -q 2>&1 | tail -3
```

Expected: all pass, **0 skipped**.

- [ ] **Step 5: Commit**

```bash
ruff check . && ruff format .
git add capture.py tests/test_capture.py tests/test_capture_store.py
git commit -F - <<'MSGEOF'
feat(capture): ask which feeds are actually due, and fail open when unsure

due_feeds keys on the last ATTEMPT, not the last success. Success-keying would
retry a broken feed every tick until it recovered -- the worst response to a
429 and pointless against a 403, which must never be retried. Attempt-keying
makes a feed's request rate equal its interval regardless of health;
failing_feeds is what reports breakage, not the retry.

Every failure path returns every feed, so an unreadable table, a missing row
or any exception degrades to today's behaviour. The worst case spends
requests, which is the only resource this was optimising. That test lives in
test_capture.py rather than test_capture_store.py: it needs no database, and
the store module's skipmark would hide it from every local run.

Nothing calls it yet.

Refs news-brief-b42.5
MSGEOF
```

---

## Task 3: `capture.run` polls only what is due

**Files:**
- Modify: `capture.py` — `Tally` (`capture.py:169`), `run` (`capture.py:292`)
- Test: `tests/test_capture.py`, `tests/test_capture_store.py`, `tests/test_common.py`

**Interfaces:**
- Consumes: `capture.due_feeds` (Task 2)
- Produces: `Tally.feeds_not_due: int`; `capture.run(conn, spacer=None, now=None) -> Tally`, polling only due feeds

- [ ] **Step 1: Write the failing tests**

First, to `tests/test_common.py` — the gap that let a broken assertion into the previous version of this plan:

```python
def test_host_spacer_keeps_a_constant_gap_across_three_fetches_of_one_host():
    """Two waits cannot distinguish a constant gap from a compounding one. On a
    frozen fake clock this implementation returns [5, 10], which is an artifact
    of the fake -- a real clock advances while sleeping. The sleeper here
    advances the clock as a real one does, which is what a spacer test must
    model."""
    waited = []
    clock = FakeClock()

    def sleeper(seconds):
        waited.append(seconds)
        clock.t += seconds

    spacer = common.HostSpacer(5, clock=clock, sleeper=sleeper)
    for _ in range(3):
        spacer.wait({"url": "https://one.example/f"})

    assert waited == [5, 5]
```

Then to `tests/test_capture_store.py`:

```python
def test_a_not_due_feed_is_skipped_and_writes_no_poll_row(store, monkeypatch):
    """A not-due feed was NOT polled, so it must not leave a feed_polls row.
    Writing one would corrupt the history the cadence measurement reads --
    entries_seen denominators and poll counts all assume a row means an
    attempt."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", minutes_ago=10)
    store.commit()
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "capture_sources", lambda: [SLOW])
    monkeypatch.setattr(
        brief, "fetch_feed_entries", lambda f: pytest.fail("a not-due feed was fetched")
    )

    before = store.execute("SELECT count(*) FROM feed_polls").fetchone()[0]
    tally = capture.run(store, spacer=common.HostSpacer(0), now=NOW)
    after = store.execute("SELECT count(*) FROM feed_polls").fetchone()[0]

    assert after == before, "a skipped feed wrote a poll row"
    assert tally.feeds_not_due == 1


def test_a_due_feed_is_still_polled_and_still_writes_a_row(store, monkeypatch):
    """The presence sibling: without it, a run that polls nothing at all passes
    the test above."""
    run = _run(store, minutes_ago=200)
    _poll(store, run, "Slow", minutes_ago=200)
    store.commit()
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "capture_sources", lambda: [SLOW])
    monkeypatch.setattr(
        brief, "fetch_feed_entries", lambda f: brief.FeedFetch(entries=[], failure=None)
    )

    before = store.execute("SELECT count(*) FROM feed_polls").fetchone()[0]
    tally = capture.run(store, spacer=common.HostSpacer(0), now=NOW)
    after = store.execute("SELECT count(*) FROM feed_polls").fetchone()[0]

    assert after == before + 1
    assert tally.feeds_not_due == 0
    assert tally.feeds_ok == 1
```

Then to `tests/test_capture.py`:

```python
def test_feeds_total_still_counts_every_source_not_just_the_due_ones(monkeypatch):
    """feeds_total is persisted to capture_runs and read by the health surface.
    Redefining it as "the due ones" would silently change what every historical
    row means."""
    feeds = [
        {"name": f"F{i}", "url": f"https://h{i}.example/f", "category": "geo"}
        for i in range(5)
    ]
    monkeypatch.setattr(capture, "capture_sources", lambda: feeds)
    monkeypatch.setattr(capture, "due_feeds", lambda conn, fs, now: fs[:2])
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "start_run", lambda conn, enabled: 1)
    monkeypatch.setattr(capture, "finish_run", lambda conn, run, tally: None)
    monkeypatch.setattr(
        capture, "record_poll", lambda conn, run, name, failure, seen: None
    )
    monkeypatch.setattr(
        brief,
        "fetch_feed_entries",
        lambda f: brief.FeedFetch(entries=[], failure="http_403"),
    )

    tally = capture.run(conn=_CommitOnlyConn(), spacer=common.HostSpacer(0))

    assert tally.feeds_total == 5, "every source is still counted"
    assert tally.feeds_not_due == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

```
py -m pytest tests/test_common.py tests/test_capture.py tests/test_capture_store.py -q \
  -k "not_due or feeds_total or three_fetches" 2>&1 | tail -6
```

Expected: `AttributeError: 'Tally' object has no attribute 'feeds_not_due'` for the capture tests. The `test_common.py` one should **pass immediately** — it characterises existing correct behaviour and exists to stop a future frozen-clock fixture from reading as a compounding bug. That is the one place in this plan where a passing test at Step 2 is expected; note it and move on.

- [ ] **Step 3: Write the minimal implementation**

In `capture.py`, add a field to `Tally` beside the other non-persisted counters:

```python
    # Not persisted: capture_runs has no items_already/items_failed columns
    # (migration 0008 is already applied), so these live in the log line only.
    items_already: int = 0
    items_failed: int = 0
    # Same -- no column, log only. A skip is neither a failure nor a poll, so it
    # needs its own name: "28 feeds, 3 ok" with no third number is exactly the
    # ambiguity capture_runs exists to remove.
    feeds_not_due: int = 0
    sources_dropped: int = 0
```

In `run`, replace:

```python
    feeds = common.order_by_host(capture_sources())
    tally.feeds_total = len(feeds)
```

with:

```python
    sources = capture_sources()
    tally.feeds_total = len(sources)
    # Due-ness first, ordering second: order_by_host must interleave the set
    # actually being fetched, not the full list.
    feeds = common.order_by_host(due_feeds(conn, sources, now))
    tally.feeds_not_due = len(sources) - len(feeds)
```

The signature also gains an injectable clock, for the same reason `spacer` is
injectable — `tests/test_capture_store.py` pins time at `NOW = 2026-09-03 12:00`
and inserts poll rows relative to it, so a `run` that read the wall clock would
see every fixture row as days stale and poll everything:

```python
def run(conn, spacer=None, now=None) -> Tally:
```

with, beside the other defaults:

```python
    now = now or datetime.now(timezone.utc)
```

`capture.py:20` imports only `timedelta` from `datetime` — widen it to
`from datetime import datetime, timedelta, timezone`.

Add the skip count to the log line at the end of `run`:

```python
        f"{tally.items_already} already held, {tally.items_failed} failed, "
        f"{tally.feeds_not_due} not due, "
        f"{tally.sources_dropped} sources dropped"
```

- [ ] **Step 4: Run the tests to verify they pass**

```
py -m pytest tests/test_common.py tests/test_capture.py tests/test_capture_store.py -q 2>&1 | tail -3
```

Expected: all pass, 0 skipped.

- [ ] **Step 5: Commit**

```bash
ruff check . && ruff format .
git add capture.py tests/test_capture.py tests/test_capture_store.py tests/test_common.py
git commit -F - <<'MSGEOF'
feat(capture): poll only the feeds that are due

A not-due feed writes NO feed_polls row, because it was not polled. Writing one
would corrupt the history the cadence measurement reads -- entries_seen
denominators, poll counts and the coverage guard all assume a row means an
attempt. Tally.feeds_not_due carries the skip instead, so a pass still reports
what it deliberately did not do.

feeds_total keeps counting every source. It is persisted to capture_runs and
read by the health surface, so redefining it as "the due ones" would silently
change what every historical row means.

Due-ness is resolved BEFORE ordering, so order_by_host interleaves the set
actually being fetched.

Also adds the three-fetch HostSpacer test. Two waits cannot tell a constant gap
from a compounding one, and a frozen fake clock makes a correct implementation
look like it compounds -- which sent one draft of this work chasing a bug that
was in the fixture.

Refs news-brief-b42.5
MSGEOF
```

---

## Task 4: `failing_feeds` gets a per-feed tolerance

**Files:**
- Modify: `capture.py` — `failing_feeds` (`capture.py:453`)
- Test: `tests/test_capture_store.py`

**Interfaces:**
- Consumes: `capture.poll_interval_minutes` (Task 1), `capture.capture_sources`
- Produces: `failing_feeds(conn, now, feeds=None) -> tuple[str, str] | None` — one new optional parameter

**Why this cannot ship separately:** `failing_feeds` filters on `last_try > cutoff` with a 90-minute tolerance derived from the *global* interval. That filter exists to stay quiet about a feed nobody polls any more, and it is correct **only** while every feed shares one interval. A 120-minute feed sits inside a 90-minute window for 75% of its cycle and is invisible for the rest. Shipping Tasks 1–3 without this runs production with its own detection disabled. Spec §1.3.

**Note on the existing tests:** `tests/test_capture_store.py:695–760` already covers `failing_feeds` using synthetic feed names (`Broken`, `Fine`, `Retired`, `Live`) that are **not** in `RSS_FEEDS`. They therefore fall back to nominal under the new code and must keep passing unchanged. If any of them breaks, the fallback is wrong — do not edit them to suit the implementation.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture_store.py`. `SLOW_TOLERANCE` is 120 × `capture.STALE_AFTER_INTERVALS` = 6 hours.

```python
SLOW_TOLERANCE = 120 * capture.STALE_AFTER_INTERVALS


def test_a_slowed_feed_is_not_alerted_inside_three_of_its_own_intervals(store):
    """Three hours of failure on a 120-minute feed is one and a half missed
    polls -- not yet news. Under the old global 90-minute tolerance this would
    have alerted."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", minutes_ago=180)
    _poll(store, run, "Slow", failure="http_500", minutes_ago=5)
    store.commit()
    assert capture.failing_feeds(store, NOW, feeds=[SLOW]) is None


def test_a_slowed_feed_IS_alerted_beyond_three_of_its_own_intervals(store):
    """The presence sibling, and the point of the task: the tolerance moved, it
    did not disappear."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Slow", minutes_ago=SLOW_TOLERANCE + 60)
    _poll(store, run, "Slow", failure="http_500", minutes_ago=5)
    store.commit()
    verdict = capture.failing_feeds(store, NOW, feeds=[SLOW])
    assert verdict is not None
    assert "Slow" in verdict[1]


def test_an_untuned_feed_keeps_todays_ninety_minute_tolerance(store):
    """Unknown interval falls back to nominal, so an untuned feed behaves exactly
    as it does now. Two separate feeds rather than two histories for one: the
    CTE takes max(polled_at) over successes, and a second, OLDER success cannot
    lower a max."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Recent", minutes_ago=TOLERANCE - 10)
    _poll(store, run, "Recent", failure="http_500", minutes_ago=5)
    _poll(store, run, "Stale", minutes_ago=TOLERANCE + 10)
    _poll(store, run, "Stale", failure="http_500", minutes_ago=5)
    store.commit()

    recent = {"name": "Recent", "url": "https://recent.example/rss"}
    stale = {"name": "Stale", "url": "https://stale.example/rss"}
    verdict = capture.failing_feeds(store, NOW, feeds=[recent, stale])

    assert verdict is not None
    assert "Stale" in verdict[1]
    assert "Recent" not in verdict[1]


def test_a_feed_absent_from_the_source_list_falls_back_to_nominal(store):
    """A poll row for a source no longer configured still gets judged, at
    nominal. It must not crash on the missing lookup, and it must not become
    un-alertable."""
    run = _run(store, minutes_ago=1)
    _poll(store, run, "Ghost", minutes_ago=TOLERANCE + 10)
    _poll(store, run, "Ghost", failure="http_500", minutes_ago=5)
    store.commit()
    verdict = capture.failing_feeds(store, NOW, feeds=[])
    assert verdict is not None
    assert "Ghost" in verdict[1]
```

- [ ] **Step 2: Run the tests to verify they fail**

```
py -m pytest tests/test_capture_store.py -q -k "slowed or untuned or absent_from" 2>&1 | tail -6
```

Expected: `TypeError: failing_feeds() got an unexpected keyword argument 'feeds'`.

- [ ] **Step 3: Write the minimal implementation**

Change the signature and the tolerance block in `failing_feeds`:

```python
def failing_feeds(conn, now, feeds=None) -> tuple[str, str] | None:
```

Replace:

```python
    tolerance = timedelta(minutes=_interval_minutes() * STALE_AFTER_INTERVALS)
    cutoff = now - tolerance
```

with:

```python
    # Per-feed now (news-brief-b42.5 Phase 1). STALE_AFTER_INTERVALS is reused
    # rather than copied -- the question is still "three missed polls", and three
    # missed polls of a 120-minute feed is six hours. A single global cutoff
    # would leave a slowed feed inside its own window for only part of its cycle
    # and invisible for the rest: the monitoring regression per-feed cadence
    # would otherwise ship (spec section 1.3).
    #
    # `feeds` is a parameter so the tolerance source is explicit and testable,
    # and so this alerting path does not read sources.json off the volume as a
    # hidden side effect.
    by_name = {f["name"]: f for f in (capture_sources() if feeds is None else feeds)}
    nominal = _interval_minutes()

    def _cutoff(source_name):
        feed = by_name.get(source_name)
        minutes = poll_interval_minutes(feed) if feed else nominal
        return now - timedelta(minutes=minutes * STALE_AFTER_INTERVALS)
```

Replace the `failing` comprehension:

```python
    failing = [
        (name, last_ok, fails, kind)
        for name, last_try, last_ok, fails, kind in rows
        if last_try > cutoff and (last_ok is None or last_ok < cutoff)
    ]
```

with:

```python
    failing = []
    for name, last_try, last_ok, fails, kind in rows:
        cutoff = _cutoff(name)
        if last_try > cutoff and (last_ok is None or last_ok < cutoff):
            failing.append((name, last_ok, fails, kind))
```

The summary line quoted a single `tolerance` that no longer exists. Replace the return so each feed carries its own:

```python
    lines = [
        f"   {name[:24]:<26}"
        + (f"last ok {_duration(now - last_ok)} ago" if last_ok else "never succeeded")
        + f", {fails} failures since ({kind})"
        + f", tolerance {_duration(now - _cutoff(name))}"
        for name, last_ok, fails, kind in failing
    ]
    return (
        "failing:" + ",".join(sorted(name for name, _, _, _ in failing)),
        f"{len(failing)} feed(s) have not polled successfully within their own "
        f"cadence:\n" + "\n".join(lines),
    )
```

Also update the docstring paragraph that describes the tolerance as global.

- [ ] **Step 4: Run the tests to verify they pass**

```
py -m pytest tests/test_capture_store.py tests/test_capture.py -q 2>&1 | tail -3
```

Expected: all pass, 0 skipped — **including the pre-existing `failing_feeds` tests at lines 695–760, unedited.**

- [ ] **Step 5: Run the full gate against a real Postgres**

```bash
ruff check .
ruff format --check .
py -m pytest -q
```

Confirm the skip count is **0**.

- [ ] **Step 6: Commit**

```bash
git add capture.py tests/test_capture_store.py
git commit -F - <<'MSGEOF'
fix(capture): give failing_feeds a per-feed tolerance, or it goes half blind

failing_feeds filtered on last_try > cutoff with a 90-minute tolerance derived
from the global interval. That filter exists to stay quiet about a feed nobody
polls any more, and its reasoning holds ONLY while every feed shares one
interval -- where "not polled recently" can only mean a human removed it.

Per-feed cadence breaks that. A 120-minute feed sits inside a 90-minute window
for 75% of its cycle and is invisible for the other 25%, so a failing feed
would alert intermittently or not at all, and the ABSENCE of an alert would
stop carrying information. That is "a dropped feed looks identical to a quiet
one", relocated one layer up where nothing about the scheduler looks wrong.

Tolerance is now each feed's own interval times STALE_AFTER_INTERVALS -- the
constant reused, not copied, because the question is still "three missed
polls". An untuned or unknown feed falls back to nominal and keeps today's 90
minutes, which is why the existing tests pass unedited. The summary line
carried a single tolerance that no longer exists, so each feed's own is printed
on its own line.

The source list is a parameter rather than a call, so the tolerance source is
explicit and this alerting path does not read sources.json off the volume as a
hidden side effect.

news-brief-b42.5
MSGEOF
```

---

## Deferred, explicitly

These are spec requirements this plan does **not** implement. They are listed so nobody mistakes silence for coverage — an earlier draft's Self-Review claimed one of them was done.

- **§9.4 — repointing the real-list interleave test at the due set.** In Phase 1 no feed declares an interval, so the due set *is* the full list and the repointed test would be identical to the current one. It becomes meaningful only once intervals are set, so it belongs to rollout step 2 or Phase 2. `tests/test_capture.py`'s real-list test is left as-is.
- **§9.5 — the pre-registered mutation run.** Worth doing once the four tasks are committed, as a separate exercise with its own count. Not a task here.
- **§4–§7.5 — the `feed_cadence` table, module, job, report, alerts and knobs.** Phase 2 by design (§2.1).

---

## After the plan

Phase 1 ships with **no feed declaring an interval**, so production behaviour is unchanged and the due-check is exercised in its fail-open path across the whole fleet. Rollout continues per spec §10.1:

1. Deploy. Confirm `Capture: … 0 not due` in the logs — the mechanism is live and inert.
2. Add `poll_every_minutes` to a few natives, widest first, from the b42.2 measurement **output** (not the bead's prose summary, whose arithmetic does not reconcile — spec §2).
3. Watch `single_sighting_fraction` in `scripts/measure_roll_off.py` and `failing_feeds` for a week.
4. Fill in the rest if the signal stays flat.

`b42.5` closes when intervals are in effect and that signal has been flat for a week — not when this plan is done. Phase 2 gets its own bead.

---

## Self-Review

**Spec coverage.** §3A key/ceiling/fail-open → Task 1. §7.1 last-attempt keying → Task 2. §7.2 no poll row + `feeds_not_due` → Task 3. §8.1 per-feed tolerance → Task 4. §9.2 self-maintaining real-list test → Task 1. §9.4 presence siblings → every task. §10.1 rollout → "After the plan". Everything else → "Deferred, explicitly".

**Type consistency.** `poll_interval_minutes(feed) -> int` is called under that name in Tasks 2 and 4. `max_poll_interval_minutes() -> int` is used in Task 1 only. `due_feeds(conn, feeds, now)` is monkeypatched in Task 3 with the same three-argument signature Task 2 defines. `failing_feeds(conn, now, feeds=None)` gains one optional parameter and every existing caller keeps working.

**Fixtures verified, not assumed.** `store` (`tests/test_capture_store.py:19`), `NOW` (392), `INTERVAL` (393), `_run` (396), `_poll` (567), `TOLERANCE` (692) all exist as used, and that module imports `datetime`, `timedelta` and `timezone` at line 3. `_CommitOnlyConn` is defined in `tests/test_capture.py`. `failing_feeds` is tested in `test_capture_store.py`, **not** `test_monitor.py`, which contains no reference to it.

**Known-vacuous test, kept knowingly.** `test_every_declared_interval_in_the_real_list_is_within_range` passes trivially until the first key is added at rollout. Retained because it is what fires when someone pastes a 240 back in, and labelled as such in its own docstring rather than left looking like coverage.

**One test is expected to pass at Step 2** — the three-fetch `HostSpacer` test in Task 3. It characterises existing correct behaviour and exists to stop a frozen-clock fixture from making a correct implementation look broken, which is what happened to the previous draft of this plan.

**Time is injected everywhere it is read.** `run(conn, spacer=None, now=None)` and `due_feeds(conn, feeds, now)` both take their clock, and `failing_feeds(conn, now, feeds=None)` already did. No path under test reads the wall clock, because `tests/test_capture_store.py` pins `NOW = 2026-09-03 12:00` and inserts every poll row relative to it — a `run` that called `datetime.now()` would see six-day-old fixtures and poll everything.

**The lesson from round 2, recorded because it generalises.** Every test in this plan constructs elapsed times directly, so every one of them would have passed while production silently halved its cadence. The bug lived in the *interaction* between two things neither test touched: a scheduler that fires on a fixed grid, and a `polled_at` written mid-pass. A test that supplies its own `now` cannot observe a defect whose cause is *when the real clock fires*. That is why `test_with_no_keys_set_every_feed_is_due_on_every_scheduler_tick` sets its poll row five seconds past the tick boundary — the seconds are the whole test, and rounding them to a tidy `minutes_ago=30` would delete it.

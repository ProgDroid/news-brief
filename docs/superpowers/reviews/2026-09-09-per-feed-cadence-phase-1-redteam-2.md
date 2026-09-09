# Red-team review #2 — Per-Feed Cadence, Phase 1 implementation plan (second draft)

**Plan under review:** `docs/superpowers/plans/2026-09-09-per-feed-cadence-phase-1.md` (revised)
**Spec:** `docs/superpowers/specs/2026-09-09-per-feed-cadence-design.md`
**First review:** `docs/superpowers/reviews/2026-09-09-per-feed-cadence-phase-1-redteam.md`
**Reviewer:** hostile staff engineer, fresh context, no attachment
**Date:** 2026-09-09
**Base:** `fd9d574` (clean tree)

Everything below was checked against the code, not against the plan's claims about the
code. Where the plan is right I say so, because a review that lists only faults gives no
way to tell "checked and fine" from "not checked".

---

## Part 0 — Did the revision fix the first review's findings, or move them?

| first review's finding | verdict |
|---|---|
| b1 — `waited == [5, 5]` impossible on a frozen clock | **FIXED, and correctly** |
| b2 — the 90-minute test's second assertion unreachable (`max()` cannot go backwards) | **FIXED, and correctly** |
| b2b — hand-rolled `_poll_row` duplicating `_poll`/`_run` | **FIXED** |
| b3 — false claim that `tests/test_monitor.py` exercises `failing_feeds` | **FIXED** |
| b3b — new `capture_sources()` dependency on the alerting path | **PARTLY** — hoisted out of the per-row path, but see finding 5 |
| b4 — fail-open test behind the DB skipmark | **FIXED** |
| (a) — Self-Review claimed §7.35/§9.4 coverage no task delivered | **FIXED** as a claim; the deferral's stated *reason* is false (finding 1) |
| (a2) — §9.5 mutation run dropped silently | **FIXED** — now in "Deferred, explicitly" |
| (c) — hardcoded `MAX_POLL_INTERVAL_MINUTES = 120` | **FIXED in code, MOVED into the test** (finding 4) |
| (c, runner-up) — `'deadline'` rows counted as poll attempts | **NOT ADDRESSED, and not deferred** (finding 3) |

Verified individually below.

### b1 is genuinely fixed

`FakeClock` exists at `tests/test_common.py:261`, has `.t` and `__call__`, and the
appended test sits after it, so it is reachable. Tracing the new sleeper against
`common.HostSpacer.wait` (`common.py:792-799`):

- `wait 1`: `now=0.0`, no `last` → `_last["one.example"] = 0.0`
- `wait 2`: `now=0.0`, `remaining = 5 - (0.0 - 0.0) = 5.0` → sleeps 5, `clock.t = 5.0`, `now = 5.0`
- `wait 3`: `now = 5.0`, `remaining = 5 - (5.0 - 5.0) = 5.0` → sleeps 5, `clock.t = 10.0`

`waited == [5.0, 5.0]`, and `[5.0, 5.0] == [5, 5]` is `True`. The assertion holds. The
docstring's explanation of *why* the frozen-clock idiom failed is also correct.

### b2 is genuinely fixed, and all four new `failing_feeds` tests hold

`_poll(store, run_id, source, failure=None, entries=0, minutes_ago=0)` is at
`tests/test_capture_store.py:567`; `_run(store, *, minutes_ago, ...)` at `:396`; `NOW` at
`:392` (frozen `2026-09-03 12:00 UTC`); `INTERVAL` at `:393`; `TOLERANCE = INTERVAL * 3`
= 90 at `:692`. Every call in the plan matches those signatures. `_poll` does **not**
commit, and every new test calls `store.commit()` — correct.

Traced against the CTE at `capture.py:472-475` (`last_ok = max(polled_at) WHERE failure IS NULL`)
and the filter `last_try > cutoff and (last_ok is None or last_ok < cutoff)`, with
`SLOW_TOLERANCE = 360`:

| test | last_try | last_ok | cutoff | failing? | asserted | ok |
|---|---|---|---|---|---|---|
| slowed, inside tolerance | NOW−5 | NOW−180 | NOW−360 | no | `is None` | ✔ |
| slowed, beyond tolerance | NOW−5 | NOW−420 | NOW−360 | yes | not None, "Slow" | ✔ |
| untuned "Recent" | NOW−5 | NOW−80 | NOW−90 | no | absent | ✔ |
| untuned "Stale" | NOW−5 | NOW−100 | NOW−90 | yes | present | ✔ |
| "Ghost", `feeds=[]` | NOW−5 | NOW−100 | NOW−90 | yes | present | ✔ |

Using two feed names instead of two histories for one is exactly the right fix. None of
the five synthetic names (`Slow`, `Recent`, `Stale`, `Ghost`, plus the pre-existing set)
collides with any of the 28 entries in `brief.RSS_FEEDS` — checked by execution.

### The pre-existing `failing_feeds` tests do still pass (plan's claim at line 676, verified)

`tests/test_capture_store.py:695-760` and `:824` use `Broken`, `Fine`, `Retired`, `Live`,
`NeverWorked`, `First`, `Second`. None is in `brief.RSS_FEEDS`, so `by_name.get(name)` is
`None` and each falls back to `nominal = 30`, i.e. today's 90-minute tolerance. None
asserts on the `"have not polled successfully in {tolerance}"` sentence the plan deletes;
they assert on names in `verdict[1]` and on episode keys. **The plan's claim is true.**

### Reachability of the new tests

- `_CommitOnlyConn` — `tests/test_capture.py:446`, module level, and every appended test
  lands after it. **Reachable.**
- `FakeClock` — `tests/test_common.py:261`, same. **Reachable.**
- `tests/test_capture.py` imports `re, brief, capture, common, scheduler` and **not**
  `datetime`/`timezone` (`tests/test_capture.py:1-9`). The plan flags this and tells the
  executor to add what is missing, but gives no import line — a small friction, not a
  defect.
- `tests/test_capture.py` carries **no** module-level skipmark. Correct.
- `tests/test_capture_store.py` carries `pytestmark` at `:13-15`. Correct.

### Signature and monkeypatch consistency

- `monkeypatch.setattr(capture, "due_feeds", lambda conn, fs, now: fs[:2])` matches
  `due_feeds(conn, feeds, now)` and `run` will call it as a bare module global.
- `record_poll` stub `lambda conn, run, name, failure, seen` matches the positional call
  at `capture.py:320`/`:329`/`:341`/`:346`.
- `failing_feeds(conn, now, feeds=None)` — the only production caller is
  `brief.py:4163-4166`, which calls `produce(conn, now)`. Compatible.
- The Task 4 edit anchor is disambiguated by `cutoff = now - tolerance`; the bare
  `tolerance = timedelta(...)` line is byte-identical at `capture.py:422` inside
  `liveness`. The plan gives both lines, so an `Edit` will be unique.
- `Tally.feeds_not_due` is inserted between `items_failed` and `sources_dropped`. Nothing
  constructs `Tally` positionally and `finish_run` (`capture.py:200-210`) names its
  fields, so the insertion is safe.
- `rank_ordered` is host-derived, and the four `capture_url` overrides
  (`brief.py:170,185,276,300`) are **also** `news.google.com`, so the predicate agrees
  over `brief.RSS_FEEDS` and over `capture_sources()`'s substituted output. I went looking
  for a divergence here and there is none.

---

## (a) The due-check, as written, is not "identical to today's behaviour" — it halves the fleet's poll rate on the deploy before any interval is set

This is the objection. It is not about a test; it is about production.

**Spec §10.1 step 2:** "Deploy with no keys set — behaviour is identical to today."
**Plan line 892:** "Phase 1 ships with **no feed declaring an interval**, so production
behaviour is unchanged."
**Plan line 894:** "Deploy. Confirm `Capture: … 0 not due` in the logs."

All three are false under the implementation in Task 2 and Task 3.

**The mechanism.** Three facts, each checked:

1. `capture` is an `interval` schedule of 30 minutes (`scheduler.py:74`), and
   `previous_fire` for `kind == "interval"` snaps to a **fixed grid from midnight**
   (`scheduler.py:94-97`). Capture fires at `:00` and `:30`, not "30 minutes after the
   last pass finished".
2. Task 3 computes the due set **once, at the top of the pass**:
   `due_feeds(conn, sources, datetime.now(timezone.utc))`.
3. `record_poll` (`capture.py:214-219`) does not pass `polled_at`; the column defaults to
   Postgres `now()` (`migrations/0008_capture_telemetry_up.sql:37`), i.e. the moment the
   feed was actually fetched — which is *later in the pass* than the `now` in (2).

Let pass *N* start at grid time `T` plus dispatch latency `j` (the supervisor ticks every
`TICK_SECONDS = 30`, `scheduler.py:20`). Feed *f* is polled at `T + j + d_f`, where `d_f`
is how far into the loop *f* sits. Pass *N+1* starts at `T + 30min + j`. Then:

```
now - polled_at  =  30min - d_f   <  30min   for every feed with d_f > 0
```

`due_feeds` requires `>= timedelta(minutes=30)`. So **every feed except whatever is
fetched in the first fraction of a second is not due at the next fire**, and becomes due
only at pass *N+2*. The fleet settles into a deterministic every-other-pass skip:
**an effective 60-minute cadence for all 28 feeds, immediately, with zero keys set.**

`d_f > 0` is not marginal. A pass is 28 network fetches with `HOST_GAP_SECONDS = 5`
sleeps between same-host pairs, and `DEADLINE_SECONDS = 600` exists precisely because
passes run into minutes. `j` is bounded by one 30-second supervisor tick. `j >= d_f` is
false for essentially the whole list.

**Three consequences, in increasing order of expense:**

- The rollout's first checkpoint is unsatisfiable. Step 1 tells the operator to confirm
  `0 not due`; they will see roughly 20–27 on alternating passes and have no way to tell
  a bug from the mechanism working.
- `single_sighting_fraction` — the *only* live signal §3.2 nominates to validate the
  hand-set intervals — degrades on the same deploy, before any interval exists to
  attribute it to. The validation instrument is contaminated by the change it is meant to
  validate.
- `scripts/measure_roll_off.py`'s `poll_pairs` (`:151`) discards every consecutive pair
  wider than `nominal * MAX_GAP_FACTOR` = 45 minutes. At an effective 60-minute cadence
  **every pair is discarded** and the measurement that Phase 2 is built on returns
  nothing — silently, since an empty pair list is not an error.

**Also falsified by the same mechanism:** the plan's "Deferred, explicitly" rationale for
§9.4 — "In Phase 1 no feed declares an interval, so the due set *is* the full list and the
repointed test would be identical to the current one." The due set is not the full list;
it is roughly half of it, alternating. Whether or not the repoint is deferred, the reason
given for deferring it is not true, and `tests/test_capture.py:262`'s docstring ("Kept
here against the REAL list, **which is what capture orders**") becomes false the moment
Task 3 lands.

**The fix is small**, which is the argument for doing it now rather than discovering it in
production: subtract a grace from the comparison, so due-ness is a property of the
*schedule grid* rather than of intra-pass timing. Something like

```python
GRACE = timedelta(minutes=_interval_minutes() // 2)   # or DEADLINE_SECONDS
... if at is None or now - at >= timedelta(minutes=poll_interval_minutes(feed)) - GRACE
```

with a test that pre-registers the case: *a feed polled 29 minutes ago, on a 30-minute
interval, is still due*. That test does not exist in the plan and nothing else in it can
fail on this.

---

## (b) The plan assumes `capture.run` can be driven by the frozen `NOW`; it reads the wall clock, and Task 3's not-due test therefore cannot pass

`tests/test_capture_store.py:392`:

```python
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
```

That is a **fixed date, six days in the past** as of today (2026-09-09), and it is what
`_poll`'s `minutes_ago` is measured from (`:567`: `NOW - timedelta(minutes=minutes_ago)`).
There is no `freezegun`/`time_machine` in this repo and `tests/conftest.py` freezes
nothing.

Task 3's implementation makes `run` read the real clock:

```python
feeds = common.order_by_host(due_feeds(conn, sources, datetime.now(timezone.utc)))
```

Now trace `test_a_not_due_feed_is_skipped_and_writes_no_poll_row`:

```python
run = _run(store, minutes_ago=1)
_poll(store, run, "Slow", minutes_ago=10)     # polled_at = 2026-09-03 11:50 UTC
...
tally = capture.run(store, spacer=common.HostSpacer(0))
```

`run` computes `now ≈ 2026-09-09`, so `now - polled_at ≈ 6 days ≥ 120 minutes`. `SLOW` is
**due**. `run` reaches `brief.fetch_feed_entries`, which the test has monkeypatched to
`pytest.fail("a not-due feed was fetched")`.

**The test fails, with a message that accuses the implementation.** An executor arriving
at Task 3 Step 4 has just written the `run` change and is told "a not-due feed was
fetched"; the obvious diagnosis — my skip logic is broken — is wrong, and the obvious
repair is to go and break working code. This is precisely the shape the first review
called "the most expensive", relocated from the `HostSpacer` assertion into the `run`
tests. The presence sibling
(`test_a_due_feed_is_still_polled_and_still_writes_a_row`) passes, but for the wrong
reason — it would pass against a `run` with no due-check at all — so the pair reads as
"the skip is broken, the poll works", which is the maximally misleading signal.

Two related, smaller instances of the same assumption:

- Every DB test in Task 2 (`due_feeds(store, [SLOW], NOW)`) is fine, because `now` is a
  *parameter* there. The break is confined to the two tests that go through
  `capture.run`, which is exactly where the clock stops being injectable.
- The plan's Self-Review says "**Fixtures verified, not assumed.** … `NOW` (392) … exist
  as used". `NOW` exists; the claim that it is used correctly is the thing that is false.

**Fix:** give `run` an injectable clock — `def run(conn, spacer=None, now=None)` with
`now = now or datetime.now(timezone.utc)` — and pass `NOW` from both tests. That is also
the seam finding (a)'s grace test needs, so the two repairs share one change.

---

## (c) `'deadline'` rows are counted as poll attempts, and once a feed sits at 120 minutes that silently doubles its real gap while `feed_polls` reports the cadence as honoured

The first review raised this as its runner-up and listed it as item 8 of "what I would
change before executing". The revised plan neither implements it nor lists it under
"Deferred, explicitly". It is simply gone.

`capture.py:319-320`, inside `run`:

```python
if time.monotonic() >= deadline:
    record_poll(conn, run_id, feed["name"], "deadline", 0)
```

That row means *the pass ran out of time before reaching this feed*. **No fetch happened.**
Spec §7.2's own reasoning is that "a row means an attempt" — the deadline row means the
exact opposite, and it is written with the default `polled_at = now()`.

`due_feeds` as written takes `max(polled_at)` over **all** rows, with no `WHERE` on
`failure`:

```sql
SELECT source_name, max(polled_at) FROM feed_polls GROUP BY source_name
```

**Today this is invisible**, because every feed is polled every pass regardless of what
the last row said. That is why it will not show up in review or in the first week of
rollout.

**The six-month mechanism.** Rollout step 3 sets `poll_every_minutes` on a handful of
natives. A slow day arrives — a Nitter instance timing out, a `comprehend` backlog, a
host with 20-second timeouts — and a pass hits `DEADLINE_SECONDS`. A feed at a 120-minute
interval takes a `'deadline'` row at minute 0 of its cycle. Its next *real* fetch is
pushed out a full extra interval: **240 minutes between actual fetches**, while
`feed_polls` shows a row every 120 and every downstream reader — the roll-off script's
`entries_seen` denominators, §6.3's coverage guard, and the `/capture` surface — reads the
cadence as honoured. The gap is invisible in exactly the telemetry built to detect it.

It compounds with (a): the deadline path is *more* likely to fire once the due set shrinks
unevenly, because §7.35's collapse concentrates the 8 pinned Google News proxies into
every pass and pushes natives to the tail of the ordering, which is where the deadline
lands.

Two-line fix, either half of which closes it:

- `... FROM feed_polls WHERE failure IS DISTINCT FROM 'deadline' GROUP BY source_name`, or
- stop writing a poll row on the deadline path and carry the skip in `Tally` (which now
  has `feeds_not_due` to model it on).

The first is smaller; the second is more honest about what the table means. Either needs a
test — *a feed whose only recent row is a deadline row is due* — which nothing in the plan
currently asserts.

---

## Further findings (real, smaller, or second-order)

### 4. The test guarding review #1's objection (c) cannot detect the defect it names

The plan correctly replaced `MAX_POLL_INTERVAL_MINUTES = 120` with
`_interval_minutes() * MAX_INTERVAL_FACTOR`. The test it wrote to protect that
(`test_the_ceiling_is_derived_from_nominal_rather_than_hardcoded`, plan lines 101-107) is:

```python
assert capture.max_poll_interval_minutes() == capture._interval_minutes() * 4
assert capture.max_poll_interval_minutes() > capture._interval_minutes()
```

`_interval_minutes()` returns **30** (verified by execution), so both assertions reduce to
`120 == 120` and `120 > 30`. **A hardcoded `MAX_POLL_INTERVAL_MINUTES = 120` passes this
test.** Its own docstring says "A hardcoded 120 silently collapses every declared interval
if capture is ever retimed to 60" — and the test cannot tell a hardcoded 120 from the
derived one. Apply the repo's own technique (`tests-asserting-less-than-their-name`):
mentally replace the implementation with the constant the first review rejected, and the
test still passes.

The discriminating version has to move the schedule:

```python
def test_the_ceiling_moves_when_the_capture_schedule_does(monkeypatch):
    monkeypatch.setattr(capture, "_interval_minutes", lambda: 60)
    assert capture.max_poll_interval_minutes() == 240
```

This matters because the constant is the *only* part of review #1's objection (c) the plan
addressed; the guard it shipped alongside is the part that would catch a future
regression, and it is inert. This is the defect being **moved from the code into the
test**, which is the harder place to see it.

### 5. `failing_feeds`' comment claims a property the production caller does not get

The Task 4 comment says:

> `feeds` is a parameter so the tolerance source is explicit and testable, and so this
> alerting path does not read `sources.json` off the volume as a hidden side effect.

The default is `feeds=None`, and the only production caller — `brief.py:4163`, inside
`capture_quality_alert` — passes nothing. So in production `failing_feeds` **does** call
`capture_sources()` → `brief.load_temp_sources()` (`brief.py:496-508`) → `config.sources()`
(`config.py:601`), a database read of the source store, on the monitor path, on every
invocation. It fails open (`load_temp_sources` catches and returns `[]` with a logged
traceback) and `config.sources()` is cached, so the *cost* is small — but the comment
describes a property that only the tests enjoy, and a reader auditing the alerting path's
dependencies will believe it. Either say what it actually does, or have
`capture_quality_alert` pass the list it already has access to.

Note also the second-order effect of the cache: the tolerance source can be stale relative
to an `/addsource` that just landed. Harmless (a missing name falls back to nominal), but
it is a new way for the alerting path to be wrong that did not exist before.

### 6. Things I checked and found fine

- Every line-number claim in the plan is correct: `_interval_minutes` `capture.py:387`,
  `Tally` `:169` (decorator `:168`), `run` `:292`, `failing_feeds` `:453`, `timedelta`
  imported at `:20` with `datetime`/`timezone` absent, and the exact two-line block
  `feeds = common.order_by_host(capture_sources())` / `tally.feeds_total = len(feeds)`
  verbatim at `:313-314`.
- `poll_interval_minutes`'s `bool` exclusion is correct and its test covers `True`.
- `test_no_rank_ordered_feed_in_the_real_list_declares_an_interval`'s two control
  assertions both hold today: 8 of 28 feeds are `news.google.com`, 20 are not.
- `test_every_declared_interval_in_the_real_list_is_within_range` is labelled vacuous in
  its own docstring and in the Self-Review. Correct handling of a knowingly-vacuous test.
- `Tally.feeds_not_due` is not persisted and `capture_runs` has no column for it; the plan
  says so and adds it only to the log line. Consistent with the `items_already` /
  `items_failed` precedent immediately above it.
- `store_items(conn, id, [])` returning `(0,0,0)` and `_lookup_item_ids` early-returning on
  an empty hash list still hold, so `feeds_ok == 1` is reachable in the due-feed test —
  *if* the clock problem in (b) is fixed.
- The four `capture_url` overrides do not change the host, so `rank_ordered` cannot
  disagree between `brief.RSS_FEEDS` and `capture_sources()`. I specifically looked for
  this and it is not a defect.

---

## What I would change before executing

1. Add a grace to the due comparison so a 30-minute feed polled 29 minutes into the last
   pass is still due, with a pre-registered test for that case. Until then Phase 1 is not
   behaviour-preserving and the rollout's own checkpoint cannot be met. **(a)**
2. Give `run` an injectable `now`, and pass `NOW` from both Task 3 store tests. **(b)**
3. Exclude `failure = 'deadline'` from the due-check's `max(polled_at)`, or stop writing a
   poll row on that path; test it. **(c)**
4. Replace the ceiling test with one that monkeypatches `_interval_minutes` and asserts
   the ceiling moved. **(4)**
5. Correct the `feeds` parameter comment, or pass the source list from
   `capture_quality_alert`. **(5)**
6. Fix the §9.4 deferral's stated reason, which rests on "the due set is the full list".
   **(a)**

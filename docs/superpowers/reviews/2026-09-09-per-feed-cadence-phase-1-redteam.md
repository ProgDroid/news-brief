# Red-team review — Per-Feed Cadence, Phase 1 implementation plan

**Plan under review:** `docs/superpowers/plans/2026-09-09-per-feed-cadence-phase-1.md`
**Spec:** `docs/superpowers/specs/2026-09-09-per-feed-cadence-design.md`
**Reviewer:** hostile staff engineer, fresh context, no attachment to the plan
**Date:** 2026-09-09
**Base:** `fd9d574` (clean tree)

Everything below was checked against the code, not against the plan's claims about
the code. Where the plan is right, I say so, because a review that only lists faults
gives no way to tell "checked and fine" from "not checked".

---

## Summary of verdicts

| plan claim | verdict |
|---|---|
| `_interval_minutes()` at `capture.py:387` | **true** |
| `Tally` at `capture.py:169`, `run` at `:292`, `failing_feeds` at `:453` | **true** (dataclass decorator at 168) |
| `timedelta` imported at `capture.py:20` | **true**; `datetime`/`timezone` are **not**, and the plan says so |
| `common.feed_host`, `common.order_by_host`, `common.HostSpacer` exist | **true** (`common.py:739`, `:743`, `:769`) |
| `HostSpacer(gap_seconds, *, clock, sleeper)` signature | **true** (`common.py:784-786`) |
| fixture is `store`; `capture.run(store)` at `tests/test_capture_store.py:309` | **true** |
| `_CommitOnlyConn` exists in `tests/test_capture.py` | **true**, `:446`, and reachable from appended tests |
| `MAX_GAP_FACTOR = 1.5`; `poll_pairs` at `measure_roll_off.py:151` | **true** (constant is at `:105`, function at `:151`) |
| the `feeds = common.order_by_host(capture_sources())` / `tally.feeds_total = len(feeds)` block to replace | **true**, verbatim at `capture.py:313-314` |
| plan's `feed_polls` INSERT column list | **true** — all five columns exist, FK to `capture_runs` satisfied by the `start_run` call in `_poll_row` |
| `test_monitor.py` "is the caller of `failing_feeds` and asserts on its message" | **FALSE** — see (b) |
| `waited == [5, 5]` | **FALSE** — see (b) |
| the `_failing_history` ninety-minute test | **FALSE** — see (b) |
| Self-Review: "§7.35 due-set ordering → Task 3" | **FALSE** — see (a) |

---

## (a) The one spec requirement the plan claims to cover and does not: §7.35 / §9.4

The spec says this twice, and in §13 calls it "the genuinely serious half" of the
red-team finding it came from:

> §7.35: **The regression test does not survive, and that is the real defect.**
> `tests/test_capture.py:277` asserts over `capture.order_by_host(list(brief.RSS_FEEDS))`
> — the *whole* list, which production would no longer order. It keeps passing while
> testing something production stopped doing: a regression test made vacuous by the
> change it should be guarding. §9.4 repoints it at the **due set**.
> […] **Phase 1 carries this too, since any per-feed cadence shrinks the due set.**

> §9.4: **The interleave test is repointed at the due set (§7.35).** […] It must order
> what `due_feeds` returns.

The test is real. It lives at `tests/test_capture.py:262-273`, named
`test_the_real_feed_list_never_polls_one_host_back_to_back`, and its body is:

```python
    ordered = common.order_by_host(list(brief.RSS_FEEDS))
    hosts = [common.feed_host(f) for f in ordered]
    assert len(ordered) == len(brief.RSS_FEEDS)
    assert all(a != b for a, b in zip(hosts, hosts[1:])), hosts
```

(The spec's `:277` and `capture.order_by_host` are both slightly stale — it moved to
`common.` in `news-brief-bzo` — but the test it names is unambiguously this one, and
its own docstring says "Kept here against the REAL list, **which is what capture
orders**". After Task 3 that sentence is false: capture orders `due_feeds(...)`.)

**The plan never touches it.** Not in the File Structure table (`tests/test_capture.py`
is listed as receiving only "the interval accessor, the pinned-vs-native control, the
real-list invariant"), not in any task's Files block, not in any step. Every task
step is an *append*.

What the plan offers instead is
`test_the_due_set_is_ordered_and_every_same_host_collision_is_spaced`, and that test
does not discharge the requirement:

1. It monkeypatches `due_feeds` to the identity function
   (`lambda conn, fs, now: fs`), so it never exercises due-ness at all.
2. All three of its feeds are `https://one.example/f` — one host — so
   `order_by_host` is a no-op over its input. Despite the name, **it asserts nothing
   about ordering**; it asserts `HostSpacer` sleeps.
3. `tests/test_capture.py:454-487`
   (`test_a_pass_past_its_deadline_never_spaces_the_feeds_it_skips`) already builds
   five same-host feeds and a `HostSpacer(5, clock=..., sleeper=...)`. The new test is
   that test with the deadline removed. The *marginal* coverage over the existing
   suite is close to zero.

So after executing this plan:

- production orders the due set; the only test asserting host-spreadability still
  orders `brief.RSS_FEEDS` — exactly the vacuous-regression-test failure the spec
  identified as the reason §7.35 exists;
- the plan's Self-Review asserts "§7.35 due-set ordering → Task 3", which a reader
  checking coverage would take at face value.

This is the repo's own dominant defect class (`tests-asserting-less-than-their-name`)
being *introduced* by a plan whose §9 rationale is about avoiding it.

**Secondary, same category.** The spec's §9.5 pre-registered mutation table is a
required deliverable ("Written before the implementation. Not 'does a test fail' but
*which* test"), with four rows that apply to Phase 1: the `rank_ordered` guard, the
`due_feeds` fail-open, the not-due skip, and `failing_feeds` back to a global
tolerance. The plan neither executes the mutation exercise nor lists §9.5 among the
things it deliberately defers to Phase 2 (its "Deliberately not covered" line names
§4, §5, §6, §7.4/§7.5, §8.3 only). Per `mutation-diagnostic-demands-a-count`, that
exercise is the technique that has actually found defects in this repo; dropping it
silently is worse than dropping it loudly.

---

## (b) What the plan assumes about the codebase that is false

Three items. The first two would stop an executor dead at a "run the tests to verify
they pass" step, with a failure that reads as an implementation bug when it is a test
bug — the most expensive shape, because the executor will go and "fix" working code.

### b1. `waited == [5, 5]` is arithmetically impossible. Measured: `[5.0, 10.0]`.

Task 3, `test_the_due_set_is_ordered_and_every_same_host_collision_is_spaced`, ends:

```python
    spacer = common.HostSpacer(5, clock=lambda: 0.0, sleeper=waited.append)
    capture.run(conn=_CommitOnlyConn(), spacer=spacer)
    assert waited == [5, 5], "same-host fetches in the due set were not spaced"
```

`HostSpacer.wait` (`common.py:792-799`) advances its *own* bookkeeping clock by the
amount it slept, because the injected sleeper does not really sleep:

```python
    def wait(self, feed: dict) -> None:
        host = feed_host(feed)
        now = self._clock()
        last = self._last.get(host)
        if last is not None and (remaining := self._gap - (now - last)) > 0:
            self._sleeper(remaining)
            now += remaining
        self._last[host] = now
```

With `clock=lambda: 0.0` frozen at zero, `self._last[host]` becomes `0.0`, then
`5.0`, then `10.0`, and each `remaining` is `5 - (0 - last)` = `5 + last`. Three
same-host feeds therefore produce `[5.0, 10.0]`, not `[5, 5]`.

Verified by execution, not by reading:

```
$ py - <<'PYEOF'
import common
waited = []
s = common.HostSpacer(5, clock=lambda: 0.0, sleeper=waited.append)
feeds = [{"name": f"F{i}", "url": "https://one.example/f"} for i in range(3)]
for f in common.order_by_host(feeds):
    s.wait(f)
print("waited =", waited)
PYEOF
waited = [5.0, 10.0]
```

The plan copied the frozen-clock idiom from
`tests/test_capture.py:481-487`, where the assertion is `waited == []` — the one value
for which the accumulation is invisible. The idiom does not survive being pointed at
a case where the spacer actually fires.

An executor hitting this at Task 3 Step 4 sees "the due-set spacing test fails" after
having just written the `run` change, and the obvious diagnosis — "my due-set change
broke host spacing" — is wrong. Correct assertion is `waited == [5.0, 10.0]`, or a
clock that advances, or `assert len(waited) == 2 and all(w > 0 for w in waited)`.

### b2. The ninety-minute-tolerance test can never pass, because `max()` does not move backwards.

Task 4, `test_a_feed_with_no_declared_interval_keeps_todays_ninety_minute_tolerance`:

```python
    _failing_history(store, "Default", timedelta(minutes=80), timedelta(minutes=5), now)
    assert capture.failing_feeds(store, now) is None

    _failing_history(store, "Default", timedelta(minutes=100), timedelta(minutes=5), now)
    assert capture.failing_feeds(store, now) is not None
```

`_failing_history` **inserts**; it does not replace. `failing_feeds`'s CTE
(`capture.py:472-475`) is:

```sql
WITH ok AS (
  SELECT source_name, max(polled_at) AS last_ok FROM feed_polls
  WHERE failure IS NULL GROUP BY source_name
)
```

After both calls, `Default` has successful polls at `now-80m` **and** `now-100m`.
`max(polled_at)` is `now-80m`. The cutoff is `now-90m`. The filter requires
`last_ok < cutoff`, i.e. `now-80m < now-90m` — false. `failing_feeds` returns `None`
and the second assertion fails.

Adding an *older* success cannot make a feed look staler. The test needs a fresh feed
name (or a fresh `store`), not a second call on the same one. This one is worse than
b1 because it fails on the "verify they pass" step *after* a correct implementation
has been written, and the natural next move — loosening the tolerance until the test
goes green — breaks the feature.

While in there: the plan invents `_poll_row` when `tests/test_capture_store.py`
already has `_poll(store, run_id, name, failure=..., minutes_ago=...)` and `_run`,
used by every existing `failing_feeds` test (`:695-760`), alongside module constants
`NOW` (`:392`) and `INTERVAL`/`TOLERANCE` (`:393`, `:692`). The plan's own Task 2 Step 1
instructs the executor to "check the top of that file for the existing fixture name
and reuse it — do not invent a new one" and then does exactly that for the helpers.
A second helper writing the same table with different conventions (`_poll_row` opens a
brand-new `capture_runs` row per poll) is how two test dialects end up in one file.

### b3. `tests/test_monitor.py` does not reference `failing_feeds` at all.

Task 4 Step 4:

> `test_monitor.py` is included because the monitor is `failing_feeds`' caller and
> asserts on its message.

```
$ grep -rn "failing_feeds" tests/ | grep -v pycache
tests/test_capture_store.py:705
tests/test_capture_store.py:722
tests/test_capture_store.py:737
tests/test_capture_store.py:750
tests/test_capture_store.py:756
tests/test_capture_store.py:796
tests/test_capture_store.py:824
```

Zero hits in `tests/test_monitor.py`. The caller is `brief.py:4163`
(`(CAPTURE_FAILING_KEY, capture.failing_feeds)` inside the quality-alert table), and
every test of it lives in `tests/test_capture_store.py:695-760` plus
`test_failing_feeds_alert_once_per_episode_and_again_after_a_recovery` at `:824`.

Running `test_monitor.py` is harmless; the problem is what the false belief conceals.
The plan changes `failing_feeds`' message string and adds a `capture_sources()` call
to it, and it never looked at the seven existing tests that actually exercise it.
I checked them so the plan would not have to be trusted:

- **They survive the message change.** They assert on feed names in `verdict[1]`
  (`"Broken" in`, `"Retired" not in`, `"never" in ...lower()`) and on episode keys.
  None asserts the `"have not polled successfully in {tolerance}"` sentence the plan
  deletes. Good.
- **They survive the per-feed tolerance.** Their feed names (`Broken`, `Fine`, `Live`,
  `Retired`, `NeverWorked`, `First`, `Second`, `Other`) are absent from
  `brief.RSS_FEEDS`, so `by_name.get(name)` is `None` and each falls back to nominal —
  today's 90 minutes exactly. Good.
- **They now depend on `capture_sources()` succeeding.** `capture_sources()` calls
  `brief.load_temp_sources()`, which calls `config.sources()` — a **database** read
  (`brief.py`, `load_temp_sources`). `failing_feeds` was previously a single
  self-contained query; it is now a function that reads the source store on the
  monitor path, once per invocation, including the four calls inside
  `test_failing_feeds_alert_once_per_episode_and_again_after_a_recovery`. It fails
  open (`load_temp_sources` catches and returns `[]` with a logged traceback), so this
  is a cost and a noise issue rather than a correctness one — but it is an unremarked
  new dependency of the alerting path on the config store, and the plan should hoist
  it or note it.

### b4 (minor). Placement of the fail-open test

`test_the_due_check_fails_open_when_the_table_cannot_be_read` takes no `store`
fixture and needs no database, yet the plan appends it to
`tests/test_capture_store.py`, which carries
`pytestmark = pytest.mark.skipif(not db.is_configured())` at `:13`. The plan's own
File Structure note says "**Place each test by what it NEEDS, not what it is
ABOUT**".

I checked before calling this a CI gap and it is **not** one:
`.github/workflows/docker-publish.yml:47-49` runs a `postgres:18-alpine` service and
`:80` exports `DATABASE_URL`, so the DB-marked file does run in CI. The consequence is
confined to local runs — the single test guarding "every failure degrades to today's
behaviour" is silently skipped for anyone running `pytest -q` without a database,
which is the default state this repo's CLAUDE.md warns about. Move it to
`tests/test_capture.py`.

### Things I checked that are fine

- `monkeypatch.setattr(capture, "due_feeds", ...)` works: `run` will call it as a bare
  module global, per `newsbrief-flag-access-module-attr`.
- `test_a_due_feed_is_still_polled_and_still_writes_a_row` reaches `feeds_ok == 1`:
  `SLOW` has no `outlet`/`kind`, so `resolve_outlet` (`capture.py:56`) falls back to
  `brief.outlet_for(feed)` → `"Slow"` and `feed.get("kind", "regional")`, finds no
  row, inserts one, returns an id. `store_items(conn, id, [])` returns `(0,0,0)` and
  `_lookup_item_ids` early-returns `{}` on an empty hash list (`capture.py:157-158`).
- `pytest.fail` in the not-due test is fine: `pytest` is imported at
  `tests/test_capture_store.py:5`. `datetime`, `timedelta`, `timezone` at `:3`.
- The `feed_polls` INSERT's FK is satisfied — `_poll_row` calls `start_run` first.
- `common.HostSpacer(0)` is a valid positional call.
- `poll_interval_minutes`'s `bool` exclusion is correct (`True` is an `int`).
- The Task 4 anchor `tolerance = timedelta(minutes=_interval_minutes() * STALE_AFTER_INTERVALS)`
  is **not unique** in the file — `liveness` has the byte-identical line at
  `capture.py:422`. The prose scopes the edit to `failing_feeds`, but an `Edit` call
  on that string alone will be rejected as ambiguous. Give the executor the
  surrounding `cutoff = now - tolerance` line as part of the match (the plan does)
  and it is fine; worth a word so nobody deletes the wrong one.

---

## (c) What breaks in six months

**`MAX_POLL_INTERVAL_MINUTES = 120` is a hardcoded constant whose only meaning is
"four times nominal", sitting one screen below the plan's own rule that nominal must
never be hardcoded.**

Global Constraint: *"`_interval_minutes()` is the only source of 'nominal'. Never
hardcode 30; it reads the capture schedule so retiming does not leave a constant
behind."* `_interval_minutes` (`capture.py:387-397`) exists precisely for this, and
its docstring names the defect: *"A constant here would be a knob tracking another
knob."* The spec's §5.3 flags the same defect in `measure_roll_off.py:121`
(`NOMINAL_MINUTES = 30`) and cites `the-prediction-had-a-generator`.

The plan then writes:

```python
MAX_POLL_INTERVAL_MINUTES = 120
```

with a justification chain that is entirely relative to the 30-minute tick:
`MAX_GAP_FACTOR = 1.5` × 30 = 45 minutes of observation, and 120 is "2.7×" that. Both
numbers are a function of nominal. The constant is not.

The mechanism, concretely. `poll_interval_minutes` is:

```python
    nominal = _interval_minutes()
    ...
    if declared <= nominal:
        return nominal
    return min(declared, MAX_POLL_INTERVAL_MINUTES)
```

and the real-list guard test asserts `nominal < declared <= MAX_POLL_INTERVAL_MINUTES`.

Retiming capture is a live possibility — it is the natural follow-on once per-feed
cadence has cut the request count, and it is exactly the scenario `_interval_minutes`
was written to survive. Change `scheduler.SCHEDULES`' capture entry from 30 to 60 and:

- every declared interval of 60 or less silently collapses to nominal. No log line,
  no alert, no test failure. The feature quietly does less, and `feeds_not_due` in
  the log goes *down*, which reads as the mechanism working harder rather than less;
- the valid range narrows from `(30, 120]` to `(60, 120]`, so any feed already tuned
  into the lower half now fails
  `test_every_declared_interval_in_the_real_list_is_within_range` with the message
  `f["name"]` and nothing about why;
- at nominal ≥ 120 the range `(120, 120]` is **empty**. Every declared key fails CI,
  and the only way to read the failure is to know that a constant in `capture.py` and
  a number in `scheduler.py` are related — a relationship stated nowhere in code, only
  in a comment about `MAX_GAP_FACTOR`.

And the ceiling's *evidence* moves with nominal in the opposite direction to
intuition. `poll_pairs` keeps pairs within `nominal * 1.5`, so at a 60-minute tick the
observed spans become ≤90 minutes and 120 becomes a 1.3× extrapolation — the ceiling
could safely rise. Frozen at 120, the system gets more conservative exactly when the
data says it may relax. A constant that is wrong in both directions depending on which
way another constant moved is the definition of a knob tracking a knob.

Six months is the right horizon because nothing surfaces this until someone retimes
capture, and the plan ships no signal that would. The fix is one line —
`MAX_POLL_INTERVAL_MINUTES` becomes `_interval_minutes() * MAX_INTERVAL_FACTOR` with
`MAX_INTERVAL_FACTOR = 4` and the provenance comment attached to the factor — plus a
test that the ceiling moves when the schedule does, which is the only test that can
fail before production does.

**Runner-up, recorded because it is cheap to fix now and invisible later.**
`due_feeds` treats a `'deadline'` row as an attempt. `capture.run` writes
`record_poll(conn, run_id, feed["name"], "deadline", 0)` at `capture.py:319` for a
feed the pass **ran out of time to reach** — no fetch happened. §7.2's own reasoning
is that "a row means an attempt"; the deadline row means the exact opposite. Today
this is harmless because every feed is polled every 30 minutes regardless. Once feeds
sit at 120, a deadline row lands at minute 0 of a slow feed's cycle and pushes its
next real fetch out a full extra interval — 240 minutes between actual fetches, while
`feed_polls` shows a row every 120 and the coverage/roll-off measurement reads the
cadence as honoured. `due_feeds` should exclude `failure = 'deadline'` from
`max(polled_at)`, which is a `WHERE` clause, or the deadline path should stop writing
a poll row.

---

## What I would change before executing

1. Repoint `tests/test_capture.py:262` at the due set, or delete it and say so — but
   do not leave the Self-Review claiming §7.35 is covered. **(a)**
2. Fix `waited == [5, 5]` → `[5.0, 10.0]`. **(b1)**
3. Fix the ninety-minute test to use a second feed name. **(b2)**
4. Drop `_poll_row`; use the file's existing `_poll`/`_run`/`NOW`/`INTERVAL`. **(b2)**
5. Delete the `test_monitor.py` claim; run the real `failing_feeds` tests at
   `tests/test_capture_store.py:695-760` and `:824` instead, and hoist the new
   `capture_sources()` call out of the per-row path. **(b3)**
6. Move the fail-open test to `tests/test_capture.py`. **(b4)**
7. Derive `MAX_POLL_INTERVAL_MINUTES` from `_interval_minutes()`. **(c)**
8. Exclude `'deadline'` rows from the due-check. **(c, runner-up)**
9. Add §9.5's mutation run to Task 4, with a pre-registered count. **(a, secondary)**

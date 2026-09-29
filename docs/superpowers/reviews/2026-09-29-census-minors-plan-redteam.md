# Red team: `docs/superpowers/plans/2026-09-29-census-minors.md`

**Date:** 2026-09-29 · **Reviewer stance:** hostile staff engineer · **Method:** read-only. I read the plan,
the spec (rev 7), the implementation record, `census.py`, `census_metrics.py`, `labeller.py`,
`labeller_static/label.js`, `scripts/census_report.py`, `scripts/census_grants.py`, migration 0017 up and
down, the `/label` code in `brief.py`, the runbook, `tests/census_fixtures.py` and the census and labeller
tests. I ran no pytest and did not touch the database. `git log origin/main..main` shows the 18 census
commits are unpushed against the local tracking ref, so "0017 is not deployed and not pushed" holds as of
now. I did not fetch, so that ref may be stale.

## The three strongest objections

### 1. D1 is specified against a `census.py` that does not exist. As written, Batch 3 either commits mid-write or breaks about ten existing tests and fixtures, and the plan budgets for neither

The plan says only: "the write guard, in `census.py` writers ... tests for a write to a non-served window
in each kind". Three facts in the code make that sentence under-specified in ways that matter.

**(a) `current_task` commits.** It is `@_transaction` (census.py:650), and `_transaction` calls
`conn.commit()` on return (census.py:471-482). Every writer is also `@_transaction` and takes its
`FOR UPDATE` row lock first through `_lock_window` (census.py:494-505). A guard that calls the public
`current_task(conn, at)` after `_lock_window` commits, which releases the F17 lock. The rest of the write
then runs unlocked, and F17 (a Finish and a late assignment interleaving) is open again. The concurrency
test the plan adds in the same batch cannot see this. It holds the lock from a second connection and
watches `_lock_window` wait, and `_lock_window` still waits. The fix is easy: split `current_task` into an
undecorated `_current_task(conn, now)` that the writers call, as `census_report.py` already does for its
own reasons (its docstring, lines 8-11). But the plan has to say so, and it has to add a test that fails
if the guard commits. One way: assert inside a writer that `conn.info.transaction_status` is still
INTRANS after the guard, or run the lock test with the guard placed after the lock.

**(b) The guard's order relative to `WindowClosed` is unstated, and it changes public behaviour.** Today a
write to a finished window raises `WindowClosed`, and the labeller returns 409. label.js shows "window
closed — reload" for a 409 (label.js:91). After a Finish, the finished window is usually served as
`precision`, so D1's "blind writes for the blind window" rule rejects a blind write to it. If the guard
runs before `_raise_if_closed`, that becomes `BadWrite` → 400, which label.js shows as the generic "not
saved: the server refused a change (400) — reload". These tests assert the current behaviour and would
flip:
- `tests/test_labeller.py:690` `test_assign_after_finish_is_409` (assign, groups and finish all expect 409)
- `tests/test_census_state.py:150` and the `WindowClosed` rows at lines 700-701

The plan must pin the order: closed-check first, then the guard. It also needs to say that the heartbeat
400 path (label.js:197) and the queued-action 409 path keep their current meanings.

**(c) The existing corpus uses the writers as setup, in an order that is not the served order.** Each of
these call sites writes to a window `current_task` is not serving, so D1 raises at setup:

| site | what it does | served at that moment |
|---|---|---|
| `tests/test_census_report.py:64-67` | labels windows 1-16 with 8 groups each and never answers precision | precision(1), from the first iteration on |
| `tests/test_census_report.py:82` | labels the repeat straight after `pass_gate` | precision(1) or window 3; the repeat is not eligible |
| `tests/test_census_report.py:92` | abandons the repeat | not served |
| `tests/test_census_state.py:168` | abandons w2 after labelling w1 with 3 groups | precision(w1) |
| `tests/test_census_state.py:181` | `create_group(w2)` | w1 |
| `tests/test_census_state.py:452`, `:472` | abandon window 2 after labelling w1 with 8 groups | precision(w1) |
| `tests/test_census_state.py:631` | abandons w3 | precision(w1) |
| `tests/test_census_state.py:693` | abandons w3 in the no-open-transaction sweep | not served |
| `tests/test_census_access.py:160` | abandons window 3 as `census_labeller`; this is the only exercise of abandon's UPDATE grant | not served |

`cf.label` itself (census_fixtures.py:162) is sequence-agnostic.

There are 118 writer call sites across these files. Nobody has audited them. The Global Constraint
"pre-registered counts" cannot be met until someone does. The cheap way out, a `guard=False` parameter or
a module flag the fixtures flip, becomes a production bypass the moment it exists. The plan should state
the fixture strategy up front:
- a `cf.advance_to(order_no)` that walks the served order and answers precision on the way; or
- SQL seeding of statuses for tests that only need end states.

It should also forbid a bypass parameter.

**(d) The "reload message" from the server is never displayed.** For a 400, label.js does not read the
response body. It reads the body only for 403 (label.js:109). What the operator sees is label.js's
generic string, and Batch 3's "maps it to 400 with 'reload'" tests a JSON field no browser shows. Either
test the status alone, or change label.js to show the server's text for 400. That second option is a JS
change, and the plan's D2 browser run is its only coverage.

### 2. D1 can lock out a legitimate first action, at a boundary that falls near the operator's usual labelling time

The priority order in `current_task` (census.py:651-717) puts step 5, the repeat, ahead of step 6, the
next pass-1 window. Step 5's eligibility depends on `now`: `anchor.blind_done_at + REPEAT_MIN_DAYS`
(census.py:700-703). Only status `open` protects a window from being pre-empted (F16), and only
`save_assignments` sets `open`. `create_group` and the `open` event do not (census.py:766-775, 843-847).

The failure sequence:
1. Window 8 is done, and the repeat is not yet eligible.
2. The operator taps `/label` and the page serves window 9, still `prepared`.
3. He reads for a few minutes. Meanwhile `now` crosses window 2's `blind_done_at + 7 days`.
4. His first action enqueues `/api/groups`. The guard now computes `blind(repeat)`, so the call returns
   `BadWrite` → 400.
5. label.js calls `stop()`, which clears the queue and discards that action (label.js:82-88). The banner
   says reload.
6. The reload serves the repeat: different items and a different task.

**This is not a far-fetched race.** Window 2 was finished at the time of day the operator labels. So "7
days later" lands in the same part of the day, which is exactly when he is most likely to have a page
loaded and untouched. Without D1 the write would have succeeded and made window 9 `open`, and step 3
would then keep serving window 9. **D1 introduces this lockout.**

The other scenarios the brief asked about are safe as the code stands:
- **Two tabs on one window:** both are served.
- **A precision step reached while a blind tab is open:** the blind tab's writes already get
  `WindowClosed`, and its heartbeat already gets 400.
- **A heartbeat after Finish:** it already gets 400 through `_BLIND_EVENT_KINDS`, and label.js stops
  heartbeating on it silently.
- **The precision page:** it sends no heartbeat. `startHeartbeat` runs only in `blindPage`
  (label.js:545).

**Fix options, pick one and write it into the plan:**
- make repeat eligibility not pre-empt a pass-1 window that has an `open` event since the previous
  window's completion (the page was served and loaded); or
- let the guard accept "the served window OR the window the same `current_task` would serve with the
  repeat still ineligible"; or
- accept it, and add a test that pins it plus a runbook line.

A smaller residual from the same mechanism: `_page_data` calls `current_task` (which commits), then
`record_event(open)`, and the guard recomputes `current_task` between the two. A state change in that gap
turns a page load into a 400, which is today's raw-JSON GET error. Batch 4's HTML error page covers the
rendering, but the plan should either exempt `open` from the guard or catch it in `_page_data`.

### 3. The readout fix "wall-clock minutes start at the window's first `open` event" makes wall-clock minutes wrong for nearly every window

`open` is stamped on every `GET /` that serves a blind or precision task (labeller.py:217, 230). label.js
reloads automatically:
- after Finish (label.js:518);
- after Abandon (label.js:539);
- after the last precision answer (label.js:576).

A window with no multi-outlet group completes at Finish (census.py:877-884), and a precision window
completes on its last answer. **So the reload that follows immediately serves window N+1, and window
N+1's first `open` event carries the timestamp of window N's completion.** The `/label` link and the
morning nudge add further opens whenever they are tapped.

Suppose the operator labels one window per sitting, roughly daily. Then "first open → `blind_done_at`"
measures the gap between sittings: hours to days, not the session. That replaces a finding recorded as
"understates by the time before the first assignment" with one that overstates by a day. The number goes
straight into the per-window table the operator reads after every window (runbook step 8), and into the
spec §11 record.

The spec asks for "active and wall-clock minutes" of the session (§6.3, §8). Candidate definitions that
fit that:
- the **last** `open` at or before the first `action` or assignment; or
- the first event of the contiguous burst that ends at `blind_done_at`, using the same `IDLE_CAP_MINUTES`
  split `active_minutes` uses, and printed as such.

Whichever is chosen, the test should put an `open` event a day before the labelling burst. A
first-`open` implementation must fail that test.

## Further findings (ranked)

4. **No ordering gate against the runbook.**
   - "Edit 0017 in place" is valid only until runbook step 1 deploys. After that the host's
     `schema_migrations` records 0017 as applied, and `UNIQUE (order_no, pass)` never reaches it while
     the tests pass.
   - The runbook's census freeze (from step 3, on or after 2026-10-01, two days out) forbids the D1
     guard, the `_windows` change and the constant moves. Those changes are exactly what makes `/label`
     (unpinned image) and the page (pinned image) disagree, the R15 hazard.
   - Add a Global Constraint: "every batch lands before runbook step 1 is executed; if step 1 has run,
     0017 changes become 0018 and census.py changes wait for step 10".
5. **The ARI item has no behavioural delta, so "red first" is impossible.** `denom == 0`
   (census_metrics.py:284-286) is reachable only when both labellings are all-singleton or both are one
   cluster (AM-GM: `sa*sb/T = (sa+sb)/2` iff `sa = sb ∈ {0, T}`). Both cases are special-cased to 1.0 at
   line 267, so the branch is dead. The one real divergence from scikit-learn is `n == 0` → `nan`, where
   sklearn gives 1.0. `nan` is the right answer here, because the bootstrap drops `nan` replicates. The
   item should read "delete the dead branch; pin the reachable special cases and `n == 0 → nan`". As
   written, it cannot be tested.
6. **The D2 browser run is mis-stated.**
   - "The queue recovers after a callback exception" is not what label.js does. It **stops** with "not
     saved: unexpected page error — reload" (label.js:158-165). m11 fixed a silent stall, not a
     recovery. A recorder will either log a behaviour that does not exist or "fix" the JS to match, with
     no test.
   - `HEARTBEAT_SECONDS = 60` is a constant, so the 400-stop evidence costs a minute of wall clock per
     attempt. Plan for clock control.
   - Running against "the test DB" conflicts with the plan's own Global Constraint that every fixture
     drops the schema. The run must hold the database exclusively, with no pytest from anyone.
7. **Step 5's password handling contradicts itself.** Batch 3 says the password never goes on a command
   line. The runbook already explains `REAL_EXIT=1`, but its remedy is
   `export CENSUS_LABELLER_PASSWORD=<the .env value>` (runbook:198). That is a command line, so it lands
   in shell history. Batch 5 asks for "the exact command". Write `set -a; . ./.env; set +a` (or
   `export $(grep ^CENSUS_LABELLER_PASSWORD= .env)`) instead. Also fix `census_grants.py`'s docstring
   (`Run: CENSUS_LABELLER_PASSWORD=... py ...`). The runbook file is not in Batch 3's file list, but
   Batch 3 edits it.
8. **`_CENSUS_TABLES` from the catalog widens the grant with every future `census_%` table**, including
   sub-project 1's adjudication tables. It also turns `LABELLER_GRANTS`, a module constant that tests and
   `missing_privileges` iterate, into something that needs a connection. Combined with the new startup
   refusal, any later migration that adds a `census_*` table stops the labeller until the grants are
   re-run. That is loud, which is good, but the plan should say it is intended.
9. **`privilege_surplus` has no stated scope.**
   - Which objects: all of `public`? functions? And which privileges? PG 17+ added `MAINTAIN`, and
     column-only `UPDATE` is invisible to `has_table_privilege`.
   - Its scope must equal what `apply_labeller_grants` revokes. Otherwise a stale grant outside
     census_*/items/outlets makes the labeller refuse to start, and the grants script cannot clear it.
10. **The `_read_windows` reuse needs more than extra columns.**
    - `_Window` is a dataclass, but the readout consumes 9-tuples positionally in six functions.
    - The readout slots the repeat after order_no 8, while `_windows` orders `pass, order_no`.
    - Keep `_windows`' ORDER BY, and sort in the readout. `current_task`'s step 3 and 4 loops iterate in
      that order.
11. **D3 edge.** `_half_lines` classifies by `window_start` alone. A window whose six hours contain the
    deploy lands in the "before" half. State it in the header, or exclude straddling windows.

## Deferred-finding → checkbox map

Every line of the record's "Deferred findings" maps to a checkbox, and D4 covers `/label@botname`. Three
are mis-stated:

| deferred line | batch | status |
|---|---|---|
| ARI denom==0 | 2 | vacuous as written (finding 5) |
| wall-clock starts at opened_at | 4 | fix makes it worse (objection 3) |
| runbook step 5 REAL_EXIT=1 | 5 | already explained in the runbook; the remaining defect is the history leak (finding 7) |
| label.js queue after callback exception | 5 | behaviour mis-described (finding 6) |
| writers do not check the served task (D1) | 3 | under-specified (objections 1 and 2) |

# Event census: deferred-minors cleanup plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Clear every deferred minor finding recorded in `docs/2026-09-29-event-census-implementation-record.md`
("Deferred findings"), so the census tooling ships with no known open finding.

**Architecture:** No new components. Five tasks, grouped by file so each task has one owner and one
review: schema, metrics, census state and access, labeller/Telegram/compose/readout, and runbook plus a
recorded browser run.

**Spec:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` (rev 7). Original plan:
`docs/superpowers/plans/2026-09-28-event-census.md` (its Global Constraints bind here too).

## Global Constraints

- Every Global Constraint of the original plan applies (names, imports, pinned values, time passed as
  `now`, one connection per operation with no idle transaction, http.client in tests, the four headers,
  the three-command gate with DB tests that RUN).
- Migration 0017 is not deployed and not pushed: edit it in place.
- One implementer at a time: the test database is shared, and every DB fixture drops the schema.
- A change to behaviour gets a test that fails without it (red first). A test-only item gets a
  mutation that the new test catches, with the count pre-registered.
- **Ordering gate:** every task lands before host runbook step 1 is executed. If step 1 has run,
  0017 changes become a new migration 0018 and `census.py` changes wait for the census to end
  (the runbook's freeze). As of 2026-09-29 step 1 has not run.
- **No bypass.** No `guard=False` parameter, module flag or environment switch that disables the
  write guard, for tests or anything else. Tests reach a state by walking the served order
  (`cf.advance_to`) or by seeding statuses in SQL.
- Red-team review of this plan: `docs/superpowers/reviews/2026-09-29-census-minors-plan-redteam.md`.

## Operator decisions (2026-09-29)

- D1 **Write guard, enforce:** saves, group creation, heartbeats/actions, finish, abandon and
  precision answers are accepted only for the window the census is serving (blind writes for the
  blind window, precision answers for the precision window); anything else is `BadWrite` → 400.
- D1a **Claim on serve** (operator, after the plan red team): a pass-1 window whose page was served
  (it has an `open` event) since the most recent window completion is never pre-empted by the
  repeat. The repeat therefore takes over only at a session boundary; it may land one window
  later than "right after window 8", which still meets spec §4.6's lower bounds (after window 8,
  ≥ 7 days after window 2's blind pass).
- D2 **JS behaviours:** one recorded real-browser run (Playwright) instead of a JS test runner.
- D3 **By-half split** at `census_block.c439ade_deployed_at` when it falls inside the block, else the
  block midpoint, and the table header says which.
- D4 **`/cmd@botname`:** not applicable; the bot serves one private chat. Recorded, no code change.

---

### Task 1: schema and window arithmetic

**Files:** `migrations/0017_census_up.sql`, `migrations/0017_census_down.sql`, `census.py`,
`tests/test_census_schema.py`, `tests/test_census_prepare.py`

- [x] `census_windows` gains `UNIQUE (order_no, pass)`; test that a duplicate (order_no, pass) raises.
- [x] The down script's refusal `DO` block tolerates a missing `census_assignments` table
      (`to_regclass`), so a partially applied up can still be reverted; test it.
- [x] The truncate test asserts the "held by the event census" message, and a no-block control
      shows `TRUNCATE items CASCADE` succeeds when no `census_block` row exists.
- [x] The down test runs on a freshly reset schema, not the one it just used.
- [x] The negative-CHECK test also drives `null_published = -1`.
- [x] `window_stats`: assert (raise `CensusRefusal`) if the block length is not a multiple of
      `WINDOW_HOURS`, instead of assuming it; test with a hand-built non-multiple block.

### Task 2: metrics and named constants

**Files:** `census_metrics.py`, `census.py`, `scripts/census_report.py`, `tests/test_census_metrics.py`,
`tests/test_census_state.py`, `tests/test_census_report.py`

- [x] `adjusted_rand_index`: the `denom == 0` branch is dead (reachable only for all-singleton or
      one-cluster pairs, which are special-cased to 1.0 earlier — confirm by reading, then by a
      test over every partition pair of n ≤ 5 items that the branch is never hit). Delete it; pin
      the reachable special cases (both all-singleton → 1.0, both one cluster → 1.0) and
      `n == 0 → nan` (deliberately unlike scikit-learn's 1.0: the bootstrap drops NaN replicates),
      each with a comment saying why.
- [x] `pairwise_agreement` F1 tested with a non-trivial value (precision ≠ recall, both in (0, 1)).
- [x] `precision_estimate` with every `asked == 0` returns `(nan, None, None)`, never
      `ZeroDivisionError`; tested.
- [x] The pinned thresholds live once, as named constants in `census_metrics`
      (`GO_MIN_MEAN_GROUPS = 8`, `GO_MAX_MEDIAN_MINUTES = 80`, `PRECISION_PAIRS = 10`,
      `IDLE_CAP_MINUTES = 5`); `go_no_go`, `draw_precision_sample` and `active_minutes` use them as
      defaults; `census.py` re-exports them rather than redefining. The existing pin tests keep passing.
- [x] The consistency bootstrap computes `pairwise_agreement` once per replicate, not three times
      (one stat returning all three components, sliced); the printed values are unchanged (test pins
      them on a fixed fixture before and after).

### Task 3: census state and access

**Files:** `census.py`, `scripts/census_grants.py`, `labeller.py` (`_page_data`, self-check),
`tests/census_fixtures.py`, `tests/test_census_state.py`, `tests/test_census_access.py`,
`tests/test_labeller.py`, `tests/test_census_report.py` (fixture call sites only)

- [x] D1a first: `current_task`'s logic moves into an undecorated `_current_task(conn, now)`
      (the public `@_transaction` wrapper stays for the labeller and `/label`). Step 5 (the repeat)
      does not fire while the lowest not-done pass-1 window has an `open` event later than the most
      recent completion timestamp of any window (the max over every window's `blind_done_at`,
      `completed_at`, and whatever timestamp `abandon` records — read the code). Tests: the
      red team's sequence (window 9 served, eligibility passes, window 9 still served; and without
      the open event the repeat is served), plus the reload after window 9 completes serving the
      repeat.
- [x] Page loads: a new `serve_page(conn, now) -> Task` computes `_current_task` and stamps the
      `open` event for a blind or precision task in ONE transaction; `labeller._page_data` uses it
      instead of `current_task` + `record_event(open)`. `record_event` no longer accepts `open`
      (it was only the labeller's page-load stamp); update its callers and tests.
- [x] D1: the write guard in every writer (`save_assignments`, `create_group`, `record_event`,
      `finish_blind`, `abandon`, `save_precision`), placed AFTER `_lock_window` and AFTER the
      closed-window check (so a write to a finished window still raises `WindowClosed` → 409, as
      `test_assign_after_finish_is_409` and the `WindowClosed` rows in test_census_state.py pin).
      It calls `_current_task(conn, at)` — never the committing wrapper — and raises `BadWrite`
      unless the served task is blind on this window (blind writes, events, finish, abandon) or
      precision on this window (`save_precision`). Tests: one non-served write per writer; a test
      that the guard does not commit (e.g. `conn.info.transaction_status` is still INTRANS after
      the guard inside the writer, or the lock test run with the guard in place); heartbeat after
      Finish still 400; queued-action 409 unchanged. No JS change: label.js's generic 400 text
      already says reload, so labeller tests assert the status, not a body string.
- [x] Fixtures: audit every writer call site in the census tests (the red team lists
      `test_census_report.py:64-67, :82, :92`, `test_census_state.py:168, :181, :452, :472, :631,
      :693`, `test_census_access.py:160` — re-derive, don't trust the list) and make each reach its
      state legitimately: add `cf.advance_to(conn, order_no)` that walks the served order
      (labelling, answering precision, advancing `now` for the repeat), or seed statuses in SQL
      where a test needs only an end state. Report the count of sites changed.
- [x] Tests for the two untested `current_task` priority boundaries (see the Task 4 review:
      `.superpowers` is gone, so derive them: gate_failed vs an open window, and precision vs an
      eligible repeat).
- [x] A test pins `precision_pairs`' `Random(window_id)` seed (a known window id → a known pair set).
- [x] A concurrency test for `save_assignments`' `FOR UPDATE`: a second connection holding the row
      lock makes the save wait (use `lock_timeout` to observe it) — the test fails if the lock is
      removed.
- [x] `apply_labeller_grants` first revokes everything the role holds on every table and sequence in
      schema `public` (including column-level privileges — verify on PG 18 that a table-level
      REVOKE ALL clears them, else revoke columns explicitly) plus schema CREATE, then grants
      `LABELLER_GRANTS`, so the result is exactly that set; test by adding a stale table grant, a
      stale column grant and a stale sequence grant, re-applying, and asserting the exact set.
- [x] `/label reset` race: `open_session` and `revoke_all` serialise on one transaction-scoped
      advisory lock, so a session cannot be created between reset's read and its update; test with
      two connections.
- [x] Over-privileged role: `privilege_surplus(conn) -> list[str]` over EXACTLY the scope
      `apply_labeller_grants` revokes (tables, columns and sequences in `public`, schema CREATE —
      every privilege type the server knows, including PG 17+'s MAINTAIN), plus role attributes
      (SUPERUSER, CREATEROLE, CREATEDB, BYPASSRLS). Functions are out of scope (PUBLIC holds
      EXECUTE by default); say so in the docstring. The labeller's startup refuses (exit 3, naming
      each surplus) when it is non-empty; tests for a stale grant and for an attribute.
- [x] `_CENSUS_TABLES` stays a hand-written constant (deriving it from the catalog would silently
      widen the labeller's SELECT to every future `census_*` table, e.g. sub-project 1's). Instead
      a drift test asserts it equals the set of `public` tables named `census\_%` after
      migrations, so a new census table fails the suite until someone decides its grant.
- [x] `revoke_all`'s expiry clause is tested (an expired unconsumed link is not counted/updated).
- [x] `scripts/census_grants.py` writes its errors to stderr, and its docstring no longer shows
      `CENSUS_LABELLER_PASSWORD=... py ...` (a command line lands in shell history); it shows
      loading `.env` instead. (The runbook's matching text is Task 5.)
- [x] `open_session` also compares with `hmac.compare_digest` (spec §6.2 conformance).

### Task 4: labeller, Telegram, compose, readout

**Files:** `labeller.py`, `brief.py`, `docker-compose.yml`, `scripts/census_report.py`,
`tests/test_labeller.py`, `tests/test_commands.py`, `tests/test_packaging.py`,
`tests/test_census_report.py`

- [x] A behavioural socket-timeout test: a client that connects and sends nothing is dropped within
      the configured timeout (fails if the timeout attribute is removed).
- [x] `LABELLER_BASE_URL` is `html.escape`d (quote=True) in the Telegram message; tested with a
      value containing `&` and `"`.
- [x] Tests for the `/label` precision link and the waiting / complete / not_prepared texts.
- [x] The reset test asserts the whole message.
- [x] The Host check accepts the base URL with or without its scheme's default port
      (`http://h:80` ≡ `http://h`); tested both ways.
- [x] GET errors render a short HTML status page, never raw JSON; tested.
- [x] Compose tests: block-end detection ignores comment lines and blank lines inside the block; the
      env extractor also reads mapping-form `NAME: value`; pins the `127.0.0.1` bind default, the
      empty `CENSUS_LABELLER_PASSWORD` default and `restart: unless-stopped`.
- [x] Readout: `_read_windows` reuses `census._windows` (extend `_Window` with the columns the
      readout needs) instead of re-querying. Keep `_windows`' ORDER BY (`current_task` iterates
      it); the readout sorts for its own display order (the repeat slotted after order_no 8) and
      its six positional-tuple consumers move to attribute access.
- [x] Wall-clock minutes start at the LAST `open` event at or before the window's first activity
      (its earliest `action` event or assignment), falling back to `opened_at` when there is none.
      Not the first `open`: the auto-reload after the previous window's completion stamps an open
      hours or days before the sitting. Test: an `open` a day before the labelling burst plus a
      second `open` just before it; a first-`open` implementation must fail. The readout's column
      note states the definition.
- [x] D3's by-half split: windows are classified by `window_start` (a window whose six hours
      contain the deploy counts as "before"); the header says which split point was used (the
      recorded deploy time, or the block midpoint when the deploy falls outside the block) and
      states the straddle rule. Tests for both split points and the straddling window.

### Task 5: runbook, records, and the browser run

**Files:** `docs/2026-10-01-host-runbook-census.md`,
`docs/2026-09-29-event-census-implementation-record.md`,
`docs/superpowers/reviews/2026-09-29-census-browser-verification.md`

- [x] Runbook step 11: after `DELETE FROM census_block`, `census_prepare` would prepare a NEW block —
      say so, and say not to re-run it unless a new census is intended.
- [x] Runbook step 5: the remedy for an unexported password is `set -a; . ./.env; set +a` (loads
      `.env` without the value ever appearing on a command line), replacing the current
      `export CENSUS_LABELLER_PASSWORD=<the .env value>` line, which lands in shell history.
- [x] Runbook: an external database (DATABASE_URL on the host) needs a role with CREATEROLE for the
      grants script.
- [x] Runbook (from Task 3): the labeller now refuses to start (exit 3) naming any surplus
      privilege. The grants script clears stale grants on `public` objects that it made; role
      attributes (SUPERUSER, CREATEROLE, CREATEDB, BYPASSRLS, REPLICATION) and memberships
      ("member of role X") must be removed by a superuser (`ALTER ROLE census_labeller NO...;`,
      `REVOKE X FROM census_labeller;`). Say where the refusal appears (labeller container logs)
      and the exact remedy commands.
- [x] Runbook (from Task 3): a `/label` tap or nudge does not claim a window; only loading the
      page does — and a loaded window is never pre-empted by the repeat, which therefore may run
      one window after window 8 (D1a). One sentence where the runbook describes the repeat.
- [x] D2: drive `labeller.py` (against the test DB, as `census_labeller`) in a real browser and record
      evidence for: the heartbeat stops after a 400 (control the page clock — `HEARTBEAT_SECONDS`
      is 60 — rather than waiting); a callback exception STOPS the queue with "not saved:
      unexpected page error — reload" instead of stalling silently (that is what m11 built; it
      does not recover); the two 403 messages ("session expired" / "wrong address"); a D1
      non-served write showing the 400 reload text. The run holds the test database EXCLUSIVELY —
      no pytest from anyone while it runs. Screenshot or console evidence into the verification
      record.
- [x] Update the implementation record: every deferred item marked fixed (commit), or D4 recorded
      as not applicable.

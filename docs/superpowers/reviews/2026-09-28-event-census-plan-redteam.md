# Red team: event census implementation plan

**Target:** `docs/superpowers/plans/2026-09-28-event-census.md`, against spec
`docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` rev 7
**Date:** 2026-09-28 · **Reviewer stance:** hostile, fresh context

**How this was checked.** I read the plan, the spec and the code it names. I ran read-only
`py` snippets for `common.is_quote_page`, the MDE table, `datetime.fromisoformat` under 3.12
and 3.14, psycopg error classes, and `conninfo_to_dict`. I also ran `docker compose config
--services`. **The Docker daemon was not running** (`npipe ... dockerDesktopLinuxEngine: cannot
find the file`), so I executed nothing against Postgres. The Postgres claims below (B1 in
particular) come from the documented privilege model, and I mark them where it matters.

---

## Blockers: the plan as written fails, or produces wrong behaviour

### B1. As the labeller role, the test DB cannot see schema `public`, so every test that runs as the labeller fails

- **The mechanism.** All 16 DB fixtures reset with `DROP SCHEMA public CASCADE; CREATE SCHEMA
  public` (for example `tests/test_comprehend_integration.py:20-21`; grep lists 16 files).
  - A schema created with `CREATE SCHEMA` gives PUBLIC **no** privileges.
  - Only the `public` schema made by initdb carries `USAGE` for PUBLIC (PG15+ owns it by
    `pg_database_owner`, with `=U`).
  - So in every test DB, `census_labeller` has no `USAGE` on `public`.
- **What the labeller then hits.**
  - Unqualified names resolve through a search path that silently skips schemas without
    `USAGE`.
  - `has_table_privilege('census_events', 'INSERT')` therefore raises `UndefinedTable`
    ("relation does not exist") instead of returning false. So does every query.
- **Tests that fail:**
  - `test_grants_are_idempotent_and_complete`;
  - `test_missing_privileges_names_a_revoked_grant`;
  - `test_labeller_role_cannot_read_settings_or_write_items`, which expects
    `InsufficientPrivilege` but gets `UndefinedTable`;
  - both subprocess tests in Task 6. The service exits 3 on its self-check, so `GET /` never
    connects.
- **Why it misleads.** The error names a table that plainly exists, which invites the wrong
  hunt.
- **Missing from the plan's lists.** `LABELLER_GRANTS` is "exactly spec §6.1's list", which has
  no `USAGE ON SCHEMA public`. `missing_privileges` has no `has_schema_privilege` check.
- **Production.** It happens to work there, because initdb's grant survives. A restore into a
  recreated schema would reproduce the test failure on the host, and the self-check would
  report it as "unreachable DB" rather than naming the grant.
- **Same area, related:** `CREATE ROLE census_labeller` defaults to `NOLOGIN`. The plan's
  `apply_labeller_grants` text says only "CREATE ROLE IF NOT EXISTS".
- **Fix:**
  - `CREATE ROLE ... LOGIN`, and `ALTER ROLE ... LOGIN PASSWORD`;
  - add `Grant('schema', 'public', 'USAGE')` to `LABELLER_GRANTS`;
  - check it first in `missing_privileges` with `has_schema_privilege`, and qualify every
    object as `public.<name>` in the privilege checks;
  - state this deviation from the spec's list.
- *Confidence:* high, from the documented privilege model. Not executed (daemon down). The
  first run of Task 5 settles it.

### B2. Task ordering: `test_packaging` goes red at Task 3 and stays red until Task 8, while Task 7 Step 4 expects it green

- **The mechanism.** `tests/test_packaging.py` `_required_modules()` walks **every** import,
  function-level ones included (`ast.walk`, lines 71-92), starting from the COPY list, which
  includes `brief.py`.
  - Task 3 adds `import census` to `brief.py` (in `mode_census_prepare`).
  - `census.py` enters the Dockerfile COPY line only in Task 8.
- **The consequence.** `test_every_module_the_image_imports_is_copied_into_it` fails from Task
  3 onward. Task 7 Step 4 ("`py -m pytest tests/test_commands.py tests/test_packaging.py -q` →
  PASS") cannot pass. Any push between Task 3 and Task 8 fails CI's `test` job, and no image is
  published.
- **Fix:** move the COPY, ruff-list and `paths:` edits for each module into the task that
  creates the module's first importer:
  - `census.py` and `census_metrics.py` in Task 3;
  - `labeller.py` and `labeller_static/` in Task 6.

  Task 8 then keeps only the compose service and its tests.

### B3. The pass-2 repeat window has no items

- **What the plan writes.** Task 3 step 7 inserts `census_window_items` "for all 16".
  `test_prepare_freezes_16_windows_and_the_repeat` asserts it "covers exactly the eligible
  items of the 16". The 17th (pass-2) row therefore has **no** membership rows.
- **What reads it.** Task 4's `window_items(conn, window_id)` and `window_assignments(...)`
  ("all window items") take a `window_id`. Nothing says to follow `repeat_of`.
- **The consequence.** Session 17 serves an empty page, and Finish on it records zero items.
  The consistency readout (Task 9) compares pass 1 with nothing. No test serves or reads the
  repeat's items: `test_repeat_is_served...` checks only `current_task`, and
  `test_report_scores_window_two_pass_one` builds pass 2 by direct insert.
- **Fix:** either copy order_no 2's `census_window_items` rows onto the pass-2 id in `prepare`
  (preferred: every reader stays keyed by id, and the RESTRICT FK pins the same items), or
  resolve through `COALESCE(repeat_of, id)` in every reader. Add a test that
  `window_items(pass2)` equals `window_items(order_no 2, pass 1)`.

### B4. Abandon, which the spec allows, deadlocks the census or silently skips the gate

The failures follow from `current_task`'s rules as the plan writes them:

- **Order_no 1 or 2 abandoned.**
  - `go_status` is "None until order_no 1 and 2 are blind_done", so rule 3 never fires.
  - The census proceeds to windows 3–16 **with no go/no-go at all**. That is the pre-registered
    gate of §4.5, bypassed without a word.
- **Order_no 2 (pass 1) abandoned.** Repeat eligibility needs "7 days since order_no 2's
  `blind_done_at`", which never exists. After window 16, rule 6 returns `waiting` forever with
  a None date, and `complete` is unreachable.
- **Order_no 8 abandoned.** Eligibility needs "order_no 8 is `blind_done`", so the same
  deadlock follows.
- **The pass-2 window abandoned.** Order_no 2's precision is held "until the pass-2 window is
  `blind_done`", so it is held forever.

`test_abandon_counts_as_a_session_and_is_listed` covers none of these.

**Fix: decide each case and test it.** Suggested rulings:
- window 1 or 2 abandoned → `gate_failed`, because the gate cannot be evaluated and it goes to
  the operator;
- "8 is done" means `blind_done` or `abandoned`;
- order 2 pass 1 abandoned → the repeat is void (pass 2 is marked abandoned at once, and
  consistency is reported as unavailable);
- pass 2 abandoned → window 2's precision is released.

### B5. Two tabs (Review Focus #1): a later edit silently loses, and the test cannot see it

- **The mechanism.**
  - `client_seq` is a per-tab counter seeded at page load from `max_client_seq + 1`.
  - The latest state per item is the highest `client_seq`.
- **The scenario.** Phone and laptop both load at max = 10. The phone makes 30 edits (seq
  11–40), including item X → group A at seq 25. The laptop then moves X → B at seq 11.
- **The result.** The server returns 204, and the stored state is A, because 25 > 11. The
  laptop's UI shows B, which is not what was saved. Nothing reports it. With blind labels,
  nothing downstream measures it either.
- **Why the test misses it.** The plan's stated expectation, "the later write wins", holds only
  for **equal** seqs, and `test_equal_client_seq_resolves_to_the_later_row` tests exactly and
  only that case. Right and wrong code agree on that fixture.
- **What `client_seq` actually buys.** A tab sends its queue serially and in order (Task 6,
  "retry in order"). Within one tab, arrival order is therefore already intent order, so
  `client_seq` protects against nothing there. It is actively wrong across tabs.
- **Fix, either of:**
  - (a) order by `id` (arrival), with a per-page `tab_id` column so that a retried
    `(tab_id, client_seq)` already stored is ignored;
  - (b) the server answers 409 "stale, reload" when a write's `client_seq` ≤ the window's
    current max written by a *different* `tab_id`.

  Add the divergent-sequence test above. Note that M3's prediction changes with either fix.

### B6. The XSS and entity tests assert against a layer the plan does not say renders the titles

- **The plan says two things.**
  - Titles are rendered `html.escape(html.unescape(title))`.
  - "Page data goes into a `<script type="application/json" id="data">` block ... There is no
    inline JS." The UI (chips and "Add to…" entries showing a group's first title, most
    recently touched first; the precision pair) is by necessity built and updated by
    `label.js`.
- **The consequences, by rendering choice.**
  - **Items rendered client-side from the JSON:** the body contains `<script>...`, not
    `&lt;script&gt;`, so `test_hostile_title_and_javascript_url_render_inert` and
    `test_entity_titles_render_once_escaped` fail against correct code.
  - **Rows server-rendered and titles re-inserted by JS (chips and menu):** those Python tests
    pass, and the place where an `innerHTML` XSS or a double-escaped `AT&amp;T` would actually
    occur, `label.js`, has **no test at all**. The `http`/`https` scheme check has the same
    gap, if JS builds any link.
- **This is the-probe-measured-the-wrong-layer.** §6.2's guarantee lives in the JS path, and
  the tests read the server path.
- **Fix:**
  - Pick one path and say it.
  - The JSON carries `html.unescape(title)` (raw text), and `label.js` builds DOM with
    `textContent` or `createElement` only.
  - Add a static test that `label.js` contains no `innerHTML`, `outerHTML`,
    `insertAdjacentHTML` or `document.write`, and no `href` assignment outside a function
    that checks the scheme.
  - Add a test that the JSON block of a hostile title contains no literal `<`.

### B7. Task 10 runs the mutations before it starts the database, and M8's "0" cannot be told apart from "skipped"

- **The ordering.** Step 2 (mutations) runs before Step 3.1 ("Start the test Postgres and export
  `DATABASE_URL`"). M2, M3, M8 and M9 run DB-only files.
- **The consequence.** With no DB they **skip**, and report 0 failures. M8 predicts exactly 0,
  so a skipped run *confirms* the prediction. (Memory `mutation-diagnostic-demands-a-count`: a
  zero can mean the probe did not run.)
- **Fix:**
  - Start the DB before Step 2.
  - Run each mutation with `-rs` and record `passed`/`failed`/`skipped` per run.
  - A DB-file run with skipped > 0 is UNKNOWN, not a count.

**Blocker count: 7.**

---

## Pre-registered mutation counts (Task 10)

| # | Verdict | Why |
|---|---|---|
| M1 | Conditional | 1 only if `test_pooled_share_weighs_groups_not_windows` builds its unconfirmed window-B group with **no** shared event. If B's group has a shared event within one outlet (a natural way to make "not confirmed"), the mutation flips it and the count is 2. Pin the fixture in the plan. |
| M2 | Count right, target wrong | 2 is right for what it mutates. But spec §10 names a mutation of **token comparison**. The plan mutates expiry and revocation instead. A mutation of the comparison itself (`hmac.compare_digest` → True) predicts **0**, because the row is already selected `WHERE token_sha256 = $1`: the compare is decorative. Either drop the claim that the compare matters, or say so and add a kind mutation (see F12). |
| M3 | Under-specified | The latest-state ordering is used in **two** readers, `current_assignments` and `window_assignments`, and possibly a third SQL in `max_client_seq`-adjacent code. Only `current_assignments` has an out-of-order test. Mutating `window_assignments` alone predicts **0** today, and that reader feeds the go/no-go, precision and the readout. Mutate each site alone (memory: "a zero can mean the guard is DUPLICATED"), and add an out-of-order test through `window_assignments`. B5's fix changes this row. |
| M4 | 2 | Correct. |
| M5 | 2 | Correct. Recomputed: `[8]*16` → 21.76 / 24.42 / 32.98; `[10]*8` at ρ = 0.1 → 35.5 at t₇ and 30.5 at z. |
| M6 | 1 | Correct. |
| M7 | 1 | Correct: 2026-09-30 gives 9 days, and `9 < 9` is false. |
| M8 | 0, then 1 | Correct only once B7 is fixed. |
| M9 | Conditional | 1 only if the down script uses `DROP TRIGGER IF EXISTS` / `DROP FUNCTION IF EXISTS`. Without `IF EXISTS`, the mutated up leaves no truncate trigger, the down errors, and `test_down_refuses_with_labels_and_runs_when_empty` also fails: **2**. The 0006 precedent uses `IF EXISTS`; make it a stated requirement. |

---

## Fixes an executor could make inline

### Migration 0017 (Task 1)

- **F1. The RAISE placeholder.** The message is written with `%s`. In plpgsql `RAISE` the
  placeholder is `%`; `%s` renders as "...+00s are held". Write `RAISE EXCEPTION 'items
  captured before % are held ...', v_block_end;`.
- **F2. The row trigger must `RETURN OLD`.** A `BEFORE DELETE` row trigger that returns NULL (or
  `NEW`, which is NULL in a DELETE) **silently skips the delete** without error.
  `test_post_block_item_delete_succeeds` and the release test say only "deletes". If they
  assert no exception, a `RETURN NEW` trigger passes them while making every item undeletable
  forever. Assert `cur.rowcount == 1` and that the row is gone.
- **F3. "Children survive after rollback" is vacuous.** Rollback undoes any cascade anyway. Add
  the positive control: with no `census_block` row, the same delete removes the assertion and
  the triage row. That proves the fixture's children really are cascade-linked, and so that
  "survive" is not free.
- **F4. `census_events` needs an id column.** Spec §7 gives it none. `pg_get_serial_sequence`
  needs a serial or identity column, and the grant derivation errors on a table without one.
  Add `id BIGSERIAL`, or skip sequences for id-less tables.

### Tasks 2 and 3 (metrics and prepare)

- **F5. The second quote-page assertion cannot discriminate.** In
  `test_quote_pages_are_excluded_through_common`, "Brent Crude Oil" is **not** a quote page
  under the real function: `common.is_quote_page('Brent Crude Oil') == False`, measured. Its
  "→ None under the patch" half passes whether or not the real function is also consulted.
  Use `'LCO - Reuters'` (True under the real function, measured) or
  `'... stock price & latest news'`.
- **F6. `test_order_survives_corpus_growth_and_a_predicate_change` cannot fail.** The second
  `prepare` returns at step 1 ("already prepared") before reading anything. The real risk is a
  **reader** that re-derives membership from `items` by time range. Move the assertion to Task
  4: after 500 more items and the patched predicate, `census.window_items(conn, id)` returns
  exactly the frozen set.
- **F7. Add the ≥ 40-item rule test the spec requires (§10, "the ≥ 40-item rules hold").** It
  is absent. Seed one window with 39 eligible items and one with 40. Assert that the 39-item
  window lands in `census_skipped_windows` with reason `fewer than 40 eligible items (39)`, and
  that the 40-item window is eligible. Count a backlog item in the 40-item window so the rule
  is shown to count *eligible* items.
- **F8. `window_stats` must bucket in UTC.** `stratum = start.hour // 6` uses the hour of
  whatever offset psycopg returns, which is the session `TimeZone`. That is UTC in the docker
  image only by default. Apply `.astimezone(timezone.utc)` before bucketing, or `SET TIME ZONE
  'UTC'` on the connection.
- **F9. `gap_check` should count distinct item pairs.** "All pairs of assertions" over-counts
  when an item holds several assertions on one event. Use `SELECT DISTINCT a1.item_id,
  a2.item_id ... a1.item_id < a2.item_id`.
- **F10. `test_precision_estimate_weights_each_sampled_group_once` cannot discriminate.** With
  `[(9, 10), (1, 10)]` the pooled p (0.5) equals the mean of the window proportions (0.5), and
  an interval clipped to [0, 1] always contains 0.5. Use unequal `asked`, for example
  `[(9, 10), (1, 2)]`: pooled 10/12 = 0.833 against a mean of proportions of 0.7.
- **F11. `pairwise_agreement` and `adjusted_rand_index` with `None` are untested.** Singletons
  are `None`. A naive implementation that keys on group id puts every `None` item into one
  giant cluster, which inflates pass-to-pass consistency hugely, since most items are
  singletons in both passes. Add `ref={1:None, 2:None}`, `other={1:None, 2:None}` → zero pairs
  and no division by zero. Also state whether unsure items are excluded from consistency.

### Task 4 (session state) and Task 5 (access)

- **F12. Token kinds.**
  - `session_valid` must require `kind = 'session'`.
  - `open_session`'s consume must require `kind = 'link'`.
  - Otherwise a link token works as a session cookie until it expires, and a session token can
    mint sessions.
  - Test: a link token presented as `census_session` → 403.
- **F13. Compare times with the passed `now`, never SQL `now()`.** Spec §6.2's SQL uses
  `now()`, while the plan passes `now` into `mint_link` and `open_session`. If fixtures pin
  `now` to a fixed date, as the rest of the plan does, and the SQL keeps `now()`, then
  `test_link_is_single_use` starts failing on the day the fixed date is more than 10 minutes in
  the past, which is immediately. `test_expired_link_is_refused` is then meaningless. Say it
  once in the plan.
- **F14. `current_task` priority against Task 4's test fixtures.** Rule 2 (precision) outranks
  rule 3 (gate) and rule 5 (next window). As written, three tests get `precision` for window 1
  instead of the expected task:
  - `test_gate_blocks_after_two_thin_windows` expects `gate_failed`;
  - `test_gate_override_unblocks` expects order_no 3;
  - `test_repeat_is_served_after_window_eight_and_seven_days` ("windows 1–8 blind_done") expects
    order_no 9. It also needs ≥ 8 multi-outlet groups in windows 1–2, or an override, or it
    gets `gate_failed`.

  Each fixture must answer precision, use zero-group windows, or set `complete` directly.
- **F15. `test_window_two_precision_waits_for_the_repeat` checks only the withholding.** A hold
  that never lifts passes it. Add: after pass 2 is `blind_done`, the next task is `precision`
  on order_no 2 pass 1.
- **F16. The repeat pre-empts a window in progress.** Rule 4 outranks rule 5. If order_no 11 is
  `open`, half-labelled across sessions, when the repeat becomes eligible, the operator is
  switched mid-window. Serve an `open` window before starting the repeat.
- **F17. Guard `save_assignments` against a concurrent Finish.** Its closed-check is a separate
  read, so a concurrent Finish can interleave, which is Review Focus #4 under concurrency.
  `SELECT ... FROM census_windows WHERE id = %s FOR UPDATE` first. The labeller's column-level
  `UPDATE` on `census_windows` is enough for `FOR UPDATE`.
- **F18. Validate ids on write.** `save_assignments` should reject `item_id`s not in the window
  and `group_id`s from another window. Group ids are global, so a stale page can otherwise
  join items across windows. `save_precision` should accept only a pair from
  `precision_pairs`, normalised to `a < b`.
- **F19. Scope active minutes to the blind pass.** `window_active_minutes` should include only
  events up to `blind_done_at`; precision-page `open` events otherwise inflate the blind-pass
  minutes of the §4.5 gate.
  - *Design risk (spec-level):* reading 187 titles for more than 5 minutes before the first
    action logs nothing, and gets dropped as idle. Active minutes therefore undercount
    exactly the busy windows, biased toward passing the ≤ 80 gate.
  - A 60-second `visibilitychange`/heartbeat `action` while the page is visible would close
    it.

### Task 6 (labeller)

- **F20. Use `http.client`, not `requests`.** The conftest blocks `requests` **even to
  loopback**: `_no_outbound_http` patches `Session.request` unconditionally
  (`tests/conftest.py:144-171`). Any labeller test written with `requests` fails with
  `BlockedNetwork` and a teardown failure. `http.client.HTTPConnection` is also what lets the
  test see the 303 (urllib follows it) and send a mismatched `Host`. Raw sockets to 127.0.0.1
  pass the socket guard (`_is_loopback`), and the subprocess is unaffected.
- **F21. Do not let a connection sit idle in transaction.** A labeller connection left idle in
  transaction (autocommit off, a SELECT, no commit) holds `AccessShareLock`, and the next
  fixture's `DROP SCHEMA public CASCADE` then **hangs the suite**, in the foreground.
  - Use one connection per request inside `with db.connect() as c:`.
  - Shut the in-process server down in a fixture finalizer.
  - `terminate()` and `wait()` the subprocess in `finally`.
  - The same state in production would block migrations.
- **F22. Put the four headers on every response path.** `BaseHTTPRequestHandler.send_error`
  paths (400 malformed, 404, 501 for PUT/HEAD/other methods, 414) bypass `_send`, so they carry
  none of the four headers. That contradicts "every response". Override `end_headers` (or
  `send_error`) to inject them, and add 404 and 501 to
  `test_every_response_carries_the_security_headers`.
- **F23. An item count shown on every session but the repeat is itself the tell** (§4.6: "the
  header shows only the session number"). Show the count on all sessions or on none.
- **F24. Autosave, Finish and 409.**
  - Finish must go through the same ordered queue, behind unsaved actions. It must not jump it:
    otherwise a DB blip, then Finish, then the queued edits 409, loses labels.
  - A 409 must stop the retry loop and say "closed; reload". "Retry every 3 s" otherwise
    retries a 409 forever.
- **F25. Use `ThreadingHTTPServer`.** A single-threaded `HTTPServer` is stalled by one slow
  phone connection.

### Task 7 (Telegram) and Task 9 (readout)

- **F26. `/label` tests must stub `db.connect`.** Use `_FakeConn` as `_capture_db` does
  (`tests/test_commands.py:1199-1218`). Otherwise `_label_render` really connects:
  `test_label_sends_progress_and_a_link` fails with "Could not read" on a machine without
  `DATABASE_URL`, because `test_commands.py` has no skip marker.
- **F27. Move the nudge to the end of `mode_collect` and give it a timeout.**
  - "Right after `deliver`" puts it **before** `extract_signals`, `save_signals`, `mode_paper`
    and `clear_batch_state` (`brief.py:3123-3140`).
  - `_census_nudge` has no `connect_timeout`. A stalled Postgres then hangs collect before the
    batch state is cleared: the failure `test_collect_trading_failure_does_not_duplicate_brief`
    (`tests/test_trading.py:593`) exists for.
  - Use `connect_timeout=JOBS_DB_TIMEOUT_SECONDS`, plus an `options` `statement_timeout`.
- **F28. Assert the stage after the nudge.** `test_collect_still_delivers_when_the_nudge_fails`
  asserts `deliver`, which runs *before* the nudge. Assert `clear_batch_state` was called.
- **F29. Stub the nudge in the existing collect test.** `tests/test_trading.py:593` stubs
  neither `telegram_send` nor `db`. Once the nudge exists, that test connects to whatever DB is
  exported. If the census tables are in a `blind` state left by an earlier module, it sends,
  trips the network guard, and fails at teardown. Today only alphabetical module order saves
  it (`test_probe_*` and `test_score_*` re-drop the schema after the census modules). Stub
  `_census_nudge` there, or in an autouse fixture.
- **F30. Enforce read-only in the readout.** `test_report_is_read_only` compares row counts,
  which cannot see an UPDATE. Run `render` in `SET TRANSACTION READ ONLY`, in the test and in
  `main()`, so any write raises.
- **F31. Handle a lock `prepare` did not get.** `mode_census_prepare` must check the bool that
  `db.advisory_lock` yields (`db.py:119-130`). If it was not acquired, print "already running"
  and exit non-zero.

---

## Spec requirements the plan drops

1. **§4.3:** the readout prints "the capture-gap distribution". The plan stores and prints only
   the share, pair count, window count and band.
2. **§4.5:** "An abandoned census still reports its detectable difference at the current K". The
   plan prints the MDE only "after order_no 16 is blind_done", so a census stopped at the gate
   never reports it.
3. **§4.6:** consistency is "printed with its interval". The plan has no interval.
4. **§4.2(3):** the confirmed `c439ade` date is recorded, then never printed or used. The "by
   block half" split point is unspecified. Print the date, and whether the block straddles it.
5. **§6.1:** "If the host uses `DATABASE_URL` instead, the runbook sets the labeller's own
   equivalent". The runbook has no such step.
6. **§10:** a *revoked link* at `/open` → 403. Only revoked sessions are tested.

---

## Runbook (Task 10 Step 1)

- **Step 2: image creation time is not deploy time.**
  - `docker image inspect --format '{{.Created}}'` is when CI **built** the image, not when the
    host pulled and ran it.
  - Older images may be pruned or untagged, so `docker image ls <repo>` may list only the
    current one (UNKNOWN for this host).
  - Stronger evidence: the image's `org.opencontainers.image.revision` label, set by
    `docker/metadata-action`, shows whether an image contains `c439ade`. For the time, use the
    container's `.State.StartedAt`, or the supervisor's first `job_runs` row after the switch.
  - The spec accepted creation time, but the step should say what the number is.
- **Step 5: `-e CENSUS_LABELLER_PASSWORD` with no value.** Whether this resolves from `.env` or
  only from the invoking shell under compose v5 is UNKNOWN. If it is shell-only, the script
  exits 2. Add a check: `docker compose run --rm -e CENSUS_LABELLER_PASSWORD --entrypoint sh
  newsbrief -c 'test -n "$CENSUS_LABELLER_PASSWORD"; echo REAL_EXIT=$?'`.

---

## What breaks in six months

- **`news-brief-115` removes `scripts/` from the image** (it is marked TEMPORARY,
  `Dockerfile:43-55`). Runbook steps 5 and 8 then fail on the host, silently until someone runs
  them. The bead note is intent, not enforcement. Add a packaging test that every script the
  census runbook invokes is under a copied directory.
- **The labeller runs `:latest`** (the plan's service block). Any deploy during the census,
  which spans weeks because of the 7-day repeat gap, changes the labelling tool and
  `census.py` semantics mid-census. Sub-project 1 work will touch `census.py`. Pin the image
  digest in `.env` for the census duration.
- **Migration number 0017** is claimed by no file today (`ls migrations/`, top is 0016). But
  `uh0`, `6wc` and phase 2 are all queued schema work. Re-check at commit time.
- **Hidden coupling of collect tests to DB state and module order** (F29).
- **Fixed-date fixtures against SQL `now()`** (F13) are a time bomb, not a present failure.
- **The hold rejects whole statements.** `uh0`'s bulk `DELETE FROM items WHERE created_at < X`
  fails in its entirety on the first held row, not per row. Its acceptance text says "skip
  held items", so the retention query must filter `created_at >= census_block.block_end`
  itself.
- **Local interpreter drift.** It is Python 3.14.4 (`py --version`), while the image and CI are
  3.12. Anything 3.13+-only passes locally and dies in the container. `fromisoformat` of
  Docker's 9-digit fraction is fine on both, measured.
- **Spec typo, not a plan defect.** §4.4's m̄ = 6, ρ = 0.1 cell says 27; the formula gives
  26.49, which rounds to 26. The plan's 26.5 is right.

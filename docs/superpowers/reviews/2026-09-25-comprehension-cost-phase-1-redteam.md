# Red-team: Comprehension Cost Redesign, Phase 1 plan

**Reviewed:** `docs/superpowers/plans/2026-09-25-comprehension-cost-phase-1.md`, against spec
`docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md` §4, §6.1, §7, §8.
**Date:** 2026-09-25. **Method:** I read the code, and ran nothing: no tests, no DB, no network.
"Verified" means I read the code it refers to. "Inferred" means it rests on reasoning or on
outside knowledge, and each such item names the check that would settle it.

---

## (a) Task 4 keys the P1 fix on HTTP 402, and its own tests pin 400 as "not an account failure"

**Claim.** Running Task 4 may not deliver spec §4.2 ("stop charging items for account
failures"). The classifier is `_ACCOUNT_STATUSES = {401, 402, 403}`, and
`test_other_statuses_are_not_account_failures` parametrises **400** as a status that must
*not* abort. Every end-to-end test raises a synthetic `HTTPError(402)` at the
`call_triage`/`call_integration` seam, so the tests prove only that the code handles the status
the plan assumed. They say nothing about which status the host actually got.

**What is verified:**
- The status does reach the classifier: `brief._post_messages` (brief.py:2723) calls
  `raise_for_status()` and re-raises the `HTTPError`, and `resp` is attached.
- The evidence behind "402" is thin. The bead `news-brief-0rg` has "402 billing_error" in its
  title, but its description says "every hourly pass gets a **4xx** billing error". Spec §2.4
  says "a 4xx (`billing_error`, 402)". Nothing in the repo records the observed status line.
- Neither code path records the response body. `_timed_post` logs only elapsed time on failure,
  and `requests.HTTPError.__str__` is `"<status> Client Error: ... for url"`. So the host log
  holds the status and never the error `type`.

**What is inferred (outside knowledge, not measured here):** the Anthropic API's *documented*
402 is `billing_error`. But the widely reported response for an **exhausted credit balance** is
`HTTP 400, invalid_request_error, "Your credit balance is too low to access the Anthropic
API..."`. If that is what the host got on 2026-09-25, then after Task 4:
- the 402 branch never fires;
- `_is_transient` still returns False for the 400;
- triage still records `failed` and charges `attempts`, and integration still burns
  `integrate_defers` and then `integrate_attempts`;
- the bead closes "complete" on a green, mutation-checked suite, and the next empty balance
  retires items exactly as before.

**Fix.**
1. Before Task 4, settle the status from the host. The 25 Sep log holds lines like
   `Signals API attempt 1 failed: 4xx Client Error ...` (brief.py:2727). Grep them for the
   status, and write the observed line into the bead.
2. Classify by status **and** body. `_account_failure` should also parse `exc.response.json()`
   and treat `error.type == "billing_error"`, or a 400 whose message contains `credit balance`,
   as `"billing"`. Spec §4.2 already allows the `type` string "as confirmation".
3. Replace the synthetic 400 case in `test_other_statuses_are_not_account_failures` with a
   *real-shaped* 400 body, `{"error": {"type": "invalid_request_error", "message": "Your credit
   balance is too low..."}}`, which must classify as `billing`. Keep a plain 400
   (`"max_tokens: ..."`) as the negative control.
4. Log `resp.text` on the failure path. That is the repo's own
   `http-error-body-is-the-diagnosis` rule, and without it the next outage is diagnosable only
   from a status code.

---

## (b) The runbook's "Set the `TRIAGE_MODEL` row" writes a key nothing reads. Haiku never engages, and the by-effect check cannot tell.

**Verified in code:**
- `common.py` KNOBS declares `"TRIAGE_MODEL": Knob(str, "", env="NEWSBRIEF_TRIAGE_MODEL")`.
- `config.knob` (config.py:216-218) resolves `settings().get(spec.key(name))`, and
  `Knob.key()` returns `self.env or name`. So the row key is **`NEWSBRIEF_TRIAGE_MODEL`**. The
  compose anchor agrees (docker-compose.yml:135).
- Plan Task 11 Step 1.3 says to set "the `TRIAGE_MODEL` row" with the old runbook's
  `INSERT ... ON CONFLICT` shape. A row `('TRIAGE_MODEL', NULL, 'claude-haiku-4-5')` is
  **accepted and inert**. `_triage_model()` then keeps returning `common.MODEL`
  (`claude-sonnet-5`), and triage runs on Sonnet at 2x the price the §4 estimate assumes.
  Spec §4.5 uses the same wrong name.
- It will not be noticed. Runbook step 5 verifies "by effect" with
  `SELECT count(*), max(at) FROM comprehend_spend` and a `stop_reason=tool_use` log line.
  `_timed_post`'s log line (comprehend.py:260-266) does **not** print the model, and Sonnet also
  returns `tool_use`. Neither observation tells Haiku from Sonnet. `ignored_env_knobs` will not
  flag it either, because the row is not an environment variable.
- The unpriced-model guard does not help: Sonnet is priced.

**Fix.**
1. In the runbook, use key `NEWSBRIEF_TRIAGE_MODEL`. Make the statement `RETURNING key, value`.
2. Make the effect check discriminate:
   `SELECT model, count(*) FROM comprehend_spend WHERE stage='triage' GROUP BY 1` must show
   `claude-haiku-4-5` and no `claude-sonnet-5` rows after the flip.
3. Add `model=` to `_timed_post`'s log line.
4. Correct spec §4.5's "via the `TRIAGE_MODEL` row" too (`the-correction-didn't-propagate`).

---

## (c) In six months the budget alert has fired exactly once and can never fire again, while the horizon silently drops what the budget starves

**Verified in the plan's code (Task 9 `open_budget`, Task 10 `budget_verdict`):**
- The episode key is `budget:<exhausted_at>`. `mark_exhausted` uses `setdefault`, so the key
  is fixed for the whole episode.
- The key clears only in `open_budget`, when `balance >= allowance`, i.e. $1.50 banked.
- Every enabled pass spends whatever has accrued. The gate is `balance > 0`, and accrual is
  continuous at $0.0625/h. Whenever demand is at or above the allowance, the balance hovers at
  or just below zero and **never reaches $1.50**. That is spec §4's own central estimate:
  $1.40–2.40/day against $1.50, plus a 24k backlog and 643 awaiting.
- Even when demand drops slightly below the allowance (say $1.40/day), the balance climbs by
  about $0.10/day, so recovery takes ~15 days.

**Consequences:**
1. **The first exhaustion becomes permanent.** The first one will be runbook step 6's
   *deliberate* $0.01 test, or the backlog on day one. After that, `budget_verdict` returns the
   same key forever and `_alert_once` never speaks again. A later genuine event is invisible,
   such as a surge that `item_surge` misses (a new outlet, or a non-Reuters quote-page shape).
   Spec §6.2 relies on "the runbook watches the exhaustion alert" during the phase-2 gate
   window, and that alert will have been spent weeks earlier.
2. **The loss has no alert at all.** Under a steady-state shortfall, D4's policy converts
   budget starvation into `stale` verdicts and `aged_out` items. `Tally.aged_out` and
   `Tally.stale` are log-only. No monitor reads them, and `retirement()` counts only
   `integrate_attempts >= 3`. So "the KB silently stopped covering X% of material news" is
   exactly the failure the parent spec's gate cannot see. The gate sees `last_event_at` fresh,
   because *something* is always being integrated.
3. The alert text says "this clears once a full day's allowance is back". That promise is false
   in the steady state the spec predicts.

**Fix** (either works; the second is better):
- Re-key the episode per UTC day while exhausted (`budget:<date>`). That gives one message a
  day with the day's spend, `stale`-by-budget and `aged_out` counts, which is a digest rather
  than a pager.
- Or define recovery by **demand met**: a pass that finished without hitting the gate and with
  no pending material inside the horizon. Separately, add an alert keyed on the *loss*: the
  count of items that reached `stale` or `aged_out` without ever being offered to the model,
  per day, against the arrival count. That is the number that says the allowance is too low.
- In either case, runbook step 6 must reset `exhausted_at` after the deliberate test, or its
  own check consumes the only alert.

---

## Additional verified defects

1. **Task 5 Step 4: `steps_back_through(schema, "0014")` asserts.** `db._available` names
   versions by the full stem (`p.name[:-len("_up.sql")]`), and `conftest.steps_back_through`
   asserts `version in applied`. The version is `"0014_triage_stale_quote_page"`, so `"0014"`
   raises `AssertionError: 0014 is not applied`. Compare `TARGET = "0009_comprehension"` in
   tests/test_comprehension_schema.py:13. The module also imports it as `conftest.` rather than
   `from conftest import`. The fixture is `kb`, not `schema`; the plan admits this but hardcodes
   `schema` in every test body.
2. **Task 2: `tests/test_capture.py:237` breaks with `KeyError: 'Reuters Markets'`.**
   `test_both_reuters_feeds_resolve_to_one_outlet` builds `named` from `brief.RSS_FEEDS`. Plan
   Step 4 lists `:227` as "synthetic, leave it", but `:237` is the neighbouring test and reads
   the real list. Step 6 "they pass" is false. Fix: rename it to `"Reuters Business"`.
3. **Task 9: `test_an_empty_bucket_makes_no_call` and
   `test_a_pass_stops_when_the_balance_runs_out` fail as written.** The state's `at` is
   `datetime.now()` taken *before* `_seed()` and `run()`. `open_budget(now)` accrues
   `1.5 × elapsed/86400` (about 1e-7 to 1e-6 for milliseconds), so balance `0.0` becomes
   `> 0`, `can_spend()` is True, and one call is made. In the second test, `-0.001 + ε` misses
   `pytest.approx(-0.001)`, whose tolerance is 1e-9. Fix: make `run(conn, now=None)`
   injectable, as `capture.run` already is, and pass the state's `at`. Also decide what "empty"
   means under continuous accrual (see (c)): a floor of one estimated call cost is more honest
   than `> 0`.
4. **Task 9 `accrue`: a naive ISO timestamp crashes every pass.** `datetime.fromisoformat` is
   inside the `try`, but `now - then` is outside it. A hand-edited `"at": "2026-09-30T00:00:00"`
   (no offset) parses fine and then raises `TypeError` (aware minus naive) on every pass. That
   is Review Focus 1's own scenario. Fix: move the subtraction inside the `try`, or reject
   `then.tzinfo is None`. Add the case to `test_a_corrupt_budget_row_is_reinitialised_not_fatal`.
5. **Task 1 Step 8 changes `record_poll(..., len(entries))` to the post-filter count.**
   `feed_polls.entries_seen` is the cap instrument: "100 entries every poll" is how capping was
   measured on 2026-09-08 (see `capture_sources` and the pinned-set test's comment). A feed
   whose 100-slot window fills with quote pages will now record, say, 3. That hides exactly the
   crowding-out that loses real articles unobservably. Fix: record the RAW `len(got.entries)` in
   `record_poll`, and apply the filter only to storage. Record sightings raw too, or accept that
   rolled_off loses them.
6. **Spec §4.3 coverage gap:** the exhaustion alert must state "the untriaged/**awaiting**
   counts". Task 10's `budget_verdict` reports only `untriaged`. Fix: add the integration-select
   count, with the same predicate as Task 7's SELECT, horizon included.
7. **Runbook step 1's verification SQL cannot see the escaped shape** that Review Focus 5 says
   exists: `ILIKE '%stock price & latest news%'` misses `&amp;`. It also has no positive
   control, so "0" cannot be told apart from a probe that matches nothing. Fix: add
   `OR i.title ILIKE '%stock price &amp; latest news%'`, and run the same query against a
   pre-deploy window, where it must be > 0.
8. **Spec §4.1 asks for a grep of every reference to the old name, including host state
   keyed by name.** Task 2 Step 4 greps `*.py` only. Name-keyed host state lives in
   `feed_sightings.source_name`, `feed_polls.source_name`, and any `poll_every_minutes`
   override. No action is needed on those (`failing_feeds` ignores a no-longer-polled name,
   which I verified at capture.py:579-650), but the plan should say that was checked rather than
   skip it.
9. **Fixed `/tmp` paths** in Task 2 Step 5 (`/tmp/rb.xml`) and Task 11 Step 3
   (`/tmp/p1-pytest.log`, read back with `tail`) are shared mutable state under concurrent
   sessions (`the-log-was-not-only-yours`). Use the session scratchpad, and truncate before
   writing.

## Checked and found sound (so these need no re-check)

- The constraint names in 0009 are `item_triage_verdict_check`, `item_triage_reason_check` and
  `item_triage_check`. The table-level CHECK is unnamed, so Postgres auto-names it. No later
  migration touches them (0010 and 0013 add columns only).
- `comprehend` importing `config` at module scope creates no cycle: config imports common and
  db, and common imports config lazily.
- `state_store` patches `config.runtime_state`, `set_runtime_state` and `clear_runtime_state`,
  and `brief.load_state` and `save_state` route through them. So Task 3's and Task 10's
  `state_store` assertions reach the real code path.
- Existing `run()` tests that do not use `state_store` will hit the real `runtime_state`
  table through a second `db.connect()`. That works, because the `kb` fixture rebuilds the
  schema per test.
- `_add_item`, `_outlet`, `_attempts`, `_defers`, `NOW`, `_run` and `_entry` all exist with the
  signatures the plan uses. The surge SQL and the `_items` fixture arithmetic bucket correctly
  (day 0 through day 7).
- "Ukraine talks stall" still matches the converted `_tracked_material`'s story "Ukraine talks",
  so Task 4's integration test survives Task 6.

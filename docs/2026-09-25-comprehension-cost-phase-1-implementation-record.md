# Comprehension cost redesign, phase 1 — implementation record

**Date:** 2026-09-25 · **Epic:** `news-brief-2r5` (open until the host runbook is run) ·
**Range:** `3652583..` this record's commit, on `main` ·
**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md` ·
**Plan:** `docs/superpowers/plans/2026-09-25-comprehension-cost-phase-1.md` ·
**Host steps:** `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md`

This records what the plan did NOT say: the decisions taken while executing it, why, and
what the reviews found that the plan and its red-team had missed. The execution ledger it is
distilled from was scratch and has been deleted.

## Outcome

Eleven tasks and eleven task reviews, followed by a final whole-branch review and one fix wave
(see the last section). The suite went from 1742 to **1829** passed, 0 skipped, run against a
real Postgres. `ruff check .` and `ruff format --check .` are clean. Seven of the eleven tasks
(3, 4, 7, 8, 9, 10, 11) needed one fix round, and no task needed a second.

**Nothing has run on the host.** `COMPREHEND_ENABLED` must stay off until the runbook's
step 2, the old gate, has run. That is possible on or after 2026-09-30 00:13:37Z.

## Defects the plan carried, found by task review

Each item below was in the plan's own code or tests. They are listed because they are the
same kinds of mistake a future plan written this way will make.

| Task | Defect | How it was found | Fix |
|---|---|---|---|
| 3 | The surge test's small-outlet fixture (5→15) failed the ratio test as well, so it could not tell whether `SURGE_MIN_EXCESS` was doing anything | A pre-registered mutation predicted 1 failure and measured 0 | Changed the fixture to 60→200, which passes the ratio test so only the floor rejects it |
| 4 | `_account_failure` raised `AttributeError` on a JSON body whose `error` was a string. It runs *inside* `run()`'s `except` handlers, so it would crash the whole pass | The reviewer built a malformed response and reproduced it | Moved the extraction inside the `try` |
| 4 | The "credit balance" body check ran for every status, so a 429 or 5xx mentioning it would abort instead of deferring | The reviewer read the code against the spec's wording ("a 400 whose…") | Gated the check on `status_code == 400` |
| 4 | The brief's `-k` filter left out a test because of its name, so a mutation read 0 failures when the true count was 1 | The implementer re-ran against the whole module | Rule: mutation checks run against whole modules, never `-k` |
| 5 | The only test of `item_triage_stale_check` was also refused by the biconditional, so the constraint itself was never tested | Controller pre-flight | Added two pairs that only the stale check rejects |
| 8 | The integration spend line had no test. A mutation predicted 0 and measured 0 | Pre-registered prediction | Added a test through `run()` |
| 8 | That new test could not tell the triage model from the integration model: with no settings row, both resolve to `common.MODEL` | The reviewer mentally deleted the code under test and saw the test still pass | Set the two models to different values in the test |
| 9 | **Budget persistence was untested.** `tests/conftest.py`'s `state_store` stores the live dict, so deleting the `set_runtime_state` call in `Budget.debit` passed the suite. In production that would refill the bucket every pass | Opus reviewer, confirmed with a probe | Added tests against the *real* `runtime_state` table, which round-trips through JSON. The fixture itself is bead `news-brief-2ln` |
| 9 | A knob set to `inf` made the cap infinite, silently disabling the guard. A stored `nan` balance healed to the full cap | Reviewer | `_finite_or_default`; `accrue` treats a non-finite balance as corrupt |
| 10 | `created_at >= %s::date` is cast in the *session* time zone, and `db.connect()` never sets one | Reviewer | Compare against a timezone-aware UTC midnight from Python. Pinned with a UTC+14 session test that read "1 went stale today" before the fix |
| 11 | The runbook's step 5 did not say to record `cutover`, which step 7 needs as `<flip>` seven days later | Reviewer | Added a "record it" line, plus a fallback query that was checked before being written in |

One pattern covers most of these: **a test whose fixture could not discriminate between
the correct code and the bug**. That meant equal knob defaults, a fixture that tripped two
conditions at once, or an aliased in-memory store. Reading the tests doesn't reveal it. A
pre-registered mutation count does.

## Rulings taken on the operator's behalf

Each ruling gives what was decided, why, and what it costs if wrong.

- **R1** Executed on `main` with no worktree. The repo is solo and commits were authorised. *Cost:* none, since each commit can be reverted.
- **R2** Task 5 used the module's real `kb` fixture and the full migration stem. *Cost:* one failing test.
- **R3** A DB-backed test that skips counts as a failure. *Cost:* none.
- **R4** Subagent commits carry their own model in the `Co-Authored-By` line. *Cost:* a cosmetic trailer difference.
- **R5** The surge fixture was changed to 60→200. *Cost:* one test edit.
- **R6** Both Task 4 defects above were fixed immediately, not deferred. *Cost:* one small diff.
- **R7** Added the two refusal pairs that only the stale check rejects. *Cost:* two test cases.
- **R8** For Haiku 4.5, `thinking: {"type": "disabled"}` is **documented as accepted, not observed**. The claude-api skill lists 400s only for Fable 5/5.1, Opus 5.5, and Opus 5 at xhigh/max. *Cost:* if wrong, the first Haiku triage batch gets a 400, and a 4xx is charged to items. That is why runbook step 3 says to watch the first pass.
- **R9** `aged_out` deliberately counts only `integrated_at IS NULL`, and **not** rows that were integrated under an older prompt version. Those are already in the KB. Counting them would make every `INTEGRATE_PROMPT_VERSION` bump look like mass loss. The "mirror the predicate" rule applies to probes that must *agree* with production, not to a count with different meaning. *Cost:* the count leaves out version-stale old rows.
- **R10** Swept the comments that still argued from ascending order. The mechanism they described changed: a stuck item is now re-paid until it passes the 14-day horizon, not forever. *Cost:* none, comments only.
- **R11** Added a test for the *integration* `can_spend` guard, which the plan left untested. *Cost:* one test.
- **R12** Made the triage and integration models differ in the spend test. *Cost:* one test edit.
- **R13** Extracted `aged_out_count(conn)` instead of copying its SQL into `budget_verdict`. *Cost:* none.
- **R14** Added tests that `_abort` persists its reason and that the abort alert is wired up. *Cost:* two tests.
- **R15** Budget persistence is tested against the real `runtime_state` table. The debit test deliberately does *not* exhaust the bucket, because `mark_exhausted` also writes the whole state and would mask a missing debit write. *Cost:* none.
- **R16** Took the cheap money-guard minors: a capped first balance, non-finite knob and balance handling, and a truthful log line. Two were deferred:
  - a debit failure is misfiled as a batch failure, which needs a DB outage mid-batch;
  - time spent in an unpriced-model abort accrues budget, bounded by the cap.

  *Cost:* small.
- **R17** The `state_store` aliasing is its own bead (`2ln`), not part of this plan. *Cost:* other tests may hide the same class of bug until that bead is done.
- **R18** Fixed the UTC day boundary. *Cost:* one test.
- **R19** **Kept** clearing the abort state on a clean pass, even one that made no paid call. A premature clear fails *loudly*: the next pass with work aborts again and the alert re-sends. The worst case is a duplicate Telegram message. A stricter rule would have to treat `unpriced_model` (proven fixed by the price check alone) differently from billing/auth (proven only by a successful call). *Cost:* one duplicate alert per mis-cleared episode.
- **R20** The controller re-reviewed the small fix rounds itself, by reading each diff and checking the pre-registered mutations, rather than dispatching a re-reviewer. The final review was told to scrutinise those commits. *Cost:* a fix-round regression would surface late.

## Facts established by reading or measuring, not by the plan

- **Prices** (claude-api skill, `python/claude-api/README.md:502,509`): `claude-sonnet-5` costs $2/$10 per million input/output tokens, and `claude-haiku-4-5` costs $1/$5.
- **Blank compose values fall back to knob defaults.** `common.coerce_knob` returns the default for `""` before converting to a number. `${COMPREHEND_DAILY_BUDGET_USD:-}` therefore gives $1.50, not $0 and not a crash.
- **`config.runtime_state()` opens its own connection.** A budget debit commits immediately, while the ledger row commits with its batch. A crash between the two leaves the budget debited without a row, which errs toward underspend.
- **The monitor runs before comprehension within the same hourly tick**, per `scheduler.SCHEDULES`. A budget-exhaustion alert therefore arrives about an hour after the exhaustion.
- **Migration mutations break both halves of the up/down pair.** Mutating an up script also breaks every rollback test that passes through it (Task 5: 4 failures measured against 2 predicted). `tests/test_db.py` hardcodes the list of migration versions.
- **`docker compose config` needs `POSTGRES_PASSWORD`**. Set any placeholder value to check that the file parses.

## Open, filed as beads

- `news-brief-2ln` (P2 bug): make the `state_store` fake round-trip values through JSON. Then see which tests break; each one was relying on the aliasing.
- `news-brief-eqs` (P3): is a timed-out request billed? If it is, the bucket never sees that spend. Runbook step 7's comparison of the ledger against the Anthropic console answers this.
- `news-brief-vlg`: revise the phase-2 plan (batching). Its prerequisites are this phase shipping and a clustering-recall spike.

## Final whole-branch review

This review was run on the most capable model. Its verdict was **"with fixes"**. It had checked, as holding under normal operation, that:

- every paid call is guarded by the budget, recorded in the ledger, debited, and routed through `_account_failure`;
- every early return is consistent;
- the flip's first hour cannot spend more than the bucket holds.

It found the one class of bug that no per-task review could see, because six tasks had edited `run()` in turn.

- **I1. The model was re-resolved at every use across a pass lasting up to 40 minutes, against a 60-second settings cache.** The reviewer's probe switched to an unpriced model mid-pass. The result was 0 ledger rows, a $0 debit, 3 items marked `failed`, and no abort. **Fixed:** the models are now resolved once at the top of `run()` and passed through everywhere, including provenance. Tests change the setting *inside* the fake call. See memory `snapshot-config-per-unit-of-work`.
- **I2. Quote pages that were triaged `material` before this deploy still reached integration,** and step 4's recovery SQL would re-queue exactly those. **Fixed:** the integration loop reclassifies them as `immaterial/quote_page` without a model call.
- **I3. Runbook step 6 would report "no alert" on a day the budget had already run dry naturally,** because the alert key is `budget:<UTC date>` and only fires once. **Fixed:** a pre-check reads `runtime_state` first.
- **I4. Runbook step 7's console comparison could not answer `eqs`.** The brief, signals and claim-verify also use Sonnet. **Fixed:** the comparison is now Haiku-only. It also names the one hardcoded Haiku caller the fix wave found, `brief_memory.reconcile_ledger`, whose daily call must be subtracted.
- Minors: step 5 now states the ~2x first-day spend, the "watch the Haiku pass" note moved into step 5, and a pre-flip check confirms the model rows resolve to priced ids. A test now pins the integration order, and `inspect_integration.py` warns that its calls are unledgered.

The fix wave was one dispatch (`9f04bd3`, `20b85e8`) followed by one scoped re-review. All findings were addressed with no new breakage. Four pre-registered mutations each failed exactly 1 test, as predicted. The gate finished at **1829 passed, 0 skipped**, with ruff clean.

- **R21** Took I1–I4 and the cheap minors in a single wave. *Cost:* none beyond that wave.
- **R22** Split three items off into beads rather than including them:
  - M5, committing the spend row immediately (`711`). This changes `write_batch`'s savepoint into an outer transaction, and the current behaviour already errs toward over-debiting.
  - M7, the triage window stalling under long exhaustion (`54x`).
  - The pre-existing session-time-zone `date_trunc` in `select_sampled` (`7gp`).

  *Cost:* until `711` lands, the `eqs` comparison is noisier.
- **R23** Reworded one sentence in runbook step 7 myself, after the re-review. It had claimed "nothing calls Haiku" as an absolute and then contradicted that in the next paragraph. This was a doc-only edit with no fix wave. *Cost:* none.

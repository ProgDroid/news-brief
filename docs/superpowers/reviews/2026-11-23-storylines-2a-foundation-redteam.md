# Red team: storylines plan 2a (foundation)

Plan: `docs/superpowers/plans/2026-11-23-storylines-2a-foundation.md`. Spec: `docs/superpowers/specs/2026-11-21-storylines-design.md` revision 5, §18 item 1. Reviewed 2026-11-23, adversarially. Settled decisions (three plans, Haiku attaches and Sonnet checks, the §8.2 bars, seed then propose then accept, no trial) are not re-argued.

## What was opened

- **Read directly:** `brief.py` (`HELP_TEXT` :716, `_handle_callback_query` :1140, `_handle_telegram_update` :1289 including `/evidence` at :1347, `load_local_theses` / `load_portfolio_theses` / `load_theses` :2156-2205, `_post_messages` :2805, `_thesis_keys_quiet` :2849, the realtime brief loop :3150-3189, `_dump_raw_batch_result` :3192-3234, `mode_watch` :4573, `MODES` :4705, `JOB_MODES` :4727).
- **Common and scheduling:** `common.py` (logging :66-100, `Knob` / `KNOBS` :156-342, `_tg_api` / `telegram_send_buttons` / `telegram_edit_text` :611-660), `scheduler.py` (whole file), `supervisor.py` :40-160.
- **Comprehension:** `comprehend.py` :455-700 (the price tables, `cost_usd`, `record_spend`, and the pre-call price check in `run`).
- **Migrations:** `0006_knowledge_base_up.sql` (`outlets`, `items`), `0015_comprehend_spend_up.sql`, and both halves of `0018_thesis_evidence`.
- **Build and CI:** `Dockerfile` :30-75, `docker-compose.yml` :20-60, :95-175 and :240-260, `.github/workflows/docker-publish.yml` (`paths:` and the ruff lines).
- **Tests:** `tests/test_packaging.py` :100-170, :260-310 and :520-540; `tests/test_db.py` :30-140; `tests/conftest.py` :80-110; `tests/test_supervisor.py` :810-822; `tests/test_job_interlock.py` :340-380; `tests/test_thesis_evidence.py` :1-40; and greps of the suite for `SCHEDULES` and `KNOBS`.
- **Located but not read in full:** in `dated_memory.py`, only the function index was read (`validate_evidence` is at :275, as cited). The `watch.py` names were checked by grep (`WatchFileError`, `EventRow.name` / `.trigger`, `load_watch`). In `.beads/issues.jsonl`, only `115`, `ag1`, `uh0`, `cg3`, `c5k`, `sse` and `al7.3` were read.
- **Not opened:** `db.py`. Nothing in the plan hangs on its line numbers.

**The line references hold**, with the exceptions named below. Those that check out:
- `brief.py` :716, :1140, :1347, :3910 (area), :4060, :4573, :4705 and :4727;
- `Dockerfile:41`, and workflow `paths:` and ruff lines :80/:81;
- `tests/test_packaging.py` :115/:148/:160/:277, and `tests/test_db.py:59`;
- `comprehend.py:529` (`record_spend`) and `dated_memory.py:275`.

---

## Objection 1 (certain failure): `record_spend(conn, "attach", …)` violates a CHECK constraint

The plan's Global Constraints and Task 6 record attach spend with `comprehend.record_spend(conn, "attach", model, usage)` into `comprehend_spend`. But `migrations/0015_comprehend_spend_up.sql:8` declares:

```sql
stage TEXT NOT NULL CHECK (stage IN ('triage', 'integration')),
```

No later migration touches that table (grep of `migrations/` for `comprehend_spend` finds only 0015's up and down), and the plan's 0019 schema does not alter it.

**The consequence follows Task 6's own ordering:** "After a successful post: `record_spend(…)`, then commits".
- Every successful attach call raises `CheckViolation` after the API call has been paid for.
- That aborts the transaction holding the chunk's `record_decision` rows. The decisions are lost, and `attach_pass` crashes.
- The next pass re-selects the same items and pays again. In production that is 48 paid, discarded calls a day, with a red `/jobs` row.

**How it gets past the plan's tests:**
- Every stubbed Task 6 test passes, `test_spend_recorded_per_successful_call` included, because it stubs `record_spend`.
- Only the DB-gated `test_end_to_end_attach_pass_on_a_real_schema` executes it. It fails at Task 6 Step 4, after 0019 was committed in Task 2.
- The implementer then has to improvise a schema change into an already-committed migration, or a 0020 that collides with plan 2b's reserved number.

**A related unpriced-model gap.** `comprehend.run` (`comprehend.py:681-686`) refuses an unpriced model before any call, because `record_spend` raises `UnpricedModel` after the paid call (the comment at :669-677 records that exact probe). `attach_pass` has no such pre-check. A `STORYLINE_ATTACH_MODEL` row naming a valid but unpriced model reproduces the paid-then-crash loop above.

**Fix:**
- 0019 drops and re-adds the CHECK with `'attach'`, and the down migration restores it. Better, 0019 also renames the stage set to cover the future `update` / `propose` / `confirm` stages, which plans 2b and 2c will otherwise hit the same way.
- `mode_attach` calls `comprehend.price_of(model)` before selecting items, and logs `status=unpriced_model`.

## Objection 2: a response that judged nothing charges every article, and nothing alerts

The Global Constraints and Task 6 say that "an unusable response" and "any other exception, or a `whole` result" call `record_failure` for every item in the 60-article chunk, at `ATTACH_MAX_ATTEMPTS = 3`.

**The plan cites memory `retry-budget-needs-a-verdict` for this, but that memory's own 2026-09-10 section (`h8p`) reverses it:**
- "a batch whose *response* could not be parsed judged nothing either, so it must not charge … 45 items were retired in one day for an `items type=dict` that said nothing about them."
- The codebase carries the result: `COMPREHEND_MAX_DEFERS` (`common.py:304-313`) is a budgeted defer counter, separate from attempts, with its own ceiling. A second sibling, `COMPREHEND_MAX_LINK_DEFERS`, exists because "do not put two different no-verdict causes on one counter".

**Concrete ways the plan retires a day of articles that no model judged:**
- **Truncation:** a `max_tokens` stop (`signals-parse-error-is-truncation` is this project's most repeated failure) makes `stop_reason != "tool_use"`, so `whole` is set and all 60 articles are charged.
- **A config row:**
  - A typo in the `STORYLINE_ATTACH_MODEL` row, a retired model id (404), or a rotated key (401) is a 4xx.
  - Under the plan a 4xx charges. After 3 passes (90 minutes), every article in the 24 h window is permanently `failed`. Every later article follows it three passes after capture.
  - Each of these is a statement about configuration, not about the article.
- **No alert:**
  - `mode_attach` returns normally in every case. It raises only on an uncaught exception, so `/jobs` stays green.
  - The only signal is `failed=N` in a log line the rollout checks once, "the next day".
  - Memory point 5 ("a one-way counter with no alerting is a stop-loss you have not built"; report the population one failure away from the door) is not applied.

**Why it matters downstream.** Plan 2b treats attachments as the nightly update's whole input. Retired-as-failed articles are invisible to every storyline, and the loss lands on the oldest articles in a pass first, the same bias the memory records.

**Fix:**
- A `whole` response, and a 4xx on the *request* (400 included, since a batch-level 400 is about the payload, not one article), defer into a separate `defers` column with its own ceiling, as `h8p` did.
- Only per-article problems from a parsed answer charge `attempts`.
- `mode_attach` raises, or alerts once per episode, when a pass ends `deferred`, or with `failed` above a small share, or with every chunk `whole`.

## Objection 3: Task 1 prices the brief at a hardcoded Sonnet 4.6 rate the project's own price table contradicts

Task 1's `CostLine.usd` is the logged `est_batch_cost`, and `summarise` projects 30 days from it. That figure is computed at `brief.py:3219-3221`:

```python
# Sonnet 4.6 Batch API: $1.50/M input, $7.50/M output, web_search $0.01/req.
est_cost = in_tok / 1e6 * 1.50 + out_tok / 1e6 * 7.50 + searches * 0.01
```

**The spec's rate and the log's rate disagree:**
- Spec §11 says the budget is "at `comprehend.PRICES_PER_MTOK` rates". There, `claude-sonnet-5` is (2.00, 10.00) with `BATCH_FACTOR` 0.5, that is $1.00 / $5.00 batch (`comprehend.py:472-485`).
- On tokens, the log figure is therefore 1.5× the project's own price for the default `MODEL`.
- Example: spec §11's $0.41 line (205,311 in, 9,907 out, 3 searches) is about $0.285 at the table rate. The "~$13 a month" brief line would be about $9, and the "at the floor" conclusion moves by roughly £3.

**Why the plan cannot correct it:**
- `CostLine` keeps `input_tokens` and `searches` but drops output tokens.
- The log line names no model.

**Realtime days are not priced either:**
- Fallback days "are counted, not priced". But the `Realtime brief:` line (`brief.py:3167-3172`) carries `usage(in=… out=… searches=…)`, so they can be priced.
- A fallback day costs full price, not batch.
- `projected_30d` is then a mean over batch days only, scaled to 30. It omits the most expensive days, which pulls in the opposite direction to the overstatement.

**What hangs on the number.** Rollout step 1 makes it decisive: "If the total is above the £20 floor, the first dial is the web searches". A wrong price can turn a dial the operator didn't need to turn, or hide a real overrun.

**Fix:** parse `out=` too, and price both line kinds from `comprehend.PRICES_PER_MTOK` and the `MODEL` row (batch factor for batch lines, full rate for realtime). Print the logged `est_batch_cost` beside the result as a cross-check, with its rate named.

---

## Fixed check: paired facts and the claim a reader infers

| Plan sentence | Facts set side by side | Inferred third claim | Verdict |
|---|---|---|---|
| Task 6: "update any test that counts `SCHEDULES` (the survey found `test_supervisor.py:810`, `test_commands.py:1108-1115` and `test_scheduler.py:259`)" | three tests found by a survey | these are the tests that break, and the list is complete | **Unsupported, and false.** `test_commands.py:1108-1115` and `test_scheduler.py:259` iterate `SCHEDULES` and adapt by themselves. The test that actually breaks is not listed: `tests/test_job_interlock.py:365` asserts `would_run == {"collect", "monitor", "capture", "comprehend"}` at 06:05 UTC, and a 30-minute `attach` with 10-minute grace is due at 06:00, so it joins the set. It is DB-gated, so it fails only with `DATABASE_URL` exported, which is in CI. |
| Task 6: "`supervisor.py:78`: the comment reads `9 schedules x 2 statements`" | a quoted comment | adding `attach` makes it 10 | **The fact is false.** `supervisor.py:78` reads `8 schedules x 2 statements x 0.5s`, and `scheduler.SCHEDULES` has 8 entries. With `attach` it must read 9 (`test_supervisor.py:820-822` enforces equality), and the total becomes 43 s plus 5, about 48 s. An implementer trusting the quote writes 10 and fails that test. |
| Global Constraints: "Spend is recorded with `record_spend(conn, "attach", …)`, into `comprehend_spend`" | the function exists, and the table exists | the table accepts stage `attach` | **Unsupported, and false** (Objection 1). |
| Task 1: "the days that logged `Realtime brief:`. Their cost is not in `est_batch_cost`, so they are counted, not priced" | realtime days are logged; their cost is not in `est_batch_cost` | they cannot be priced from the log | **Unsupported.** The `Realtime brief:` line carries usage (Objection 3). |
| Global Constraints: "`tests/test_packaging.py:277` checks one direction of that pairing, so check the other by hand" | 277 checks anchor → read | nothing checks KNOBS → anchor | **Supported.** A grep of `tests/` for `KNOBS` finds no KNOBS → anchor assertion. `test_packaging.py:235` builds the consumed set for the 277 direction. |
| Plan-level calls: "at most 300 a pass … articles older than the lookback are simply never decided" | a 24 h lookback, and a 300 cap | never-decided articles are harmless | **Unsupported but low-stakes.** Spec §6 requires that "attached to nothing" and "not looked at yet" never look the same, and a never-decided article stays in "not looked at" forever. Plan 2b's unclaimed rule (§7.3) must count undecided articles as unclaimed explicitly, or they vanish. State it in the plan for 2b. |
| Rollout step 3: "roughly $8–10, with two Sonnet runs and one Haiku run over about 90 chunks each" | 3 runs, about 90 chunks | the cost estimate | **Roughly supported.** At about 11k in and 1.5k out per chunk at table rates: Sonnet about $3.4 a run, Haiku about $0.2, total about $7. |

### Residuals that do not decompose

1. **Task 7: "Score only items that all three runs decided, and print how many were excluded."**
   - One number is printed. The parts are: items H failed or left unanswered, items S1 failed, items S2 failed, and chunks never run.
   - These are not neutral. An article Haiku failed is dropped from J(H,S1) and from H's attach rate, so a Haiku that fails 10% of articles is scored on the 90% it handled. In production those failures retire unattached.
   - Spec §8.2 step 3 asks for attach rates "on all articles".
   - **Fix:** print exclusions per run, and count an H-failed article as an empty set (a miss) in H's attach rate.
2. **Task 6's log counts.**
   - On a transient error the plan "counts the chunk's items as `deferred` … and stops". Items in the later chunks are counted nowhere, so `items ≠ attached + none + failed + deferred`.
   - `attached` is also undefined: it could count items or `item_attachments` rows.
   - Rollout step 5 reads these numbers.
   - **Fix:** count every unprocessed item as deferred, define `attached` as items, and assert the sum in `test_log_line_carries_every_count`.

The Task 7 residual is close in weight to Objection 3. It is kept as a fixed-check finding because the schema enum and `maxItems` should make per-article Haiku failures rare, but that is UNKNOWN until the check prints the split.

## Runner-up objections (not in the top three)

- **The rollout's three scripts depend on a `COPY scripts/` line that an open bead may remove.**
  - `Dockerfile:46-58` copies `scripts/` and labels it TEMPORARY.
  - `news-brief-ag1` is open, and its brief says "decide scripts/ in the Dockerfile COPY". The only test pinning that line is `test_census_runbook_scripts_ship_in_the_image` (`tests/test_packaging.py:524`), which ag1 deletes with the census.
  - `scripts/storylines_attach_check.py` must be re-run when the live count doubles and again at 60 (spec §8.2 step 7), months from now.
  - **Fix:** add a test like `test_census_runbook_scripts_ship_in_the_image` for `brief_cost.py`, `storylines_seed.py` and `storylines_attach_check.py`.
- **A lost Telegram send orphans a proposal.**
  - The seed script commits about 20 proposals, then sends them back to back.
  - `_tg_api` (`common.py:611-626`) swallows a 429 and returns `None`. There is no retry, and nothing paces the sends.
  - A proposal whose message never arrived stays pending with no button. No command lists pending proposals, and the script refuses to rerun while any are pending. `--again` would insert duplicates, because `validate_seed` checks live names, not pending ones.
  - **Fix:** count failed sends in the `seed:` line, and add `/storyline pending`, which re-sends the buttons.
- **The ILL-POSED rerun advice is self-contradictory.** "Delete `storylines_check/<date>/runs/` first" keeps `input.json`, which holds the old storyline descriptions, so the rerun re-scores the vague descriptions for about $7 and an hour. Only "use another week" works. Drop the first option, or make the script re-read storylines with `--refresh-storylines`.
- **A PASS certifies a prompt that plan 2c changes.** 2c adds live signposts to every storyline block and a suspected-hit output field. Spec §8.2 step 7 re-runs only on storyline count. Add "and whenever the attach prompt or tool schema changes" to the re-run triggers now, while the script is being written.
- **A thesis source is silently omitted at seeding.** The seed script uses `load_portfolio_theses()[0]` and drops the problem string. If the push has not landed, seeding drafts from `theses.json` only and says nothing. Print the problem, and refuse unless `--no-portfolio`.
- **`items.created_at` has no index** (0006 has only `items_outlet_hash`). `undecided_items` scans `items` every 30 minutes. That is harmless now, and grows with `uh0` unresolved. Worth one `CREATE INDEX` in 0019.

## Should another red-team round run?

**No.** All three objections are local and concrete: one migration line, one defer path with an alert, and one parser change. None re-argues a settled decision, and none depends on a hypothetical downstream consumer. Their fixes are checkable in code review against the files named above. Another plan-level round would mostly produce objections about plans 2b and 2c, which `red-team-saturation` says to cut into a checklist for those plans instead. Apply the fixes, correct the two false facts (the `supervisor.py:78` quote and the test list), and proceed to implementation.

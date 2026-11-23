# Storylines plan 2a: final whole-branch review

- **Range:** `0ca44be..9874de4`, 11 commits, 24 files.
- **Authority:** spec `docs/superpowers/specs/2026-11-21-storylines-design.md` rev 5, and plan `docs/superpowers/plans/2026-11-23-storylines-2a-foundation.md`.
- **Scope:** coherence across tasks and conformance to the spec. Each task was already reviewed on its own.
- **Data:** counts and code references only, no real data.

## Verdict

**Ready to deploy after three fixes. Do not run Rollout step 3 (the offline check) until they land.**

- There are 0 Critical and 3 Important findings. All three sit on the offline check's path from its output to a decision.
  - One check fault gets mistaken for a description problem.
  - One Rollout instruction makes a paid rerun that measures nothing new.
  - One verdict can be printed over incomplete runs.
- Everything production runs before the check is coherent across tasks: the migration, the store, `/storyline`, the seed script and the `attach` job.
  - Deploying, Rollout step 1 (`brief_cost.py`) and step 2 (seed) are safe today.
  - Two cheap Minors (M1, M2) should still land before step 2, because the seed call is one-off.

## Findings

### Critical

None.

### Important

**I1. The offline check records a configuration error as a finished chunk.**

- **Where:** `scripts/storylines_attach_check.py:157-159` (`_attach_chunk`).
- **What happens:**
  - Any non-transient exception becomes a per-article problem for the whole chunk.
  - That includes a `config` 401, 403 or 404 from `storylines.classify_error`.
  - `_do_run` then appends it to `runs/<run>.jsonl` as done.
- **Why it matters:**
  - Production treats `config` as "write nothing, stop the pass, alert" (`brief.py` `attach_pass`). The check does the opposite.
  - The 404 case is reachable: a priced model id the account cannot use, or a model that has since been withdrawn. `price_of` passes for it.
  - A bad key or unavailable model therefore runs through every chunk of every run in seconds, persists all of them, and resume never retries them.
  - The error text goes only to the jsonl. The terminal shows `excluded: S1=N S2=N H=N`, `scored items=0` and `J(S1,S2)=nan`, and the verdict is **ILL-POSED**.
  - Rollout step 3 maps ILL-POSED to "rewrite the descriptions", which sends the operator after the wrong fault.
  - The existing test `test_main_non_transient_error_marks_every_article_a_problem` covers only a 400. No test covers `config`.
- **Fix:**
  - When `classify_error(exc) == "config"`, stop the whole check without writing the chunk. Print the error and exit 2.
  - Optionally print each non-transient chunk error, so a 400 is visible in the terminal.
  - Add a test: a 401 on H's first chunk exits 2, `H.jsonl` gets no record for that chunk, and a rerun with a good post completes it.

**I2. The Rollout's ILL-POSED recovery reruns the old storylines.**

- **Where:** plan Rollout step 3, the ILL-POSED bullet (`docs/superpowers/plans/2026-11-23-storylines-2a-foundation.md:523`).
- **What it says:** "delete `storylines_check/<date>/runs/` first, or use another week."
- **Why it is wrong:**
  - `_load_or_write_snapshot` reuses `input.json` whenever it exists, and `input.json` holds the storylines as well as the items (`scripts/storylines_attach_check.py`, snapshot block).
  - Deleting only `runs/` therefore reruns the old, retired descriptions. That costs roughly $8–10 and can only reproduce the ILL-POSED verdict.
  - The bullet's own last sentence ("Storylines are re-read only for a new snapshot") contradicts its first option.
- **Fix:** replace the first option with "delete the whole `storylines_check/<date>/` directory (`input.json` too), or use another week."

**I3. The check prints a verdict, and can exit 0 on PASS, while chunks are still pending.**

- **Where:** `scripts/storylines_attach_check.py:181` (the `continue` on an exhausted transient) and `:208-247` (`_score`, which always runs).
- **What it does:** a chunk whose 3 transient retries ran out stays unrecorded, which is Ruling 5 and correct for resume. `main` then scores anyway.
- **Why it matters:**
  - Pending items count as exclusions.
  - If the pending chunks are S1's or S2's, the H-versus-S1 WARNING does not fire, and PASS (exit 0) is printed over a subset.
  - The "incomplete" line scrolls past during a run of an hour or two.
  - This verdict gates the production model choice, so it is the preview standing in for the action.
- **Fix:**
  - Have `_do_run` return the number of pending chunks.
  - If any run has pending chunks, print `INCOMPLETE: S1=a S2=b H=c chunks pending; rerun to finish` and exit 2 without a verdict.
- **Ledger:** this is the deferred Task 7 minor "scores and can exit PASS with pending chunks". It is raised to must-fix here.

### Minor

**M1. The `seed` stage has no writer, and the seed call is never priced first.**

- **Where:** `scripts/storylines_seed.py:86`.
- **What:**
  - 0019 widened `comprehend_spend.stage` for `'seed'`, but the Sonnet call there is never recorded.
  - `comprehend.price_of` is never called before it, contrary to the Global Constraint "priced before the first call".
  - The writers that do exist are `attach` (`brief.py`), `attach_check` (the check), and the existing `triage` and `integration`. Nothing writes a stage the CHECK lacks.
- **Why:** the call is one-off. Once it has run, its bill can never be recorded, and it logs no usage line either. The money is pennies, but the table loses the record for good.
- **Fix:** call `comprehend.price_of(model)` before the post (exit 2 on `UnpricedModel`). After the post returns, call `comprehend.record_spend(conn, "seed", model, resp.get("usage") or {})` before the proposals are inserted, so the existing commit covers both. Land it before Rollout step 2.

**M2. The seed run ignores a bad theses push without saying so.**

- **Where:** `scripts/storylines_seed.py:36-37`.
- **What:** `load_portfolio_theses()[0]` throws away the problem string. A missing or malformed push means seeding drafts from `theses.json` alone, and nothing tells the operator. `load_theses()`, which spec §10.5 names, would have alerted.
- **Fix:** print `theses: N (push problem: yes|no)`. Print the count only, never keys or text: the problem string can name keys. Land it before Rollout step 2.

**M3. Proposals that fail to send are left with no way to decide them.**

- **Where:** `scripts/storylines_seed.py`, the not-sent path.
- **What:**
  - A failed send leaves a pending `seed` proposal with no buttons and no command to resend or list it.
  - The next run then refuses until `--again`.
  - `--again` checks new names against live names only, not pending ones, so it can repeat a name. Accept's duplicate check makes that harmless.
- **Fix (later):** make `--again` resend pending proposals rather than draft new ones, or add `/storyline pending`.

**M4. Articles that use up their retry budget disappear without a trace.**

- **Where:** `storyline_store.py:152-168` (`undecided_items`).
- **What:**
  - An article that reaches `attempts = 3` or `defers = 10` stays `failed` or `deferred` and is never selected again.
  - No count of these is shown anywhere: not in the `Attach:` line, and not in `/storyline list`.
- **Carry-forward for plan 2b:** spec §7.3 defines "unclaimed" as decided `none`, rejected, and so on. Plan 2b must also treat `failed`, `deferred` and no-row articles as unclaimed, or they drop out of the brief.
- **Fix (later):** add `retired=N` (budget-exhausted in the lookback window) to the `Attach:` line.

**M5. A pass that judged nothing still reports `ok` and sends no alert.**

- **Where:** `brief.py:4920` and `:4945-4952`.
- **What:**
  - Suppose every chunk is deferred (a request error, or a `whole` result) on every pass, for example an article mix that keeps hitting `max_tokens`.
  - Each pass then logs `status=ok deferred=N` and clears any alert.
  - Articles retire after 10 defers, about 5 hours.
- **Why it can wait:** nothing reads attachments until plan 2b, and Rollout step 5 reads the log the next day.
- **Fix (later):** a `degraded` status when `calls > 0` and `attached + none == 0`, alerted like `config`.

**M6. The attach rate is computed only on articles all three runs decided.**

- **Where:** `scripts/storylines_attach_check.py:228`.
- **What:** spec §8.2 step 3 says "on all articles, each run's attach rate".
- **Why the current choice is defensible for the bar:** it compares the same articles.
- **Fix:** print each run's rate over its own decided articles too, and say in the result doc which population the bar used.

**M7. The check always sends full 60-article batches, while production sends small ones.**

- **What:** production calls carry about 15–19 articles (spec §8.1), and 60 only after an outage. Every check chunk carries 60.
- **What it means:**
  - A PASS transfers, because the check is the harder case.
  - A FAIL may be partly batch size. Weigh that before deciding §8.3.
- **Fix:** none needed in code. Record it in the result doc.

**M8. The image ships `scripts/` only under a line marked TEMPORARY.**

- **Where:** `Dockerfile:46-58`.
- **What:**
  - `COPY scripts/` sits under a TEMPORARY (`news-brief-115`) comment that says to delete it once that bead closes.
  - All three Rollout scripts, and spec §8.2 step 7's reruns, now depend on that line.
  - `tests/test_packaging.py:524` guards only the two census scripts.
- **Fix:** add the three storylines scripts to that test, update the Dockerfile comment, and add a note to bead `115`.

**M9. One handler reads `STORYLINE_MAX_LIVE` twice.**

- **Where:** `brief.py:4060`, then `:3964`.
- **What:** `_storyline_decide` checks the bound with one read of the knob and builds the bound message from a second read.
- **Why it is minor:** the 60 s settings cache can make the message quote a different number from the one checked. The effect is cosmetic, but it breaks "once per use".
- **Fix:** read it once and pass it into `_storyline_bound_text`.

**M10. The ledger never recorded the full suite's result.**

- **What:**
  - The controller's full suite at `41e2cbb` was pre-registered at about 2738 passed, but the ledger records no result (`progress.md:57,67`).
  - This review avoided the shared test Postgres to prevent collisions, so the DB-gated tests were not rerun here either.
- **Fix:** record that suite's result in the ledger, then the three-command gate's, before pushing.

### Spec deviations, accepted as plan refinements (no action)

- **§6, "a 4xx charges" the retry budget.**
  - The plan sends a 400 to `deferred`, bounded by 10 defers, and treats 401, 403 and 404 as `config` stops.
  - It follows the retry-budget memory: a 400 for the whole request judged no article.
  - It is documented in Global Constraints, and the code follows it.
- **§14.** `item_attach_decisions` gains `deferred` and `defers`. Proposals live only in `storyline_proposals`.
- **§8.1.** The suspected-signpost field and signposts wait for plans 2b and 2c, as the plan's "Signposts wait" call says.
- **Spend has no reader yet.** The `attach` and `attach_check` rows have no reader beyond `scripts/replay_haiku.py`, which filters on `integration`. Spec §17.4 ("as measured") will need one in a later plan.

## Triage of the ledger's parked and deferred minors

**Must fix before the host run:** the Task 7 minor "scores and can exit PASS with pending chunks". It is finding I3 above.

**Can wait:**

| Id | Why it can wait |
|---|---|
| m1, m5, m6, m9, m11, m12, m13, m17, m18, m19, m20, m24, m27 | Test strength or wording only; no behaviour reaches the host differently. |
| m2 (`cache_read` ignored in `brief_cost.price`) | The batch log line carries `cache_read`, and Anthropic's `input_tokens` excludes cache reads. Ignoring them understates the brief by at most 0.1× input on the cached tokens. Worth a later one-line `cache_read_total=` in the summary, so a nonzero value is visible. |
| m3 (bare traceback on an unpriced `--model`) | Operator-only. The default `common.MODEL` is priced. |
| m4 (rotations older than 30 days are not windowed) | `projected_30d` uses the mean, and the span is printed. |
| m7 (two same-named proposals race) | Unreachable: the commands daemon is the only writer, and it is serial. |
| m8 (`record_decision` doesn't check targets are live) | Caller contract. The ids come from the pass's own live list. |
| m10 (`/storyline addfoo` matches the add prefix) | Cosmetic. |
| m14 ("message is not modified" on an identical re-edit) | Logged only. |
| m15 (seed's model call inside an open read transaction) | It holds only AccessShare locks for at most 180 s. A grep of the repo finds no `idle_in_transaction_session_timeout`. Whether the host server sets one is UNKNOWN from the repo. |
| m16 (`validate_seed` with a truthy non-list `content`) | The API never returns that shape. |
| m21 (`plain_text` tag stripping eats prose after a stray `<`) | Production and the check share it, so the check still measures what production does. |
| m22 (an empty storyline list gives `enum []`) | Closed: `attach_pass` returns `no_storylines`, and the check's snapshot refuses an empty list. |
| m23, m25, m26 (odd errors classify as `unknown`) | All defer, and the deferral is bounded. m25 also leaves a bill unrecorded; it is rare and small. |
| m28 (an unpriced alert clears only on the next `ok` pass) | By design. |
| Task 7: misleading test name for the 0.55 floor | Naming only. Ruling 4 keeps the bar. |
| Task 7: chunk index not tied to `ATTACH_BATCH` | A constant, changed only by a deploy. Writing the batch size into `input.json` and refusing on a mismatch would close it later. |
| Task 7: no WARNING test at exactly 2 points | A boundary case on an advisory line, not a bar. |
| Task 7: the seeded-sample test passes mostly unseeded | Test strength only. |
| Task 7: bare `json.loads` on a hand-corrupted snapshot | The write is now atomic, so only hand-editing can corrupt it. |

## Checks run, per named risk

1. **Stage values and writers.**
   - Grepped `record_spend(` across non-test code. The writers are `triage` and `integration` (`comprehend.py`), `attach` (`brief.py:4901`) and `attach_check` (the check script).
   - `seed` has no writer (M1). No writer uses a stage outside the 0019 CHECK.
   - The only reader of `comprehend_spend` is `scripts/replay_haiku.py`, filtered to `integration`, so attach spend cannot leak into another budget.
2. **Production versus the check: same predicate, two callers.**
   - Read `brief.attach_pass` beside `_attach_chunk` and `_do_run`. Both call the production `storylines.build_attach_request`, `validate_attach` and `classify_error`, and neither copies them.
   - Divergences found:
     - `config` is persisted as done in the check (I1).
     - A request error, an `unknown` error or a `whole` result is deferred and retried in production, but excluded in the check. That is acceptable for a one-shot measurement, because the exclusions are printed per run.
     - Per-article problems are retried up to 3 attempts in production but get one try in the check, which is acceptable for the same reason.
   - The spend-before-validate order is the same in both.
3. **Knobs.**
   - All four `KNOBS` entries match spec §12's defaults: `STORYLINE_MAX_LIVE` 60, `STORYLINE_UPDATE_MODEL` `claude-sonnet-5`, `STORYLINES_ENABLED` false, `STORYLINE_ATTACH_MODEL` `claude-haiku-5-5`.
   - Each has its `x-newsbrief` anchor line.
   - Every read goes through `common.X`, and `common.__getattr__` resolves it through the settings cache.
   - `mode_attach`, the seed script and the check each read their knobs once.
   - The bound is `common.STORYLINE_MAX_LIVE` in `/storyline add`, in Accept (`decide_proposal`) and in the seed's `max_count`. One handler reads it twice (M9).
4. **Store contract versus callers.**
   - The job passes 24 h, 300, 3 and 10 to `undecided_items`, matching the plan.
   - The SQL filter is `attempts < max AND defers < max` on `failed`/`deferred`, oldest first.
   - All three `record_*` functions no-op on a final row (Ruling 1). `record_failure` and `record_deferral` flip the outcome between each other and keep both counters.
   - Articles that exhaust their budget are not visible anywhere (M4), and plan 2b must handle them.
5. **Rollout versus code.**
   - The scripts exist with the flags the Rollout uses: `scripts/brief_cost.py` (`--model`), `scripts/storylines_seed.py` (`--again`) and `scripts/storylines_attach_check.py` (`--week-ending`, `--sample`).
   - `--entrypoint python` works with the `newsbrief` compose service.
   - `brief_cost` reads `newsbrief.log` and `.1`–`.5`, matching `RotatingFileHandler(backupCount=5)` in `common.py`. It parses the `Batch result:` and `Realtime brief:` formats `brief.py:3242` writes.
   - The `Attach: status=… items=… calls=… attached=… none=… failed=… deferred=… stray=…` format is exact.
   - The settings INSERT matches `settings_key_global` (`ON CONFLICT (key) WHERE user_id IS NULL`) and the `updated_at` column. `'true'` is in `common._TRUTHY`, the set of values read as true.
   - The ILL-POSED bullet is wrong (I2).
6. **Privacy (spec §13).**
   - The seed's problem strings never quote item text, and it prints only ids and counts.
   - The check writes its per-article artefacts only under `DATA_DIR/storylines_check/<date>/`. Its sampled titles and names go to stdout only, which the plan permits for the operator's terminal.
   - The `attach` log lines carry counts, exception text, and stray labels capped at 40 characters. The attach prompt carries no thesis text.
   - The fixtures use invented names (`Widget tariffs`, `Gizmo strike`, `TESTCO`, `OTHERCO`), from a scan of the added test lines.
   - This review contains no real data.
7. **Packaging.**
   - `storyline_store.py` and `storylines.py` appear in the `Dockerfile:41` COPY line, the workflow `paths:` list and both ruff lines.
   - The scripts carry the `sys.path` shim, and `test_a_script_run_as_a_path_can_import_the_root_modules` covers them by glob.
   - They reach the image only through the TEMPORARY `COPY scripts/` (M8).
8. **Preview versus action.**
   - The check posts through `brief._post_messages` with `timeout=brief.ATTACH_TIMEOUT` and `max_attempts=1`.
   - It uses the same builder: `ATTACH_BATCH` 60, `ATTACH_TEXT_MAX` 600 through `plain_text`'s default, `ATTACH_MAX_TOKENS`, thinking disabled, a forced tool, S/A labels, and storylines in id order.
   - Its articles come oldest first by `created_at, id`, as `undecided_items` returns them.
   - H uses the same `STORYLINE_ATTACH_MODEL` read that production uses.
   - The remaining gaps: incomplete runs (I3), `config` handling (I1), and batch fullness (M7: a PASS transfers, a FAIL may be pessimistic).

**Commands run by this review:**

- `ruff check` on the changed modules, scripts and tests, and `ruff format --check` on the 7 changed source files: clean, exit 0.
- `py -m pytest` on the 8 storyline, packaging and interlock test files with `DATABASE_URL` unset, to avoid the shared test DB: 182 passed, 40 skipped, `REAL_EXIT=0`. The 40 skipped are DB-gated and were not run by this review (M10).

## Controller rulings during the build (from the SDD ledger)

- Ruling 1: record_decision on an already-final row (attached/none) is a silent no-op that writes neither the decision nor attachment rows; the test asserts both unchanged — the plan left it open, and a no-op keeps a duplicate pass harmless — if wrong, a double-decide goes unnoticed in logs (low cost: the selector never re-selects final rows).
- Ruling 2: Global Constraints bullet rewritten: a response that judged nothing writes outcome 'deferred'; only transient/config stops write nothing — spec §6 wants decision records for every judged-or-attempted response — if wrong, deferred rows mislead counts (visible in the Attach log line).
- Ruling 3: Task 7's check records spend per returned response with stage 'attach_check' (0019 widened the CHECK for it) — cost of a ~$9 one-off belongs in comprehend_spend — if wrong, an extra row type in the spend table (harmless). Its items-for-a-week query goes in storyline_store.py (all storyline-feature SQL lives there).
- Ruling 4: keep spec 8.2's bar verbatim although its 0.55 floor is unreachable — it is the operator's pre-registered bar; note the redundancy in the result doc — if wrong, nothing changes in any verdict.
- Ruling 5: accept implementer's call that a chunk exhausting 3 transient retries stays unrecorded (rerun resumes it; scoring counts it excluded) — matches resume semantics — if wrong, a rerun repeats a few calls.
- Final: parked — M2 residual — Ruling: ship and fix in bead news-brief-hmn; the text reaches only the operator's own host terminal (no log, no git), so spec §13 holds and only the plan's "count only" letter is broken — if wrong, up to five of the operator's own tickers show on his terminal once, when the push is broken.
- Final: parked — M1 shared transaction (spend rolls back with an add_proposal error) — Ruling: real, low, recorded in bead news-brief-hmn — if wrong, one seed call's cost is missing from comprehend_spend.

Final gate: full suite at b1ae3bc, 2751 passed (pre-registered 2751), 0 skipped, REAL_EXIT=0, 793 s; ruff check and ruff format --check clean over the tree.

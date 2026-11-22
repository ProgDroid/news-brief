# Red team: storylines phase 1 plan

Plan: `docs/superpowers/plans/2026-11-22-storylines-phase-1.md`. Spec: `docs/superpowers/specs/2026-11-21-storylines-design.md` §3, §5.3, §11–§14.
Reviewer stance: hostile staff engineer, read-only. I ran no pytest. I did run the plan's heading and entry rules (re-implemented in a scratch script, not production code) over the 15 newest archived briefs in the gitignored `from-server/briefs/` (2026-08-15..29). No content from them appears here, only counts.

Settled and not re-argued: resolution_date quarantine, 0018/0019 numbering, TodoWrite/bd process.

## The three objections

### 1. Every "unchanged" guarantee is tested by comparing the new code with itself (Tasks 3 and 4)

The plan's rollback promise is "Knob off means today's prompt, byte for byte" (Global Constraints). Two tests back it:

- Task 4, `test_prompt_with_no_window_is_byte_identical_to_today`: `build_daily_prompt(*args) == build_daily_prompt(*args, dated_window="")`.
- Task 3, `test_undated_block_is_unchanged`: `render_established_block(ledger) == render_established_block(ledger, dated=False)`.

Both calls run the post-edit function. Delete the code in your head: if the edit rewords the `## WEEKLY CONTEXT` block (brief.py:2355-2361), moves `{weekly_block}` in the f-string (brief.py:2403), or changes the row format in `render_established_block` (brief_memory.py:525-545), both tests still pass. The names promise "identical to today", and the assertions check only that a default equals itself. That's the defect class `tests-asserting-less-than-their-name.md` calls dominant in this repo.

No existing test closes the gap. `grep "WEEKLY CONTEXT\|weekly_summary\[" tests/` returns nothing. The existing `render_established_block` tests (tests/test_brief_memory.py:142-1722) check substrings and counts, not the whole rendered block.

**Fix:** before editing, capture golden output from HEAD with fixed inputs and a frozen clock (`build_daily_prompt` calls `datetime.now` at brief.py:2323, so the clock must be monkeypatched), and commit it as a literal in the test. Assert the post-edit knob-off output equals that literal. Do the same for one representative ledger through `render_established_block`. Pre-register it: mutate the weekly heading by one character, confirm the test fails, then revert.

### 2. The dated cue goes into a parenthetical that the block's own header says means something else (Task 3)

Task 3 puts `since 11-14` first in the existing parenthetical: `• [japan] (since 11-14, in doubt, corroborated) …`. The header text that the model reads beside those rows is unchanged, and it says (brief_memory.py:561-564): *"The parenthetical is how broadly the fact was corroborated across outlets: lean on 'widely corroborated' facts with confidence…"*.

In dated mode, then:
- the header misdescribes the first token of every parenthetical;
- no instruction tells the model what the date means or to cite it. The spec's whole purpose for §3.1 is the "follows from the 12th" behaviour (§1, §17.1). The plan wrote exact instruction copy for the 7-day window (Task 2) and none for the dated claims;
- the same function's comment (brief_memory.py:546-552) records that the model already took block vocabulary as literal output (`(established)`). An unexplained `(since 11-14)` is the same shape of bare marker.

So Task 3 adds tokens, but nothing the model reads tells it how to use them, and one sentence it reads contradicts them. The tests can't see this, because they check only row strings.

**Fix:** in dated mode the header must change, for example: *"The parenthetical starts with the date a previous brief first reported the fact (since MM-DD). When today's news follows from one of these, say so with that date. After the date, the parenthetical says how broadly the fact was corroborated…"*. Add a test that the dated header mentions `since` and the undated header stays at its golden literal (objection 1).

### 3. The rollout window opens at the exact minute collect starts, and a deploy then can silently cost the day's brief (Rollout)

Rollout: *"Deploy between 06:00 and 20:00 UTC: the knob is off, so nothing changes until step 2."* That window comes from spec §6.1's qap cutover (spec:249-251), where it guarded a 20:00 submit. That submit no longer exists. `scheduler.SCHEDULES` (scheduler.py:77-85) now fires submit at 04:30, collect at **06:00** (grace 120 min, waiting until 06:30, then a real-time call of up to `BRIEF_REALTIME_TIMEOUT`=600 s, or up to ~4 h on the batch), and watch at 06:30.

A deploy restarts the supervisor. `supervisor.shutdown` (supervisor.py:803-819) SIGTERMs the running collect and closes its row as `finished` with exit -15, **not** `missed`, and "No alert fires here". So the collect is not retried. A deploy at 06:05 means no brief that day and no alert. A deploy between `deliver` (brief.py:3434) and `clear_batch_state` leaves `batch_id` in state, so the operator's natural `/run collect` re-delivers a brief already sent.

**Fix:** "Deploy after collect has finished (check `/jobs`: collect ✅ today) and before 04:30 UTC; avoid Sunday 21:00 (weekly)." This is a flagged implicature (see the fixed check, item 1), and it displaces my weaker candidate (secondary finding A).

## Fixed check: pairs of facts and what a reader infers

| # | Sentence (plan) | Fact 1 | Fact 2 | What a reader will infer | Established? |
|---|---|---|---|---|---|
| 1 | Rollout: "Deploy between 06:00 and 20:00 UTC: the knob is off, so nothing changes until step 2. The migration runs on start." | Knob defaults off (true) | Migration runs on start (true) | A deploy anywhere in 06:00–20:00 is safe | **No: unsupported.** 06:00 is collect's fire time (scheduler.py:78), and shutdown kills it without retry (objection 3). |
| 2 | Review Focus 3: "evidence is written once per submit day; the second pass logs `already recorded` and makes no model call." | `has_brief_evidence` is per day | Second pass returns early when it's true | Every second collect makes no model call | **No: unsupported.** `has_brief_evidence` is true only if the first pass **wrote ≥1 row**. On `no_section`, `no_theses`, `no_answer`, an all-invalid `ok`, or a crash, the second pass calls the model again. That's harmless (no duplicates are possible, because nothing was written), but the "no model call" promise and its test name overstate. Rename to "never writes a day twice" and test the zero-row case explicitly. |
| 3 | Task 5: "Never `load_theses()`: that one sends a Telegram warning when the push is missing, and submit has already sent it." | `load_theses` warns (brief.py:2163-2169, true) | Submit calls it | The operator is told every day the push is missing, so collect can stay silent | **No: unsupported.** Submit reaches `load_theses` only through `fetch_portfolio_weights` (brief.py:2223), which returns early with no T212 key (2183), on a failed fetch (2196), with no positions (2199) or with total ≤ 0 (2218). On those days nobody warns. Meanwhile `_thesis_keys_quiet` discards `load_portfolio_theses()[1]`, so pushed theses silently drop out of extraction and the log says only `status=ok` or `no_theses`. Carry the problem string into the `Evidence:` log line (`push=missing`). |
| 4 | Task 2: "`DATED_WINDOW_MAX_CHARS = 12_000` (about 3k tokens at 4 characters a token; spec §3.2's ceiling)", and "Not in this plan": "Phase 1 adds at most ~3k tokens a day." | Ceiling is 12,000 chars | 4 chars/token | Phase 1 adds at most 3k tokens | **No: "at most" is unsupported.** The bound is in characters, and the ratio is assumed, not measured. The repo itself records that Sonnet 5's tokenizer inflated output by ~30% (brief.py:2759 comment), which would make 12k chars about 3.9k tokens. It's also gross, not net: the window replaces `weekly_summary[:1000]` (about −250 tokens) and the dated cues add a few tokens per working-set row. Say "about 3–4k tokens gross, unmeasured", or log the window's token count from the brief's `usage` line on the first knob-on day. |
| 5 | Rollout step 1: "Check that … `chars` is under 12,000. Nothing listed means the parser doesn't recognise the real headings." | `build_window` drops days to stay ≤ 12,000 | The preview prints `chars` | The chars check can fail | **No.** It can't fail by construction (Task 2 window rules). And "nothing listed" has a second cause, no qualifying archive files (wrong volume or `DATA_DIR` in the `run --rm` container, or an empty dir), which prints the same empty output (`wrong-branch-404`). The preview must print the files `load_last_n_briefs(7)` returned (dates and count) before the parsed result, so "no files" and "files, no sections" read differently. |
| 6 | Task 2: "The archive is the model's HTML with tags stripped (brief.py:3233), so entities like `&amp;` remain." | Tags stripped, no unescape (true) | — | Archives contain entities | Possible, not observed: 0 of 15 archives probed contain `&amp;`/`&lt;`. Harmless, and the unescape is still right for the raw-HTML input of Task 5. |

**Residuals.** The plan states no "N total, M present, K missing" figures. My parser probe: 15 files, and the plan's rules found `top` in 15/15 (3–4 entries), `watch` in 15/15 (4 each) and `positions` in 15/15 (3–5). That sums to 15 per section, with nothing left unexplained. In 15/15 files at least one `top` entry spans more than one line (the headline line, then prose continuation lines, some beginning with `,` or `—`), and the first line is usually the bolded headline. So "first line of the entry" is the right cut. The August archives predate the current prompt, so the plan's preview script is still needed on the host.

## Secondary findings (real, smaller)

A. **`/evidence` can't meet spec §17.3.** Success criterion 3 is "`/evidence` shows what built it up or eroded it **since a given date**". The plan hardcodes 30 days (Task 6), so nothing older than a month is reachable from Telegram until `nyy.2`. Cheap fix: `/evidence <key> since:YYYY-MM-DD`, parsed like `/dig`'s `since:` (brief.py:1392-1395).

B. **The phase 2 double-counting deferral has no tripwire.** Spec §3.3 says phase 1 "stays under the storylines layout" and must then skip developments already recorded as `source='storyline'`. The plan defers that under "Not in this plan", but files no bead and adds no guard. Once `STORYLINE_BRIEF_ENABLED` exists and both knobs are on, POSITION SIGNALS is generated *from* storyline evidence (spec §7.1.5), and this extractor re-records it as `source='brief'`, which doubles every row in `/evidence`. File a bead now, or have `_record_thesis_evidence` refuse (`status=storyline_layout`) when that knob exists and is on.

C. **Labels sit next to keys in a free-string field.** The user template shows `T1 = TESTCO`, and the tool schema's `thesis` is a bare `str`. A model that answers `TESTCO` gets every row rejected as "unknown label". That's loud in the log, but it means a day of zero evidence. Put `"enum": [labels…]` in the `thesis` property of `input_schema` and add a test that it's there.

D. **The evidence call sits inside the collect window that must not be interrupted.** It runs after `save_signals` (brief.py:3451) and before `clear_batch_state`. `RESUME_MARGIN_MINUTES = 15` (brief.py:3362) was sized for "deliver, extract signals and reconcile". `_post_messages` retries on timeout (`max_attempts` default 2, brief.py:2758), so this adds up to 2×90 s to that margin. The trading stage is deliberately placed *after* `clear_batch_state` for exactly this reason (brief.py:3477-3478 comment). Move the evidence call after `clear_batch_state`, beside trading. That also makes Review Focus 3's rerun case rarer.

E. **False code claims (name, file, line):**
- Task 6: "a `HELP_TEXT` entry after `/thesis`'s". `HELP_TEXT` (brief.py:715-769) has no `/thesis` entry, and no `/dig` either. Pick another anchor, for example after `/performance`.
- Task 4: "stub mode_submit's fetchers as tests/test_obtain_brief.py does". That file never calls `mode_submit`. The stub is `_stub_submit` in tests/test_brief_fallback.py:116-149, and it sets `load_last_weekly_summary` to `""` (line 136) and `brief_memory_enabled` to False (line 131). As written, the knob-off test's assertion "prompt contains WEEKLY CONTEXT" fails until the weekly stub returns text, and the knob-on "render_established_block received dated=True" can't happen until `brief_memory_enabled` is True and `db.connect` is stubbed. That stub also replaces `build_daily_prompt` itself (line 147), so the prompt can't be "captured".
- Task 1 and the Global Constraints say "the workflow `ruff check` list". There are two `ruff` lists (docker-publish.yml:79 `ruff check`, :80 `ruff format --check`), and `test_every_copied_module_is_linted_in_ci` iterates over every `ruff ` line (tests/test_packaging.py:140-157). Name both.
- Task 2 Step 4: the new script needs the `sys.path` shim (scripts/replay_brief_realtime.py:24-25), which `test_a_script_run_as_a_path_can_import_the_root_modules` (tests/test_packaging.py:308) enforces. Task 2's own test run doesn't include test_packaging, so the failure would first show at Task 6's gate.
- Task 5's `_record_thesis_evidence` returns early when no DB is configured. The `_stub_collect` rig (tests/test_collect_day_key.py:27-67) patches `db.connect` but not `db.is_configured`, so without a `DATABASE_URL` the collect tests would hit the "skipped, no database" branch and never reach the stubbed save. Patch `db.is_configured` in those tests.

F. **Rollout step 2's "set the settings row" is an INSERT, not an UPDATE.** The compose anchor seeds `settings` only "once, when it is empty" (docker-compose.yml:102, config.py:223). On the live host no `BRIEF_DATED_MEMORY_ENABLED` row exists, so `UPDATE … SET value='true'` changes 0 rows and step 3's log check is what catches it, a day later. Give the exact `INSERT … ON CONFLICT (key) DO UPDATE` statement.

## Round balance

These objections are concrete and local: two test and copy defects inside the plan's own tasks, and one operational instruction that is wrong under today's schedule. None is about a hypothetical downstream consumer, and none re-argues a settled decision. Each fix is a few lines in the plan. Fold them in and go straight to execution. No further red-team round is warranted for this plan.

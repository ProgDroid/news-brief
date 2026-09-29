# Event census: implementation record

**Date:** 2026-09-29 · **Spec:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md` (rev 7)
· **Plan:** `docs/superpowers/plans/2026-09-28-event-census.md` · **Runbook:** `docs/2026-10-01-host-runbook-census.md`
· **Commits:** `31672d6..6c45932` on main (18 commits); minors pass `6c45932..3428111` (14 commits) · **Beads:** epic `news-brief-vd9`, sub-project 1 epic `news-brief-6jc`

Built with subagent-driven development: one implementer and one reviewer per task, a final whole-branch review, and one fix wave. This record keeps what the gitignored SDD ledger held that would cost time to re-derive.

## Verification

- Baseline before the build: 1938 passed, 0 skipped (full suite, DB up) at `31672d6`.
- After the final fix wave: **2129 passed, 0 skipped**, REAL_EXIT=0, matching the pre-registered 2123 + 6.
- Every task passed its review; tasks 1–6 and 9 each needed one fix round, tasks 7, 8 and 10 none.
- Task 10's twelve pre-registered mutation checks all matched their predicted failure counts and test names: `docs/superpowers/reviews/2026-09-28-event-census-mutations.md`.
- The final review found no Critical; its two Important findings (readout ran from the unpinned image; an Origin failure was indistinguishable from session expiry) and six minors were fixed in `6c45932` and re-reviewed clean.

## Decisions taken during the build (controller rulings)

Each is a decision made without the operator, with what it costs if wrong. Revisit any of them.

- Ruling R1: `tests/census_fixtures.py` in Task 3 holds only `seed_corpus` and `prepared`; Task 4 adds `label` and `answer_precision` — the helpers need Task 4's functions — cost if wrong: none, pure test-helper placement.
- Ruling R2: `test_census_runbook_scripts_ship_in_the_image` moves from Task 8 to Task 9 (where `census_report.py` is created) — keeps every commit green per Global Constraints — cost if wrong: none.
- Ruling R3: `gap_check` over zero merged cross-outlet pairs makes `prepare` REFUSE ("gap check has no merged cross-outlet pairs; cannot evaluate") rather than pass on an empty mean — an unanswerable instrument refuses (memory a-ledger-dates-what-it-records); `prepared()` fixture seeds short-gap cross-outlet assertion pairs — cost if wrong: a host with no comprehension history could not prepare; it has history since 2026-09-09.
- Ruling R4: M10 pre-registered at 4 failures in test_census_schema.py (cascade control, post-block delete, block-end item, release test's unpinned delete) — the release test asserts rowcount 1 on the unpinned delete — cost if wrong: a reported mismatch, investigated not reconciled.
- Ruling R5: RestrictViolation (23001) is the correct class for ON DELETE RESTRICT; the brief's ForeignKeyViolation was wrong — verified by reviewer on PG 18 — cost if wrong: none.
- Ruling R6: fix round for Task 1 waits until Task 2's implementer commits — avoids two implementers committing at once (skill: never parallel implementers) — cost: ~10 min wall clock.
- Ruling R7: pair_recall and bcubed_recall use DIRECT links (two items linked iff they share an event id), never transitive closure — a system that links x–y and y–z has not linked x–z, and closure inflates recall in exactly the chained-partial case the census exists to see; pair_recall stays cross-outlet-only; bcubed_recall is standard B-cubed recall over the gold group with direct links, outlet-agnostic, documented as such — cost if wrong: SP1 re-derives a secondary metric; the headline (confirms/pooled_share) is unaffected.
- Ruling R8: draw_order uses a fresh rng.sample([0,1,2,3],4) per round (as implemented) and a golden test pins the SEED order — per-round permutation keeps windows 1–2 (the go/no-go pair) from always being the same two strata — cost if wrong: a different but equally valid order.
- Ruling R9: a naive (no timezone) CENSUS_C439ADE_DEPLOYED_AT is rejected with exit 2 — silently assuming UTC would misplace the by-half split by the host's offset — cost if wrong: the operator re-runs with a Z suffix.
- Ruling R10: GapCheck.windows = distinct UTC 6-hour windows touched by either endpoint of a merged pair — spec 4.3 'the 6-hour windows it rests on' — cost if wrong: one descriptive count in the readout differs; no gate reads it.
- Ruling R11: session_no = count of done windows + 1 for a 'blind' task, and = count of done windows for every other kind (precision shows the number of the window just finished; complete shows 17) — avoids 'Session 4/17' on session 3's precision and '18/17' at the end — cost if wrong: a cosmetic counter.
- Ruling R12: assignment latest-state is last ARRIVAL (highest id) with (tab_id, client_seq) dedupe, per plan B5; spec §6.3's 'highest client_seq wins' line is stale and is corrected in the spec text — cost if wrong: none, the plan's rule is the reviewed one.
- Ruling R13: after a successful save, a BadWrite from the follow-up 'action' record_event (window closed in between by another tab) is swallowed and the route returns 204 — the labels were saved; the event is timing telemetry only — cost if wrong: one missing action stamp in active minutes.
- Ruling R14: when every done pass-1 window has m=0, the readout prints 'detectable difference: undefined (no multi-outlet groups)' instead of calling mde — the NO-GO-at-zero state must still render — cost if wrong: none.
- Ruling R15: final fix wave = I1 (readout runs from the pinned labeller image under the labeller role + a documented freeze on census.py/census_metrics.py/census_* schema until the census ends), I2 (phone POST smoke test in the runbook before window 1 + distinguishable 403 bodies/messages), and cheap minors m1 (runbook: TRUNCATE CASCADE after release wipes labels), m4 (labeller restart: unless-stopped), m5 (/label message hides item count), m6 (nudge one line when stopped at go/no-go), m7 (empty-password hint), m11 (queue survives a callback exception). m2 documented in the runbook as a known NO-GO bias. m3, m8, m9, m10 deferred — cost if wrong: the deferred ones are descriptive/rare-path.

## Deferred findings: cleared 2026-09-29 (bead `news-brief-vd9.11`)

Every item below was worked in a follow-up plan, `docs/superpowers/plans/2026-09-29-census-minors.md`
(red team: `docs/superpowers/reviews/2026-09-29-census-minors-plan-redteam.md`), commits
`6c45932..3428111`. Full suite after it: see "Minors verification" below. Where an item was fixed
in a different shape from its one-liner, the shape is given.

| Item | Outcome | Commit |
|---|---|---|
| Task 1 (all five) | fixed; down script's guard is a nested IF (plpgsql plans the table reference even under `AND`) | `82946a1` |
| Task 2 ARI denom==0 | the branch was dead (reachable only for cases special-cased earlier); deleted, reachable cases and `n==0 → nan` pinned | `06dedd9` |
| Task 2 F1, precision_estimate, named constants | fixed; constants live once in `census_metrics`, re-exported by `census` | `06dedd9` |
| Task 3 total_slots | refuses a block length that is not a multiple of `WINDOW_HOURS` | `82946a1` |
| Task 4 priority boundaries, seed pin, FOR UPDATE concurrency | tested | `0cbd333` |
| Task 4 writers vs served task | **enforced (operator decision D1)**, plus **claim on serve (D1a)**: a pass-1 window whose page was served since the last completion is never pre-empted by the repeat, which may therefore run one window after window 8 | `c885792`, `0cbd333` |
| Task 5 stale grants | `apply_labeller_grants` revokes then grants — the result is exactly `LABELLER_GRANTS` | `d369d24` |
| Task 5 reset race | `open_session` and `revoke_all` serialise on one advisory lock | `d369d24` |
| Task 5 over-privileged role | labeller refuses to start (exit 3) naming each surplus, incl. role attributes and **role memberships** (found in review) | `d369d24`, `d0558f3` |
| Task 5 `_CENSUS_TABLES` | stays hand-written (deriving it would silently widen SELECT to future census tables); a drift test pins it to the catalog | `d369d24` |
| Task 5 revoke_all expiry, compare_digest, census_grants stderr/history | fixed | `d369d24` |
| Task 6 heartbeat stop on 400 | verified in a real browser (below) | `7d10bbd` |
| Task 6 timeout test | behavioural: a stalled client is dropped | `3302ee6` |
| Task 7 base URL escaping, /label texts, reset message | fixed and tested | `fb0c187` |
| Task 7 `/label@botname` | **not applicable (operator decision D4)**: the bot serves one private chat behind the single-user gate | — |
| Task 8 compose parser and pins | fixed (comments, mapping-form and quoted env items; bind, empty password, restart pinned) | `9feb6b3`, `0801153` |
| Task 9 `_read_windows` drift | reuses `census._windows` | `381f010` |
| Task 9 wall-clock start | the LAST page `open` at or before first activity (the first `open` would measure the gap between sittings, because the auto-reload after a completion stamps the next window) | `381f010` |
| Task 9 bootstrap 3× | one pass per replicate | `06dedd9` |
| Task 10 runbook steps 5 and 11 | fixed. Step 5 reads the one key from `.env` by command substitution, so the value never appears in a command line. Step 11: after the release, `census_prepare` now **refuses** (it crashed with a UniqueViolation traceback; found by the final review) | `3dc8f99`, `3428111` |
| m3 by-half split | **at the recorded c439ade deploy time (operator decision D3)**, midpoint fallback, header names which | `381f010`, `0801153` |
| m8 default port | Host and Origin accept the scheme's default port written or omitted | `3302ee6` |
| m9 CREATEROLE | runbook note, corrected after the final review tested it on PG 18.6: a superuser-created role fails at `ALTER ROLE ... PASSWORD`; the main role needs CREATEROLE before the first run, or `GRANT census_labeller TO <main> WITH ADMIN OPTION` if the role exists | `3dc8f99`, `3428111` |
| m10 raw JSON on GET errors | HTML status page (POST errors stay JSON for label.js) | `3302ee6`, `0801153` |
| label.js behaviours | recorded browser run, all four PASS: `docs/superpowers/reviews/2026-09-29-census-browser-verification.md` | `7d10bbd` |

**Still open after this pass:** `news-brief-vd9.12` (P3) — found by the browser run: `stop()` leaves
Finish and Abandon keyboard-enabled, so a keyboard Finish after a stop opens its dialog and silently
sends nothing. No data harm; fixing it needs another browser run to verify.

### Minors verification

- Baseline at `6c45932`: 2129 passed, 0 skipped.
- Every task's full-suite count matched its pre-registered prediction: 2134, 2142, 2173, 2176, 2195,
  2199, 2200. Controller gate at `3428111`: **2200 passed, 0 skipped, REAL_EXIT=0**, ruff check and
  ruff format clean. The 71 warnings are all pre-existing, in `test_capture.py` and `test_signals.py`.
- Mutation checks: every one caught its deliberate breakage. Two in Task 2 failed one MORE test than
  predicted, because the bootstrap pin also covers ARI and F1. Reported, not reconciled.
- The final whole-branch review ran two probe scripts against the test DB and found both runbook
  errors that reading alone had missed (step 11, CREATEROLE). It also measured the write guard's cost
  on a heartbeat at 11.1 ms at session 17.

### Rulings taken during the minors pass

- **D1a (operator):** claim on serve. The plan red team showed that the strict guard could reject the
  first action on a loaded window at the moment the repeat became eligible.
- **Guard placement:** the guard runs after the row lock and after the closed-window check, and calls
  an undecorated `_current_task`, so it neither commits mid-write nor turns a 409 into a 400. Page
  loads compute the task and stamp `open` in one transaction (`serve_page`). No bypass flag; tests
  walk the served order (`cf.advance_to`).
- **Wall-clock start:** the last page `open` at or before first activity. Cost if wrong: a page left
  open overnight without a reload overstates one window.
- **`_CENSUS_TABLES` stays hand-written,** with a catalog drift test, so that no future census table
  is silently granted.
- **Over-privilege scope:** the check covers the revoke scope plus role attributes and role
  memberships (memberships were added after review). Functions and PUBLIC grants are out of scope and
  documented as such.
- **D3 straddling window:** classified by its start. A window that starts before the split point
  counts as before.
- **`label.js` unchanged for D1:** its generic 400 text already says reload.
- **Deferred:** the `stop()` keyboard finding, as `news-brief-vd9.12`. Fixing it needs a JS change and
  another browser run, and it causes no data harm.

### The original list, as the final review deferred it

- Task 1: census_windows lacks UNIQUE(order_no, pass)
- Task 1: 0017 down DO block not guarded for a missing census_assignments table
- Task 1: truncate test asserts neither message nor a no-block control
- Task 1: down test reruns on the same schema, not a fresh one
- Task 1: negative-CHECK test drives backlog_excluded only, not null_published
- Task 2: ARI denom==0 branch differs from sklearn's literal behaviour
- Task 2: F1 harmonic-mean branch untested with a non-trivial value
- Task 2: precision_estimate could ZeroDivisionError on all-zero asked (unreachable)
- Task 2: pinned values are inline literals/defaults, not named constants
- Task 3: total_slots assumes block length is a multiple of WINDOW_HOURS (true by construction today)
- Task 4: two current_task priority boundaries untested
- Task 4: Random(window_id) seed not pinned by a test
- Task 4: FOR UPDATE lock (F17) has no concurrency test
- Task 4: writers do not check their target is the task current_task would serve
- Task 4: readout wording 'over the blind_done pass-1 windows' must include only pass-1, blind_done-or-complete, not abandoned — **closed by Task 9** (the readout uses exactly that set; confirmed by the final review)
- Task 5: apply_labeller_grants never revokes stale grants
- Task 5: session opened concurrently with /label reset can survive it (narrow race)
- Task 5: self-check cannot detect an over-privileged role
- Task 5: _CENSUS_TABLES is hand-maintained
- Task 5: revoke_all expiry clause untested
- Task 5: census_grants password in shell history; error to stdout
- Task 5: open_session does not use compare_digest (redundant; hash-matched lookup)
- Task 6: label.js heartbeat-stop on 400 has no automated test (M1)
- Task 6: the timeout test pins the value, not the presence of a behavioural timeout
- Task 7: LABELLER_BASE_URL interpolated unescaped into the Telegram HTML message (operator config)
- Task 7: precision /label link and waiting/complete/not_prepared texts untested
- Task 7: /label@botname not routed (same as other commands)
- Task 7: reset test asserts only '3' in the message
- Task 8: compose block-end detection stops at any 2-indent line incl. a comment; env extractor list-form only
- Task 8: nothing pins the empty password default, 127.0.0.1 bind default or restart: no
- Task 9: _read_windows re-queries census_windows beside census._windows (drift risk)
- Task 9: wall-clock minutes start at opened_at (first assignment), not first page open
- Task 9: consistency bootstrap recomputes pairwise_agreement 3x per replicate
- Task 10: runbook step 11 silent on re-running census_prepare after DELETE FROM census_block (prepare's no-op check reads that row)
- Task 10: runbook step 5 'Expected REAL_EXIT=0' reads as failure when the password is only in .env (not exported)
- Final review: by-half table splits at the block midpoint, not at the recorded `c439ade` deploy date (m3); base URL with an explicit default port (m8); an external database needs CREATEROLE for the grants script (m9); raw JSON shown on some GET errors (m10).
- `label.js` behaviours (heartbeat stop on 400, queue survival after a callback exception, the 403 message mapping) have static-text tests only: no JS runtime runs in the suite.

## Lessons

- **Parallel implementers share the test database.** Two implementers with disjoint files still collided: every DB fixture drops the public schema, and one run came back 1 failed + 22 errors. Overlap implementers only with read-only reviewers. (Memory `parallel-implementers-share-test-db`.)
- **Tests that pass on first write were audited by mutation.** Several reviews found tests that could not fail (a read-only control whose write came first, a vacuous string assertion, a bootstrap test on identical partitions). Deliberate mutations with pre-registered counts are what caught them.
- **NaN poisons percentile intervals silently.** `sort()` does not order a list containing NaN, so bootstrap intervals came out narrowed or inverted at realistic window sizes. Fixed in `census_metrics.item_bootstrap_interval`, which now drops NaN replicates and reports how many.

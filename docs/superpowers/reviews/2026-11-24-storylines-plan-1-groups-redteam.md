# Red team: storylines plan 1 (groups, embeddings, retrieval)

Reviewed: `docs/superpowers/plans/2026-11-24-storylines-plan-1-groups.md` against its spec
(`docs/superpowers/specs/2026-11-24-storylines-editor-design.md`) and the code at `909cf3f`
plus the working tree. Reading and small greps only; the suite was not run.

Evidence labels: **measured** (I read or ran it), **recorded** (a doc says so), **estimate**,
**UNKNOWN**. Settled items (Modal with an OpenAI switch, a 4B-8B open model, quality first,
the §21 cost pass) are not re-argued.

No storyline names, thesis text, holdings or host output appear below. Outlet names come from
the public `brief.py` feed list.

---

## The three objections

### 1. Migration 0020 breaks every job on the first routine deploy that lands before the image swap

**What the plan does.** Task 1 commits `migrations/0020_groups_up.sql`, which starts with an
unconditional `CREATE EXTENSION IF NOT EXISTS vector;`. Under the plan's commit policy that
commit goes to main, and the image is built from it. The Postgres swap is Rollout step 2, which
comes after all ten tasks.

**Why it fails.**
- `supervisor.startup()` runs `db.run_migrations` on every start (measured,
  `supervisor.py:463-510`). Any exception there returns `jobs_enabled=False`: no capture, no
  submit, no collect, no backup. The bot stays up.
- The documented deploy is `docker compose pull && docker compose up -d` (measured,
  `README.md:347`).
- Production runs `postgres:18-alpine` (measured, `docker-compose.yml:234`). pgvector is not in
  that image: `migrations/0012_pg_trgm_up.sql:16-18` says pg_trgm was chosen because it needs
  no image swap, "unlike bqa.7, where pgvector needs a different base image" (recorded).
- So any deploy between Task 1's push and Rollout step 2 makes 0020 fail on start and disables
  every job. That includes a deploy for an unrelated fix from one of the operator's concurrent
  sessions (recorded, memory `user-runs-concurrent-sessions`). The morning brief is lost until
  someone notices.
- That gap is the whole of Tasks 2-10. Several days is an estimate.

**What the plan says to protect against it.** Nothing. The Rollout's step order (swap, then
deploy) is correct only if no deploy happens before it.

**Six-month tail.** Every backup taken after 0020 contains `CREATE EXTENSION vector`. Such a
backup restores only onto a pgvector image. The host runbook
`docs/2026-09-22-host-runbook-comprehension-restart.md` and two memory files
(`newsbrief-kb-schema-0006.md`, `newsbrief-comprehension-pipeline.md`) still name
`postgres:18-alpine` (measured). Task 10 updates only `brief-local-run.md`.

**Fix (checklist level).** Two options:
- Make 0020 self-guarding. If `pg_available_extensions` has no `vector`, raise a clear message,
  or skip creating the vector tables. The supervisor would still disable jobs, so a skip is the
  only option that keeps the brief running.
- Or split the rollout. Do the image swap first, as its own deploy with no migration, and push
  0020 only after the host has run pgvector.

Either way, add a test that runs 0020 against a server without the extension and asserts the
chosen behaviour.

### 2. Calibration picks the model and the retrieval floor with queries production will never send, behind a reliability bar that cannot fail

The plan treats Task 8 as the "model pick and thresholds" (spec §23 item 1, §25). Three of its
measurements do not measure what they are used for.

- **The wrong queries.**
  - Step 6 uses "each storyline's name plus description" as the query, and the pre-registered
    rule picks the space with the highest nDCG@20 on those queries (plan lines 338-342,
    measured).
  - Spec §13 says the storylines live on the host now "are all retired and re-seeded": they are
    the thesis-named and composite ones of `al7.6`, which §1 calls poor input (recorded).
  - Production retrieval in plan 2 queries with open questions, actors, constraints, consensus
    and signposts (spec §7.3, recorded). Those are short, specific texts, with a query
    instruction tuned for a "topic description" (plan line 150).
  - So the model is chosen on queries of a different shape from storylines about to be
    deleted.
  - `retrieval_min_sim` comes from the same queries and becomes the default floor in
    `search_groups` (plan line 245). The cosine distribution of a long description query is not
    that of a five-word question (estimate). The floor will be applied to texts it was never
    fitted on.
- **A truncated floor.** `retrieval_min_sim` is "the highest similarity that keeps 90% of the
  judged-relevant items retrieved by that space" (plan line 338). Only the top 20 per query are
  ever judged, so relevant items below rank 20 are never seen. "Keeps 90%" is 90% of a sample
  with its lower tail already cut off, so the real recall at that floor is below 90%. This is
  the `the-audit-read-a-truncated-view` shape. Production pulls `PER_QUERY_LIMIT = 50` per
  query, so items ranked 21-50 that the floor drops are lost silently.
- **An AUC bar that cannot bind.**
  - Pairs are sampled evenly across 14 bands from 0.30 to 1.00 (plan line 331).
  - The 0.30-0.65 bands are mostly obvious "different" pairs (estimate). Each lies far below
    any positive, so it adds almost a full concordant pair to the AUC whatever the model does
    in the 0.70-0.90 region, which is the only region where grouping is decided.
  - The guard "AUC < 0.90 → grouping unreliable" (plan line 342, Rollout step 6) is therefore a
    bar that is hard to fail by construction (memory `analysis-stats-traps`, trap 9: a bar
    clause that can never bind).
  - The quantities that decide grouping are the precision at `join_sim` and the pair rule.
    They are reported but not gated.
- **A tie margin inside the noise.** "Within 0.01 nDCG@20 → prefer the cheaper GPU" uses a
  margin far below the sampling error of nDCG over a few dozen queries (estimate: the standard
  error is a few hundredths). In effect the rule picks at random among close candidates.
- **Thresholds from thin data.** About 600 pairs are spread over 14 bands and up to 5 spaces,
  with "unclear" removed. That leaves a few dozen labelled pairs near each space's threshold
  (estimate). "The lowest threshold with precision ≥ 0.95" on so few pairs sits at a noisy
  point and is biased low (a winner's curse).

**Fix.**
- Calibrate retrieval with production-shaped queries. Either generate them with the plan 2
  query builder on re-seeded storylines, or move the retrieval half of Task 8 (nDCG, model
  pick, `retrieval_min_sim`) into plan 2 after the re-seed.
- Judge to a depth beyond `PER_QUERY_LIMIT`, or fit the floor on a random sample below the cut.
- Gate grouping on precision at the chosen threshold, with an interval, not on AUC.
- Pre-register a tie margin sized from a bootstrap.

### 3. One unembeddable item stops embedding and grouping for good, by design, with no way out

Three rules in the plan combine into a wedge:
- "Nothing permanently retires an item: an item that cannot be embedded waits" (plan line 25).
- `items_missing_vectors` is oldest first, so the same chunk is retried first on every pass
  (line 189).
- `assign_pending` "stops at the first item with no vector" (line 202).

**What happens.** Suppose one input draws a per-input 4xx. That is classed `config` (line 25),
so it is retried every 30 minutes. Each retry resends the same oldest chunk of up to 128 items
and fails again:
- No later item is ever embedded.
- No item after the poisoned one is ever grouped.
- The alert rings once and stays rung.

The operator has no lever. There is no skip, no retire, and no DELETE from any 0020 table
(line 24). Plan 2's editor then sees no new groups from that moment on.

**Is the trigger plausible?** Estimate. `embed_text` strips a trailing `" - <outlet>"`, drops a
body that repeats the title, and cuts at a word boundary, so it can return an empty or
near-empty string:
- `capture.store_items` accepts any truthy title, whitespace included (measured,
  `capture.py:115`).
- X/Nitter titles can be just a link or an emoji.
- OpenAI's embeddings endpoint rejects an empty-string input (recorded, OpenAI API reference).
- The Modal path's `parse_request` has no per-text length check (plan line 151), so its
  behaviour on an empty text is UNKNOWN.

Whether such items exist on the host is UNKNOWN. The design gives no recovery if one does.

**The order guarantee does not need this.** Review Focus 1 only needs "no item is grouped
against a later one". An item that is explicitly marked unembeddable (a dated decision row) can
be skipped. It joins no group and blocks nothing, and that preserves the guarantee.

**Fix.**
- Make `embed_text` refuse empty output.
- Split a 4xx chunk to find the bad input, and record that item as `unembeddable` with the
  reason.
- Have `assign_pending` pass over recorded-unembeddable items but still stop at items that are
  merely not yet embedded.
- Add tests for one poisoned item among 300.

---

## Fixed check: facts placed side by side

| # | Plan text | The two facts | What a reader infers | Verdict |
|---|---|---|---|---|
| 1 | Task 1 Step 1: "It must pass at the count you recorded, with zero DB modules skipped. This proves the image swap alone changes nothing." | same pass count; zero skips | the swap is safe for production | **Unsupported implicature (flagged).** The test container runs initdb on a fresh glibc cluster on tmpfs. The production risk is the existing cluster initialised under musl: text index order, `datcollate` and the volume path. The suite never touches it. It proves only that a fresh pgvector cluster passes the suite. |
| 2 | Rollout steps 1 and 3: "The item count is the number the swap must preserve" … "The count must equal step 1's. If it does not, stop" | item count before; item count after | the swap is intact and indexes are fine | **Unsupported implicature (flagged).** The data files are the same, so `count(*)` is all but certain to match, and it says nothing about text indexes ordered under musl. It is also taken after the `REINDEX`. It can fail falsely too: step 1 counts while the supervisor is still running (`docker compose stop newsbrief` is step 2), and a 30-minute capture pass in between makes the counts differ with nothing lost. Decomposed: after-count = before-count + items captured between steps 1 and 2 − items lost. The plan reads the result as if the middle term were zero. Move the count after the stop, and check index health (`amcheck` `bt_index_check` on the text indexes), not row counts. |
| 3 | Line 73: "windows are thousands of rows and an exact scan is milliseconds" → no ANN index | window size; scan time | no ANN index is needed for this schema's readers | **Partly unsupported (flagged).** This holds for the 72 h grouping window: about 2.4k members at about 790 items a day (estimate from spec §21). It does not hold for the readers this plan builds for plan 2. A first run and the replay read 30 days (spec §7.1, §15.1), about 24k members, per query and across many queries per storyline (estimate). A 1024-dim vector is about 4 KB and is stored out of line (TOAST), so each comparison also pays for detoasting (estimate). Not wrong today; it should be stated as scoped to grouping. |
| 4 | Rollout step 7: "It takes minutes of GPU time, about $1 or less" | minutes; about $1 | the backfill is quick and cheap | **The pair contradicts itself (flagged).** At the plan's own T4 rate ($0.000164/s, line 120), $1 buys about 100 minutes. "Minutes" and "$1" cannot both describe a T4 run. My estimate for about 64k items (about 790 a day × 81 days) at about 256 tokens on a 4B model is 1-2 h on a T4. That is about $1 if it holds, and more on an L4 for an 8B model. The arithmetic says hours. |
| 5 | Line 21: "pgvector 0.8.7, released 2026-10-01" | release date; a 14-day age rule | the pinned image is a fixed, aged artifact | **Partly unsupported (flagged, minor).** The tag `0.8.7-pg18-trixie` exists (measured, Docker Hub), but its `last_pushed` is 2026-11-12: the tag is rebuilt in place. Pin a digest in CI and compose if "a fixed image" is meant. |
| 6 | Task 9: "trafilatura==2.3.1, released 2026-10-06; 2.4.0 is from 2026-11-23, too new" | release dates | 2.3.1 is the newest release at least 14 days old | **Supported.** PyPI (measured): 2.3.0 on 10-02, 2.3.1 on 10-06, 2.4.0 on 11-23. |
| 7 | Task 1: "the 9 test files that name `postgres:18-alpine`" next to the workflow, compose and CLAUDE.md edits | 9 test files; 3 other files | that is every place the image is named | **Partly unsupported (flagged, minor).** 9 measured. A repo grep (outside the data directories) finds 29 files = plan 1 + workflow 1 + compose 1 + CLAUDE.md 1 + tests 9 + `0012` comment 1 + `.beads/issues.jsonl` 1 + memory 3 + docs 11 = 29. Of the operational ones, a host runbook and two memory files are left stale. |
| 8 | Task 9: "The 13 existing tests keep passing" | 13 | the file's whole suite | **Supported.** There are 13 `def test_` functions in `tests/test_storylines_fetch_probe.py` (measured). |
| 9 | Rollout step 9: "`cold_start` true on most calls (30-minute spacing)" with `scaledown_window=10` | spacing; idle window | almost every live call is cold | **Supported** by the arithmetic, for the live job alone. Plan 2's query calls will change it. |
| 10 | Rollout step 6: "about $3 of Opus judging" | (an estimate, not two facts) | calibration is cheap | Not a pair. **My estimate is $5-9:** 30 calls of 20 pairs, plus a retrieval pool of up to storylines × 20 × spaces judged 30 per call, at $4/$20. |
| 11 | Rollout step 5: "The existing `MODAL_PROXY_KEY` / `MODAL_PROXY_SECRET` authorise them" | the tokens exist; the apps use proxy auth | the same tokens work for the new apps | **UNKNOWN.** True if Modal proxy-auth tokens are scoped to the workspace and the apps deploy into the podcast endpoint's workspace. Neither is shown. A curl with the tokens against one candidate before calibrating settles it. |

Flagged #1 and #2 are strong. They describe the same hole as objection 1 from another side: the
image swap is never verified at the layer where it can break. I kept objection 1 as the
headline because it costs the brief even if the Rollout is followed perfectly. #1 and #2 belong
on the same fix list.

---

## Secondary findings (checklist items, not objections)

- **The OpenAI Batch cap.** Spec §21.2 records that a batch holds at most 50,000 inputs. The
  backfill submits one batch with one saved id (plan lines 301-303). Captured items are
  probably about 64k by now (estimate), so a single batch is over the cap. Split into batches
  of at most 50,000 inputs, with one state entry per batch.
- **The outlet-suffix strip misses most Google proxies.**
  - The strip compares against `outlets.name`, which is `feed.get("outlet") or feed["name"]`
    (measured, `brief.py:545-556`).
  - Proxy feeds are named "Yonhap (English)" and "NHK World" (measured). Google probably
    appends the publisher's own source name, not those strings (estimate).
  - The test fixture covers only Reuters.
  - Strip the last `" - X"` segment on Google-proxied feeds, or test against a captured
    Yonhap and NHK title from the host (counted, not quoted).
- **The body-repeat fixture uses ASCII spaces.** `_entry_from` strips tags only (measured,
  `brief.py:1825-1826`). The `&nbsp;` in Google's description probably arrives as U+00A0
  (estimate). Compare after normalising whitespace, and use the real byte in the fixture.
- **CI lints explicit file lists.** `ruff check`/`ruff format --check` name each module
  (measured, workflow lines 78-79). The plan mentions only `paths:`.
  `test_every_copied_module_is_linted_in_ci` will catch the omission (measured); add the lists
  to Task 6's file list so it is not a surprise.
- **`COPY scripts/` is marked TEMPORARY** (news-brief-115, measured `Dockerfile`). The Rollout
  and every future re-embed run scripts from inside the container. Either retire 115's removal
  or move the backfill into a mode.
- **No interlock between the backfill and the live job.** `JOB_MODES` guards brief modes only
  (measured). Suppose someone backfills a space while `group` is live in it, for example after
  switching provider before the re-embed finishes. Two `assign_pending` loops then read the same
  `next_ungrouped` rows: the result is a primary-key violation, or two groups for one event
  (estimate). Take a per-space advisory lock in `assign_pending`.
- **Prompt version is not in the space id or the cache key.** Changing a `PROMPTS` instruction
  in `modal_apps/embed_core.py` changes query vectors without changing the space, so cached
  query vectors and the calibration go stale silently. If `retrieval.py` ever imports
  `modal_apps.embed_core` to "prepare" query text (plan line 242 is ambiguous), the image lacks
  `modal_apps/`. `test_packaging._imported_modules` considers root `.py` files only (measured),
  so it would not catch that import.
- **Dump growth.**
  - pgvector dumps vectors as text, about 10 characters per float (estimate), so a 1024-dim
    row is about 10 KB before compression.
  - The backfill alone multiplies the nightly `pg_dump --format=custom` (measured
    `backup.py:137`) several-fold, and 14 dumps are retained (recorded, compose).
  - Calibration spaces, including an `@4096` one, are permanent ("no DELETE").
  - In six months the backups could reach a size that fills the host disk, as anonymous volumes
    did on 2026-09-10 (recorded, CLAUDE.md). Estimate the dump size after backfill and say so
    in the Rollout.
- **Modal cost is a model, not the bill.** `boot_s` is "time since process start" (plan
  line 160), so billed container time before the process starts is invisible. The idle tail is
  assumed to equal `scaledown_window`. Spec §6.1 says cost is "measured, not guessed". Reconcile
  one week against Modal's usage page before the 🧮 line is treated as a recognition decision.
- **Model dtype on a T4.** The plan sets no dtype. Qwen3-Embedding-4B in fp32 is about 16 GB,
  which is the T4's whole memory (estimate). Whether bf16 runs well on a T4 (sm_75) is UNKNOWN.
  Pin `torch_dtype` (fp16 on a T4) in `embed_service.py` and record the measured memory.
- **`has_primary` is already true** for ISW, 38 North and Bank of Japan groups, whose feeds have
  `kind: "primary"` (measured, `brief.py` feed list). Spec §6.3's "primary source present"
  means a storyline's watched sources, from plan 3. Fine if intended; say so.
- **The config path writes no row.** Spec §6.1 asks for "a decision row, not a silent skip"
  when the service is down. When `active_embedder()` returns a problem, the plan makes no call
  and writes no `embedding_calls` row, only a log line and the alert.

---

## Round balance

A second full red-team round would not earn its keep.

- Objections 1 and 3 and every secondary finding are concrete fixes with obvious tests:
  checklist items for execution.
- Objection 2 is the only design change. It moves or redesigns the retrieval half of Task 8:
  the queries, the judging depth, the gate metric and the tie margin.
- That is one task, not the plan. Check it in the Task 8 implementer brief, or in a short
  targeted re-read of the revised Task 8 alone, not a third round over the whole plan.

---

## Disposition (controller, 2026-11-24)

All three objections and every flagged implicature were applied to the plan the same day:

- **Objection 1 (0020 before the image swap):** Rollout step 0 is now the image swap, run before
  any plan-1 commit is pushed; the controller holds commits locally until step 0 is confirmed.
  0020 opens with a guard whose message names step 0. The image is pinned by digest.
- **Objection 2 (Task 8's queries, floor, bar, tie margin):** queries are generated in the
  editor's field styles from sampled situations, judged over a top-30 pool; the retrieval floor is
  no longer fitted in plan 1 (NULL until plan 2's replay); the grouping bar is weighted
  precision ≥ 0.90 and recall ≥ 0.60 at the natural mix; ties use a bootstrap interval.
- **Objection 3 (one unembeddable item stalls the queue):** `item_embed_skips`, an `input` error
  kind with bisection down to single texts, and assignment that passes over skipped items.
- **Fixed check:** #1 reworded (a fresh cluster only); #2 replaced by amcheck after REINDEX, with
  the count taken after the app is stopped; #3 scoped to grouping; #4 corrected to 1–2 h;
  #5 digest pin; #7 operational docs and memories added to Task 1; #10 re-estimated after the
  redesign; #11 a curl check in the Rollout.
- **Secondary:** batches split at 50,000; Google suffix stripped by URL host, with whitespace
  normalised; CI lint lists in Task 6; `COPY scripts/` no longer temporary (bead 115 noted);
  a per-space advisory lock; a prompt version in the Modal request, the query cache key and the
  calibration row; vectors left out of `pg_dump`; a week's Modal bill reconciled in the Rollout;
  dtype pinned per GPU; `has_primary` semantics stated; the config path's record named.

No second round, per the review's own recommendation.

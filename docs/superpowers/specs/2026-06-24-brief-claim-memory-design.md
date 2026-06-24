# Brief Claim-Memory (anti-repetition) — Design Spec

**Date:** 2026-06-24
**Status:** Approved (brainstorm) → pending implementation plan
**Author:** Fernando Ferreira (with Claude)
**Related:** memory `formatter-owns-style`, `signals-delimiter-fragility`, `signals-parse-error-is-truncation`, `live-state-on-deploy-host`, `dockerfile-copy-allowlist`, `newsbrief-deferred-findings`; follow-up memory `signals-extraction-separate-call-followup`; `docs/2026-06-24-audit-todo.md` (item #1)

---

## 1. Context & Goal

The `docs/2026-06-24-audit-todo.md` item #1 proposed a "semantic topic-novelty filter" ported from a friend's keyword bot (chubbot): topic fingerprint → recency decay → suppress recently-covered topics, implemented "via existing Chroma embeddings."

**Investigation overturned that premise on two counts:**

1. **There is no embedding substrate for the brief's own content.** "Chroma" here is a remote *podcast-archive* MCP server (`search_podcasts` / `latest_on_topic`, brief.py:1206-1265) — read-only analyst context, not a store of the brief's topics. No local embedding model exists anywhere in the codebase.
2. **The repetition is not topic-level.** Classifying 7 consecutive real briefs (18→24 Jun 2026) shows the repeated material is **durable facts and analytical frames re-stated near-verbatim every day, inside topics that are legitimately covered daily.** Examples and frequency:

   | Repeated near-verbatim | Appears in |
   |---|---|
   | "BOJ lifted its key rate 25bps to 1.0%… highest since September 1995" (a one-time event from **Jun 16**) | all 7 |
   | "China could miss its 4.5–5% growth target, setting up tough Politburo decisions in July" | all 7, often word-for-word |
   | "BCA assigns 60% probability of renewed fighting" | 6–7 |
   | "Brent $90–100 floor thesis" | 6 |
   | "Iran's playbook: diplomacy while delaying delivery / mine clearance is a 30-day process" | 5–6 |
   | "Observing Japan: next hike Q4 fraught / Takaichi consumption-tax collision" | 6 |

   By Jun 24 the reader has been re-taught the Jun 16 BOJ hike **eight days running**.

**The smoking gun:** the worst repetition lives in the **back half** of each brief (Japan, China, Korea, Iran-pulse, Macro Signal). `yesterday_block` feeds `yesterday_brief[:2000]` (brief.py:1467-1473) — **~2000 chars ≈ TOP STORIES + first market-pulse bullet only.** Everything from Japan onward in yesterday's brief is truncated away before the model sees it, so it cannot tell it already wrote a near-identical section yesterday. The repetition is concentrated exactly in the region the truncation blinds it to.

Corroboration: the model *already* writes "No significant change —" where the unchanged situation happens to fall inside the 2000-char window or is salient enough to recall (UK 24th; Korea 19/20; Japan 22nd). It has the right instinct; it lacks the memory.

**Goal:** stop the brief re-explaining facts the reader already holds, while never suppressing a legitimately-recurring (or pinned) topic. Two complementary parts:
- **Part A** — remove the truncation blindfold (restore yesterday's full prose).
- **Part B** — a compact, multi-day **standing-claim ledger** so established facts stay suppressed even after a week, with no hard window.

## 2. Scope & Non-Goals

**In scope:**
- Part A: feed yesterday's brief whole (minus the machine-generated trade-update tail); strengthen the "no significant change" instruction to cover standing analytical frames.
- Part B: a new `brief_memory.py` module — a model-maintained standing-claim ledger persisted as JSON on the deploy-host volume, injected into the daily prompt and reconciled by a separate post-generation LLM call.

**Non-goals (explicit):**
- **No topic-level suppressor.** The chubbot framing is empirically wrong for this product — the repeated topics are pinned/legitimately-daily (Iran, Japan, China, Ukraine); suppressing them would degrade the brief.
- **No embeddings / semantic fingerprinting.** The model performs claim dedup in the reconcile call; we add no embedding dependency.
- **No change to the signals (`@@@SIGNALS@@@`) extraction.** Moving signals to its own post-gen call is a separate future discussion (follow-up memory `signals-extraction-separate-call-followup`); this build leaves signals as-is.
- **No multi-day full-brief feeding.** Part A is one day; Part B carries the multi-day memory compactly.

## 3. Architecture

```
 submit (pre-gen)                         collect (post-gen)
 ─────────────────                        ──────────────────
 load_ledger() ─┐                         brief text ─┐
 yesterday ─────┼─► build_daily_prompt                 ├─► reconcile_ledger() ─► save_ledger()
   (whole, no   │     · yesterday_block (full)         │      (1 small LLM call,
    trade tail) │     · ESTABLISHED block   prior ledger ┘       model returns full
                └─►                                              updated ledger)
```

One new first-party module `brief_memory.py`; two touch-points in `brief.py` (`build_daily_prompt` gains an `established_block`; the collect path calls `reconcile_ledger`). Deterministic code owns persistence, date-stamping, size caps, and retirement; the model owns claim wording and dedup.

## 4. Part A — remove the blindfold

- In `build_daily_prompt` / `yesterday_block`: replace `yesterday_brief[:2000]` with the **whole** brief body (~600 words ≈ 4k chars).
- **Strip the appended `📈 TRADE UPDATE`** before feeding back. It is generated by `trading.py`, changes every day, and is pure noise for novelty. *Build-time check:* confirm whether the trade-update block is persisted inside `brief-<date>.md` or sent as a separate Telegram message; strip only if present.
- **Strengthen the instruction** (currently brief.py:1469-1471) so "replace with a single sentence: 'No significant change — …'" explicitly applies to standing analytical frames (named theses, podcast framings, prior one-time events), not only topic situations.
- One day only.

## 5. Part B — the standing-claim ledger

### 5.1 State

One JSON file on the deploy-host volume (alongside `signals` / `book` / `sources`; **not** the dev repo — see `live-state-on-deploy-host`). Proposed name `brief_memory.json`. Shape:

```json
{
  "version": 1,
  "claims": [
    {
      "id": "c-0007",
      "claim": "BOJ raised policy rate to 1.0% on 2026-06-16 (highest since 1995)",
      "topic": "japan",
      "first_seen": "2026-06-18",
      "last_reaffirmed": "2026-06-24",
      "restate_count": 7
    }
  ]
}
```

`id` is a code-assigned stable handle (monotonic `c-NNNN`). It is the mechanism that lets dates be reconciled deterministically without any fuzzy text-matching (§5.3-§5.4).

### 5.2 Lifecycle

1. **Inject (pre-gen).** Active claims render into the prompt as an `ESTABLISHED` block: *"The reader already knows the following. Reference each in ≤1 clause only if still relevant; do NOT re-explain. Lead every section with what changed since."*
2. **Reconcile (post-gen).** One small LLM call receives `(current ledger with ids, today's brief)` and **returns the full updated ledger**. For each claim it keeps or updates, it **echoes the existing `id`**; for genuinely new facts it returns the claim with **no `id`**. The model thus performs the semantic "is this the same claim?" judgement (returning the matching id), while code never fuzzy-matches text. The model may reword a kept claim's text (e.g. a second BOJ hike rewrites the rate claim) — the echoed id tells code it is the same lineage.
3. **Decay = self-expiry.** A claim retires when not reaffirmed for **N briefs** (default **N = 7**, configurable). This is accumulated memory whose entries self-expire when they stop mattering: a fact stays suppressed while its topic is covered daily, and naturally reopens if the topic drops out for a week. No hard window, no cliff.

### 5.3 Guardrails (deterministic, in code — not the model)

- **Dates stamped by code** (`first_seen`, `last_reaffirmed`, `restate_count`), never trusted from the model. Reconciliation is purely by `id`: a returned claim **with** a known id → carry its `first_seen`, set `last_reaffirmed = today`, `restate_count += 1` (and accept any reworded text); a returned claim **without** an id → assign the next `c-NNNN`, `first_seen = last_reaffirmed = today`, `restate_count = 1`; a prior id **absent** from the return → left untouched and subject to retirement-by-N. No text matching anywhere.
- **Size cap** ~25 claims; on overflow drop least-recently-reaffirmed.
- **Schema validation** on the model's return; invalid → reject the update (§7).
- **Retirement enforced in code** using `last_reaffirmed` + N.

### 5.4 Reconcile call

- Separate, dedicated post-gen LLM call (decision: keeps ledger maintenance away from the fragile brief-output tail — see `signals-delimiter-fragility`, `signals-parse-error-is-truncation`; a failure here can never truncate or corrupt the brief).
- Structured output (tool-use / forced JSON), **not** a literal `@@@` marker.
- Model: default **Haiku 4.5** (small, cheap structured task; ~$0.001/day); configurable.

## 6. Model choice rationale (model-maintained ledger)

Letting the model return the whole updated ledger turns the hardest part — semantic claim dedup — into something the LLM does well, while code owns only what code is reliable at (dates, caps, persistence). Same division of labour as the signals JSON. Risk is mild drift, bounded by the size cap and the low stakes (worst case is a brief re-explains something — i.e. today's status quo).

## 7. Error handling (fail-safe, logged)

Per the fail-open-audit note in the audit-todo, every failure degrades to *no suppression*, logged, never a crash and never lost memory:

- Ledger file missing/corrupt → treat as empty, `log.warning`, brief runs normally.
- Reconcile call fails (network/timeout) → **keep prior ledger unchanged**, `log.warning`.
- Reconcile returns invalid/over-cap JSON → reject update, keep prior ledger, `log.warning` (check `stop_reason` for truncation per `signals-parse-error-is-truncation`).
- Empty ledger (cold start) → `ESTABLISHED` block omitted; brief identical to today's behaviour.

## 8. Testing

- **CI-safe pure functions** (no network): load/save, schema validation, date-stamp reconciliation, size-cap eviction, retirement-by-N, `ESTABLISHED` block rendering, trade-tail stripping. Unit-tested with fixtures, run under existing pytest.
- **Reconcile LLM call behind a provider seam** with a Fixture implementation for deterministic tests; the live call is operator/integration-tested (the `prices_yf` / `scorer_llm` precedent).
- Respect `formatter-owns-style` (ruff reflows on save) and the full pre-push gate (`ruff check` + `ruff format --check` + `pytest`; stage all reformatted files).

## 9. Integration & deployment

- **New first-party module `brief_memory.py`** → must be added to the Dockerfile COPY allowlist **and** the CI/workflow paths, or it ModuleNotFounds at runtime despite green CI (`dockerfile-copy-allowlist`).
- **Flag-gating.** Part A ships unconditionally (trivial, strictly better). Part B behind `BRIEF_MEMORY_ENABLED`, defaulting **off** for the first run(s) so we can eyeball one ledger + one brief before turning it on — then on. Mirrors the enrichment flag discipline.
- State file lives on the deploy-host volume; document its path and a manual reset (delete file) in the runbook.

## 10. Success criteria

On a week of live briefs with Part B on: the standing facts in §1's table appear **at most once** as a full explanation, then collapse to ≤1-clause references or "no significant change," while genuinely new developments still get full treatment and no pinned/recurring topic is dropped.

## 11. Open questions / follow-ups

- **Signals extraction → own call?** The `@@@SIGNALS@@@` block has the same truncation/delimiter exposure; moving it to a separate post-gen call (like §5.4) is recorded for next session (`signals-extraction-separate-call-followup`). Out of scope here.
- Confirm at build time whether the trade-update tail is inside `brief-<date>.md` (§4).
- N = 7 retirement window and the ~25-claim cap are starting defaults; tune after observing real ledgers.

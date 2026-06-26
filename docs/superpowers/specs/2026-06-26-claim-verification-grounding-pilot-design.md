# Claim-Verification Grounding Pilot — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorm complete) → ready for implementation plan
**Backlog item:** #6 (last item) from `external-geo-dashboards-backlog` — "Claim-verification-before-publish" (Pharos Grok pattern). **Pharos is AGPL-3.0 → idea only, reimplemented from concept, no code lifted.**

## One-line

A flag-gated, **log-only** Sonnet check that measures how often the brief's **TOP STORIES** make factual claims **not grounded in the source material we actually fed the model** — accumulate ~14 days of shadow logs, then decide promote / keep / kill against a pre-registered gate.

## Motivation & framing

The backlog item is "verify claims + store citations/discrepancies before publishing." Two realities reshape it:

1. **The publish path is fully automated, no human in the loop.** `mode_collect` polls the batch → gets the brief as markdown → `deliver()`s to Telegram immediately. There is no structured claim list at publish time, and no opportunity for a human gate.
2. **We cannot cheaply verify *truth*** (that needs the open web, which the batch model already searched). **We can cheaply verify *grounding*** — "does each significant claim trace to source material we showed the model?" That targets the real LLM failure mode (inventing specifics no source contained) without re-doing the model's web search.

The user has occasionally noticed unsupported/wrong/stale statements in delivered briefs, but not often enough to know if it's a real recurring problem. So — exactly as the GDELT and sentiment-sizing items were closed by a **cheap validation spike → confident null**, not a hunch — #6 is run as a **measurement pilot first**. The decision (promote / keep / kill) is made from data, against criteria fixed *before* running.

Because the only persisted source material (`source_index`) exists only going forward (since ~2026-06-25), the pilot is **prospective**: a live shadow that accumulates logs in production, rather than an offline backtest. The pilot artifact *is* the silent-audit-log feature in shadow mode, so a "promote" decision is near-zero extra wiring.

## Scope decisions (locked during brainstorm)

| Fork | Decision | Why |
|---|---|---|
| What does verification output? | **Silent audit log** (Option 2), run as a shadow pilot | Zero reader/delivery risk; matches the descriptive/fail-safe DNA; the log *is* the validation vehicle. Reader-flag/gate deferred until data justifies. |
| Verify against what? | **Richer per-item ground** — headlines **plus** their ≤400-char summaries | Catches magnitude/detail errors (`overstated`), not just "no source at all". Nearly free: `fetch_rss` already attaches these summaries to `feed_content`; `build_source_index` just drops them. |
| Which judge model? | **Sonnet (`claude-sonnet-4-6`)** | A measurement instrument is only as good as its judge; cost de-prioritized for a clean signal. One-line `VERIFY_MODEL` swap to Haiku if Sonnet proves overkill (same escape hatch #5b left for reconcile). |
| Which parts of the brief? | **TOP STORIES only** | Where hard, datable factual assertions live; same scope as #3/#5a. Forward-looking/analytical sections are mostly hedged language that isn't groundable → noise. Widening is a trivial follow-up. |

**Non-goals (explicit):** never affects trading; never affects what is delivered (pure shadow); no reader-facing output in the pilot; no external/web calls in the verify step; no historical backfill.

## Architecture & data flow

New top-level module **`claim_verify.py`** — mirrors `brief_memory.py`'s shape: pure functions + an injectable model call (`call=`) + a fail-safe orchestrator.

```
mode_submit (evening):
  feed_content/web_content ──► build_source_evidence() ──► claim_evidence-{day}.json
  (already-fetched RSS headlines + summaries; gated by CLAIM_VERIFY_ENABLED; wrapped fail-safe)

mode_collect (morning, AFTER deliver()):
  brief text ──► extract_top_stories() ──┐
  claim_evidence-{day}.json ─────────────┼─► Sonnet grounding call ─► verification-{day}.json
                                          │   (forced-tool JSON, 90s + 1 retry, gated, fail-safe)
review (manual, ~day 14, ≥10 briefs of data):
  scan verification-*.json ──► summarize_verifications() ──► decision-ready aggregate
```

Properties:
- **Shadow:** the verify step runs *after* `deliver()`, in its own `try/except`, sibling to the existing ledger-reconcile block. Brief delivery is never delayed or altered.
- **No external calls in verify:** grounds the brief only against feed text already pulled at submit.
- **New flag `CLAIM_VERIFY_ENABLED`**, default off, independent of `BRIEF_MEMORY_ENABLED`. The pilot is the feature in shadow mode.
- **`dockerfile-copy-allowlist` chore:** new top-level module ⇒ Dockerfile `COPY` + workflow path lists + workflow ruff file lists each need updating, or it's a runtime `ModuleNotFound` that escapes CI lint.

## Component detail

### 1. `build_source_evidence(feed_content, web_content) -> str` (submit side)
A sibling of `build_source_index` that **retains** the indented summary lines (which `build_source_index` strips). Produces a source-labelled blob: `SOURCE: <name>` headers, each followed by `- <title>` and the `  <summary>` line. Persisted as `claim_evidence-{day}.json` = `{"date": day, "evidence": <str>}` via `_write_json_atomic`, gated + wrapped fail-safe in `mode_submit` alongside the existing `save_source_index` call.

### 2. Grounding check (collect side, post-`deliver()`)

- `extract_top_stories(brief_text) -> str` — pull the TOP STORIES section. The brief uses HTML bold section headings: the section starts at the `<b>…TOP STORIES…</b>` heading and ends at the next `<b>…</b>` heading (e.g. `<b>📈 MARKET PULSE — WHAT MOVED</b>`). Operates on the **raw delivered `brief`** (HTML intact — the same value `mode_collect` already feeds to `reconcile_ledger`, *not* the tag-stripped archive). Returns empty string if the heading is absent.
- One **forced-tool** Sonnet call (`tool_choice` forcing a single tool, generous `max_tokens`, `timeout=90` + one retry, `stop_reason == "max_tokens"` treated as truncation per the `e255436` signals lesson). System prompt frames a **grounding** judge, explicitly *not* a world-fact-checker: "verify each claim only against the provided sources."
- Tool output schema:

```json
{
  "claims": [
    {
      "claim": "<factual assertion from TOP STORIES>",
      "verdict": "supported | unsupported | contradicted | overstated | unverifiable",
      "evidence": "<the source line that supports/contradicts it, or \"\">",
      "reason": "<one terse clause>"
    }
  ]
}
```

**Verdict taxonomy** (chosen to separate real signal from expected noise):

| verdict | meaning | pilot interpretation |
|---|---|---|
| `supported` | a source line backs it | healthy — the denominator |
| `unsupported` | no related source line at all | **flag, but confounded** (see below) |
| `contradicted` | a source says the opposite | **gold signal** — confound-free |
| `overstated` | source backs the gist but not the specifics | **strong signal** — largely confound-free |
| `unverifiable` | analytical/forward-looking, not a hard factual claim | **excluded from flag rate** — the noise valve |

The `unverifiable` bucket is the key choice: it gives the judge an explicit exit for hedged/analytical sentences so the denominator is only hard assertions and the flag rate actually means something.

### 3. Persisted record
`verification-{day}.json` = `{date, model, top_stories_present: bool, n_claims, counts_by_verdict: {...}, claims: [...]}`. The **full** per-claim list is kept (not only flags) so review can compute rates *and* eyeball whether the judge's verdicts are themselves trustworthy (judge-validity check).

### 4. `summarize_verifications() -> aggregate` (review side)
Scans `DATA_DIR` for `verification-*.json`, ignores malformed files, returns/prints totals: claim count, counts by verdict, flag rate, per-day breakdown, and the list of flagged claims with their evidence — the decision-ready aggregate. Invoked manually (~day 14).

## Error handling / fail-safe ladder

The brief is sacred; every new step is non-load-bearing.

| Failure | Behavior |
|---|---|
| `CLAIM_VERIFY_ENABLED` off | Both touch-points no-op. Zero change to the pipeline. |
| `build_source_evidence` throws at submit | `log.warning`, submit continues. No evidence file ⇒ collect skips verify that day. |
| `claim_evidence-{day}.json` missing/unreadable at collect | Log, skip verify. No retro-fabrication. |
| TOP STORIES section absent | Write record with `top_stories_present: false`, `n_claims: 0`. Recorded, not crashed. |
| Sonnet call errors / times out / `max_tokens` truncation | One retry; on persistent failure `log.warning`, write nothing. Brief already delivered. |
| Grounding JSON unparseable | Log, write nothing. Never partial-write a corrupt record. |

All wrapped in one `try/except` around the verify block in `mode_collect`, sibling to the existing reconcile block.

## Pre-registered decision gate

Reviewed once **≥10 briefs** have data (~day 14).

**Central confound — named up front:** the brief is generated *with web search*, so the model legitimately knows things **not in our persisted feeds**. A true claim sourced from web search will be flagged `unsupported` even though it is correct. This instrument therefore measures *"grounded in the material we showed it,"* a **subset** of *"true."* Consequences for reading verdicts:

- **`contradicted` = gold signal.** A source we gave it says the opposite — web search cannot explain it away. **Headline metric.**
- **`overstated` = strong signal.** Source backs gist, model inflated specifics — largely confound-free.
- **`unsupported` = confounded.** Needs manual triage to split "invented" from "sourced via web search we didn't persist." Not trustworthy alone.

**Gate:**
- **Gate 0 — instrument validity (first):** hand-adjudicate ~20–30 flagged claims. Is the *judge* right? If judge precision on flags < ~50% (mostly judge errors), pilot is **INCONCLUSIVE** — fix judge/ground or kill; do **not** promote on bad measurement.
- If valid, classify by **confirmed-wrong rate** (manually-verified `contradicted` + `overstated` + truly-invented `unsupported`):
  - **KILL / confident-null** — ≈ 0 confirmed-wrong (≲ 1 per ~5 briefs). Brief is well-grounded; #6 closes as a documented null (à la GDELT); code removed or left dormant. **Most likely outcome and a fine one.**
  - **KEEP as silent log** — low but nonzero. Leave the audit log running; no reader-facing change.
  - **PROMOTE to reader flag (Option 1)** — recurring, mostly `contradicted`/`overstated` (≳ 1 confirmed-wrong per ~2 briefs). Reader-facing flag becomes its own follow-up spec.

Thresholds are deliberately rough and adjustable, but **fixed before results are viewed**.

## Testing (TDD, mirrors `test_brief_memory.py`)

- `build_source_evidence` — retains summaries + `SOURCE:` labels; handles empty / `(no RSS content)`.
- `extract_top_stories` — finds section across heading variants; empty on absence; stops at next boundary.
- grounding-response parser — valid list; coerces/drops bad `verdict` values; rejects truncated/non-JSON; defensive on bool/garbage fields (same style as `_coerce_severity` / `_coerce_source_count`).
- record assembly — `counts_by_verdict`, `top_stories_present`, claim passthrough.
- `summarize_verifications` — aggregates across multiple day files; ignores malformed.
- fail-safe — evidence missing, injected `call=` raises, bad JSON ⇒ no exception escapes, no corrupt file written.

Sonnet call injected via `call=` (like `reconcile_ledger`) ⇒ all tests offline/deterministic. No pandas ⇒ no `importorskip`. Full gate per `brief-local-run`: `ruff check` + `ruff format --check` + `pytest` (stage all reformatted files).

## Rollout

1. Build + tests green locally; push to `main` (solo repo, commit straight to main).
2. Deploy (Docker) — bundle with the held #4/#5a/#5b batch if still pending, per user.
3. Set `CLAIM_VERIFY_ENABLED=1` on the deploy host. Shadow logs begin accumulating on the next `mode_submit`/`mode_collect` cycle.
4. ~Day 14 (≥10 briefs): run `summarize_verifications`, apply the gate, decide.

## References

- Item #6 in `external-geo-dashboards-backlog` memory.
- Connects to the confidence-calibration thread (`sentiment-sizing-null-decided` → "next value = brief quality") and the claim ledger (`brief-claim-memory-build`, #3 corroboration tag).
- Discipline precedent: `2026-06-25-gdelt-signal-validation-spike-design.md` (validate-the-premise → pre-registered gate → confident null).
- Lessons applied: `signals-parse-error-is-truncation` (treat `max_tokens` as truncation), `signals-extraction-separate-call-followup` (`e255436`: match post-gen call timeout to model latency), `dockerfile-copy-allowlist` (new top-level module = 3 updates).

# Claim-verification pilot — verdict, 2026-08-16

Decision for backlog item #6, the grounding-check measurement pilot specced in
`docs/superpowers/specs/2026-06-26-claim-verification-grounding-pilot-design.md`.

**Data:** 45 briefs, 2026-06-27 → 2026-08-16, 867 claims. The gate called for a read
once ≥10 briefs had data (~day 14); this is 45.

**Verdict: KEEP as a silent log.** Not KILL, not PROMOTE. Reasoning below, judged
against criteria fixed before the results were viewed.

## Results

| Verdict | n | % of gradable | per brief |
| --- | --- | --- | --- |
| supported | 229 | 37.5% | 5.09 |
| unsupported | 324 | 53.0% | 7.20 |
| overstated | 54 | 8.8% | 1.20 |
| **contradicted** | **4** | **0.7%** | **0.09** |
| unverifiable | 256 | — | 5.69 |

Gradable = 611 (claims excluding `unverifiable`). Flag rate 44.1%.

## Gate 0 — instrument validity

**Passes on judge accuracy, fails on judge completeness.**

All 22 `overstated`/`contradicted` flags carrying a reason were hand-adjudicated
(the gate asks for 20–30). **Judge precision ≈ 91%** — 2 of 22 are outright judge
errors, comfortably clear of the ~50% bar. The judge is accurate about what it
claims to measure.

But **41.1% of all 382 flags carry no reason at all** (157/382) — worst exactly
where the gate leads:

| Verdict | unreasoned |
| --- | --- |
| contradicted | 2 / 4 = 50.0% |
| overstated | 34 / 54 = 63.0% |
| unsupported | 121 / 324 = 37.3% |

The cause was the schema, not the model: `reason` was absent from the tool's
`required` list, so omitting it was compliant. **Fixed** — `reason` is now required,
and `summarize_verifications` reports `unreasoned_flags` / `unreasoned_rate` so
instrument health is visible in the report instead of being rediscovered by hand.

Only 7.6% of flags cite what a source *does* say; 45.0% assert absence only, which
is the pre-registered web-search confound by construction.

## The headline metric is zero

All four `contradicted` flags in 45 briefs:

| Date | Claim | Adjudication |
| --- | --- | --- |
| 2026-06-30 | "At least 17 regions have imposed mandatory restrictions" | **Judge error** — source said "more than 20", which *entails* "at least 17". Not a contradiction. |
| 2026-07-30 | "USD and gold both firmed even as equities sold off" | **Dubious** — the judge's own reason is hedged and does not establish a contradiction. |
| 2026-07-28 | Netanyahu polling numbers | Unadjudicable (no reason) |
| 2026-08-16 | Iran/Oman shipping framework | Unadjudicable (no reason) |

**Zero confirmed contradictions in 45 briefs.**

## The pre-registration was wrong about `overstated`

The spec called `overstated` "largely confound-free". **It is not.** Roughly 7 of
the 22 adjudicated flags are true, checkable facts the brief obtained via its own
web search that our persisted feeds never carried — the "Ulchi Freedom Shield" drill
name and its 17 August start, the KOSPI's −8%, the Khamenei funeral dates. Flagged
only because we did not persist the source.

This is a finding about the instrument's design, not about the brief. It means the
confound contaminates `overstated` about as much as `unsupported`, and the gate's
"lead on contradicted + overstated" advice only half survives.

## There is a real defect pattern

About 6 of the 22 are genuine grounding failures, and they cluster into three kinds:

- **Quote precision** — words inside quotation marks that appear verbatim in no
  source: Hegseth's "Now they pay"; Trump's "really bad"; the USS Cole line;
  "decisive and swift response" where the source said "swift response".
- **Inflation to a pattern or superlative** — "North Korean-sourced missiles *keep
  landing* on civilian targets" from a source reporting **one** incident; "the
  *deepest hit yet*" as an added superlative.
- **Numeric drift** — the brief said **11 wounded** where our own source said **12**.
  This one arguably belonged in `contradicted`.

None are severe. All are the kind of small inflation that erodes trust in a brief
whose value is precision.

## Against the pre-registered thresholds

Confirmed-wrong rate is **2/45 strict (1 per 22 briefs)** to **~10/45 generous
(1 per 4.5)**, depending on whether quote-precision and superlative inflation count.

- KILL was defined as ≲1 per 5 briefs → the generous count sits right at the boundary
- PROMOTE was defined as ≳1 per 2 briefs → not close

**→ KEEP as a silent log.** The quote-precision pattern is real and recurring enough
not to close as a null, but the headline metric is zero and the strong metric turned
out confounded, so no reader-facing flag is justified.

## Two caveats that soften every number here

1. **The judge changed mid-pilot.** `claude-sonnet-4-6` for the first 5 briefs,
   `claude-sonnet-5` for the remaining 40, from the 2026-07-02 model bump
   (`newsbrief-model-config`). `overstated` went 1.2% → 10.0% of gradable across that
   boundary. n=5 on the old judge makes this weak evidence, but the pilot is not a
   single instrument end to end.
2. **Supported-share rose 30.0% → 45.3%** between the first and second halves. The
   likely cause is mechanical — more feeds persisted (RSS_FEEDS grew; the 2026-08-09
   source fixes landed) means more claims are traceable — rather than the brief
   becoming better grounded. Do not read it as improvement.

Same family as [[backtest-nonstationarity-check]] and `analysis-stats-traps`: the
measuring stick changed during the measurement.

## What happens next

- Pilot keeps running, silent, unchanged in behaviour.
- With `reason` now required, the next read can execute Gate 0 properly on the whole
  flag set instead of the 58% that happened to carry one.
- Worth revisiting after ~30 more briefs on the fixed instrument. If the
  quote-precision pattern persists at a measurable rate, the cheapest intervention is
  a brief-prompt constraint (do not put words in quotation marks unless they appear
  in a source), not a reader-facing flag.

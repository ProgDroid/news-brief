---
name: analysis-stats-traps
description: "Four measurement traps that produced confident wrong numbers in this repo — correlated observations, ties counted as failures, a one-directional pre-registered gate, and an LLM eval primed with the field it was scoring"
metadata: 
  node_type: memory
  type: project
  originSessionId: 951b41de-741d-4e04-a8b4-945ab0025650
  modified: 2026-08-29T21:44:14.379Z
---

Two statistical traps hit in the 2026-08-16 trading retrospective, both producing a
confident-looking number that was wrong. This project runs a lot of attribution and
backtesting, so both will recur.

**1. Correlated observations inflate significance — check the unit before the test.**
The paper book's 130 closed legs are not 130 independent observations: 105 of them
are `reversal` closes, so the same instrument appears repeatedly as alternating legs
of one continuous engagement. A sign test on the legs gave **p=0.033** for "the
signals are inverted". Collapsing to ticker-**episodes** (a maximal chain where each
close is immediately followed by the next entry) and re-testing gave **p=1.000, hit
exactly 50.0%** — the entire result was the correlation. Group by **instrument**, not
ticker string: the book carries the same instrument under several spellings
(`RRl_EQ`/`RRl`, `FLRK`/`FLRKl_EQ`) and grouping by name silently re-introduces the
dependence you are trying to remove.

**2. A paired sign test must DROP ties, not count them as failures.** Comparing
policy variants per instrument, instruments with a single signal behave identically
under every policy and contribute `diff = 0`. Counting those as "not better" gave
`no reversal` **12/21, p=0.66**; dropping the 3 ties gave **12/18, p=0.238**, and
`cooldown 14d` went from p=0.0266 to **p=0.0044**. The tie count is also a useful
diagnostic in its own right — it says how many units the policy even touched.

**How to apply:** before any sign test or IC on book data, state what one independent
observation *is* and justify it. Prefer a **paired within-unit design** (same name,
same window, vary only the mechanism) — that is what made a mechanism claim
defensible at n=21 when aggregate tests could not, because unit-selection affects
both arms equally. Then report the effect size *and* the tie count, and sanity-check
robustness by dropping the largest contributors (the 17/21 result survived removing
the two worst chains, all 10+-leg chains, and restricting to 2–4-leg chains).

**3. A one-directional pre-registration is a blind gate (2026-08-29, gold set).** Spec
§12.3 pre-registered the regression condition as "a run that improves recall while
dropping precision below baseline". The first real run failed in the **mirror**
direction — precision 33%→75%, recall 100%→42.9%, losing 4 of 7 true breaks — and the
gate said nothing, because it only watched one way. Pre-registration protects against
motivated reading only if it names *both* directions, or states the asymmetric cost
that makes one worse. Here the cost was already written down elsewhere in the same spec
(§6.1: a missed break is a permanent integration error, a false break is a synthesis
error lasting a day) — it simply had not been connected to the gate. **Before accepting
a pre-registered criterion, ask what result would pass it and still be bad.**

**4. Do not feed the model the label you intend to score.** The gold-set probe handed
each item's ledger row to the model with `severity` already populated from the seed run,
then reported severity variance as evidence the field was healthy. It came back
**unchanged on 21 of 23** — the "variance" was echo, not judgment. Only `status`, which
the probe left for the model to assign, measured anything. Caught by explicitly
comparing input to output rather than trusting the distribution. **In any LLM eval,
list what the prompt already contains of the answer before reading the result.**

Sibling of [[backtest-nonstationarity-check]] — that one is about regime instability
over time, traps 1–2 about dependence across observations, traps 3–4 about the
measurement design itself letting a wrong number look right. Context:
[[trading-retrospective-2026-08-16]], [[newsbrief-kb-architecture-2026-08-29]].

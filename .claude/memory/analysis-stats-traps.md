---
name: analysis-stats-traps
description: "Two significance traps that both fired in one session on the paper book — correlated observations, and ties counted as failures in a paired sign test"
metadata: 
  node_type: memory
  type: project
  originSessionId: 951b41de-741d-4e04-a8b4-945ab0025650
  modified: 2026-08-16T11:03:13.135Z
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

Sibling of [[backtest-nonstationarity-check]] — that one is about regime instability
over time, this one about dependence across observations. Both turn an apparently
significant result into a null. Context: [[trading-retrospective-2026-08-16]].

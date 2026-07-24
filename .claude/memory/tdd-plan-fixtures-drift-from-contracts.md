---
name: tdd-plan-fixtures-drift-from-contracts
description: "When executing these detailed TDD plans, the plan's own test fixtures/assertions can use fields the real functions don't — verify fixtures against real contracts, not just the functions called"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b859e710-6883-40ff-8249-13c07d61a21b
  modified: 2026-07-22T11:40:37.562Z
---

When executing the news-brief `docs/superpowers/plans/*.md` TDD plans (Sleeve A/B era, same author/style), the plan verifies the FUNCTIONS it calls against the foundation but its **test fixtures and assertions can silently disagree with the real function contracts** — the red step then fails for the wrong reason, or the green step is unreachable.

**Why:** the plan author hand-wrote both test and impl in one pass without running them; field-name/shape mismatches slip through. Sleeve A (2026-07-22) hit THREE in 7 tasks: (1) Task 3 fixture `_parse_pg_market` omitted `question` while impl read `parsed["question"]` → KeyError; (2) Task 6 `trade_history` list-fallback referenced unbound `items` → NameError on a list response; (3) Task 7 test asserted `agg["overall"]["n"]` on rows carrying `realized_return`, but `validation._stats` scores ONLY `net_return` → `overall` is None, assertion crashes regardless of the fix. Live rows carry `realized_return` (no `net_return`), so they're already excluded from `aggregate_performance` by _stats naturally; the `execution!="live"` guard is belt-and-suspenders.

**Sleeve B (2026-07-22) hit THREE more in 7 tasks, incl. a NEW flavor — the fixture can be INTERNALLY SELF-INCONSISTENT, not just drifting from a real symbol:** (1) Task-2 `_sleeve_b_open_ok` test asserted amount 10 was "within both caps" yet amount 9 was "over total" at the SAME exposure 20 / total-cap 25 — mathematically impossible (10>9, so if 9 is over-total then 10 is too); NO correct impl of the described logic can pass it. Fixed the happy-path amount to 5.0 (20+5=25, inclusive boundary), intent preserved. (2) Task-5 `fake_open` omitted the `id` key the real `open_live_position` always sets and `_predict_commit` reads (`row["id"]`) → KeyError. (3) `brief.py` needed `import common` added so the wizard reads flags as module attrs (see [[newsbrief-flag-access-module-attr]]).

**How to apply:** before trusting a plan's red→green, sanity-check the fixture field names against the REAL symbol bodies (read `_parse_pg_market`, `_stats`, `close_live_position`, etc. with serena), not just that the called function exists. Also **arithmetic-check the fixture's own assertions for internal consistency** (a monotonic guard can't call a larger amount "within" and a smaller one "over" at fixed caps). When a plan test fails at the red step for a reason other than "symbol missing" (KeyError/NameError/None-subscript/contradictory-assert), suspect a fixture bug and fix the test to the real contract / a self-consistent case — preserve the test's INTENT, don't rubber-stamp the plan's literal fixture. Flag each such deviation in the commit body. Six such defects across Sleeve A+B (3 each) — treat it as expected, not exceptional. Relates to [[polygram-live-trading-spec]].

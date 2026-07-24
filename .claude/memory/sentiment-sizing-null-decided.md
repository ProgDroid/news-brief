---
name: sentiment-sizing-null-decided
description: "DECISION 2026-06-24 — sentiment does NOT size positions (confident null backtest); thread CLOSED; bigdata REST key repurposed to descriptive brief depth, not a sizing backtest"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5774a799-ee95-48c6-9140-94365c67f319
---

**Sentiment-sizing thread CLOSED 2026-06-24.** The decisive AV backtest ([[av-sentiment-backtest-validated]]) returned a **confident null**: news-tone held-out rank IC −0.008 at 21d (n=7,063, ~0.7 SE — clean zero on a large sample, NOT underpowered); transcript +0.016 at 1d (n=659, null + underpowered, discovery ICs sign-flip = noise). **AV sentiment does NOT predict forward returns → sentiment-driven position SIZING is NOT supported.** This resolves the project's single biggest open question (the "what is bigdata worth / does sentiment earn the right to size" fork from [[bigdata-evaluation-and-trading-split]]).

**Why:** the n≈7k figure is what makes it actionable — the earlier MCP-approx pilot was n≈18 ("we don't know"); n≈7k null = "we know it's ~0." Decisions can now be made *on* the result, not deferred for more data.

**Three decisions banked (don't reopen):**
1. **Enrichment is permanently descriptive-only / never-sizing** — was "flag-off *until* proven"; the condition resolved to *no, forever*. The hard invariant was right. Remaining enrichment question is purely product (ship as brief *context* vs stay dark), not "earn sizing rights."
2. **The faithful bigdata sizing-backtest is dropped.** `backtest/sources_rest.py` (unbuilt) existed only to test bigdata's specific sentiment for *sizing*; with a clean zero on the AV proxy, paying to test whether a fancier measure beats that null is a weak bet. Not building it.
3. **The pending bigdata business-email REST key is no longer a backtest blocker** — see below.

**REST key REPURPOSED → descriptive brief depth (user goal, stated 2026-06-24).** If/when the key lands, use it for **more in-depth briefs**: catching per-stock blind spots without hand-researching every exposure (user's example: "the AVAV lawsuit is nice to know about without spending ages researching every stock I'm exposed to"). This is the "offload research time" goal — already *evidenced* in the 2026-06-19 trial (the AVAV securities class-action lawsuit + investor-day catch, the CVX Tengiz/$360M-legal blind spots; see [[bigdata-evaluation-and-trading-split]] line ~33). The descriptive case never needed sentiment to be a sizing factor — this backtest only kills the sizing-alpha case, NOT the research-offload case (a separate, already-supported claim). Prefer the `x-api-key` MCP path when the key lands (live tool shapes already captured → no unverified-field risk; see [[bigdata-mcp-enrichment-brainstorm]]).

**Where the project's value now sits:** brief quality (the informational product) + paper/validation-gate discipline — NOT chasing a sentiment alpha the data says isn't there. Highest-leverage *next builds* (from `2026-06-24-audit-todo.md`): (1) semantic topic-novelty "already-covered" filter via Chroma (serves brief unpinned-selection + deferred Bigdata thematic dedup), (2) confidence calibration (Brier + reliability curve in `validation.py`) — now MORE valuable since the brief's own confidence enum is the remaining provable predictive claim.

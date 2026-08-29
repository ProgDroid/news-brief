---
name: newsbrief-kb-architecture-2026-08-29
description: 2026-08-29 project redirection — brief becomes a render over an accumulated knowledge base; spec written, replay experiment RUN with results, 6 epics wired in bd, no code written yet
metadata:
  type: project
---

**2026-08-29 session. Design + measurement only — NO implementation started, nothing committed.**

Spec: `docs/superpowers/specs/2026-08-29-knowledge-base-architecture-design.md` (755 lines,
uncommitted). Read it before touching any of this. Work state is in **bd** — `bd ready`.

## The redirection

The daily brief stops being a search-and-print job and becomes a **render over accumulated
knowledge**. User's own framing: "build the brief from the accumulated knowledge rather than go
look it up each day from scratch." The brief stays; it becomes a consumer, not the pipeline.

Root cause of every complaint: **the system is engineered for novelty and structurally hostile to
continuity.** All three memory channels are suppressive — `yesterday_brief` ("never re-explain"),
`weekly_summary` ("do not repeat it"), the claim ledger ("do NOT re-explain or restate as news").
Meanwhile MARKET PULSE asks the model to explain moves and flag unexplained ones. Yesterday's
driver is by construction not today's news.

## Measured findings (not inferred — these were run)

- **Citation-newline corruption:** `brief.py:2948` joins `web_search` content blocks with `"\n"`.
  The API splits prose at citation boundaries, so every citation inserts a mid-sentence newline,
  often right before a comma. **All 90 archived briefs affected.** One-character fix.
- **Capture blindness:** `fetch_rss(max_items=5)`, one call site, one poll/day = 130 headlines.
  Items that appear and roll off are never seen.
- **Crowding-out, confirmed:** chip/semiconductor names appear in **58 of 90 briefs (64%)**; the
  live ledger holds **zero** chip claims (25 claims: Ukraine ×11, Iran ×8).
- **Severity classifier is degenerate:** `high` on **25/25** live claims. Populated, passes every
  completeness check, carries no information.
- **Replay experiment (the gate) — RAN, 90 briefs, Haiku 4.5.** 70 resolutions (24 `broken`,
  46 `challenged`), ~0.27 breaks/brief, ~61% hand-audited precision. Positive control passed
  (Patriot claim broken by id, 2 days). **Verdict: build propagation rule 1.**
- **The mechanism is the CAP, not the TTL** — this reversed both the spec's and the red team's
  assumption. 56/70 resolutions arrive *inside* the claim's TTL window; 54 of 68 were lost
  anyway. Claims are crowded out, not aged out. Duplication feeds it: **816 new rows in 90 days**.
- **False positives have one signature:** restatement or confirmation misread as contradiction.

## Decisions made

- **gbrain REJECTED as substrate** — no numeric series table anywhere in ~66 tables; `raw_data`
  is `UNIQUE(page_id, source)`, a snapshot not a log; schema packs emit zero DDL so the gap can't
  be filled; invalidation is inert (cascade fires only on hard delete, which resolve/supersede
  never do). **Steal its `takes` resolution vocabulary** (`correct/incorrect/partial/unresolvable`
  + `superseded_by`). Note: the CLAUDE.md gbrain block is about gbrain as *Claude's* memory —
  a different question, deliberately not edited.
- **Hermes NOT adopted.** `NousResearch/hermes-agent` is an agent runtime, not a KB. Cron
  adoption buys retries/incidents for the price of s6-overlay and 135 env vars; gateway adoption
  is invasive (sole bot-token consumer → the existing command surface must migrate). Independent
  of gbrain — "gbrain" appears 0 times in the Hermes tree.
- **Chroma STAYS.** It's a separately deployed Modal app (`CHROMA_MCP_URL`, `brief.py:122`), not a
  stack service. Augmentation, not substrate. An earlier draft wrongly retired it.
- **Storage DEFERRED**, and it is downstream of an undecided process-architecture question:
  several containers → Postgres; one consolidated service → SQLite suffices and is a much smaller
  jump (the stack has no database today). Nothing is blocked by deferring.
- **Renderer is NOT read-only** — that rule was withdrawn. 29.5% of brief statements (5.69/brief)
  are unsourced analytical framing, and that *is* the product. Replaced by
  `origin: extracted | authored`: authored interpretation is persisted and scorable but can never
  support a claim or be rendered back as established fact.
- **Equity paper book kept, re-founded on theses** (opens on conviction, holds to horizon,
  resolves with the thesis; no daily reversal, no DCA) — but **deferred**, since it can't grade a
  thesis engine that doesn't exist yet.
- **No numeric confidence scoring. Stories use tags, not trees.**

## Where to resume

`bd ready`. **Epic 1 (`news-brief-jx9`) is the whole next step** — it needs no database decision,
fixes documented defects, and produces the baseline that sizes everything after it. Every line is
reusable under the full design. Epic 3 (`news-brief-bqa`) opens with two DECIDE issues; Epic 6 is
deferred to 2026-12-01.

Replay harness and audit scripts are in the session scratchpad (not the repo). The 24 audited
break detections are the seed for the gold set (`news-brief-jx9.7`); target precision ≥85% against
a 61% baseline.

**Why:** the user asked to explore widely before funnelling, and the red-team pass plus the replay
overturned three load-bearing assumptions — treat the spec's measured sections as settled and its
deferred ones as genuinely open.

**How to apply:** start from `bd ready` and the spec, not from a fresh redesign. See
[[brief-claim-memory-build]] for the ledger's original design and
[[brief-sources-and-edge-latency-thread]] for the earlier brief-quality pass.

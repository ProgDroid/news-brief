# news-brief — Improvement TODO (2026-06-24)

Ideas surfaced while comparing news-brief's scoring against a friend's geomacro/insider trading bot
("chubbot") and reviewing the Bigdata.com plans. news-brief is the more disciplined system
(LLM-native + validation-gated); these are net-new improvements, not fixes.

Context: chubbot is **algorithmic-first** (deterministic keyword score → numeric tension → drives
live sizing, no validation gate). news-brief is **LLM-native + validation-gated** (Claude does
ranking/dedup/scoring holistically; signals can't size until they clear the `validation.py` go-live
gate). The transfer from chubbot is narrow — most of its machinery is redundant here or made so by
the Bigdata sentiment layer.

---

## High value — the one genuinely worth porting

- [ ] **Build a topic-novelty / "already-covered" filter for the unpinned-significance decision.**
      Today, whether an *unpinned* topic gets a section is 100% LLM discretion with **no memory of
      what the brief already covered** (`brief.py` ~1542). chubbot has a ready-made design:
      topic fingerprint → recency decay weight → suppress recently-covered topics
      (`geomacro/signals/{models.py:38 extract_topic_hash, feeds.py:208-216}`). 
      - Implement it **semantically via your existing Chroma embeddings**, NOT chubbot's
        capitalised-word regex (which is order-sensitive and weak).
      - This is also the **thematic-novelty filter you DEFERRED** in the Bigdata spec (open-Q4:
        "pass thematic search through an embedding-dedup + claim-check novelty filter"). One build
        serves both the brief's unpinned selection and the Bigdata thematic bundles.

## Medium value — shared gaps worth closing

- [ ] **Numeric confidence calibration (Brier score + reliability curve).** Confidence is an
      LLM-emitted 3-level enum used only as a medium/high gate (`brief.py:1700-1758`); it's never
      checked against realized outcomes numerically. The paper tracker now makes this closable — you
      already have realized net/edge by confidence tier. Add a Brier-score / reliability-curve
      function to `validation.py` (alongside `evaluate_gate`); feed the calibration summary into the
      daily prompt's `performance_prompt_block`.
- [ ] **Semantic cross-feed dedup before the prompt.** Duplicate stories from multiple feeds are
      currently concatenated verbatim and left to Claude to dedup in prose (`brief.py:1998-2005`).
      A cheap embedding-similarity pass (you have Chroma) would cut tokens and reduce the chance the
      brief double-counts a story's significance. (chubbot's lexical version is the wrong approach —
      do it semantically.)

## Lower value / optional

- [ ] **Deterministic keyword baseline in `backtest/`.** chubbot's negation-aware keyword scorer
      (`geomacro/signals/keywords.py`) could be a free, reproducible "naive scorer" to benchmark
      whether Claude/Bigdata sentiment actually beats keyword-counting on Rank IC. Only worth it if
      you want that baseline in the backtest's go/no-go evidence.
- [ ] **Fail-open audit.** chubbot's recurring bug was "missing data silently becomes a default
      value." Sweep news-brief for the same pattern — places where a feed/API error or empty result
      degrades to a silent default rather than being surfaced. (Lower risk here since you don't place
      orders, but worth a pass.)

## Sentiment-sizing question — CLOSED (2026-06-24, confident null)

The decisive AV backtest (n=7,063 news / n=659 transcript) returned a **confident null**:
held-out rank IC −0.008 (news, 21d) / +0.016 (transcript, 1d). **AV sentiment does NOT predict
forward returns → sentiment-driven position sizing is NOT supported.** This resolves the project's
single biggest open question. Consequences, now settled:

- **Enrichment is permanently descriptive-only / never-sizing** — no longer a "flag-off *until*
  proven" state; the condition resolved to *no*. Remaining enrichment question is purely product
  (ship as brief *context* vs stay dark), not "does it earn sizing rights."
- **The faithful bigdata sizing-backtest is dropped.** `backtest/sources_rest.py` (unbuilt) was
  justified solely to test bigdata's specific sentiment numbers for *sizing*. With a clean zero on
  the AV proxy at n≈7k, paying to test whether a fancier measure beats that null is a weak bet. Not
  building it.

## Bigdata.com REST key — repurposed (descriptive brief depth, not sizing)

The pending business-email REST key is **no longer a backtest blocker**. Its only remaining purpose
is **descriptive enrichment for more in-depth briefs** — catching per-stock blind spots without the
user hand-researching every exposure (e.g. the AVAV securities class-action lawsuit / investor-day
catch the 2026-06-19 trial already surfaced). This is the "offload research time" goal, already
evidenced in the trial; it never needed sentiment to be a sizing factor.

- [ ] When the key lands: confirm `BigdataProvider` endpoint paths + JSON field names against
      `docs.bigdata.com` via Context7 (currently documented-shape guesses, `enrichment` plan ~558);
      prefer the `x-api-key` MCP path (live tool shapes already captured → no unverified-field risk).
- [ ] Wire descriptive bundles (events calendar + thematic search + entity news) into the brief as
      **context only**. ToS: do NOT persist raw bigdata content snapshots (store derived fields).

## Cross-project note
The bigger transfer runs news-brief → chubbot, not the reverse: chubbot needs your validation-gate
discipline (`edge = net − benchmark`, go-live gate, read-only-until-proven) far more than you need
any of its algorithmic scoring. See the chubbot repo's `2026-06-24-audit-todo.md` / `CODE_REVIEW.md`.

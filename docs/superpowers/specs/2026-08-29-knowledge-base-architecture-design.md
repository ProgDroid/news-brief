# Knowledge Base Architecture — Design

**Date:** 2026-08-29
**Status:** Design, awaiting review
**Supersedes in direction:** the daily search-and-print pipeline (`mode_submit` / `mode_collect`)

---

## 1. Why

The daily brief is a search-and-print job. Each morning it fetches sources, sends one large
prompt, prints the result, and forgets. Three months of output show what that costs.

### 1.1 Four documented failures

All four were found in three consecutive briefs supplied by the reader.

**The Patriot reversal.** Day 1 asserted that Trump had agreed to Patriot production licenses
for Ukraine, framed it as "a third sovereignty-of-supply transfer" after France's SCALP and the
UK's jammer IP, and drew a structural conclusion: "reducing Kyiv's exposure to Western stockpile
discipline over the next 6-12 months. Supportive for European defense-tech exposure (ARMG)."

Day 3 reported the reversal — as a fresh top story, with no acknowledgement that it destroyed
Day 1's thesis. ARMG vanished from POSITION SIGNALS rather than being marked down. The Ukraine
section wrote "arms-localization tracks (French SCALP, UK jammer IP) remain the operative story",
silently retconning its own three-item list back to two.

**The chip whipsaw.** Three days, three verdicts on one thesis, each stated with identical
confidence: MU -11.3% read as "capitulation, not a break in the memory-supercycle thesis";
MU +10.4% read as "reversal confirms the thesis survived this week's flush"; MU -10.4% read as
"yesterday's reversal thesis is dead — pure momentum." Day 2 upgraded a single day's bounce to
confirmation of a multi-quarter thesis. Day 3 corrected the conclusion and preserved the method.

**The vanishing driver.** Day 1 attributed an ASML selloff to "a Chinese state-backed company"
beginning mass production — unnamed. Day 2 explained MU +10.4% with no reference to it at all.
Day 3 called it "CXMT's Shanghai debut ... the same overhang", as though continuous. The causal
driver disappeared on the day it was most needed and returned as established fact.

**Withheld explanation as a feature.** The standing-claim ledger's prompt block is headed
`## ESTABLISHED — THE READER ALREADY KNOWS THESE` and instructs "Do NOT re-explain or restate
them as news." The model adopted the label as inline vocabulary — briefs contain literal
`(established)` tags meaning *I know this and am deliberately not telling you*.

### 1.2 The common cause

Every memory channel in the pipeline is **suppressive**:

| Channel | Instruction |
|---|---|
| `yesterday_brief` (`brief.py:2347`) | "replace the paragraph with a single sentence ... never re-explain" |
| `weekly_summary` (`brief.py:2358`) | "Do not repeat it — use it as background framing only" |
| Claim ledger (`brief_memory.py:200`) | "Do NOT re-explain or restate them as news" |

Three memory systems, none permitted to answer *why is this moving?* — while the MARKET PULSE
section asks for exactly that and instructs the model to "flag any move NOT explained by today's
news." Yesterday's driver is by construction not today's news. The system is engineered for
novelty and structurally hostile to continuity.

### 1.3 Two contributing defects

**Prose corruption.** `brief.py:2948` joins `web_search` content blocks with `"\n"`. The API
splits prose into multiple `text` blocks at citation boundaries, so every citation inserts a
newline mid-sentence, often immediately before a comma. Measured across 90 archived briefs:
**85 affected, mean 4.7 breaks, worst 12**, flat across June-August. Filed separately; fix is
`"".join(...)`.

**Capture blindness.** `fetch_rss(feed, max_items=5)` called with the default across 26 feeds
means **130 headlines captured once daily at 20:00 UTC**. Items that appeared and rolled past
position 5 were never seen at all. RSS feeds are windows, not archives — a missed poll is
unrecoverable.

---

## 2. Direction

The brief remains the product. It stops being a search-and-print job and becomes a **render over
accumulated knowledge**.

```
  Ingest  ──▶  Integrate  ──▶  [ Knowledge Base ]  ──▶  Render
 (capture)   (comprehend)              │              (brief, chat,
                                       │               alerts, views)
                                  Review pass
                            (resolve, invalidate, score)
```

Two properties govern everything below.

**Capture and comprehension are separate concerns.** Capture must be continuous and cheap
because feeds are windows and a missed item is gone forever. Comprehension can be tiered, lazy
and batched because once captured, an item can be reprocessed indefinitely.

**Comprehension depth is what costs — but cadence is not free either.** An earlier draft claimed
polling every five minutes was effectively costless. That is wrong on two counts, both
establishable from this repo:

- **Rate limits are real and already documented.** 26 feeds at 5-minute intervals is ~7,500
  requests/day on one User-Agent. `brief.py:1819-1822` already records the self-hosted Nitter
  429ing at *current* cadence, with adjacent feeds silently dropping out ("a dropped feed looks
  identical to a quiet one downstream").
- **The volume gain is largely illusory.** Feed composition across the 26: 5 weekly Substacks,
  3 Google News `when:7d` queries returning single digits per week, 9 Google News `site:` proxies
  whose windows re-return the same items on every poll, 2 rate-limited Nitter, ~10 native RSS.
  Real volume concentrates in ~4 wires — and those are disproportionately `state_funded`, which
  is the worst place to add throughput.

**Corrected position: the win is coverage, not throughput.** Raising `max_items` from 5 and
polling a few times daily recovers the items that roll off a feed window between polls — which is
the actual documented defect. Integration volume is unchanged either way (~120 material items/day
on this design's own figures), so five-minute polling buys reordering, not knowledge. The case for
continuous polling is **not made** and should not be built on speculation; set the interval
empirically against observed roll-off, per-feed.

---

## 3. Schema

Objects derived by modelling the three briefs as records, not from theory.

### 3.1 Objects

| Object | Fields |
|---|---|
| **Source** | outlet, kind (wire/analyst/regional/primary), perspective, state_funded |
| **Item** | url, title, body, published_at, source, content_hash |
| **Event** | what, when, type, commitment_state, entities, assertions[] |
| **Assertion** | item → event edge: standing, source_relationship, asserted_at |
| **Observation** | instrument, metric, value, timestamp |
| **Entity** | name, aliases[], type, instrument_mappings[] |
| **Claim** | text, horizon, falsifier, falsifier_kind, resolution_date, evidence[], status, resolved_outcome, horizon_elapsed |
| **Thesis** | text, horizon, supporting[], undermining[], confidence, triggers[] |
| **Story** | name, scope, state, members[], open_questions[], last_material_change |
| **Link** | from, to, mechanism, effect_kind, expected_persistence, decay_check_date, falsifier, status |

### 3.2 Three orthogonal fields on events

Geopolitical reporting mixes things that happened, things people said, and things people claim
happened. One field cannot carry that. Three can.

**`event.type`** — what kind of thing this is.

- `action` — something was done (a strike launched, a rate set, a missile landed)
- `statement` — someone said something; the saying is the event
- `disclosure` — information was revealed or reported

**`event.commitment_state`** — how binding, and therefore how reversible.

- `in_force` — done, hard to undo (rate decision, completed strike)
- `committed` — formally undertaken, reversible at cost (signed treaty, contract)
- `intended` — stated intention, freely reversible
- `proposed` — floated, not adopted

**`assertion.standing`** — how well established, recorded per assertion rather than per event,
because two sources can assert the same event with very different weight.

- `verified` — multiple independent, or physically observable
- `official` — asserted on the record by the acting party
- `reported` — a news organisation asserts it, sourced
- `attributed` — anonymous or single-sourced via a news organisation
- `alleged` — asserted by an interested party

Worked examples:

| Brief text | type | commitment | standing |
|---|---|---|---|
| "Trump agreed to give Ukraine licenses for Patriot missiles" (via Zelensky) | statement | intended | alleged |
| "Trump reversed course ... 'We have to be very careful'" (direct quote) | statement | intended | official |
| "BOJ held at 1% on an 8-1 vote" | action | in_force | verified |
| "IRGC claims to hit 2 tankers in Hormuz" | action | in_force | alleged |
| "Trump declared the ceasefire 'over'" | statement | intended | official |

The last row is the hard case and the reason for the split. The *statement* is a verifiable fact;
whether the ceasefire *is* over is a separate Claim with its own standing. The Day 1 brief
conflated them.

**Guard:** a Claim with a horizon beyond 90 days requires at least one input event at
`in_force` or `committed`. The Patriot claim (`intended` + `alleged`, 6-12 month horizon) is
blocked or downgraded at write time.

### 3.3 Claim evidence and falsifiers

`Claim.evidence[]` records the events and observations it rests on, with a span. A claim resting
on one observation over one day cannot assert confirmation of a multi-quarter thesis. This is a
schema constraint, not a prompt instruction — the system already demonstrates that prompt
instructions about confidence get satisfied syntactically ("reads as X rather than Y" appears
roughly fifteen times across three briefs) rather than substantively.

`falsifier_kind` splits into two, because only one can propagate automatically:

- `event_triggered` — resolvable by a contradicting event on a named entity ("any of the three
  transfers is revoked"). Auto-resolves.
- `review_required` — needs a look ("Kyiv's domestic production fails to start"). Queued.

Without this split, half of all claims would silently never resolve.

`horizon_elapsed` is recorded at resolution. "Broken at 2 days against a 180-day horizon" is a
calibration datapoint; "broken" is not.

**Claim text is immutable once `status != standing`.** The 2026-08-29 replay marked the Patriot
claim broken *and rewrote its text* into a description of the reversal — "Trump reversed course
on Ukraine's Patriot-production license" — destroying the original assertion. Read back, the
ledger then says the reversal was reversed. The original claim is precisely what accountability
is measured against: it is what the reader was told. Rewording is correct for a refinement of a
`standing` claim and wrong for a break; the reversal belongs in `broken_by`.

**Ephemeral measurements are not durable claims.** The replay admitted "Japan's 10-year JGB held
around 2.88%" as a claim, which then "broke" when the yield moved to 2.70% — a price print
superseding a price print, not a contradiction. The current reconcile prompt already instructs
against this ("NOT ephemeral daily price moves") and it happens anyway, so the rule needs
enforcement at merge time rather than restatement in the prompt. Market levels are
`Observation` rows; a claim may *cite* them but must not *be* one.

### 3.4 Link effect kinds

*How long does an event keep explaining a price move?* has no single answer — it depends on the
mechanism.

| `effect_kind` | Decay behaviour | Example |
|---|---|---|
| `re_rating` | Never; the baseline moved | CXMT compressing Western capex multiples |
| `risk_premium` | Decays as uncertainty resolves | Hormuz transit risk in Brent |
| `flow` | Fast, frequently reverses | The three-day MU whipsaw |
| `fundamental_revision` | Persists until data confirms or denies | An earnings-path change |

For `re_rating`, "when does it stop explaining?" is the wrong question — it never stops being
true, it stops producing *new* moves. Confusing a `re_rating` driver with `flow` is precisely
what produced three contradictory chip verdicts in 72 hours.

`expected_persistence` is an ordinal guess (`session` / `days` / `weeks` / `structural`) checked
at `decay_check_date` **relative to sector peers**, so market beta does not swamp the measurement.
Outcomes accumulate into empirical persistence-by-class.

### 3.5 Stories

`scope` is `episodic` (a rate decision, an earnings print) or `structural` (Kyiv arms sovereignty,
Hormuz transit). Structural stories outlive their parent topics — arms sovereignty survives the
war's end, whether Ukraine re-arms to deter or to recover territory. Membership is by tag with
multiple membership permitted; **no hierarchy**, because reality does not reliably fit a tree and
the breaks are narrow enough to be invisible until they corrupt something.

Members carry status. The renderer **reads** the member list; it never rebuilds it. Day 3's
silent retcon happened because the list was re-derived from scratch.

### 3.6 Confidence

Ordinal states plus an explicit evidence list. **No numeric scoring.** Bayesian-looking
arithmetic over ordinal judgments manufactures precision the inputs do not contain, and makes
output look more rigorous than it is.

---

## 4. Propagation rules

The rules are the intelligence. Each closes a documented failure.

1. **Contradiction.** A new Event contradicting an existing Event marks the old `superseded`;
   every Claim deriving from it goes `challenged`; if a Claim's `event_triggered` falsifier
   matches, it goes `broken` and resolves with `horizon_elapsed`. Theses supported by broken
   claims are re-scored. *Closes the Patriot reversal.*
2. **Unexplained observation.** An Observation with no explaining Link is flagged and becomes a
   research trigger — go search for the antecedent. *Closes the vanishing driver, and replaces
   volume alerts (see §8).*
3. **Evidence floor.** A Claim resting on a single Observation cannot assert confirmation.
   *Closes the chip whipsaw.*
4. **Staleness.** A Claim past `resolution_date` with nothing resolving it goes `stale` and
   enters the review queue.
5. **Link decay.** At `decay_check_date`, a Link is re-checked against sector-relative
   displacement; the outcome updates persistence priors for its `effect_kind`.

### 4.1 Interpretation is quarantined, not banned

An earlier draft of this spec mandated that *nothing the brief says may be invented at render
time*. That rule is withdrawn. It does not survive measurement.

Re-aggregating the 45 claim-verification artifacts in `from-server/` (867 adjudicated claims,
mean 19.3/brief) gives: `unsupported` 324, `unverifiable` 256, `supported` 229, `overstated` 54,
`contradicted` 4. The 256 `unverifiable` — **5.69 statements per brief, 29.5%** — are marked so
for reasons like "analytical framing", "forward-looking speculation", "analytical framing about
market transmission".

**Those are not defects. They are the product.** Nobody reads a morning brief to learn that the
BOJ held at 1%. A strictly read-only renderer emits a structured changelog and loses the layer
the reader is actually there for — and none of the §1.1 failures was "too much analysis".

The alternative reading — have a reasoning pass write the analysis into the KB first — is
*strictly worse than today*, and §6.1 says why: synthesis errors last one day, integration errors
are permanent. That rule would relocate every act of interpretation from the one-day tier to the
permanent tier. Today the chip whipsaw is three embarrassing paragraphs. Under that rule it is
three `Claim` rows marked `standing`, cited by a `Thesis`, read back as established state every
subsequent morning, removable only by a human queue action.

**The real defect is that interpretation is unlabelled, not that it is ephemeral.** Today's
ledger stores "Iran and Oman are negotiating a shipping framework" with the same schema and the
same durability as "this represents a genuine escalation ladder". One is a sourced fact; the
other is the model's opinion.

**The rule: the renderer may invent interpretation freely, but nothing invented at render time
may be read back as evidence.** One column, `origin`:

- **`extracted`** — source-grounded. The *only* rows a propagation rule, a Thesis, or a
  calibration aggregate may read.
- **`authored`** — the brief's own interpretation. Persisted (required, or the whipsaw cannot be
  detected), and **scorable** against later events. It may never support a claim, and is never
  rendered back into a prompt as established fact.

This keeps what the original rule got right — nothing silently vanishes, which was the Patriot
mechanism — and drops the part that breaks the product.

Consequence unchanged: `reconcile_ledger` in `brief_memory.py` already extracts durable statements
from a finished brief and already receives today's brief *and* today's source headlines
(`build_reconcile_prompt(ledger, brief_text, source_index)`). It has every input needed to mark a
contradiction and is simply never asked to.

### 4.2 The brief as a query

1. Claims resolved since the last brief — confirmed or broken. **Lead with these.**
2. Events contradicting prior events.
3. New material events, grouped by story.
4. Observations nothing explains.
5. Theses whose confidence moved.
6. Open questions past due.

Against the Day 3 data this opens with *"Tuesday's Patriot claim is broken, 2 days into a 6-12
month horizon; ARMG's supporting case is withdrawn"* — rather than burying the reversal in
bullet two and dropping the position without comment.

---

## 5. Storage

**Decision: DEFERRED. Leading candidate is one PostgreSQL instance with `pgvector`, but the
choice is downstream of a process-architecture decision that has not been made, and nothing is
blocked by not deciding — §12.3 needs no database at all.**

**The decision this actually hangs on.** If the runtime is several containers (capture,
integrate, render, MCP, web — matching today's compose-per-mode pattern), a database *server* is
required and that means Postgres. If it consolidates into one long-running service with internal
scheduling — which continuous ingestion arguably pushes toward anyway — SQLite is sufficient and
substantially lighter. Decide the process architecture first.

**The field, scored on what matters here.** At ~120 writes/day and thousands of rows/month,
performance is irrelevant. The real criteria are ops burden, backup, inspectability, and
migration risk.

| Option | Ops | Concurrency | Time series | Graph | Vector | FTS | Backup |
|---|---|---|---|---|---|---|---|
| **SQLite** + sqlite-vec + FTS5 | Lowest — one file, no service | Single writer; WAL helps, but multi-container writes over a Docker volume are a known hazard | Fine | Recursive CTE | sqlite-vec, less mature | FTS5 native | `cp` |
| **Postgres** + pgvector | New service, but boringly standard | Native MVCC | Fine | Recursive CTE | pgvector, mature | `tsvector` | `pg_dump` |
| **DuckDB** | One file | Poor concurrent write | Excellent | OK | vss ext | OK | `cp` |
| Postgres + DuckDB read replica | Two engines | — | Excellent analytics | — | — | — | Premature |
| Graph-first (Kuzu / Neo4j) | New service, less familiar | Varies | Poor | Native | Varies | Varies | Varies |
| Bitemporal (XTDB) | Exotic; single-operator liability | — | — | — | — | — | — |
| Keep JSON files | Zero new | File locks (already in use) | Poor | No | No | No | `cp` |

Notes worth recording:

- **Postgres would be new infrastructure.** The compose stack currently has no database — state
  is JSON on a volume. SQLite is a far smaller jump from where the system actually is.
- **DuckDB is the best fit for the ad-hoc charting** in §7 ("graph this against that") and the
  worst fit for transactional propagation. If analytics becomes a real workload it belongs as a
  read-side companion, not as the store.
- **Graph-first is not justified.** Traversal depth in this schema is 2-3 hops — a recursive CTE,
  not a graph engine.
- **Bitemporality is real here** ("what did we believe on date X" is genuinely valid-time versus
  transaction-time) but two timestamp columns handle it at this scale. An exotic engine is a
  maintenance liability for a single operator.

The mapping below assumes the Postgres branch.

| Need | Mechanism |
|---|---|
| Lifecycle, transactions | Native relational. The propagation rules require transactional integrity. |
| Temporal queries | `TIMESTAMPTZ` columns, `valid_from` / `valid_until`. Proper date types, not TEXT. |
| Time series (Observations) | Plain table with a composite index. Thousands of rows/day; TimescaleDB is unjustified. |
| Graph traversal (Links, Stories) | Recursive CTEs. Traversal depth here is 2-3 hops. |
| Semantic retrieval | `pgvector` in the same database, joinable to the entities embeddings belong to. |
| Full-text | Native `tsvector`. |
| Inspection | SQL, plus the web UI (§7). |

**Rejecting my own earlier suggestion.** A hybrid — "relational spine plus graph DB plus vector
store, each doing what it does best" — sounds right and is wrong at this scale. Multiple stores
mean multiple failure modes, sync problems, and no transactional integrity across the propagation
rules, which are the entire point. One engine, one backup, one connection string.

**Chroma stays — an earlier draft was wrong to retire it.** `CHROMA_MCP_URL` (`brief.py:122`)
points at `https://progdroid--podcast-mcp-server-mcp-server.modal.run/mcp`: a **separately
deployed Modal app** with its own podcast ingestion pipeline, reached over MCP. It is not a
service in this compose stack, so there is nothing to remove, and "migrating the corpus" would
mean rebuilding a working ingestion pipeline to gain a SQL join whose value was overstated —
finding an antecedent means searching semantically and then mapping to entities, and that mapping
is application code, not a join. Chroma is augmentation, not substrate. New embeddings go
wherever the KB lands; the podcast corpus stays where it is, behind its existing interface.

**Counter-argument, stated honestly.** Postgres is weaker at deep graph traversal. If the
reasoning layer later wants many-hop pattern discovery, recursive CTEs get painful. Mitigation:
measure before assuming. A materialised edge table plus 3-hop CTEs covers every query specified
here, and if real traversal is later needed, a graph engine can be added as a **derived index
rebuilt from Postgres** — an addition, not a migration.

---

## 6. Models and providers

### 6.1 Tiers

| Tier | Work | Default | Error lifetime |
|---|---|---|---|
| Triage | new? duplicate? touches anything tracked? | Haiku 4.5 (rules first) | Recoverable — raw item retained |
| Integration | entities, events, claims, story linking | Sonnet 5 | **Permanent — poisons the KB** |
| Synthesis | brief, thesis review | Opus 5 | One day |
| Reasoning | contradiction, staleness, gap, decay passes | Sonnet 5 | Flagged and reviewable |

Model substitution has poor leverage here. The tier where a cheap model is safest — triage — is
already the cheapest. The tier that dominates the bill is the one where errors persist. **Work
the free levers first**: micro-batching, caching, batch endpoints, salience tuning.

### 6.2 Portability

Provider adapters **declare capabilities**; the pipeline degrades rather than breaks. No batch
endpoint → run inline. No prompt caching → pay full input. **Cost varies by provider;
correctness does not.** Micro-batching is exempt — it is a prompt-shape choice and works
everywhere, and it is the largest lever regardless.

OpenRouter is the portability layer: model choice becomes configuration, new models can be tried
as they ship, and per-path model selection is possible.

Local Ollama (24GB VRAM, constrained context) is viable **only** for title-and-summary-scale
work — triage, dedup, entity tagging, alias resolution. Full-article integration does not fit.
Availability is not guaranteed, so any Ollama path needs a declared fallback.

### 6.3 Perspective panel

A fixed panel of questions on China/Russia/Iran/Taiwan topics, asked across models, with framing
divergence recorded as a first-class Observation. This measures **alignment-shaped framing**, not
analytical quality. A non-Claude model here is an instrument, never a peer analyst whose opinion
gets blended into the output.

### 6.4 Cost

At roughly 1,200 captured items/day with ~10% integrated:

| Tier | Approx. |
|---|---|
| Capture | ~$0 |
| Triage (micro-batched) | ~$6/mo |
| Integration (batched + cached) | ~$13/mo |
| Synthesis | ~$3/mo |
| Reasoning passes | ~$5-10/mo |

**~$25-35/mo.** Synthesis gets *cheaper* than today: rendering from curated KB state ships far
fewer tokens than 130 raw headlines plus a live web-search loop. Chat conducted over MCP runs on
the existing Claude subscription rather than the metered API bill.

---

## 7. Surfaces

All surfaces are thin clients over one KB query API. No surface reimplements query logic.

**Telegram** — brief delivery, quick commands, and **conversational drill-down**. The long-poll
daemon exists. Chat needs a per-conversation token guard. This is also where edit-as-signal data
is naturally captured.

**MCP server** — exposes the KB to Claude Code and the desktop app. Drill-down, discussion and
thesis formation with no chat UI to build, and the same interface serves development.

**Web UI** — three jobs, mostly not AI:

1. *Inspection.* Story timelines, claim and thesis boards, entity pages, the link graph, and
   ad-hoc charting ("graph this against that") — which the `Observation` table supports directly.
   An accumulated world model that cannot be inspected cannot be trusted.
2. *Configuration.* Settings in the database, read at runtime. This eliminates the documented
   `env-var-needs-compose-passthrough` class of bug, where a new knob is invisible inside the
   container until the compose anchor declares it and a fail-closed flag then silently no-ops.
3. *Trading monitoring.* PolyGram positions, exposure, caps, and thesis-versus-market results.

**Review queue.** Staleness and `review_required` falsifiers produce a short list — "5 claims are
past due, confirm or kill." Designed as a 90-second queue that gets cleared, not a corpus to
browse. The format does not solve review; **queuing** does.

---

## 8. Trading — DEFERRED, not in v1

**Status: cut from the first build.** The design below is the intended destination and the
reasoning is preserved, but it must not be built yet, for two reasons:

1. It is a scorecard for a thesis engine that does not exist. Theses have to accumulate before
   anything can grade them.
2. It would be rebuilt on the performance layer that the 2026-08-16 retrospective found carried
   **three data bugs**. Rebuild it now and a poor result cannot distinguish bad theses from
   bug #4 — the measurement instrument would be less trustworthy than the thing it measures.

Revisit once theses exist and have started resolving.

**PolyGram: unchanged.** Live, working, out of scope here.

**Equity paper book: re-founded on theses, not dropped.** What is dead is the reactive daily book
— open on a headline signal, score daily, reverse on contrary news (the reversal rule cost
-6.02% against -0.28% for holding, p=0.0072, and is already removed). What is worth keeping is
the *instrument*.

A paper position exists to test a **thesis**: it opens when conviction crosses a threshold,
carries the claim that triggered it as entry rationale, holds to the thesis horizon, and resolves
with the thesis. No daily reversal. No DCA.

This changes what is measured — from "was today's signal directionally right" (settled: 50/50,
p=1.000) to "did the thesis pay over its horizon" (untested). It also gives the thesis engine an
**external** scorecard; without it, theses resolve on the model's own judgment, which is marking
its own homework. The performance layer requires rebuilding regardless after the three data bugs
found in the 2026-08-16 retrospective, so it is rebuilt around this.

**Volume alerts: repurposed.** A volume anomaly is not news, it is an *unexplained event* — the
same shape as an unexplained price move. It becomes propagation rule 2: query the KB for an
explaining story; if found, attach as corroboration and stay silent; if not, raise a research
trigger. Same detector, same data, no standalone notifications.

---

## 9. Learning

Three loops, all grounded in measurement rather than self-report:

1. **Claim calibration.** `horizon_elapsed` at resolution, aggregated over a **deliberately
   coarse** partition.

   An earlier draft aggregated by `commitment_state` × `standing` — 4 × 5 = 20 cells. At the
   measured rate of **5.09 source-grounded claims per brief**, with horizons of 90-180 days
   before anything resolves, n≈5 per cell arrives in roughly **two years**. The loop could not
   produce signal, and §9's own "delete any loop that resolves at chance" could never fire —
   a self-defeating design.

   **Corrected: collapse to two cells** — inputs at `in_force`/`committed` versus
   `intended`/`proposed`. That is the distinction the Patriot case actually turns on, it reaches
   n≈25/cell in months rather than years, and finer partitions can be introduced later *if* the
   coarse one shows separation. Any calibration aggregate must state up front how long it needs
   to become readable; one that cannot answer within a year is not a learning loop.
2. **Link persistence.** Decay checks by `effect_kind` produce empirical persistence windows.
3. **Demand signal.** A question asked is a measurement that the brief under-explained something.
   A drill-down is a salience upvote. A rejected thesis is a *method* correction, not just a
   content one — the pattern ported from `dynamic_day_planner`, where a user edit to an LLM
   output injects a correction into the next similar run.

This is deliberately narrower and more testable than generic self-improvement: every loop
produces a number that can be checked, and any loop whose predictions resolve at chance is
deleted.

---

## 10. Rejected and deferred

| | Decision | Reason |
|---|---|---|
| **gbrain** as substrate | Rejected | No numeric series storage anywhere in ~66 tables; `raw_data` is `UNIQUE(page_id, source)` — a snapshot, not an append log. Schema packs emit zero DDL, so the gap cannot be filled. Invalidation is inert: `synthesis_evidence` cascades only on hard delete, which `takes_resolve`/`takes_supersede` never do. No falsifier column; `since_date`/`until_date` are TEXT. Markdown-in-git as source of truth imposes a maintenance model the reader has said will not hold. |
| gbrain's `takes` design | **Adopted** | Resolution vocabulary `correct / incorrect / partial / unresolvable` and `superseded_by` retaining rows for archaeology are better than anything drafted here. |
| **Hermes** runtime | Deferred | Not a knowledge base; memory is a pluggable provider ABC. Cron adoption is low-coupling but buys retries/incidents/chaining that are ~50 lines on top of the existing `telegram_alert`, at the price of s6-overlay and 135 env vars. Gateway adoption is invasive — it becomes the sole bot-token consumer, forcing the existing command surface to migrate (two long-poll consumers is the documented 409). |
| Numeric confidence scoring | Rejected | Manufactures precision from ordinal inputs. |
| Story hierarchy | Rejected | Reality does not fit a tree; breaks are narrow and invisible until they corrupt. |
| Markdown as substrate | Rejected | Human review is solved by queuing, not by format. |
| Static rendered pages | Rejected | Reader preference; Telegram plus web UI covers it. |
| Moving submit earlier | Rejected | The defect is cadence and coverage, not a few hours of staleness. |

---

## 11. What survives from the current codebase

- `common.py` — Telegram transport, atomic writes, HTML sanitisation, file locking
- `trading.py` — market data fetch, portfolio weights, PolyGram live path, volume detection
  (re-pointed per §8)
- `brief_memory.py::reconcile_ledger` — becomes the claim-extraction pass (§4.1)
- Source definitions, perspective tagging, `state_funded` flags — the tagging already produces
  the best analysis in the sample and carries into `Source`
- `enrichment/` — Bigdata integration, descriptive; a candidate retrieval path for finding
  antecedents to unexplained observations
- Test suite and CI

Retired: the reactive paper book and its signal-scoring, the daily search-and-print prompt
assembly, the flat 25-claim ledger, standalone volume alerts.

Explicitly **not** retired: Chroma (§5) — a separately deployed Modal app providing augmentation,
not substrate.

---

## 12. Gating experiments and hard preconditions

### 12.1 Gate: RUN 2026-08-29 — rule 1 has a base rate, and the mechanism is not what we assumed

An earlier draft of this section generalised from **three** briefs while 90 were archived, and
treated "these failures require state rather than prompt repair" as established when it was
asserted. The experiment has now been run against all 90.

**Method.** All 90 archived briefs replayed in date order through a status-aware reconcile
(Haiku 4.5), rebuilding the ledger forward from empty — no historical ledger snapshots are
retained, so the trajectory has to be reconstructed. Two ledgers ran in parallel: the model saw
an unbounded-ish one (cap 60, no TTL) so that a null could never be an artefact of eviction,
while production retention (TTL 7/14d, cap 25, evict by severity-then-recency) was simulated
alongside purely to ask whether each resolved claim would still have been present.

**Positive control (mandatory before believing any result):** the Patriot claim created
2026-07-30 was echoed by id on 07-31 as `standing`, and on 08-01 returned with the same id,
`status: broken`, and a `broken_by` quoting Trump's reversal. Same row, real lifecycle
transition, 2 days. Control passes.

**Result.**

| Measure | Value |
|---|---|
| Resolutions across 90 briefs | **70** — 24 `broken`, 46 `challenged` |
| Rate | 0.78 resolutions/brief; 0.27 breaks/brief |
| Days to resolution (broken) | median 5, mean 8.2, max 35 |
| Hand-audited precision on the 24 breaks | ~~**~61%**~~ **SUPERSEDED: 33.3%** (7 of 21) — see `docs/2026-08-29-gold-set-first-run.md`. The 61% was read off `audit.py`, which prints `claim[:105]` of a field `replay.py` had already truncated to 150 chars. |
| Topics | Iran–US 19, Energy 11, **Semiconductors 8**, Iran nuclear 5, Japan 6, Ukraine 4 |

Genuine catches include the exact failure classes that motivated this spec: a brief asserting
"Romania invoked Article 4" corrected two days later by "Romania did not ultimately initiate
Article 4 consultations"; one Trump reversal on the Hormuz toll killing two separate standing
claims; and — directly — "SK Hynix +13% **confirms** memory-AI bull case" broken the next day by
"MU -7.4% and SKHY -3.0% giving back the bounce". That last one is the §1.1 chip whipsaw,
detected.

**Verdict: build rule 1.** ~15 genuine resolutions per quarter, concentrated in exactly the
cases the reader raised.

**The mechanism is the CAP, not the TTL — both this spec and its red-team review had this
wrong.** 56 of 70 resolutions arrive *inside* the claim's own TTL window, and 54 of the 68 that
production would have lost were lost **despite** being inside it. Claims are not ageing out on
silence; they are being **crowded out**. Corroborating evidence: chip/semiconductor names appear
in **58 of 90 briefs (64%)** while the live production ledger contains **zero** chip claims — it
holds 25 claims, Ukraine ×11 and Iran ×8. The replay, with room, produced 8 semiconductor
resolutions.

This reorders the fix priority in §12.3: **decoupling storage from the 25-item working set is
load-bearing; the TTL exemption is secondary.**

**Held loosely.** The "97% would have been evicted" figure is inflated — the model saw 60 slots
where production sees 25 and duplicated freely (816 new rows created across the run), so the
simulated cap was partly filled with rows production would not have made. Direction is
independently corroborated by the live ledger; treat the magnitude as "most", not "97%".

**Two defects the replay exposed, both fixable in the prompt:**

1. **Restatement misread as contradiction** — the whole false-positive class. "BOJ raised policy
   rate to 1.0%" was broken by "BOJ rate decision executed at 1.0%, not just expected"; "Over 80%
   of Hormuz traffic routes via Oman" broken by "more than 80% of liquids transits now run the
   Omani route". The template already carries the mirror guard ("absence is not contradiction")
   and that one worked, so the fix is known-shaped: *a restatement, escalation, or confirmation
   is not a break.*
2. **Ephemeral price claims admitted as durable** — "Japan's 10-year JGB held around 2.88%" is
   stored, then "breaks" when the price moves. The template already forbids this and it happens
   anyway; the admission rule needs enforcement, not restatement.

Note the claim-verification pilot never answered this question and could not have: it tested
within-day claim-versus-source, while rule 1 is across-day claim-versus-later-event. Its
artifacts carry only `{claim, verdict, reason}` — no claim IDs, no cross-day linkage, no
resolution field.

### 12.2 Precondition: provenance columns, before any DDL

§6.2 makes the model a configuration value; §6.1 states integration errors are permanent. As
drafted, **no row records which extractor wrote it.** This repo has already seen a silent
extractor shift: the Sonnet 4.6 → 5 swap changed thinking behaviour and inflated tokens ~30%,
truncating signals at `max_tokens=2048` — output shape changed, no error raised.

Compounding it, `standing: reported` versus `attributed` has no objective referent. A 15%
boundary drift in how a model draws that line yields a §9.1 prior that measures the labeller
rather than the world — and does so undetectably, since the only feature differentiating this
design becomes unfalsifiable.

Required, and cheap only if done before the first migration:

- `extractor_model` and `prompt_version` on every extracted row.
- A **frozen 50-item gold set**, hand-labelled, gating any extractor or prompt change.
- A documented decay policy for claims that are never reviewed.

**Seed the gold set from the replay.** The 2026-08-29 run produced 24 `broken` detections against
a known corpus. (The "~61% precision" here is superseded: relabelling the untruncated text gives **33.3%**, and the false-positive class is broader than one mode. See `docs/2026-08-29-gold-set-first-run.md`.) Those 24,
labelled, are the first gold-set entries and a ready-made regression test for fix #4 in §12.3:
the restatement guard must convert the known false positives without losing the known true ones.

**Field completeness is about design, not count — three measured data points now say so.**

| Field | Outcome | Why |
|---|---|---|
| `reason` (claim-verify pilot) | **46.8% missing** (406/867) | Absent from `required` |
| `severity` (live ledger) | **degenerate — `high` 25/25** | Rubric states "use `normal` by default" but its *examples* all skew major |
| `status` (replay) | **100% present, usable** | Explicit per-value rules, a stated default, and an explicit negative case ("absence is not contradiction") |

So the risk is not that §3.2 adds three fields per event, four per claim and three per link. It is
that a field without a stated default, worked examples spanning its range, and an explicit
negative case will come back either absent or uniform — and **a uniform field is worse than a
missing one, because it looks populated.** Every field in §3.2 must be specified to the `status`
standard before it earns a column, and the gold set must check each for **variance**, not just
presence.

Note also the pilot pre-registered `overstated` as "confound-free" and was wrong (~7 of 22 flags
were web-search artifacts). §3.2's "three narrow questions beat one fuzzy one" is the same class
of untested bet, and now has a way to be tested cheaply.

### 12.3 The minimal repair — build this first regardless

Roughly 370 lines against the existing system, **every line of which is reusable under the full
design**. It is not an alternative to the KB; it is the KB's first increment and its measurement
baseline.

Ordered by measured impact (§12.1), not by size. The first three are the ones the replay shows
are load-bearing.

| # | Fix | Where | ~Lines |
|---|---|---|---|
| 1 | **Split the store from the working set.** The 25-cap is a *prompt-budget* limit — `brief_memory.py:24` documents it as ~2,400 output tokens because reconcile echoes the whole ledger back — **not** a storage limit. Store without bound; send a selected window. | `brief_memory.py` | ~30 |
| 2 | **Claim deduplication / id reuse.** The replay created **816 new rows in 90 days**, with three near-identical Patriot claims on one day. Duplicates burn cap slots, so this *is* part of the crowding-out mechanism, and any rule keyed on id misses fragmented claims. | `merge_ledger` + prompt | ~40 |
| 3 | `status ∈ {standing, challenged, broken}` + `broke_on` + `broken_by`; ask reconcile to mark contradictions | `merge_ledger` | ~40 |
| 4 | **Restatement guard** in the reconcile prompt: *a restatement, escalation, or confirmation is not a break* — the entire measured false-positive class (§12.1) | prompt | string |
| 5 | **Claim-admission guard**: reject ephemeral price levels as durable claims. Currently instructed and ignored, so it needs enforcement, not restatement. | `merge_ledger` + prompt | ~15 |
| 6 | **Claim text is immutable once `status != standing`** — the replay rewrote a broken claim into a description of its own reversal, destroying the original assertion (see §3.3) | `merge_ledger` | ~5 |
| 7 | `"".join` for content blocks | `brief.py:2948` | 1 |
| 8 | `max_items` 5 → 25 (single call site) | `brief.py:3026` | 1 |
| 9 | Rewrite the `ESTABLISHED` block header and instruction | `brief_memory.py:200` | string |
| 10 | `driver` field on ledger claims + permission to restate it | `brief_memory.py` | small |
| 11 | Exempt non-`standing` claims from TTL and cap eviction | `merge_ledger` | ~10 |
| 12 | `horizon_days` / `resolution_date` on claims | `brief_memory.py` | ~15 |
| 13 | `origin: extracted \| authored` (§4.1) | `brief_memory.py` | ~10 |
| 14 | `extractor_model` / `prompt_version` (§12.2) | `brief_memory.py` | ~10 |
| 15 | Half-hourly `capture` mode → dedup'd JSONL keyed on content hash | new module + compose service | ~120 |

**Why the reorder.** An earlier draft called the TTL exemption (#11) load-bearing, on the theory
that "the TTL evicts on silence — the same condition under which a claim is quietly falsified."
The replay refutes that: **56 of 70 resolutions arrive inside the claim's own TTL window**, and
54 of the 68 production would have lost were lost anyway. The TTL is not what kills them. The cap
is, and duplication is filling it. #11 remains correct and cheap, but it is not the fix.

### 12.4 Still open

1. **Poll interval and salience threshold.** Set empirically per-feed against observed roll-off
   and rate-limit headroom (§2), not by assumption.
2. **Build sequencing beyond §12.3.** Belongs in an implementation plan, and this spec is roughly
   four sub-projects — KB core, render path, surfaces, thesis engine — each wanting its own plan.
3. **Web UI scope for v1.** Configuration alone is the smallest useful slice and removes a
   documented footgun; inspection is larger and more valuable.
4. **Review-queue throughput.** §3.3 implies roughly half of claims land as `review_required`
   — about 2.5/day for a single operator. A KB whose claims all go `stale` is today's ledger with
   a database attached.

---

## 13. Success criteria

1. A claim contradicted by later events is surfaced as broken in the next brief, with elapsed
   horizon — the Patriot case, mechanically.
2. An unexplained market move produces a research trigger rather than a shrug — the NVDA/RAM case.
3. No confirmation claimed from a single observation — the chip whipsaw.
4. Zero occurrences of `(established)` or equivalent withheld-explanation markers.
5. Mid-sentence prose breaks at zero.
6. A question asked in Telegram or over MCP is answerable from the KB without re-fetching.

Two of these now have measured baselines from the 2026-08-29 replay, so they are testable rather
than aspirational:

- **Criterion 1 — break-detection precision ≥ 85%**, measured against the hand-labelled
  detections now frozen as `tests/fixtures/gold_set_breaks.json`. **Baseline: 33.3%**, not the
  ~61% first recorded. The restatement guard (§12.3 #4) is the intervention; the labelled set is
  the regression test. A run that improves recall while dropping precision below baseline is a
  regression, not progress.

  **Amended 2026-08-29 after the first run** (`docs/2026-08-29-gold-set-first-run.md`): the
  guard reached 75.0% precision but dropped recall from 100% to 42.9%, losing 4 of 7 true
  breaks. The pre-registered condition only covers one direction, and it is the *safer* one —
  §6.1 makes a lost break a permanent integration error while a false break is a synthesis
  error that lasts a day. **Both directions are regressions.** n is 21 classifiable, so read the
  per-item table, not the aggregate.
- **Criterion 3 — zero confirmations claimed from a single observation.** The replay contains a
  live instance to test against: "SK Hynix +13% confirms memory-AI bull case", asserted on
  2026-07-15 and broken on 2026-07-16. Under the evidence floor (§3.3) that claim must never be
  writable in the first place.

A third is worth stating explicitly because the corpus shows it is currently failing silently:

7. **No structured field is degenerate.** Every enum field must show variance across a month of
   output. `severity` currently returns `high` on 25 of 25 live claims (§12.2) — a field that is
   populated, passes every completeness check, and carries no information.

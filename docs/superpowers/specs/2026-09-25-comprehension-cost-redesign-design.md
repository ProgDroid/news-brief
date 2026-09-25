# Comprehension cost redesign

**Date:** 2026-09-25
**Status:** DESIGN APPROVED in conversation 2026-09-25; this written spec awaits operator review.
**Parent spec:** `2026-09-04-comprehension-pipeline-design.md` (§8.2 gate, Amendments 1–2)
**Beads filed so far:** `news-brief-0rg` (billing errors charged as verdicts, P1, bleeding),
`news-brief-b5s` (Reuters native sitemap, P4, future), `news-brief-3cc` (Google News re-mints, P3)
**Red-team review:** `docs/superpowers/reviews/2026-09-25-comprehension-cost-redesign-redteam.md`.
Objections (b) and (c) were adopted: §4.1, §4.3, §4.4, §6.1, §8. Objection (a) was declined
by the operator (D8).

## 1. Why this exists

After `COMPREHEND_ENABLED` went back on at 2026-09-22 18:13:37Z for the Amendment 2 gate
window, the operator was topping up the Anthropic account by ~£15 a day, and that credit was
gone within a day. The 2026-09-10 handover had forecast *under* $3.25/day. On 2026-09-25 the
balance hit zero and comprehension stopped. The operator then set the flag to false.

The goal: **comprehension serves two purposes, equally.** It is groundwork for a KB-rendered
brief (the 2026-08-29 direction), so gaps in its history are a real loss. It is also an
experiment (the §8.2 gate), so it should cost as little as it can until the gate says the event
layer earns its keep. **Nothing in production reads the KB today.** `brief.py` touches
`comprehend` only to run it (`mode_comprehend`) and for its retirement alert.

## 2. What was measured (2026-09-25)

Every figure was measured on the host or from the Anthropic console by the operator, or fetched
live from this machine. None is inferred.

### 2.1 The bill tracks items integrated, at about $0.0027 each

| day | Sonnet 5 tokens | items integrated | tokens / item |
|---|---:|---:|---:|
| 22 Sep (from 18:13) | 843,900 | 878 | ~960 |
| 23 Sep | 771,250 | 890 | ~870 |
| 24 Sep | 5,303,413 | 6,505 | ~815 |
| 25 Sep (partial) | 2,435,575 | 3,254 | ~750 |

At $2/$10 per MTok (Sonnet 5; checked against the current model table), with the 83/17
input/output split measured over 7–10 Sep, that is **~$0.0027 per integrated item**, and it
is steady from day to day. 24 Sep cost ≈ $17.80 (≈ £13). This was not a retry storm. The
pipeline did what it was built to do, over far more items than anyone expected.

### 2.2 Where the items came from

- **Backlog.** On 25 Sep there were **24,045 untriaged items** and 643 awaiting integration.
  Both selects are oldest-first with no age limit (`comprehend.py:389-394`, `:610-626`).
- **Arrival rate tripled on 2026-09-16, and no deploy caused it.** Items captured per day went
  from ~1,100 (5–15 Sep) to ~3,500 (16–22 Sep).
- **One outlet explains all of the increase.** Reuters went from 2,897 items (9–15 Sep) to
  18,109 (16–22 Sep), +15,212, against +14,504 for all outlets combined. Every other outlet
  was flat or down. Since 16 Sep, Reuters is **~78% of the corpus**.
- **The volume is not duplication.** Rows minus distinct `(title, published_at)` is 2–4% of
  Reuters rows per day. Distinct articles rose ~9×.
- **The cause is quote pages flooding the Markets proxy.** Live fetches on 25 Sep:

  | feed (`when:6h`) | 2026-09-08 (memory) | per fetch now | union of 3 fetches |
  |---|---:|---:|---:|
  | `site:reuters.com/markets` | 17–24 | **100 (cap)** | **191** |
  | `site:reuters.com/world` | 40 | 37 | 37 |
  | `site:kyivindependent.com` (control) | — | 23 | 23 |

  The Markets items are Reuters instrument pages: `MSTS.DE - Reuters`,
  `SOGN.HA - | Stock Price & Latest News - Reuters`. Google began indexing them under
  `/markets` around 15 Sep. The proxy returns a rotating capped sample of a very large pool,
  so 48 polls a day collect ~2,000 quote pages a day.

- **Query variants tested live:**

  | variant | per fetch | quote pages |
  |---|---:|---|
  | `-inurl:companies` | 100 | unchanged |
  | `-companies` | 29 | 28 of 29 |
  | `/markets/us`, `/europe`, `/asia` | 0 | — |
  | **`site:reuters.com/business`** | **34** | **0** |

  **Reuters' own news sitemap** (`/arc/outboundfeeds/news-sitemap/?outputType=xml`) returned
  HTTP 200: 50 real article URLs covering ~36 minutes, across all sections and languages.
  Recorded as option B (`news-brief-b5s`).

### 2.3 How fast corroboration arrives (sets the batching design)

Over the 861 multi-outlet events in the KB, the gap between the first outlet's report and the
second outlet's:

| within | events | share |
|---|---:|---:|
| 1 h | 177 | 21% |
| 2 h | 289 | 34% |
| 6 h | 530 | 62% |
| median | 4.0 h | |

**This is a floor.** Pairs whose items fell into the same micro-batch of 5 could never be
matched, so they are absent from the count. **Batching that blinds a submission to its own
contents would lose at least a third of corroboration**, on a metric already below its floor.

### 2.4 A defect found while measuring: an empty account permanently retires items

An exhausted balance returns a 4xx (`billing_error`, 402). `_is_transient`
(`comprehend.py:202-224`) spares only 429 and 5xx, so the failure is charged to the ITEM:

- triage bumps `attempts` and gives an item up after 3;
- integration burns `integrate_defers` (10) and then `integrate_attempts` (3, a one-way door).

This happened from the moment the account ran dry on 25 Sep until the flag was flipped. Filed
as `news-brief-0rg`.

## 3. Decisions

| # | decision | alternatives rejected, and why |
|---|---|---|
| D1 | **Approach 1: batched integration with clustered requests**, delivered in two phases | Haiku for integration: unknown extraction quality, and the gate was registered on Sonnet (kept as a later, stackable experiment). Selectivity only: leaves the price unchanged. |
| D2 | **Reuters option A:** swap Markets for `site:reuters.com/business`, plus a quote-page guard | Option B (native sitemap): more complete, but ~2× Reuters' cost share, with nothing reading the KB to justify it. Recorded as `b5s`. |
| D3 | **A token-bucket budget:** a daily USD allowance that accumulates, capped at N days | A fixed daily cap: a busy day cannot borrow from quiet ones. No cap: an upstream surprise becomes a daily top-up again. |
| D4 | **Newest-first, with a 14-day staleness horizon.** Under a sustained over-budget stretch, OLD items give way | Oldest-first: nothing is skipped, but the KB lags the news without bound, and a lagging KB cannot corroborate current events. |
| D5 | **Triage: rules decide *material* only for explicitly tracked topics; an entity mention goes to a Haiku model** | Keeping "any known entity = material": its precision FALLS as the KB grows, which is how the material rate reached ~86%. |
| D6 | **Integration batched, triage real-time** | Batching triage saves ~$0.07/day and adds a second hop of latency. |
| D7 | **Restart comprehension after phase 1, and only after the old gate has run** (not before 2026-09-30 00:13:37Z), before phase 2 ships | Waiting for phase 2: the KB gap grows, and the horizon silently drops what arrives meanwhile. Phase 1 is bucket-bounded. Restarting before the old gate runs would hand it an admissible-looking mixed cohort (§6.1). |
| D8 | **Phase 2 is COMMITTED, not conditional on phase 1's measured spend** (operator decision, 2026-09-25, after the red-team) | Red-team objection (a): with triage selective (parent spec planned ~10% material), phase 1 alone may cost ~$0.50/day, and phase 2 halves that. A pre-registered condition (build phase 2 only if phase-1 spend > $1.00/day) was offered and declined. Recorded so the choice reads as deliberate. |

**Deferred, and not part of this spec:** candidate-cap reduction (corroboration rests on
`recall@30`; only with measurement), Google News re-mint dedupe (`3cc`; changing identity
mid-corpus triggers a one-off re-insert), prompt caching (777-token prefix < Sonnet 5's
1,024 minimum, unchanged), body truncation (bodies average 154 chars).

## 4. Phase 1 — make a restart safe and bounded

Still real-time Sonnet. Expected ~1,040 items/day (~740 non-Reuters + ~300 Reuters) ⇒
**~$1.40–2.40/day (≈ £1.00–1.80)** if 50–86% of items are material, capped by the bucket.
**The material rate is the biggest unknown in this spec.** The 86% was set by the rules half,
which D5 narrows. The model half, whose prompt is broad by design (`comprehend.py:809-818`),
has never decided at volume, and the parent spec planned for ~10%. At 20% material, phase 1
would cost ~$0.56/day. The phase-1 week measures it (§7).

### 4.1 Capture: the Reuters fix

- **Swap the Markets proxy.** In `brief.py` (the feed at `:149-167`), replace
  `site:reuters.com/markets` with `site:reuters.com/business` in BOTH `url` (the brief's
  reader, `when:2d`) and `capture_url` (`when:6h`). The daily brief has been reading quote
  pages as headlines through `url` too.
- **Rename the source "Reuters Markets" → "Reuters Business".** A source should be named for
  what it fetches. The plan must grep every reference to the name: `feed_sightings.source_name`,
  capture liveness, any host state keyed by name. Old `feed_sightings` rows are left alone;
  they record what the old source saw.
- **Add one shared predicate, `common.is_quote_page(title) -> bool`.** It matches a bare-symbol
  title (`^\S+ - Reuters$`) and any title containing `Stock Price & Latest News`. It lives in
  `common.py` so that no new top-level module has to be added to the Dockerfile allowlist.
  Two callers:
  - `capture.run` drops matching entries before insert and counts them in its `Tally`.
  - `brief.fetch_rss` (`brief.py:1768`) drops them before the brief sees them.
- **Tests use real titles from the 25 Sep probe**, including the `SOGN.HA - | Stock Price…`
  shape the throwaway probe's regex missed, plus real news titles that must NOT match
  (e.g. `Mapping the Market: Why 3M shares might be set to rally again - Reuters`).
- **Add a per-outlet surge alert to capture.** Capture alerts only on ABSENCE
  (`capture.liveness`, `capture.py:525`); nothing alerts on a spike. The only detector that
  has ever caught a surge here is the Anthropic balance running out, and the budget in §4.3
  would remove even that. The rule: an outlet's items in the last 24 h exceed 3× its trailing
  7-day daily median, **and** that is at least 200 items above it, so a quiet outlet's small
  wobble cannot trip it. The alert names the outlet and both numbers, and fires once per
  episode (the `capture.liveness` contract). It would have fired on Reuters on 2026-09-16:
  ~2,600 against a median of ~300. Test: that exact shape fires, and a 3× rise from 5 to 15
  items does not.

### 4.2 Comprehension: `0rg`, stop charging items for account failures

- **A 401, 402 or 403 from either call aborts the pass immediately.** No attempt, defer or
  triage verdict is recorded. The pass logs the failure and returns a `Tally` naming it
  (`aborted: billing` or `auth`).
- **The check keys on status code** (the error `type` string may be used as confirmation).
  Every later call in the pass would fail identically, so continuing only burns the budgets.

### 4.3 Comprehension: the budget bucket

- **Knobs** (settings rows, each a `KNOBS` entry plus a compose anchor line):
  - `COMPREHEND_DAILY_BUDGET_USD`, default `1.50`;
  - `COMPREHEND_BUDGET_MAX_DAYS`, default `3`, the cap on accumulated balance. It was 7 in the
    first draft. The red-team pointed out that a 7-day cap lets a single surge day spend
    ~$12, which is the failure this whole redesign exists to prevent.
- **Accrual.** The balance accrues continuously at `allowance / 86400` per second **while
  enabled**, clamped to `allowance × max_days`. Balance and last-update time are stored in
  `runtime_state`. Re-enabling after a pause starts accrual from the flip, not from the pause.
- **Spend.** Each response's `usage` × a **per-model price table** in `comprehend.py`:
  Sonnet 5 $2/$10, Haiku 4.5 $1/$5 per MTok; batch factor 0.5 in phase 2. The table records
  the date and source of its prices. **A model missing from the table refuses to run.** The
  pass aborts with `aborted: unpriced_model`, because a silent $0 would make the budget an
  accepted-and-inert config. **The refusal also sends a Telegram alert** naming the model. A
  `NEWSBRIEF_MODEL` row change needs no redeploy, and `_integrate_model` follows it, so
  without an alert one settings edit would stop comprehension with nothing but a log line to
  show for it.
- **A spend ledger.** New table `comprehend_spend (id, at, stage, model, input_tokens,
  output_tokens, usd, batch_id NULL)`, one row per call (per request result in phase 2).
  **"What did comprehension cost yesterday" becomes a query.** The 2026-09-10 handover took a
  billing console and three days to answer it because the number was never recorded.
- **Gating.** Before each call the pass checks balance > 0 and stops if not. The worst-case
  overdraft is one call (≤ ~$0.09 at `INTEGRATE_MAX_TOKENS` 8192).
- **Alerting.** A Telegram message goes out once per exhaustion episode, stating the balance,
  the allowance and the untriaged/awaiting counts. It copies the `capture.liveness` contract:
  the key names the situation, and it is sent before the key is stored. **"Recovered" needs a
  definition**, because the balance accrues continuously and would otherwise count as
  recovered the moment it goes above zero. The key clears only when the balance is back to
  **≥ one day's allowance**. That hysteresis is what separates "fires once per episode" from
  "fires every hour".

### 4.4 Comprehension: newest-first with a staleness horizon

- **Both `pending_triage` and the integration select use `ORDER BY i.id DESC`.** Rewrite the
  `pending_triage` docstring: its "oldest first so nothing starves" argument assumed a small
  backlog, and D4 reverses that choice on purpose.
- **Horizon = `CANDIDATE_WINDOW_DAYS` (14)**, measured on `coalesce(published_at, created_at)`,
  the same basis as `events.occurred_at`. An item older than that cannot be matched against
  current events anyway.
  - An **untriaged** item past the horizon is recorded `verdict='stale', reason='stale'`, with
    no model call.
  - A **material, un-integrated** item past the horizon is excluded by the integration select
    and counted in a new `Tally.aged_out`. Its row is not rewritten, so its triage reason is
    preserved.
- **`stale` is a policy, not a one-way door.** It is terminal for the pipeline, because an item
  comprehended after 14 days can no longer be matched against its own news cycle:
  `candidate_events` windows on `now()`. It is reversible for the operator: a later backfill
  could delete `stale` rows and integrate them with `candidate_events(..., as_of=published_at)`,
  which the function already accepts. Out of scope here; noted so that a gap is never
  mistaken for a permanent loss.
- **Migration** (next number): widen `item_triage.verdict` to include `stale`; widen `reason`
  to include `quote_page` and `stale`; rewrite the biconditional as
  `(verdict = 'material') = (reason NOT IN ('none', 'error', 'quote_page', 'stale'))`.
  Down-migration included. Historical rows are unaffected.

### 4.5 Triage: selective, but not too tight

- **Rule → immaterial.** `is_quote_page(title)` records `verdict='immaterial',
  reason='quote_page'`, `triage_model` NULL. This clears the Reuters quote pages already in the
  24k backlog **with no model call and no hand-run SQL on production.**
- **Rule → material, narrowed.** `triage_by_rules` returns a hit only when the matched surface
  form is a **claim topic or an open story** (`tracked_claim`, `tracked_story`). An
  entity-only hit no longer decides; the item goes to the model. `tracked_entity` stays valid
  in the CHECK for historical rows. Entity hits are still used for integration candidates
  (`index.match` in `run`), so nothing is lost there.
- **The `sampled` control arm must exclude `reason='quote_page'`.** `select_sampled`
  (`comprehend.py:937-942`) promotes the newest `verdict='immaterial'` rows. Unguarded, it
  would promote quote pages: paying to integrate junk, and replacing the unconfounded control
  with rows a structural rule rejected. The predicate becomes `verdict = 'immaterial' AND
  reason = 'none'`, i.e. the items *judged* immaterial. Test: a pool of only quote-page rows
  promotes nothing.
- **Rule order:** stale first, then quote page, then tracked topic, then the model. All three
  rules are free; stale goes first so that no index match runs on an item that will be
  skipped anyway.
- **The model half moves to Haiku 4.5** via the `TRIAGE_MODEL` row. The plan verifies which
  `thinking` value Haiku 4.5 accepts: the call currently sends `{"type": "disabled"}`, which is
  correct for Sonnet 5.
- **The triage prompt is not tightened in this phase.** Routing entity-only items to the model
  IS the tightening. The new material rate is measured after deploy (§7) before anything
  further is changed.

### 4.6 Outage recovery (runbook, after `0rg` deploys)

There is no `updated_at` on `item_triage`, so the reset cannot be scoped to the outage window.
It is blanket but bounded:

```sql
UPDATE item_triage SET integrate_attempts = 0, integrate_defers = 0
 WHERE integrated_at IS NULL AND (integrate_attempts > 0 OR integrate_defers > 0);
UPDATE item_triage SET attempts = 0 WHERE verdict = 'failed';
```

This is acceptable because `failures={}` since 2026-09-08 says genuinely bad items are rare. A
few re-fail and are re-charged at bounded cost. Most outage-hit items will be past the horizon
by then and become `stale` anyway. **Record the two row counts** the statements return.

## 5. Phase 2 — batched integration with clustering

Expected ~**$0.70–1.20/day (≈ £0.50–0.90)** at ~1,040 items/day.

### 5.1 The pass becomes collect-then-submit

Still one hourly job:

1. **Collect.** If an integration batch is in flight, check its status (non-blocking). When it
   has `ended`, stream the results, key them by `custom_id` (never by position), and apply them
   in item-id order through the existing parse → validate → `write_batch` path.
2. **Triage** runs real-time (Haiku), as in phase 1.
3. **Submit.** Only when **no batch is in flight.** Build clustered requests for the material
   items and submit ONE batch.

**At most one batch outstanding** means every submission sees every event from the previous
one. Typical lag is ~1–2 h. A batch still running at the next pass is simply awaited; new
material queues behind it.

### 5.2 Persistence

A new table `comprehend_batches (id, batch_id, submitted_at, collected_at NULL, status,
manifest JSONB, reserved_usd)`. The manifest holds, per `custom_id`, the item ids **and the
exact `ENT`/`EVT` label maps offered**. The parser needs the labels that were offered, hours
after they were built. This is the persisted form of the in-memory `label_map` contract (see
`never-give-a-model-raw-database-ids`).

`brief.py`'s batch helpers are single-request, text-only and blocking (`submit_batch`,
`poll_batch`, `fetch_batch_results`). Comprehension gets its own multi-request, non-blocking
client in `comprehend.py`. It reuses `ANTHROPIC_HEADERS` and the HTTP style, and keeps the
same network-seam pattern so tests can monkeypatch it.

### 5.3 Clustered requests

- Group the submission's material items by **headline similarity** with `pg_trgm`, the measure
  that won the ranking bake-off. Greedy: newest unassigned item, then its unassigned neighbours
  at similarity ≥ `CLUSTER_SIMILARITY` (0.35), up to `CLUSTER_MAX_ITEMS` (8). Leftover
  singletons are packed `COMPREHEND_INTEGRATE_BATCH` (5) per request, as today.
- **The threshold is low-stakes, and the constant's comment must say why.** It decides only
  which items SHARE A REQUEST. The model still decides whether they report the same event, so
  a wrong grouping costs a slightly larger prompt, never a false merge. That is why the
  uncalibrated 0.35 separator is acceptable here and nowhere else.
- Candidates are gathered per request from that request's titles and entity hits, as
  `candidate_events` already does per batch.

### 5.4 In-request linking

This closes the blind spot that exists today: no item can refer to another item's NEW event
in the same call.

- **Schema.** A new event may carry `new_label` (`NEW1`, `NEW2`, …). `candidate` may name an
  offered `EVTn` OR a `NEWn` declared by an EARLIER item in the same response. The `anyOf`
  branches stay: matched `[candidate, standing]`, new `[summary, type, standing]`.
- **Validation** (`_validate_item`) rejects:
  - a `NEWn` reference with no earlier declaration;
  - a duplicate declaration;
  - a `NEWn` reference to a label declared by the same item.

  Each rejection gets its own `failures` key.
- **The write** (`write_batch`) processes items in response order and resolves each `NEWn` to
  the event row created when it was declared. A tally counter `events_linked_in_request`
  measures use.
- **The prompt text** gains two sentences explaining `NEW` labels, alongside the existing
  CANDIDATE LABELS paragraph.

### 5.5 Output trimming (measured, not assumed)

- `aliases`: the description says to supply it only for a NEW entity.
- Event `summary`: the description says one sentence, at most ~25 words.
- Measured via `out=` per item before and after (§7). If output per item does not drop, the
  trim is reported as ineffective rather than kept on faith.

### 5.6 The prompt-version trap

- `INTEGRATE_PROMPT_VERSION` 2 → 3, **for provenance only.**
- The integration select changes from `(integrated_at IS NULL OR integrate_prompt_version < %s)`
  to **`integrated_at IS NULL`**, so a version bump never re-selects completed items.
  Re-extraction becomes an explicit operation, not a side effect of a deploy. **Record on
  `news-brief-3wb` that it becomes unreachable.**
- `retirement()`'s at-risk query mirrors the same predicate (it must match its consumer).

### 5.7 Failure handling

| failure | treatment |
|---|---|
| request `errored` with `invalid_request_error` | charged: `integrate_attempts + 1` (the request itself was refused) |
| request `errored` with any other type, or `expired`, or `canceled` | returned to the queue **uncharged**; counted in `deferred_batch` |
| 401/402/403 at submit or status check | pass aborts, nothing charged (`0rg`) |
| batch not ended at next pass | wait; no new submission |
| batch past 24 h | treated as `expired` for every request still unresolved |
| succeeded but unparseable | the existing budgeted-deferral path (`integrate_defers`, ceiling 10) |

### 5.8 Budget in phase 2

- **At submit:** reserve an estimate. Input tokens ≈ request characters / 3.5; output tokens
  ≈ items × the trailing per-item output mean from `comprehend_spend`, defaulting to 150. Both
  are priced at the batch rate. The submission is sized down, dropping the oldest clusters
  first, so the reservation fits the balance.
- **At collect:** reconcile against actual `usage` per request, write ledger rows with
  `batch_id`, and release the difference into the bucket.

## 6. The gate

### 6.1 The 2026-09-22 window: run the old gate BEFORE any restart

Comprehension stopped on 25 Sep: the account emptied, then the flag was flipped. The corpus
stopped growing mid-cohort. `zp8` (`score_comprehension.py:231`, `:266-267`) refuses with
`not_measurable` when `max(created_at) FROM events`, **the newest event anywhere in the KB**,
is older than `end − horizon`. That branch was pre-registered on 2026-09-22.

**`zp8` sees a corpus that stopped. It cannot see a HOLE.** It checks only the newest event,
never continuity through the window. If comprehension restarted before
**2026-09-29 12:13:37Z** (`end − 6h` for a run at the earliest honest time), `last_event_at`
would be fresh. The gate would then return a binding, no-appeal verdict on a cohort mixing
the old pipeline, a multi-day gap and the new one. The first draft of this spec missed this;
the red-team caught it.

**So the ordering is a hard constraint.** Comprehension stays OFF until the old gate has run.
The runbook's step 5 runs as written, on or after 2026-09-30 00:13:37Z, and its output is
recorded verbatim. Only then does phase 1 flip (D7). Phase 1 takes days to build, so the
constraint costs nothing. It must still be written into the runbook, because nothing in code
enforces it. **`news-brief-li9` makes `zp8` check continuity** (e.g. the largest gap
between consecutive events inside the cohort), so a future hole refuses on its own rather
than relying on the ordering being remembered. If it returns a verdict rather than refusing, that verdict stands.
Amendment 2 spent the one void: *"a second appeal to instrument error is not available."*
Nothing here appeals. The pre-registered admissibility rule answers.

### 6.2 Amendment 3 — re-registering the cutover for the redesigned pipeline

Written into the parent spec §8.2 when phase 2 is ready to flip, and before it flips:

- **Unchanged:** `--horizon-hours 6`, the 7-day window, `CORROBORATION_FLOOR` 0.10,
  ceiling 0.60, `MIN_DISTINCT_AT_10PCT` 2.
- **Cutover = the phase-2 flip**, captured from the flipping statement as in Amendment 2 step 2.
  Not phase 1: the gate measures the pipeline that will stand.
- **State plainly that phase 2 changes the measured system in a direction that favours
  corroboration.** In-request linking exists because of §2.3's blind spot. Improving the system
  after seeing it fail is legitimate; moving a threshold is not. The amendment names the
  difference.
- **Name the new hazard.** A dry bucket stalls `last_event_at`, and the gate would refuse
  again. During the window the runbook watches the exhaustion alert, and the allowance must be
  comfortable for those 7 days (the phase-2 estimate is below the $1.50 default).

## 7. Testing and verification

**Code (TDD, Postgres container exported, so the DB layer runs rather than skipping):**

- Each guard gets a test that fails when the guard is removed, **mutation-checked with a
  count**: the `0rg` classification and abort, `is_quote_page` (both shapes plus the negative
  controls), the unpriced-model refusal, the bucket's clamp and gate, the horizon split
  (`stale` vs `aged_out`), the narrowed rules half (an entity-only hit reaches the model), each
  `NEW`-label rejection, and "a version bump does not re-select a completed item".
- The migration: its up and down both run, the biconditional rejects `immaterial/quote_page`
  paired wrongly, and it accepts each new legal pair.
- The batch client goes through a monkeypatched seam. Fixtures cover
  `succeeded`/`errored`(both types)/`expired`/`canceled` inside one ended batch, results
  delivered out of order, and a manifest round trip.
- Cold start: an empty KB with an in-request `NEW` link, so the deadlock of 2026-09-07 cannot
  recur through the new path.

**Host, by effect, never by reading a row back:**

| after | observation |
|---|---|
| phase 1 deploy | no new Reuters rows since deploy match `is_quote_page`; `untriaged` falls as `immaterial/quote_page` and `stale` rows appear; each pass logs spend and balance; `comprehend_spend` has rows |
| a deliberate $0.01 allowance row for one pass | the pass stops at zero; the alert fires once, then the row is restored |
| phase 1 plus a few days | the new material rate (the D5 measurement); `usd/day` from the ledger against the estimate |
| phase 2 flip | `batch submitted` and `collected` log lines; `events_linked_in_request` > 0; output tokens per item vs the phase-1 ledger |

## 8. Rollout order

1. Phase 1 code (4.1–4.5) with its migration. Deploy **with `COMPREHEND_ENABLED` still false.**
   Capture's changes (§4.1) take effect immediately and need no flag.
2. On or after 2026-09-30 00:13:37Z: run the old gate once and record its output verbatim
   (§6.1). **Comprehension must not be enabled before this step.**
3. Host: set the `TRIAGE_MODEL` row. Run the §4.6 recovery. Flip `COMPREHEND_ENABLED` true.
   Verify by effect (§7).
4. Phase 2 code (§5). Write Amendment 3 (§6.2). Deploy with integration batching live.
5. Host: capture the phase-2 cutover from the flipping statement. The 7-day window starts.
6. Docs: rewrite `docs/2026-09-22-host-runbook-comprehension-restart.md` for the new sequence;
   update the memory entries on comprehension cost and the pipeline, plus a new one on Reuters
   quote pages as a capture lesson.

## 9. What this does not decide

- **How much comprehension is worth.** The allowance is a dial precisely because this cannot be
  known until something reads the KB. Revisit once a KB-rendered brief exists.
- **Haiku for integration.** It stacks with batching (≈ ¼ price) but needs its own quality
  measurement against the gate's registered extractor.
- **Reuters completeness** (`b5s`) and **re-mint dedupe** (`3cc`).

# Event census: design (KB redesign, sub-project 0)

**Date:** 2026-09-28 · **Status:** revision 7, for operator review
**Parks:** `news-brief-vlg` (phase-2 batching plan) and `news-brief-sse` (Haiku link direction)
**Red teams:** `docs/superpowers/reviews/2026-09-28-gold-set-labelling-redteam.md` (revision 1),
`-redteam-r2.md` to `-redteam-r6.md` (revisions 2 to 6)

**Naming.** This is the **event census**: tables `census_*`, mode `census_prepare`, scripts
`census_*`. It is unrelated to the existing **claim-break gold set** (`scripts/score_gold_set.py`,
`tests/test_gold_set.py`, `tests/fixtures/gold_set_breaks.json`). This file keeps its original
name for continuity with its reviews.

**Scope (revision 7).** This spec covers building and labelling the census. **How systems are
scored against it belongs to sub-project 1**, whose spec must answer the §14 checklist before
any system is scored. Rounds 5 and 6 showed that those rules are real and pre-registration
content, but they are fixed before *scoring*, not before *labelling*: the blind labels are the
same whatever the scoring rules turn out to be.

## 1. Why this exists

The knowledge base exists for three things. The operator named them on 2026-09-27:

1. **Confirmation**: the same event reported by more than one outlet.
2. **Development**: how a story evolves over days, weeks and longer.
3. **Connections**: links between events that are not obvious on first reading.

The brief becomes a composite of news and that comprehension.

Only goal 1 is attempted today, and it fails its own gate. Corroboration is about 6.4%, against
a 10% floor. The current evidence points at **retrieval**:
- batched recall@30 is 44% under the live pg_trgm ranking;
- merging every duplicate the probe detected moves corroboration only to 6.7%.

The project has been steering by proxies. In M2, model-vs-model agreement hid the answer: Haiku
"qualified" while disagreeing with Sonnet on 45% of link decisions (`analysis-stats-traps`
Trap 8). And a gold set chosen by the signal under test is not a benchmark (Trap 5). So the
redesign starts from ground truth.

## 2. The redesign, decomposed

Each sub-project gets its own spec, plan and build.

| # | Sub-project | Depends on |
|---|---|---|
| **0** | **Event census: this spec** | — |
| 1 | Confirmation: event coreference, retrieval included; **owns the scoring protocol (§14)** | 0 |
| 2 | Development: story threads; owns `same_story` labels and cross-window links | 0, 1 |
| 3 | Connections: non-obvious links | 1, 2 |
| 4 | The composite brief | 1 onward |

Unchanged by this spec: the phase-1 runbook, the 2026-09-30 gate run, and capture.

## 3. Decisions (operator, 2026-09-27/28)

| Topic | Decision |
|---|---|
| Direction | **Window census.** The operator groups every item of a window into events. |
| Window | **Runs of adjacent slots.** A window is **6 hours** (two adjacent 3-hour slots), labelled as one unit. Group ids are global. |
| Sizing | **16 windows, all labelled blind before any system is scored** (round 5). |
| Adjudication | **None inside the census** beyond the precision sample. Pooled adjudication happens at scoring time, under sub-project 1's protocol (§14). |
| Scoring protocol | **Sub-project 1's spec** (round 6), bound by the §14 checklist. |
| Retention | **Enforced in the database**: while the census exists, no item captured before the block end can be deleted or truncated (§7). |
| Serving | A **separate compose service**, holding DB credentials only, on the home LAN, and removed after the census. |
| Headline metric | **Group-level**: was each multi-outlet event connected? |
| Guideline case 5 | **Different** events. |

Carried forward:
- the unit is raw **items**, never extracted events;
- the operator sees no system opinion at any point of the census;
- labelling is recognition wherever possible.

## 4. Windows

### 4.1 Unit and population

A **window** is every item captured (`items.created_at`) in one 6-hour run aligned to
00/06/12/18 UTC. Excluded from it:
- quote pages, through **`common.is_quote_page`** called in Python, never re-derived in SQL;
- items with empty titles;
- **backlog** items, whose `published_at` is more than 24 h before `created_at`. The number
  excluded is stored per window.

Items with a NULL `published_at` are **kept**, and their share is reported. The population is
all captured items, not only `material` ones. There is **no size cap**: busy hours are where
retrieval fails. A window can be labelled across several sessions.

### 4.2 The block and the window order

1. **At first preparation**, eligible windows are enumerated **once** and persisted to
   `census_window_order`. They come from a pre-registered block: **the 14 days ending 3 days
   before first preparation**.
   - The block's start is truncated so that every window has ≥ 14 days of prior capture
     (`CANDIDATE_WINDOW_DAYS`, `comprehend.py:1519`). Capture began 2026-09-04, so no window
     starts before **2026-09-18**.
   - If truncation leaves fewer than 10 days, `census_prepare` refuses. **The earliest first
     preparation is therefore 2026-10-01.**
   - Windows need ≥ 40 eligible items. Skipped windows are stored with a reason.
2. **The four time-of-day positions are the strata** (00–06, 06–12, 12–18, 18–24 UTC). **Four**
   windows are drawn per stratum, shuffled with seed `20260928`, and interleaved, so the order
   holds 16. If any stratum has fewer than 4 eligible windows, `census_prepare` refuses and names
   the stratum. The order is **the** order, and it is never re-enumerated.
3. **Capture changes inside the block.** The block starts after the ~2026-09-15 quote-page flood
   began, so it does **not** straddle it. Whether it straddles the Reuters proxy move
   (`c439ade`, committed 2026-09-25) depends on its host deploy date. The runbook shows the
   operator the host's candidates (the running image's creation time from `docker inspect`) and
   he confirms one, which `census_prepare` records. A table **by block half** is printed as
   description only.

### 4.3 Before window 1: the gap check (free, on the host)

From all cross-outlet item pairs production has merged into one event, the readout prints:
- the capture-gap distribution (g = hours between the two captures);
- **the expected split share under 6-hour windows, `E[min(g/6, 1)]`**;
- the number of merged pairs and 6-hour windows it rests on.

There is no restriction to a ranking version: the gap between two captures is a property of
capture, not of ranking, and no recorded field dates the ranking change.

**The check is biased toward passing.** Pairs production failed to merge are missing from it, and
comprehension ran only around 9–10 Sep and 22–25 Sep, so gaps across the pause between cannot
appear. Pre-registered:
- **above 50%:** stop, and bring the window length back to the operator (2026-10-02: it stopped
  at 67.1%, and the operator ruled to proceed under the within-6h label; §11);
- **40–50%:** proceed, and the headline is labelled everywhere as covering confirmation
  **within 6 hours only**;
- **below 40%:** proceed.

### 4.4 The metric and the sizing

**Multi-outlet group.** A group with items from at least two outlets, after excluding unsure
items (§5).

**Confirms.** A system confirms a group when it places at least two of the group's items, from
different outlets, in one event.

**Headline.** The **pooled** share of multi-outlet groups confirmed: each group counts once, so a
busy window weighs what its groups weigh. That is what "was each multi-outlet event connected?"
means, and it keeps busy hours, where retrieval fails (§4.1), at their true weight.

**Why 6-hour windows.** Labelled as one window, the two adjacent slots have no internal
boundary, so only the outer boundaries split events, which §4.3 measures. Cross-window matching
moves to sub-project 2 (§13).

**Sizing basis.** The sizing below is for the pooled estimator, with windows as the unit of
resampling. Sub-project 1's test must therefore test the **pooled** difference, clustered by
window (§14 item 1). A paired **sign-flip randomization over the 16 windows** does this exactly
(2¹⁶ sign patterns), and a window with no multi-outlet group simply contributes zero.

- Paired McNemar sizing at Δ = 0.25, d = 0.5 gives 63 effective groups.
- Design effect of the pooled estimator: `DE = 1 + ((1 + CV²) × m̄ − 1) × ρ`, where m̄ =
  multi-outlet groups per window, CV its coefficient of variation, and ρ the intra-window
  correlation of discordance. ρ is **assumed** at 0.1, with 0.05 and 0.3 printed. It cannot be
  measured in sub-project 0.
- `MDE = (t₀.₉₇₅,₁₅ + t₀.₈₀,₁₅) × sqrt(d × DE / (16 × m̄))`, and `2.131 + 0.866 = 3.00`.

**Detectable difference at K = 16, computed 2026-09-28 and reproduced by the round-6 review**
(points, at ρ = 0.05 / 0.1 / 0.3):

| m̄ | CV | K = 16 |
|---|---|---|
| 6 | 0 | 24 / 27 / 34 |
| 8 | 0 | 22 / 24 / 33 |
| 8 | 0.5 | 23 / 26 / 36 |
| 10 | 0 | 20 / 23 / 32 |
| 10 | 0.5 | 21 / 25 / 35 |

The census resolves differences of roughly **23–27 points** at ρ = 0.1. It cannot settle a
difference of 10. Whether that is enough depends on differences not yet measured at this layer:
the historical 31–44 points measured visibility and 14–17 measured recall@30, both upstream of
"confirms" (§14 item 7).

After window 16 the readout prints the **achieved** detectable difference from the measured m̄
and CV. There is no extension.

### 4.5 Go / no-go after windows 1 and 2 (pre-registered)

Continue only if **both**:
- mean multi-outlet groups per window ≥ **8**, the floor at which K = 16 reaches about 25 points
  at ρ = 0.1. Both window values are printed, because a mean of two windows from two strata is a
  weak estimate;
- median active minutes per window ≤ **80**.

Otherwise stop and bring it to the operator, with both measurements. An abandoned census still
reports its detectable difference at the current K.

### 4.6 Consistency

- Window 2 is designated for repeat **in advance**. **Pass 1 is the scored pass**; pass 2 exists
  only to measure consistency. Window 2's precision sample (§6.4) is deferred until after the
  repeat, so the operator does not review his own window 2 groups in between.
- After window 8, and ≥ 7 days after window 2's blind pass, window 2 is served again as a pass-2
  blind pass.
- The header shows only the session number. The progress counter counts **sessions** (17), so
  the repeat leaves no gap. The titles themselves remain a fingerprint that nothing can hide.
- Agreement is measured pass-to-pass: pairwise precision, recall and F1, plus ARI. It is printed
  with its interval, and one window is stated as thin. It is the same operator with the same aid,
  so shared systematic misses agree with each other: it bounds inconsistency, not misses.

### 4.7 Out of scope, stated

- Same-event pairs split across windows. §4.3 measures how many.
- Operator misses. The census does not measure them; sub-project 1's pooled adjudication does
  (§14 items 2–4).

## 5. The labelling guideline (shown on the page)

> Put items in one group only if they report **the same occurrence**: the same action,
> statement or disclosure, by the same actor(s), at the same time. Different outlets,
> headlines, angles, sourcing or updated figures about that one occurrence still belong
> together. A **reaction, consequence or follow-up** is a different occurrence: leave it out of
> the group, even though it is the same story. Leave an item ungrouped when it matches nothing.
> Mark it **unsure** when you cannot tell from what is shown, even after opening the link.

Boundary cases:
1. "Israel strikes depot near Tyre" and "Lebanon says Israeli strike killed two": **same**.
2. "Earthquake kills 12" and "Death toll rises to 40": **same**, because the figure is an
   update.
3. "Iran vows retaliation after strike" and the strike: **different**, because it is a
   reaction.
4. "Stocks fall as oil jumps on Middle East strikes" and the strike: **different**, because it
   is its own occurrence.
5. "Fed holds rates" and "Powell: cuts not imminent", from the same press conference:
   **different**. The operator ruled this on 2026-09-28. It matches done-vs-said.
6. The same story through two feeds of one outlet: **same**. This is rare, because capture
   deduplicates per outlet (`capture.py:45–55`).

**Unsure, one rule.** An unsure item is excluded from every metric. Its group is scored on its
remaining items, and it drops out only if fewer than two outlets remain. The unsure share is
reported.

## 6. The system

### 6.1 Two processes, split by privilege

**`census_prepare`: a `brief.py` mode, run manually in the main container**
(`docker compose run --rm newsbrief census_prepare`), added to the usage string
(`brief.py:4057`). It has no `Schedule` and makes **no model call**. It runs under `brief.py`'s
normal startup (`REQUIRED_ENV` and the seed block, `brief.py:4010–4040`), which is harmless in
the main container. It is idempotent, and it does, in one run:
1. the §4.3 gap check;
2. enumerating and persisting the 16-window order (§4.2);
3. freezing **all 16 windows'** membership;
4. recording the confirmed `c439ade` deploy date (§4.2), and writing the `census_block` row that
   arms the retention hold (§7).

**`labeller`: a separate compose service**, `profiles: [census]`. It uses the same image with
`entrypoint: ["python", "labeller.py"]`: a **standalone module importing only `db`, `common`
and the standard library**. It does not go through `brief.py`, whose `__main__` requires the
Anthropic and Telegram keys (`brief.py:4010`) and runs seed writes.
- **Environment:**
  - `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, and `POSTGRES_USER=census_labeller` with its
    own password. If the host uses `DATABASE_URL` instead, the runbook sets the labeller's own
    equivalent. A wrong database fails the startup self-check loudly;
  - `LABELLER_BASE_URL`, the LAN URL, used for the `Host` and `Origin` checks;
  - `NEWSBRIEF_LOG_FILE=0`.

  Nothing from the shared anchor. `LABELLER_BASE_URL` is a **plain environment variable, not a
  knob**, declared in both this service and `newsbrief`, because the daemon builds the link.
- **No knob reads.** `census_labeller` cannot read `settings`, so `labeller.py` must never trigger
  `common.__getattr__` for a `KNOBS` name (`common.py:374–394`). A test enforces it (§10).
- **Grants:** a re-runnable host-runbook script, `scripts/census_grants.sql`. It runs after 0017
  is confirmed in `schema_migrations`, and again after any restore. Its `CREATE ROLE` is
  idempotent (a `DO` block with `IF NOT EXISTS`), because roles are cluster-global and survive
  `DROP SCHEMA`.
  - `SELECT` on `items`, `outlets` and every `census_*` table;
  - `INSERT` on `census_assignments`, `census_groups`, `census_adjudications`, `census_events`,
    `census_sessions`;
  - `UPDATE (consumed_at)` on `census_sessions`;
  - `UPDATE (status, abandon_reason, opened_at, blind_done_at, completed_at)` on
    `census_windows`;
  - `USAGE` on the sequences of every table it inserts into.
- **Startup self-check:** the labeller verifies every required privilege with
  `has_table_privilege`, `has_column_privilege` and `has_sequence_privilege`. It **exits
  non-zero naming each missing grant**, rather than failing on the first autosave.
- **Port:** its own `ports:` entry, bound to the host LAN address. A failed bind stops only this
  service.
- **Lifecycle:** `docker compose --profile census up -d labeller`, then stop and remove after the
  census. The repo compose file carries the service; the host runbook carries the steps.
- **Packaging:** `labeller.py` and its static asset directory are added to the Dockerfile COPY
  allowlist, and `test_packaging` is extended to cover the directory.

### 6.2 Access and hardening

**Tokens and sessions.**
- `/label`, in the existing `commands` daemon, mints a **one-time link token**:
  `secrets.token_urlsafe(32)`, valid for 10 minutes. Only its SHA-256 is stored.
- Opening the link renders the page directly. The token is consumed atomically:

  ```sql
  UPDATE ... SET consumed_at = now()
  WHERE token_sha256 = $1 AND consumed_at IS NULL
    AND revoked_at IS NULL AND expires_at > now()
  RETURNING id
  ```

  The page then sets a random 30-day session cookie (hashed, `HttpOnly`, `SameSite=Lax`) and
  strips the token with `history.replaceState`.
- Each request hashes the cookie and compares it with `hmac.compare_digest`, and the link token
  gets the same comparison.
- POSTs also require `Origin` equal to `LABELLER_BASE_URL`, and `Host` is checked against it.
  Anything else gets a bare 403.
- `/label reset` revokes all sessions. No plaintext token is ever stored.

**Rendering.**
- Every field is passed through `html.escape`.
- Links use the `http`/`https` schemes only, with `rel="noopener noreferrer"`.
- JS and CSS are served as separate static files.

**Headers on every response, the 403 included:**
- `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
  connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store`

**Accepted risk, stated:** cleartext HTTP on the home LAN. The asset is label integrity only.
The service holds no other credential, and its role can write only the census tables.

### 6.3 The blind pass

```
Session 3/17 · 187 items                                 [guideline ▾]
┌──────────────────────────────────────────────────────────┐
│ ○ 06:02 Reuters  Israel strikes depot near Tyre       🔗 │
│ ● 06:05 AP  Lebanon says strike killed two            🔗 │  ← selected
│ [Tyre strike] 06:11 BBC  <title>                      🔗 │  ← grouped
│ ?  06:14 AFP  <title>                                 🔗 │  ← unsure
└──────────────────────────────────────────────────────────┘
Selected: 2  [ New group ] [ Add to… ▾ ] [ Ungroup ] [ Unsure ]
                                        [ Finish blind pass ✓ ]
```

- **Order:** capture time. There is no system help; an operator-typed text filter is allowed.
- **Groups show content.** A chip, and each "Add to…" entry, shows the group's first title,
  most recently touched first.
- **Ungrouped** means singleton.
- **Autosave:**
  - every action carries a per-tab id and a **client sequence number**; a retried
    (tab, number) already stored is ignored;
  - the **last write to arrive** per (window, item) wins (highest `id`). Revised during
    planning (plan review B5): ordering by client number let an older tab's later edit lose
    silently to another tab's higher counter;
  - a failed write shows a red "not saved" banner and retries in order.
- **Timing:** page-open, each action and Finish are logged to `census_events`. **Active minutes**
  exclude idle gaps longer than 5 minutes. Active and wall-clock minutes are both reported.
- **Abandon** records `status = 'abandoned'` with a typed reason. It counts toward time, and is
  listed.

### 6.4 The precision sample

After Finish, the page shows up to **10 blind-joined cross-outlet pairs**, asking **same?**
- Multi-outlet groups are drawn **without replacement**, `min(10, groups)` of them, and **one
  cross-outlet pair** is drawn uniformly within each.
- Blind precision is the **group-weighted** mean: each sampled group counts once, whatever its
  size. Its interval is clustered by window.
- It is shown only after the blind pass is saved. Decisions go to `census_adjudications` with
  kind `precision` and `created_at` as the ruling date. Blind rows are never modified.

### 6.5 Telegram

- `/label` returns the progress line and a fresh link.
- At morning-brief delivery, one line is added if a window is waiting:
  `🏷 Session 3/17 ready · /label`. It is fail-safe: any error means no line, never a failed
  delivery. It is silent after session 17.

## 7. Storage: migration 0017

| Table | Contents |
|---|---|
| `census_block` | singleton (`id boolean PRIMARY KEY DEFAULT true CHECK (id)`), `block_start`, `block_end`, `prepared_at`, `c439ade_deployed_at` |
| `census_window_order` | `order_no`, `window_start`, `stratum` (persisted once) |
| `census_skipped_windows` | `window_start`, `reason` |
| `census_windows` | `id`, `order_no`, `window_start`, `pass`, `repeat_of`, `status` (`prepared`/`open`/`blind_done`/`complete`/`abandoned`), `abandon_reason`, `backlog_excluded`, `null_published`, `opened_at`, `blind_done_at`, `completed_at` |
| `census_window_items` | `window_id`, `item_id` (FK `items`, `ON DELETE RESTRICT`) |
| `census_groups` | `id` (global), `window_id`, `created_at` |
| `census_assignments` | `id`, `window_id`, `item_id`, `group_id` (FK `census_groups`, NULL = ungrouped), `unsure`, `client_seq`, `created_at` |
| `census_adjudications` | `id`, `window_id`, `kind` (`precision`; sub-project 1 adds its own kinds), `item_a`, `item_b`, `decision`, `created_at`; `CHECK (item_a < item_b)`, `UNIQUE (kind, item_a, item_b)` |
| `census_sessions` | `id`, `kind` (`link`/`session`), `token_sha256`, `expires_at`, `consumed_at`, `revoked_at` |
| `census_events` | `window_id`, `kind`, `at` |

**The retention hold.**
- 0017 adds a `BEFORE DELETE` row trigger on `items` that raises when the `census_block` row
  exists and the item's `created_at` is before its `block_end`, and a `BEFORE TRUNCATE`
  statement trigger on `items` that raises while the row exists. Row triggers do not fire on
  `TRUNCATE`, and the suite already uses that idiom (`tests/test_score_comprehension.py:731`).
- **Why.** The replay in §14 item 6 must match production. A naive item delete already fails
  today, because `feed_sightings.item_id` has no `ON DELETE` action
  (`migrations/0008_capture_telemetry_up.sql:54`). The silent case is a retention job that
  prunes sightings first, which is `news-brief-uh0`'s shape: the item delete then cascades into
  `assertions` and `item_triage` (`migrations/0006_knowledge_base_up.sql:109`,
  `0009_comprehension_up.sql:9`).
- **Limits, stated.** `session_replication_role = replica` bypasses both triggers.
- **Release.** A named runbook step deletes the `census_block` row and, when the census itself is
  retired, the `census_*` tables. `census_window_items`' RESTRICT FK keeps pinning the 16 windows'
  items until then.
- **The down migration refuses** while `census_assignments` has rows, so a migration reversal
  cannot silently destroy the census. When it does run, it also drops the trigger functions,
  following `migrations/0006_knowledge_base_down.sql` and its test (`tests/test_kb_schema.py:803`).
- The error message names the hold and the row that releases it. `uh0`'s acceptance criteria
  require its retention to skip held items.

No change to `comprehend_spend`: the census makes no model call.

## 8. Readout: `scripts/census_report.py`

Read-only and free. It prints:
- **Before window 1:** the §4.3 gap check and its band.
- **Per window:**
  - items, groups, singletons;
  - unsure and NULL-`published_at` shares;
  - multi-outlet groups;
  - active and wall-clock minutes;
  - status.
- **After window 2:** the §4.5 go/no-go, with both measurements and both window values.
- **After window 16:**
  - the achieved detectable difference at each ρ, from the measured m̄ and CV;
  - blind precision, with its window-clustered interval;
  - consistency;
  - the by-block-half table, labelled descriptive.

## 9. Failure handling

- **DB unreachable:** the banner, then ordered retries.
- **Missing grant:** startup exits and names each grant.
- **Labeller crash or bind failure:** affects only the `labeller` service.
- **`census_prepare` failure:** loud. It is idempotent, so re-run it.
- **No window ready, or the census is complete:** `/label` says which.
- **Refusals that name the decision needed:**
  - go/no-go fails;
  - gap share above 50%;
  - block shorter than 10 days, which is any run before 2026-10-01;
  - a stratum with fewer than 4 eligible windows.
- **A delete or truncate of held items, or a down migration over a labelled census:** raises,
  naming the hold (§7).

## 10. Testing

**The service really starts.**
- `labeller.py` is launched as a **subprocess** with only the §6.1 environment, against the test
  Postgres, under a real `census_labeller` role created by `census_grants.sql`. The test DB user
  is a superuser, so it can create the role; the script runs twice to prove it is idempotent.
- It must serve a 403 on `/` without a cookie.
- With one grant revoked, it must exit non-zero, naming that grant.
- **No knob reads:** `common.__getattr__` is patched to fail on any `KNOBS` name while every
  labeller route runs.

**Access:**
- an expired, reused or revoked link returns 403;
- no cookie returns 403;
- a wrong `Origin` or `Host` returns 403;
- every response header is present, the 403 included;
- a **hostile `<script>` title and a `javascript:` URL** render inert;
- no plaintext token appears in any table.

**Ordering.** Out-of-order retries resolve by `client_seq`.

**Windows:**
- the 16-window order is persisted once, then survives corpus growth and a predicate change (the
  corpus is mutated after first preparation);
- four windows per stratum, and refusal when a stratum has three;
- the look-back and ≥ 40-item rules hold;
- the backlog is excluded and NULL `published_at` is kept;
- `is_quote_page` is patched to prove it is the function called;
- a preparation dated 2026-09-30 refuses, and 2026-10-01 succeeds.

**Retention hold:**
- deleting a held item that has `assertions` and `item_triage` rows raises, and both child rows
  survive;
- `TRUNCATE items CASCADE` raises while `census_block` exists;
- deleting a post-block item succeeds;
- with the `census_block` row removed, the pre-block delete succeeds, except for items pinned by
  `census_window_items`;
- a second `census_block` row is rejected;
- the 0017 down migration refuses with a labelled census, and runs (dropping its functions) on an
  empty one.

**Metrics:**
- on hand-built partitions: "confirms", the unsure rule, the pooled group-level share, and pair
  and B-cubed recall;
- window 2's scored pass is pass 1;
- the achieved-difference formula on known inputs, with an independent hand computation as the
  control;
- go/no-go at both thresholds;
- the gap-check bands at 39.9%, 40%, 50% and 50.1%.

**Precision sample:**
- it draws only blind-joined cross-outlet pairs, groups without replacement, one pair per group;
- with fewer than 10 groups it draws one per group and stops;
- the estimator weights each sampled group once;
- `(b, a)` is rejected by the `CHECK`;
- window 2's sample is withheld until after its repeat.

**Nudge:** an error inside it produces no line and a successful delivery.

**Mutation checks** on:
- "confirms";
- token comparison;
- `client_seq`;
- the group-first precision sampling;
- the go/no-go;
- the block-length refusal;
- the retention trigger's comparison, and the truncate trigger.

The plan pre-registers the failure count for each.

## 11. Recorded values (filled in by the process, never edited after)

- **Gap check (§4.3):** **STOP, 2026-10-02.** `census_prepare` against the production database
  refused with expected split share **67.1%** under 6-hour windows (band `stop`, above 50%);
  nothing was written. Breakdown of the merged cross-outlet pairs (2,549, the same pair set as
  `census.gap_check`; the buckets' points sum to 67.1, which is the control):

  | capture gap | pairs | % of pairs | points of the 67.1 |
  |---|---|---|---|
  | < 1 h | 417 | 16.4 | 1.0 |
  | 1–6 h | 906 | 35.5 | 18.1 |
  | 6–24 h | 750 | 29.4 | 29.4 |
  | 1–3 d | 312 | 12.2 | 12.2 |
  | > 3 d | 164 | 6.4 | 6.4 |

  - **No event dominates:** the ten largest events hold 379 pairs (15%), the largest 72 (2.8%).
    Pairs straddling the 11–21 Sep comprehension pause (a later item can join an older event)
    contribute at most the 6.4 points above 3 days.
  - **Robust to the most favourable cut:** dropping every pair over 24 h still leaves 59.7%
    (48.5 points over 81.3% of pairs), so no reading of the pair set passes at 6 hours.
  - **Other window lengths, computed after the 6-hour result was seen** (same pair set,
    `E[min(g/W, 1)]`, the 6-hour row reproducing 67.1): **12 h 53.7%, 24 h 39.6%.** 24 h is 0.4
    points under the 40% line, and the check is biased toward passing.
  - **24 h is not feasible as designed:** the block floor is 2026-09-18, so a block prepared on
    2026-10-02 holds 11 days (16 needs `BLOCK_DAYS` ≥ 16 and preparation on or after
    2026-10-07, with no draw left); the time-of-day strata disappear; and labelling grows about
    4× against §12's ~19 h.
  - **Operator ruling, 2026-10-02: keep 6-hour windows and proceed with the headline narrowed
    to confirmation within 6 hours** (the `within_6h_only` band, here over a share above 50%).
    Reasons: it reuses the design as built, the 40–50% band's label and §13's assignment of
    cross-window joining to sub-project 2; 24 h fails on feasibility above; neighbouring-window
    links in the labeller need new UI, and machine-proposed candidates risk inheriting
    production's ranking blind spots. Cost: the headline speaks only to the roughly one-third of
    merge pairs a window keeps together. Downstream consequence carried by §14 item 12, not by
    another revision here. Mechanism: `CENSUS_GAP_RULING=within_6h_only` on `census_prepare`
    (refused when the gap check did not stop); the readout marks the band "by operator ruling".
  - **Prepared, 2026-10-02** (runbook step 3, after redeploying and repinning the labeller):
    `prepared block 2026-09-18 to 2026-09-29: 16 windows drawn plus the repeat, 3 skipped, gap
    band within_6h_only by operator ruling (2549 pairs, 52 windows)`. The 2,549 pairs equal the
    decomposition's total above, so the ruling was applied to the pair set it was made on.
    `c439ade` deploy confirmed as 2026-09-30T15:30:00+00:00, after the block's end, so the block
    does not straddle it.
- **Go/no-go (§4.5):** *(after window 2)*
- **Achieved detectable difference (§4.4):** *(after window 16)*

## 12. Success criteria

- The operator can label a window over one or more phone sessions at home, within the §4.5
  budget.
- The census completes 16 windows plus the repeat (17 sessions, about 19 h at 65–80 minutes
  each), with consistency and blind precision recorded. Or it stops at the go/no-go with
  measured reasons.
- The readout reproduces known values on synthetic data.
- The items the sub-project 1 replay needs are protected by the §7 hold for as long as the
  census exists.

## 13. Out of scope

- Scoring any system: sub-project 1, under §14.
- `same_story`, cross-window links and connections: sub-projects 2 and 3.
- Development needs the stretches between windows. Global group ids and the persisted window
  order let sub-project 2 **extend** windows contiguously later, at the cost of a new census of
  the stretch, and join groups across a boundary by group-to-group recognition.
- Access from outside the home LAN.

## 14. Checklist sub-project 1's spec must answer before scoring any system

Raised by rounds 5 and 6 (`-redteam-r5.md`, `-redteam-r6.md`, which hold the full arguments).
Each item needs a pre-registered answer; "not applicable, because…" is an answer.

1. **The test.** It tests the **pooled** group-level difference, clustered by window, which is
   what §4.4's sizing assumes. A paired sign-flip randomization over the 16 windows is the
   recommended form. The point estimate and the interval measure the same quantity.
2. **Operator misses enter the headline, at group level.** The operator ruled on 2026-09-28 that
   confirmed links join the headline. Round 6 showed that merging by single pair rulings,
   transitively, lets one borderline ruling override a deliberate blind split (guideline cases
   3–5) and build mega-groups. So when a pool "same" would join two non-singleton blind groups,
   the operator is shown both groups and asked "same occurrence?" once.
3. **Rulings apply in both directions.** A pool "different" on a blind-joined pair counts as
   evidence too. Otherwise false joins stand, and they count against the system that correctly
   kept those items apart.
4. **A precision guard in the decision rule.** "Confirms" is recall-only, so a system that links
   everything would score 100%. The primary decision needs a precision condition, for example
   "B beats A only if B's pool precision is not worse than A's by more than X".
5. **No sampling inside the headline.** The pool is adjudicated in full. If that is too large,
   the comparison is over budget and says so; a sampled pool feeds only a separately labelled
   estimate.
6. **Freeze, then retire.** Declare the first set of systems before any scoring. Adjudicate their
   union pool in one sitting, shuffled, with system identity hidden, and freeze the result. Later
   systems are scored against the frozen partition, and their new pairs are reported separately.
   Name a rule for retiring the 16 windows, because every pool review shows the operator, who is
   also the developer, each system's errors on the only scored set. Record each ruling's date
   (`created_at`) and report the effect of adjudication lag.
7. **The attenuation estimate.** Estimate, from the replay, how upstream differences (visibility,
   recall@30) shrink by the time they reach "confirms", and state whether 23–27 points is enough.
8. **Replay runs.** Two runs per arm (Sonnet's 77% self-agreement). Fix how runs combine into one
   share, and which runs' links enter the pool.
9. **Several comparisons.** One primary per decision. Secondaries are gatekept (tested only if
   the primary rejects), or Holm-corrected across all comparisons, primary included.
10. **Development data.** Name it before the first comparison (for example, days outside the
    block), and never tune on the 16 windows.
11. **Replay from capture day one** (2026-09-04), so entity state matches production. About
    $40–60 per system per run, so about $160–240 per two-arm comparison. Budget it.
12. **The headline covers within-6-hour confirmation only** (§11, gap-check ruling of
    2026-10-02). Two-thirds of production's cross-outlet merge pairs are split by a 6-hour
    window, and long-gap confirmation, which asks a ranking to find an older event in a growing
    corpus, may be exactly where systems differ. State what a within-window result licenses
    about cross-window matching, and what it does not, before scoring.

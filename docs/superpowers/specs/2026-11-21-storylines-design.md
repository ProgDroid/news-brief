# Storylines: design

Bead `news-brief-al7` (rethink sub-project 1). Brainstormed with the operator 2026-11-20/22.
Parent: `docs/superpowers/specs/2026-11-20-rethink-charter-design.md` (§3 success criteria,
§7 the shape, §8.1 the decisions this spec owes). Status: **revision 3**, after a builder check
and red-team rounds 1 and 2 (`docs/superpowers/reviews/2026-11-21-storylines-redteam.md`,
`…-redteam-r2.md`). Round 2 recommended stopping red-team rounds here: its fixes were small and
local, and are checked again in plan review. Revision 4 (2026-11-22) moves the brief's batch
from the evening to the early morning (§6.1); nothing else changed. Revision 5 (2026-11-23)
splits phase 2 into three plans (§18) and absorbs what changed since: phase 1 took migration
0018, `d7w` removes the signals call and the paper sim (`cg3`), and the brief's cost has been
measured on the host (§11).

The repo is public. Nothing in this document, its tests or its fixtures names a holding, an
amount, or the text of an operator thesis. §13 says where that material lives instead.

## 1. What this builds

Two phases, one build.

- **Phase 1, the first slice** (charter §8.1): the existing brief gets dated memory. Ledger rows
  show their dates, a fixed 7-day window of past headlines replaces the 1,000-character weekly
  slice, and a thesis evidence log is kept from the brief's own Position Signals lines. It is
  cheap, needs no attachment, and is the rollback target and the comparison point for phase 2.
- **Phase 2, storylines:** a bounded, dated world model the brief writes against. Captured
  articles attach to named storylines. Only storylines touched today get an LLM update. Each
  keeps its current state, a dated timeline, signposts, the consensus-vs-constraints pair, and
  links to other storylines and to theses. The brief, alerts and the Sunday message read from it.
- **Before either, the brief moves to the early morning** (§6.1): submitted at 04:30 instead of
  20:00, so the 06:00 brief covers Asia's day and the US close. It changes no layout and ships
  first, on its own.

Both answer the charter's main letdown, continuity: the brief says "this follows from the 12th"
instead of re-surprising, stops repeating itself, and has somewhere to put second- and
third-order effects.

**What the model actually reads.** Capture stores each feed entry's RSS summary as `items.body`
(`capture.py:130`); no article page is fetched. Summaries are short (about 40–260 characters as
measured on the brief's 400-character-capped text in August; `items.body` itself is stored
uncapped), and for the Google News proxy feeds (7 of 27, both Reuters feeds among them) the
summary is the title plus the outlet. Storyline state is therefore written from **headlines and ledes**, as today's
brief is. Character caps below are ceilings, mostly not reached.

## 2. Decisions

Operator decisions from the brainstorm:

| # | Decision | Operator's call |
|---|---|---|
| D1 | Storylines are seeded, proposed by the system, and accepted or rejected on Telegram; the operator can also add and retire them | "I name some, system proposes others, I accept or reject" |
| D2 | New brief layout: 🧵 threads that moved (from storylines) leads; 🌍 top stories reads the articles no storyline consumed | Chosen over "memory only" after the assistant retracted a wrong cost argument |
| D3 | The per-topic dynamic sections are dropped; pinned topics become pinned storylines | "drop them, the layout makes sense" |
| D4 | Each storyline carries the consensus-vs-constraints pair from the first build | "a" |
| D5 | Haiku attaches every article on every attach pass; Sonnet confirms alerts and corrects attachments in the nightly pass | "haiku attaches, sonnet checks works" |
| D6 | A weekly connections pass, sent as its own Sunday message | "weekly works" |
| D7 | Out-of-brief alerts on signpost hits, bursts and surges on known storylines, with an on-demand flash brief | Raised by the operator. The surge trigger was added while writing, after a review noted a big unforeseen development on a tracked storyline would otherwise not ring |
| D8 | The defaults in §10 (lifecycle, inputs, seeding, commands, rollout) | "sounds good" |
| D9 | The charter's first slice is built first, as phase 1 (§3) | "let's do a", after red-team round 1 found the spec had dropped it |

Calls made by the assistant (from the builder check and red-team round 1), for the operator to
check:

- **Workbench event triggers stay the watch job's.** They are not copied into signposts. The
  nightly update and the brief read them live (`watch.load_watch`) as context. No sync path, and
  no double ring for one development.
- **`/pin` and `/unpin` retarget to storylines** (the `pinned` flag). `/focus` and `/mute` stay
  free-text reader preferences applied to the whole brief, as today.
- **One rule for leaks:** an attached article not consumed by a landed nightly update returns to
  the unclaimed pool (§7.3). It covers the per-storyline cap, the 02:30–04:30 gap and a late batch.
- **Rollback is accepted as slightly degraded:** the old claim reconcile stops while the
  storylines brief is on; on rollback the claims block is omitted if more than 7 days stale, and
  phase 1's 7-day window carries the memory.
- **The offline check is scored on articles at least one run attached**, with an absolute floor
  for Haiku (§8.2).

## 3. Phase 1: the first slice

Behind `BRIEF_DATED_MEMORY_ENABLED`. Works on today's layout and stays under the storylines
layout.

1. **Dated claims.** `brief_memory.render_established_block` renders `first_seen` and, when set,
   `resolution_date` on every row: `• [topic] (since 11-14, resolves 12-01) claim — driver`.
2. **A 7-day headline window.** Replaces `weekly_summary[:1000]` in `build_daily_prompt`. From
   `load_last_n_briefs(7)`, take each brief's 🌍 TOP STORIES, 🧵 THREADS THAT MOVED (each entry's
   first line) and 👁 WATCH / FORWARD bullets, dated, each cut at 200 characters, total ceiling
   ~3k tokens. A brief in the old layout has no 🧵 section and contributes the other two; in the
   new layout Top Stories holds only unclaimed news, so the 🧵 lines are what carry tracked news
   into the window. Yesterday's full brief (`[:6000]`) stays while the old layout is live, because
   its "No significant change" rule depends on it.
3. **Thesis evidence log.** After `collect` delivers the brief, one Haiku forced-tool call reads
   the brief's 📌 POSITION SIGNALS section and the thesis keys from `load_theses()`, and writes
   `thesis_evidence` rows (thesis key, date, supports/challenges, one line; `source = 'brief'`).
   Rows are dated by the brief's **submit** day (`state["date"]`), not collect-time `now()`.
   After §6.1 both run on the same UTC date, but a fallback or a manual rerun can still straddle
   midnight, and `now()` keying is the bug fixed as `news-brief-hw8`. Phase 2's nightly update
   writes the same table with `source = 'storyline'`. **No double counting:** under the storylines
   layout the extractor also receives that day's storyline evidence rows and records only
   developments not already among them (in practice, unclaimed news that bears on a thesis).
4. **A read surface for the log:** `/evidence` on Telegram, with a thesis picker, lists the last
   30 days of rows for that thesis. (The MCP interface, `nyy.2`, reads the same table later.)

Cost: about one Haiku call a day plus ~3k extra brief input tokens. Pennies a month.

## 4. The storyline

Example (a public topic, already one of the brief's web-search topics):

> **BoJ normalisation**
> *Description:* Bank of Japan moving away from ultra-loose policy: rate decisions, yield-curve
> control, intervention, and the wage data that feeds it.
> *Where it stands:* Hiked to 0.75% in Oct; markets price one more by March. Yen weakness is the
> pressure, the wage round is the gate.
> *Consensus:* one more hike by March; the yen recovers.
> *Constraints:* debt-service cost caps how far rates go; an election year limits tolerance for
> yen weakness.
> *Timeline:* 11-14 Tokyo CPI above forecast (Nikkei) · 11-18 Ueda signals "no rush" → yen −1.2%
> (Reuters) · 11-20 Rengo asks for 5%+ again (NHK, state-funded)
> *Signposts:* intervention confirmed → ⬆ hike odds · spring wage round under 4% → ⬇ hike odds
> *Links:* storyline "US rates path" (carry-trade unwind risk); thesis `<key>` (supports)

| Field | Written by | Kept |
|---|---|---|
| name, description | seeding, proposal, or operator; edited only by the operator | current; edits are new rows |
| where it stands, consensus, constraints | nightly update | append-only, one row per update; current = latest |
| timeline line (date, one line, outlets with vantage tags, article refs) | nightly update | append-only |
| signposts (text, direction) | nightly update, operator | until hit or retired; hits are dated |
| links (to storyline or thesis key, reason, kind) | nightly update, weekly pass | `first_seen`, `last_affirmed`, `retired_on` |
| thesis evidence (thesis key, date, supports/challenges, line, refs, source) | phase 1 extraction, nightly update | append-only |

**Vantage tags travel with the news.** `outlets` holds each source's kind, perspective and
state-funded flag (`capture.py:72-89`), and the brief's `SYSTEM_PROMPT` relies on them ("Beijing's
read, via SCMP"). The nightly update prompt, the timeline line and the brief's claimed-headline
block (§7.2) all carry them.

**Status:** `proposed` → `active` ⇄ `dormant` → `retired`; `proposed` → `rejected`; `active` →
`retired` with `merged_into` (§9.5). A `pinned` flag marks the ones the operator wants a line on
every day.

## 5. The bounds

Stated separately, as the charter requires (the claim ledger once conflated them, bead `6wc`).

### 5.1 Storage

Every row is kept, dated; dormancy, retirement and merging are dates, never DELETEs. Per year, at
the ceiling of 20 updates a day (§5.2):

| Table | Rows a year |
|---|---|
| `storyline_states` | ≤ 20 × 365 = 7,300 |
| `storyline_timeline` | ≥ 7,300 |
| `item_attach_decisions` | ~800 × 365 ≈ 292,000 |
| `item_attachments` | attach rate × labels × 292,000; at most ~876,000 |
| `thesis_evidence` | a few thousand |

About 0.3–1.2 million small rows a year. Trivial for Postgres; no retention policy now. `items`
retention is bead `uh0` and unchanged.

### 5.2 The storyline count

**At most 60 storylines that are `active` or `dormant`** (`STORYLINE_MAX_LIVE`). At the bound,
`propose` stops proposing and offers retirements and merges instead (§9.4–9.5); `/storyline add`
says the bound is reached and offers the same. The bound is what keeps every prompt below finite.

Expect the set to settle near the bound, since only the operator shrinks it. So the design is
**priced at the bound** (§11), and the nightly update has its own daily ceiling:
**at most 20 storyline updates a day** (`STORYLINE_MAX_UPDATES_PER_DAY`), taken in order of
pinned first, then most newly attached articles. A storyline skipped by the ceiling keeps its
previous state, and its unconsumed articles go to the unclaimed pool (§7.3).

### 5.3 Prompts

| Call | Sees (ceiling) |
|---|---|
| Attach (per pass) | every live storyline's name, description and live signposts (≤ 60); the pass's new articles, title + body ≤ 600 chars, at most 60 per call (a bigger backlog splits into several calls) |
| Nightly update (per touched storyline) | current state, pair, the last 15 timeline lines whatever their age, live signposts, links; up to 15 newly attached articles with outlet and vantage tag; a one-line index of the other storylines; thesis keys with one line each; live watch triggers (one line each) |
| Brief | moved storylines, at most 12 in full (state, pair, the last 5 timeline lines whatever their age, so earlier dates are always there to cite, plus today's lines, signpost hits, new links, thesis evidence) and the rest one line each; claimed-headline block ≤ 20k tokens; unclaimed articles ≤ 60k tokens; phase 1's 7-day window ≤ 3k tokens; existing market, portfolio, podcast, TWIE and web-search blocks |
| Burst / surge candidates | unclaimed headlines from the last 6 h, ≤ 300; per-storyline outlet counts |
| Confirm (signpost, burst, surge) | one storyline's state, timeline and signposts; ≤ 8 triggering articles |
| Flash brief | one storyline's state, pair, links and linked storylines' one-liners; ≤ 10 articles |
| Propose | unclaimed headlines from the last 24 h, ≤ 800; rejected proposal names from the last 90 days; the live storyline names |
| Weekly | every live storyline's state, pair, links and ≤ 5 timeline lines from the week; thesis keys |
| Phase 1 evidence extraction | the brief's Position Signals section; thesis keys |

Prompts must stay under Haiku 5.5's 100k long-prompt tier, where every token bills at
$0.50/$2.50 (`comprehend.py:472-485`). The attach call is far below it; the offline check's
batching (§8.2) must respect it too.

## 6. Pipeline and schedule

All times UTC, all jobs in `scheduler.SCHEDULES` (no host cron). Interval schedules share fire
times and `Schedule` has no dependency field, so `attach` simply processes every article with no
decision yet; an article captured in the same tick is picked up next pass.

| Job | When | Model | Does |
|---|---|---|---|
| `attach` (new) | every 30 min | Haiku 5.5, real time | decides every undecided article: up to 3 storylines, or none; flags suspected signpost hits |
| `burst` (new) | every 3 h | Haiku 5.5 → Sonnet to confirm | bursts in unclaimed headlines (§9.2) and surges on known storylines (§9.3) |
| `storylines` (new) | 02:30, batch | Sonnet 5 | nightly update of every storyline with new attachments since its last landed update |
| `submit` (existing, moved) | 04:30, batch (was 20:00) | unchanged | builds the brief (§7) |
| `collect` (existing) | 06:00 | unchanged + Haiku | delivers the brief, falling back to real time at 06:30 (§6.1), then runs phase 1's evidence extraction |
| `propose` (new) | 07:00 | Sonnet 5 | proposes up to 3 storylines, or offers retirements and merges at the bound |
| `weekly` (existing, rewritten) | Sun 21:00, batch | Sonnet 5, extended thinking | week in threads + connections (§9.6) |
| `storylines_seed` (new, one-off) | by hand on the host | Sonnet 5 | drafts the starting storylines as proposals (§10.5) |

**Decision records, not inferences.** `attach` writes one decision row per article: `attached`,
`none` or `failed`. "Attached to nothing" and "not looked at yet" never look the same. A `failed`
article is retried next pass; a timeout or 429 defers without charging a retry budget, a 4xx
charges it.

**Labels, not ids.** Every prompt refers to storylines and articles by opaque per-call labels
(`S1`, `A1`), mapped back in code; an unknown label is an error, never a fall-through.

**Batch timing.** At `submit`, a storyline whose 02:30 update has not landed is rendered from its
previous state, and its unconsumed articles go to the unclaimed pool (§7.3). `STORYLINE_UPDATE_MODE`
switches the nightly update to real time if batches prove too slow.

### 6.1 Brief timing

The brief was submitted at 20:00 for 10 hours of batch slack before the 06:00 delivery. That
slack cost the brief everything published in those 10 hours: Asia's trading day (Bank of Japan
decisions land around 03:00), and in winter the US close (21:00). Batches have in practice
finished in minutes, so the slack is not worth that cost (operator decision, 2026-11-22). Submit
moves to 04:30 and stays a batch, at batch price.

**Fallback.** `collect` at 06:00 polls the batch as today, but only until `BRIEF_FALLBACK_AT`
(06:30). If it has not ended by then, collect sends the same request in real time, leaving the
batch running:

- **Submit persists the request** it batched (`brief_request-{submit_day}.json` on the volume,
  keyed by `state["date"]` like its siblings), since collect runs in another process and cannot
  rebuild it.
- **Cancel only with a replacement in hand** (operator decision, 2026-11-22). If the real-time
  call succeeds, collect delivers it and then cancels the batch. If it fails, collect goes back
  to waiting on the batch until just under the 4 h job limit, as today. The fallback never does
  worse than the wait it replaces. A brief is never delivered twice.
- **The real-time call** has its own timeout (`BRIEF_REALTIME_TIMEOUT`), set from one measured
  run on the host before the bead closes. An overloaded or rate-limited reply (429, 5xx, 529) is
  retried, up to 3 attempts. A timeout is not retried, because that request may have finished
  and been billed.
- **No failure in this path crashes collect.** A failed status check is logged and polling
  continues; any other error ends in "no brief" and the alert below.
- **The path is recorded**: `brief_path=batch|realtime` in the collect log line, and a fallback
  brief's Telegram header says "(live fallback)", so a run of fallbacks shows up without reading
  logs. (`job_runs` has no column for it, and adding one is not worth a migration.)
- If the real-time call fails and the batch never ends either, collect alerts as today.

A fallback day costs that day's brief at full price instead of half: about $0.25–0.35 extra on
§11's brief line, so about $1–1.50 a month at one fallback a week. Rarely it costs both: a batch
that finishes while the real-time call runs is billed too (about $0.10–0.20).

**Cutover.** Deploy the change between 06:00 and 20:00. Deployed after a 20:00 submit and before
its 06:00 collect, the 04:30 submit sees yesterday's date in state, submits a second batch and
overwrites the first batch's id, leaving the first one orphaned.

**Correction in the nightly pass.** The update may reject an attachment ("not about this
storyline"). A rejected article returns to the unclaimed pool unless another storyline consumed it.

## 7. The brief

### 7.1 Layout

Replaces the OUTPUT FORMAT block of `build_daily_prompt` when `STORYLINE_BRIEF_ENABLED` is on.

1. **🧵 Threads that moved.** One entry per moved storyline: what changed, since when ("follows
   from 11-14"), whose reading (vantage), signposts hit, which thesis it touches and which way,
   linked storylines worth watching. Quiet pinned storylines get one line at the end.
2. **🌍 Top stories.** Written from unclaimed articles (§7.3) only. Must not repeat a thread from
   section 1 or a headline in phase 1's 7-day window.
3. **📈 Market pulse.** Unchanged.
4. **📊 Macro signal.** Unchanged.
5. **📌 Position signals.** From today's storyline thesis-evidence rows (`source = 'storyline'`)
   plus unclaimed news that bears on a holding. It never reads `source = 'brief'` rows, so the log
   cannot feed itself. The separate forced-tool signals call is removed by `cg3` (`d7w` ruled
   it not useful, 2026-11-23); this section never depended on it.
6. **👁 Watch / forward.** From the live signposts of moved and pinned storylines and the live
   watch triggers, not invented daily.

The rule "No significant change — [one line]" and yesterday's full brief are removed in this
layout: a quiet storyline does not appear, and the 7-day window prevents repeats.

**Moved** means: the storyline has a landed state update since the previous brief's submit. With
the 02:30 update and the 04:30 submit, that is normally the update of the same night.

### 7.2 The claimed-headline block

Under each moved storyline, the articles its update consumed: outlet, vantage tag, title, about
25 tokens each, ceiling 20k tokens for the whole block. The brief can then attribute claimed news
("Tokyo's read, via NHK") and judge significance across everything, not only across what the
update summarised.

### 7.3 Inputs

- **Articles come from the capture database.** Capture polls `RSS_FEEDS` and every temporary feed
  source (`capture.py:293-297`). Temporary sources of type `page` and the Mauldin TWIE scrape
  (`brief.py:3082`, not captured) keep their existing fetch paths and join the unclaimed input.
- **Unclaimed** means: captured since the previous brief's cutoff, and not consumed by a landed
  nightly update. That is, decided `none`, rejected by every storyline it was attached to,
  attached after its storyline's update was built, beyond the update's 15-article cap, or
  attached to a storyline whose update has not landed. Rendered as outlet, vantage tag, title,
  body ≤ 400 chars. Ceiling 60k tokens; above it, keep by number of outlets carrying the same
  development, then recency.
- **Fallback:** if `capture.liveness` reports capture stale at submit time, the brief falls back
  to today's live fetch and the old layout, and says so in its header. Liveness ignores quality
  (a day with both Reuters feeds failing is "live"); today's live fetch has the same exposure, so
  this is not a regression.
- **Removed in this layout:** yesterday's brief, the claims block and its reconcile. **Kept:**
  phase 1's 7-day window and the five web-search topics (`TOPICS`).

## 8. Attachment

### 8.1 The call

One Haiku 5.5 forced-tool call per batch of up to 60 articles. Output, schema-validated: per
article label, 0–3 storyline labels and optionally one suspected signpost label. Thinking set
explicitly to disabled. Expected volume ~700–900 articles a day (recorded range 392–1,030), about
15–19 per pass.

### 8.2 Offline check

Runs inside the build, before `STORYLINE_BRIEF_ENABLED` or `STORYLINE_ALERTS_ENABLED` go on.
`attach` itself may run earlier, since its rows only feed the nightly update.

1. On the host, export one past week of `items` (~5–6k articles) and the seeded storylines to the
   host data folder. Neither enters git.
2. Run the attach call three times: Sonnet 5 twice (S1, S2), Haiku 5.5 once (H). Every request
   stays under 100k input tokens.
3. Metrics, on the **either-attached subset** (articles at least one of the two compared runs
   attached): mean Jaccard of the attached sets. Also report, on all articles, each run's attach
   rate, and where H and S1 differ whether H attached less, more, or elsewhere (answers bead
   `sse` on this task).
4. **Pre-registered bars:**
   - Sonnet self-agreement J(S1, S2) ≥ 0.65. Below that the task is ill-posed: descriptions get
     rewritten and the check reruns. Haiku is not judged.
   - Haiku passes if J(H, S1) ≥ max(J(S1, S2) − 0.10, 0.55) **and** H's attach rate is at least
     S1's minus 15 points (a Haiku that attaches much less starves the storylines).
5. The operator sees 20 sampled H/S1 disagreements at the end, to sanity-check the verdict. No
   labelling.
6. The result goes in `docs/` as counts and percentages, with storyline names replaced by labels.
7. **Re-run** whenever the live storyline count doubles from the count last measured, and again
   when it first reaches the bound of 60. The check script lives in `scripts/` for that.

### 8.3 If Haiku fails the bar

Embeddings shortlist the 5 nearest storylines and Haiku chooses among them. That needs bead
`bqa.7` (pgvector image, an embedding model) and a rerun of §8.2. Not built unless the check fails.

## 9. Alerts, proposals, merges and the weekly pass

All alerts follow the `capture.liveness` contract: return `(episode_key, message) | None`; the
caller dedupes against the **set** of sent keys in `storyline_alerts` (not a single last key,
which re-rings when two storylines alternate); send before storing. Alerts ring, like the watch
job's TRIGGERED alerts.

### 9.1 Signpost hits

`attach` flags a suspected hit; a real-time Sonnet call confirms it against the storyline state
and the article. Confirmed:

> ⚡ **BoJ normalisation** — signpost hit: intervention confirmed
> [2–3 article links] · Follows from: 11-18 Ueda "no rush".
> [Flash brief]

The **Flash brief** button runs a real-time Sonnet call over the §5.3 flash input; paid only when
pressed. The signpost is marked hit (dated) either way. Episode key: storyline + signpost.

### 9.2 Bursts

`burst` asks Haiku whether the last 6 h of unclaimed headlines hold one new development carried
by ≥ 3 outlets. Sonnet confirms. A confirmed burst alerts (with Flash brief) and queues a
proposal. Episode key: a normalised title of the proposed storyline.

### 9.3 Surges on a known storyline

Closes the gap between 9.1 and 9.2: a major, unforeseen development inside a tracked storyline
attaches to it (so it is not a burst) and may match no signpost.

- **Candidate rule:** attachments in the last 6 h from ≥ 3 outlets **and** at least
  `STORYLINE_SURGE_FACTOR` (3) × the storyline's own trailing 14-day average per 6 h. A storyline
  younger than 14 days needs ≥ 5 outlets in 6 h instead (cold start).
- **Confirm (Sonnet):** is this a material new development not already in the state? Only a
  confirmed surge alerts, and the next nightly update is told to consider a signpost for it.
- The factor is a starting value, not a measured threshold; Sonnet's confirm is the real filter.
  Candidate and confirm counts are logged so the factor can be tuned.
- Episode key: storyline + the 6 h window's start.

### 9.4 Proposals

`propose` reads the §5.3 propose input and proposes up to 3 storylines (name + description).
Telegram shows each with Accept / Reject via `_handle_callback_query` (`brief.py:1136`).
Rejections are stored for good, but `propose` only sees the last 90 days of them; an occasional
re-proposal is accepted as the price of a bounded prompt. Dormant storylines are offered for
retirement the same way after 60 days.

### 9.5 Merges

The nightly update flags two live storylines whose attachments overlap heavily (≥ 50% of either
one's articles over the last 14 days share both labels), and the weekly pass may flag a merge in
prose. Telegram offers Merge / Keep both. A merge retires one storyline with `merged_into` set
(dated); its history stays, its future attachments go to the survivor, and the survivor's next
update sees the retired one's last state once. "Keep both" suppresses the offer for that pair
for 30 days.

### 9.6 Weekly

`mode_weekly` (`brief.py:3263`) is rewritten behind `STORYLINE_WEEKLY_ENABLED`. One batch call
with extended thinking (explicit thinking budget, `max_tokens` sized to it) reads the §5.3 weekly
input and writes:

- **The week in threads:** what moved, from the timelines.
- **Connections:** chains (A moves B, which pressures C), gaps (constraints contradict
  consensus), links nobody has drawn (two storylines sharing a driver), and what each means for
  the theses.

New links it finds are stored and appear in the daily threads section from then on. Links are
applied without approval and carry their reason. A link not re-affirmed by an nightly update or a
weekly pass for 30 days is retired (dated). The paper-book mark-to-market tail of today's
`mode_weekly` goes with the paper sim (`cg3`); the rewritten weekly does not carry it.

## 10. Defaults agreed

1. **Lifecycle.** Fewer than 3 consumed attachments (accepted by an nightly update) in 14 days →
   `dormant`. (A zero-attachment rule would rarely fire, since Haiku may attach up to 3 storylines
   per article.) Dormant means out of the brief, still in the attach prompt, so a burst of news
   wakes it: "back after 5 weeks". 60 days dormant → offered for
   retirement. More than 12 moved in a day → overflow gets one line each. At most 60 live (§5.2).
2. **Inputs.** As §7.3.
3. **What goes away** while `STORYLINE_BRIEF_ENABLED` is on: the claims block, the ledger
   reconcile and yesterday's full brief. Ledger tables stay as read-only history; on rollback the
   claims block is omitted if more than 7 days stale. Bead `6wc` closes as superseded. The ledger
   and `comprehend.py` code are removed in a later bead, once the new brief has run without
   rollback. `claim_verify` (a shadow, log-only check on TOP STORIES) is left as is.
4. **Weekly** as §9.6.
5. **Seeding.** `storylines_seed` runs once on the host. Sonnet drafts about 20 storylines from
   `TOPICS`, the pinned topics (`DEFAULT_PINS` or the operator's overrides, which become pinned
   storylines), and the theses from `load_theses()`, with live watch triggers as context. They
   arrive as proposals through §9.4's buttons. Text drafted from theses goes straight into the
   host database.
6. **Telegram.** Accept/Reject (proposals), Retire/Keep (dormant), Merge/Keep both; `/storyline
   add <name> — <description>`, `/storyline retire` (picker), `/storyline list`; `/pin` and
   `/unpin` pick storylines; `/evidence` (§3). Editing signposts by hand waits for `f71`. New
   commands join the `setMyCommands` self-sync.
7. **Theses** come from `load_theses()` (theses.json plus the pushed portfolio file, fed by the
   workbench's `7qu`). Thesis evidence lives in the database only.
8. **Rollout.** Each part is a settings row (§12). Phase 1 ships first. The offline check runs
   before the storylines brief and alerts go on. No weeks-long trial (charter §10).

## 11. Budget

Estimates at `comprehend.PRICES_PER_MTOK` rates (Sonnet 5 $2/$10, Haiku 5.5 $0.10/$0.50,
`BATCH_FACTOR` 0.5), **priced at the bound** of 60 live storylines (§5.2). Recomputed after
red-team rounds 1 and 2.

| Line | Per month |
|---|---|
| Phase 1 (evidence extraction, window tokens) | < $0.20 |
| Attach (48 calls a day; ~6k-token storyline block at 60 + articles; ≤ ~450k in / 40k out a day, Haiku real time) | ≤ ~$2 |
| Burst and surge (8 Haiku calls a day + confirms) | < $0.50 |
| Nightly updates (ceiling 20 a day × ~5k in / 0.7k out, Sonnet batch) | ≤ ~$5.10 |
| Brief | **measure first**; ~$7–10.70 on the August composition |
| Proposals + signpost confirms | ~$1.50 |
| Weekly with thinking (60 storylines) | ~$0.60–1 |
| ~~Signals call~~ (removed by `cg3`) | $0 |
| **Total of the priced lines** | **~$17–21 (about £13–16)** on the August brief estimate; see the measurement below |

**Measured 2026-11-23 (revision 5).** Three batch briefs on the host:

| Day | Input tokens | Output tokens | Searches | Cost |
|---|---|---|---|---|
| 11-21 | 397k | 9.1k | 3 | ≈ $0.47 |
| 11-22 | 92k | 8.3k | 0 | ≈ $0.13 |
| 11-23 | 205k | 9.9k | 3 | ≈ $0.29 |

- **The costs are re-priced** at `PRICES_PER_MTOK` × `BATCH_FACTOR`, plus $0.01 per search.
  - The log's own `est_batch_cost` reads $0.69, $0.20 and $0.41.
  - It is hard-coded at older rates and runs 1.5× high on tokens. Bead `5ni` fixes it.
  - An earlier draft of this paragraph quoted it, and concluded wrongly that the brief was over estimate.
- **What drives the size:** the base prompt is ~90k tokens; search and code-execution results add 100–300k.
- **At their mean the brief line is ~$9 a month,** inside the $7–10.70 estimate, so the total stays ~$17–21.
- **Three days is not a month.** Plan 2a's first task re-prices this table from up to 30 days of host log, by tokens rather than `est_batch_cost`.

The dial order below stands.

Not priced: the watch job's calls, `claim_verify` if enabled, flash briefs (on demand), and the
one-off offline check.

**The brief line is the uncertain one.** The August baseline (65 runs, mean 170k input tokens)
predates two 2026-08-29 commits that push in opposite directions (`240a4cb`, feeds 5 → 25 items;
`4579910`, dynamic-filtering web search). About 70% of that input tracked the web searches
(51.5k + 22.7k per search × 5.23 searches), which this spec keeps. **The plan's first task reads
the host's current `usage(in=… searches=…)` lines** and sets the brief line and the 60k unclaimed
ceiling from them. The web searches now overlap with storylines and are the first setting for the
budget dial (`3km`) to turn.

Under the £20 floor on these numbers, but the headroom (£3–6) rests on the brief line, which is
unmeasured. If the measurement puts the total above the floor, the order of dials is: cap or drop
the web searches, then lower `STORYLINE_MAX_UPDATES_PER_DAY`, then `STORYLINE_MAX_LIVE`. That is a
settings change, never a redesign (charter §5).

## 12. Settings

New `KNOBS` entries, each with its compose anchor line. Every job resolves its knobs once per
pass.

| Knob | Default | Turns on |
|---|---|---|
| `BRIEF_DATED_MEMORY_ENABLED` | false | phase 1 (§3) |
| `BRIEF_FALLBACK_AT` | `06:30` | when collect stops waiting for the batch and runs the brief in real time (§6.1); `off` = wait as today (12 h, cut to 4 h by `JOB_MAX_RUNTIME_MINUTES`) |
| `BRIEF_REALTIME_TIMEOUT` | 600 | read timeout of each real-time brief call, in seconds; retune from the host measurement (§6.1) |
| `STORYLINES_ENABLED` | false | `attach`, `storylines`, `propose` |
| `STORYLINE_BRIEF_ENABLED` | false | the new layout and inputs (§7); off = today's layout (with phase 1 if on) |
| `STORYLINE_ALERTS_ENABLED` | false | `burst`, signpost and surge alerts |
| `STORYLINE_WEEKLY_ENABLED` | false | the rewritten weekly; off = today's weekly |
| `STORYLINE_ATTACH_MODEL` | `claude-haiku-5-5` | attach and burst candidates |
| `STORYLINE_UPDATE_MODEL` | `claude-sonnet-5` | nightly update, confirms, proposals, flash, weekly |
| `STORYLINE_UPDATE_MODE` | `batch` | `batch` or `realtime` nightly update |
| `STORYLINE_MAX_LIVE` | 60 | §5.2 |
| `STORYLINE_MAX_UPDATES_PER_DAY` | 20 | §5.2 |
| `STORYLINE_DORMANT_DAYS` / `STORYLINE_RETIRE_OFFER_DAYS` / `STORYLINE_BRIEF_MAX_MOVED` | 14 / 60 / 12 | §10.1 |
| `STORYLINE_SURGE_FACTOR` | 3 | §9.3 |

## 13. Privacy

- **Never in git:** storyline names and descriptions drafted from theses, thesis evidence rows,
  consensus/constraints text that names holdings, watch-trigger text, the offline-check export and
  its per-article results. All of it lives in the host database or data folder.
- **Fine in git:** the code, prompts that contain no thesis text, the five public `TOPICS`,
  synthetic test fixtures with invented storyline names, and the offline-check result as counts.
- **No external service** gets thesis text except the Anthropic API calls that already receive
  theses today.

## 14. Data model (for the plan to refine)

Migration `0019_storylines` (phase 1 took `0018_thesis_evidence`, which already created
`thesis_evidence`; 0019 adds its `storyline_id` foreign key). All tables have `created_at`; code
never DELETEs from them.

- `storylines` (id, name, description, status, pinned, origin `seed|proposal|operator|burst`,
  last_attached_at, dormant_since, retired_on, merged_into)
- `storyline_states` (storyline_id, as_of, where_it_stands, consensus, constraints)
- `storyline_timeline` (storyline_id, on_date, line, item_ids)
- `storyline_signposts` (storyline_id, text, direction, origin, hit_on, hit_item_id, retired_on)
- `storyline_links` (from_storyline_id, to_storyline_id | thesis_key, kind, reason, first_seen,
  last_affirmed, retired_on)
- `thesis_evidence` (thesis_key, storyline_id nullable, on_date, direction, line, item_ids,
  source `brief|storyline`)
- `item_attach_decisions` (item_id, decided_at, outcome `attached|none|failed`, model, attempts)
- `item_attachments` (item_id, storyline_id, status `attached|rejected|consumed`, update_id)
- `storyline_updates` (id, storyline_id, built_at, landed_at, mode) — what "consumed" and "landed"
  refer to
- `storyline_proposals` (name, description, origin, proposed_at, decided_at, outcome)
- `storyline_merge_offers` (a_id, b_id, offered_at, outcome, suppressed_until)
- `storyline_alerts` (episode_key, sent_at, kind `signpost|burst|surge`, storyline_id)

## 15. Bead disposition

- `c5k`: the Haiku 5.5 price entry exists; `attach` runs on `claude-haiku-5-5`, which discharges
  it. Close when `attach` runs.
- `sse`: answered by §8.2 step 3. Close with that result.
- `6wc`: superseded (§10.3).
- `bqa.7`: stays deferred; it is §8.3's fallback.
- New, later: remove the claim-ledger and comprehension code once the new brief has run without
  rollback.
- `qap` (new, first): move the brief to 04:30 with the real-time fallback (§6.1). Independent of both
  phases.

## 16. Out of scope

Event skeletons (`uk1`), the MCP query interface (`nyy.2`), Telegram `/set` and signpost editing
(`f71`), the budget dial (`3km`), removing the signals call and paper sim (`cg3`), fetching full
article pages, multi-user, and a web UI.

## 17. Success, for this sub-project

From charter §3, judged by the operator reading the output, with phase 1 as the comparison point:

1. The brief references earlier dates for developments that follow from earlier ones, and stops
   repeating unchanged stories. Phase 1 alone may get part of this; phase 2 should visibly do
   better on follow-ups and links.
2. The threads section and the Sunday connections surface links the operator would not have made
   unprompted.
3. For each thesis, `/evidence` shows what built it up or eroded it since a given date.
4. Monthly spend on this sub-project's lines stays within §11, as measured.

## 18. Build order for phase 2 (revision 5)

Operator decision 2026-11-23: phase 2 is built as **three plans**, in order, each ending in
something checkable on the host. Approaches considered: one plan for all of phase 2 (what "one
build" in §1 implied; rejected as 30+ tasks that land nothing for weeks and are hard to review),
two plans (foundation and brief together, then the rest), and three. Three was chosen.

1. **Plan 2a, foundation.** Task 1 re-prices §11 from 30 days of host `est_batch_cost` lines.
   Then: migration 0019, the storyline store, `/storyline add|retire|list`, Accept/Reject buttons
   (needed by seeding), `storylines_seed`, the `attach` job with its decision records
   (`STORYLINES_ENABLED`), and the §8.2 offline check script. Nothing the operator reads changes.
   It ends with the offline check's verdict, which gates plan 2b's brief. If Haiku fails, §8.3
   is decided before 2b starts.
2. **Plan 2b, the brief.** The nightly `storylines` update, `storyline_updates` and
   consumed/unclaimed accounting, storyline thesis evidence with §3.3's no-double-counting rule,
   the §7 layout and inputs (`STORYLINE_BRIEF_ENABLED`), `/pin` and `/unpin` retargeted, and the
   claims-block omission on rollback. Closes `6wc` as superseded.
3. **Plan 2c, the rest.** Signpost, burst and surge alerts with the Flash brief button
   (`STORYLINE_ALERTS_ENABLED`), the daily `propose` job, dormancy and retirement offers, merges,
   and the weekly rewrite (`STORYLINE_WEEKLY_ENABLED`).

Each plan is written when the previous one has landed, so it plans against the code as built.

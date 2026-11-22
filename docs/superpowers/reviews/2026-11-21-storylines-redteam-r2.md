# Red team, round 2: storylines design (`news-brief-al7`)

Reviewed: `docs/superpowers/specs/2026-11-21-storylines-design.md`, revision 2, against the round-1
review (`2026-11-21-storylines-redteam.md`), the charter (`2026-11-20-rethink-charter-design.md`)
and the code at `7ec445c`. Stance: hostile staff engineer arguing this is the wrong thing to build.

Not re-argued: approach A, single user, curated outlets, no weeks-long trial, the £20 floor, and
D1-D9 as directions. Nothing here comes from running the system or a database. The archived
briefs and log used below are the committed `from-server/` snapshot, which ends 2026-08-29.
Host state after that date is UNKNOWN unless stated otherwise.

## What revision 2 answered

| Round-1 objection | Answered? |
|---|---|
| First slice dropped | **Yes.** Phase 1 (§3) and D9. |
| Attached articles that reach neither the update nor the unclaimed pool | **Yes**, by the leak rule plus the claimed-headline block. The two together are more than the hole needs (see Smaller points). |
| Vantage tags lost at the storyline seam | **Yes** (§4, §5.3, §7.2). |
| TWIE dropped | **Yes** (§7.3). |
| No storyline-count bound, no merge, unbounded `propose` and confirm prompts | **Partly.** The bound and merges exist, and every call now has a prompt ceiling. But the bound was set without pricing it (objection 2), and confirm calls have a size ceiling and no count ceiling. |
| Agreement metric inflated by easy `none`s | **Yes**: either-attached Jaccard, an absolute floor of 0.55, and an attach-rate guard. |
| Budget stale and "total" not the total | **Mostly.** Signals added, unpriced lines listed, brief line marked "measure first". The headroom sentence still overstates (FIXED CHECK F2). |

## Spec claims checked against the code

| Claim | Verdict |
|---|---|
| `items.body` is the RSS summary (`capture.py:130`) | **Holds.** But `_entry_from` (`brief.py:1769-1771`) stores the **full** stripped summary, with the 400-char cap applied only in the brief's renderer (`brief.py:1930`). The spec's "40-260 characters" came from round 1's measurement of that capped render path, not of `items.body`. |
| "10 of 27" feeds are Google News proxies, both Reuters feeds among them | **Count wrong.** `RSS_FEEDS` holds 27 entries: 7 Google News (Reuters business, Reuters world, ISW, Yonhap, 38 North, NHK, BoJ), 3 Nitter, 17 direct. 7 + 3 + 17 = 27. The Reuters part holds. Round 1's "10 + 3 + 14" was wrong at the same commit, and the spec copied it. |
| Outlet kind/perspective/state-funded (`capture.py:72-89`) | **Holds** (the shape tuple and the outlets lookup, lines 66-92). |
| Capture polls `RSS_FEEDS` and temp feed sources (`capture.py:293-297`) | **Holds.** |
| TWIE scrape at `brief.py:3082`, not captured | **Holds.** |
| `_handle_callback_query` at `brief.py:1136`; `mode_weekly` at 3263; paper-book tail at 3303-3312 | **Hold.** |
| Prices and the 100k Haiku tier (`comprehend.py:472-485`) | **Hold** (lines 471-484; `BATCH_FACTOR = 0.5`). |
| Alert contract `(episode_key, message) \| None` | **Holds** (`capture.liveness`, `capture.py:541`). |
| Schedule: submit 20:00, collect 06:00, weekly Sun 21:00 | **Holds** (`scheduler.SCHEDULES`). See Smaller points for what the 20:00 submit costs and what it already broke. |
| `DEFAULT_PINS` | **Holds** (`brief.py:631`, five labels). |
| `load_last_n_briefs` | **Holds** (`brief.py:2275`). It returns whole files, so phase 1 needs a heading parser, as the spec says. |
| The two headings phase 1 parses "exist in both layouts" | **Headings hold, content does not** (FIXED CHECK F1). In the archive, 🌍 TOP STORIES is in 90 of 90 briefs; 👁 WATCH / FORWARD in 75, and the other 15 (2026-05-31 to 06-14) use "👁 WATCH LIST". 75 + 15 = 90. |
| 📌 Position Signals as the evidence-log input | **Holds.** It is in 90 of 90 archived briefs, with 2-11 bullets (mean 4.96, median 4, about 760 characters). Phase 1's extractor has real input. |
| The brief's 600-word limit | **Not mentioned by the spec.** It lives inside the OUTPUT FORMAT block (`brief.py:2442`) that §7.1 replaces. See Smaller points. |

## Objection 1 (b): assumed, not established — that the storylines brief can say "follows from <date>"

Criterion 1 is the reason for the whole sub-project. Revision 2 never gives the brief a dated
record of earlier storyline developments. Trace each input that could carry a past date into the
storylines brief:

1. **Phase 1's 7-day window** parses only 🌍 TOP STORIES and 👁 WATCH / FORWARD (§3.2). In the
   storylines layout, Top Stories holds only *unclaimed* news (§7.1.2). Tracked developments are
   written under 🧵 Threads, which the window never reads. So the window remembers the residue,
   not the threads.
2. **The brief's storyline input** (§5.3) is "state, pair, **today's** timeline lines, signpost
   hits, new links, thesis evidence". It contains no earlier timeline lines.
3. **The evening update** sees the last 7 days of timeline. Its output reaches the brief only as
   the rewritten "where it stands" paragraph.

So for anything tracked, the only carrier of an earlier date is one prose paragraph that Sonnet
rewrites from itself every day. Nothing in the spec asks it to keep dates. The §4 example's
"Hiked to 0.75% in Oct" shows the paragraph *can* hold a coarse date. The spec gives no
guarantee that it *will*, and every daily rewrite is a chance to drop one.

The consequences:

- **Within 7 days**, the storylines brief has *less* dated memory than phase 1 on the old layout,
  where Top Stories carried everything. The "follows from 11-14" in §7.1 has no input that holds
  "11-14", unless the paragraph happened to keep it.
- **Beyond 7 days**, which is the one thing storylines can do and a 7-day window cannot, the
  dated timeline is stored and then read by no daily prompt. The weekly pass sees 5 lines from
  the week, and the MCP interface comes later. §10.1's "back after 5 weeks" wakes a storyline
  whose update sees an empty 7-day timeline and a five-week-old paragraph.
- §7.1 says "the 7-day window prevents repeats". It does so only for the residue (point 1). The
  threads section's repeats are prevented only by the "moved" rule, and that rule fails when an
  update lands late (Smaller points).

**Fix (cheap, about 2-4k brief tokens):**

- Give the brief, for each moved storyline, its last N timeline lines (say 5) **regardless of
  age**. That costs about 5 × 30 tokens × 12 storylines.
- Give the evening update the last K lines regardless of age (say 15) instead of "the last 7
  days". For a slow storyline that reaches back months, at a fixed cost.
- Add 🧵 THREADS to phase 1's window parser, so the window covers the same content in both
  layouts and rollback lands on a window that remembers the threads.

## Objection 2 (c): in six months — the system settles at the storyline bound, and the budget is priced at half of it

Revision 2 answered round 1's "a set that only grows" with a bound of 60 live storylines (§5.2).
The budget (§11) and the storage table (§5.1) are both computed at "~30 storylines and ~15
updates a day". Nothing in the design keeps N near 30:

- **Growth:** `propose` offers up to 3 a day, a confirmed burst queues one more, and
  `/storyline add` adds more.
- **Shrinkage needs a tap.** Dormancy still needs 14 days with **zero** attachments (§10.1), from
  a model allowed 3 labels per article over ~800 articles a day. Dormant storylines still count
  toward the 60. Retirement and merging happen only when the operator taps.
- **At the bound**, `propose` switches to offering retirements and merges. The bound is the only
  pressure on N, so the steady state is N ≈ 60, not 30.

Priced at the bound on the spec's own ratio (15 touched a day at 30 live, so about 30 at 60):

| Line | Spec (N≈30) | At N=60 |
|---|---|---|
| Evening updates: touched × 30.4 days × (5k × $1 + 0.7k × $5) / 1M | 15 → $3.88 | 30 → $7.75 |
| Attach: 48 passes, storyline block about 120 tokens × N | ~$2 | ~$2.3 |
| Total of the priced lines | $16.4-20.6 | about $20.6-24.9 |
| At $1.30 to the pound | £12.6-15.8 | about **£15.8-19.2** |

At the high end that leaves under £1 of headroom. The unpriced lines (watch-job calls, flash
briefs, `claim_verify`) and the uncounted signpost confirms (Smaller points) still sit outside
it. That is the charter's anti-goal 1, "a system that only works at its spend ceiling", reached
by normal operation rather than by a news surge. Two smaller effects arrive at the same bound:

- the brief's 12-thread cap turns about 18 moved storylines a day into one-liners;
- §8.2's re-run rule ("whenever the live count doubles from the count last measured") fires once,
  at about 40 from a seed of about 20, and never measures the 60 the system actually runs at.

**Fix:**

- Price the design at the bound, or set the bound from the budget.
- Add a daily ceiling on evening updates, `STORYLINE_MAX_UPDATES_PER_DAY` (for example 20, ranked
  by attachments from distinct outlets). The leak rule already returns the articles of a skipped
  update to the unclaimed pool, so this adds no new data path, and the evening line becomes a
  hard ceiling (20 → about $5.2 a month).
- Make dormancy not need zero attachments: for example, fewer than k attachments from at least 2
  outlets in 14 days, or no landed update that changed the state.
- Re-run §8.2 when N reaches the bound, as well as on doubling.

## Objection 3 (b): assumed — that the thesis evidence log records each development once

Revision 2 keeps phase 1's extractor running under the storylines layout (§3: "stays under the
storylines layout") and makes the storylines brief's 📌 Position signals "from today's
thesis-evidence rows" (§7.1.5). Together they form a loop:

1. At 18:00 on day D, the evening update writes `thesis_evidence` rows with `source = 'storyline'`.
2. The 20:00 brief renders those rows as Position signals.
3. At 06:00 on D+1, after `collect`, phase 1's extractor reads that section and writes the same
   developments again, paraphrased, with `source = 'brief'`.
4. The date is unspecified. Every collect-side artifact in the codebase is keyed by the collect
   day (`brief-{today}.md`, `save_signals(..., today)`, `brief.py:3186-3207`), so the default
   implementation dates the echo D+1. The 20:00 brief on D+1 then reads it as "today's
   thesis-evidence rows" and re-presents yesterday's signal as today's. Each brief feeds the next.

Even without step 4, `/evidence` (criterion 3, "what built it up or eroded it") lists each
storyline-sourced development twice, under two dates and in two wordings. Any supports/challenges
tally doubles. Nothing in §3, §7.1 or §14 dedupes.

**Fix (one rule):** while `STORYLINE_BRIEF_ENABLED` is on, Position signals reads only
`source = 'storyline'` rows. The extractor is given those rows and records only the lines that
do not trace to one of them, which covers unclaimed news bearing on a holding. Brief-sourced rows
are dated by the submit day (`state["date"]`), never the collect day.

## FIXED CHECK: two facts side by side, and the third claim a reader infers

**F1 (§3.2). FLAGGED: unsupported implicature, and it feeds objection 1.**
- The pair: "The parser keys on those two headings" + "which exist in both layouts".
- The inferred claim: the 7-day window holds the same content under both layouts, so §7.1's "the
  7-day window prevents repeats" and §2's rollback ("phase 1's 7-day window carries the memory")
  both hold.
- Neither fact establishes it. The headings survive. Their meaning does not: in the storylines
  layout, Top Stories is the unclaimed residue and the tracked news sits under 🧵. Rollback
  therefore lands on a window of residue, and the window prevents repeats only in the section
  that holds the least tracked news.

**F2 (§11). FLAGGED: unsupported implicature.**
- The pair: "Total of the priced lines ~$17-21 (about £13-16)" + "Under the £20 floor on these
  numbers, with roughly £4-7 of headroom".
- The inferred claim: the design as specified runs with £4-7 to spare.
- The arithmetic holds. Lines summed with each "<" at its bound:
  - low: 0.2 + 2 + 0.5 + 3.8 + 7 + 1.5 + 0.5 + 0.9 = **$16.4**;
  - high: 0.2 + 2 + 0.5 + 3.8 + 10.7 + 1.5 + 1 + 0.9 = **$20.6**.
  - At $1.30: £12.6-15.8, so headroom £4.2-7.4 ✓ (the low end is $16.4, not "~17").
- The implicature does not hold, for four reasons:
  - The brief line is the spec's own "measure first".
  - Its $10.70 top cannot be rebuilt from parts stated in either document (see below).
  - The unpriced lines come out of the same headroom.
  - It is priced at N≈30, not at the bound the system settles at (objection 2: about £1-4).

**F3 (§1). FLAGGED: a wrong count and a measurement taken on another path.**
- The pair: "Summaries run about 40-260 characters" + "for the Google News proxy feeds (10 of
  27 ...) the summary is the title plus the outlet".
- The inferred claim: what capture stores is short everywhere, so the caps are "mostly not
  reached".
- The count is 7 of 27, not 10 (decomposed in the residuals table below).
- The 40-260 figure is round 1's per-source mean for 25 of 27 sources, measured on
  `claim_evidence` text that the brief's renderer had already cut to 400 characters. The other 2
  sources fall outside the range or have no data. `items.body` is uncapped (`_entry_from`
  docstring). So whether the 600- and 400-character caps bind is UNKNOWN until someone runs
  `SELECT outlet, percentile_cont(.9) ... length(body)` on the host.
- Cost is protected either way, because the caps are explicit.

**Not flagged:**
- §8.1 "about 15-19 per pass": 700/48 = 14.6 and 900/48 = 18.75 ✓.
- §3 "~3k tokens" for the window: 7 briefs × about 9 bullets × 200 chars ≈ 12.6k chars ≈ 3.2k
  tokens ✓.
- §7.3's "today's live fetch has the same exposure, so this is not a regression" ✓.

### §11 re-checked line by line

| Line | Recomputed | Spec | Verdict |
|---|---|---|---|
| Phase 1 | Haiku about 3k in a day (pennies) + 3k × $1/M × 30 = $0.09 | < $0.20 | ✓ |
| Attach: 450k in, 40k out, Haiku | (0.045 + 0.020) × 30.4 = $1.98 | ~$2 | ✓ (at N≈30) |
| Burst/surge: 8 × ≤300 headlines × 25 tok | 60k a day × $0.10/M = $0.18 + confirms | < $0.50 | plausible |
| Evening: 15 × (5k × $1 + 0.7k × $5)/1M | $0.1275 × 30.4 = $3.88 | ~$3.80 | ✓ (at N≈30) |
| Brief | see below | $7-10.70 | **top end not derivable** |
| Proposals + signpost confirms | `propose` alone at its 800-headline ceiling: about 23k in + 0.5k out, Sonnet real time, ≈ $0.051 a day = **$1.55**. At a realistic 320-560 unclaimed headlines, $0.9-1.1. Leaves $0-0.6 for signpost confirms at about $0.01 each | ~$1.50 | **confirm count unbounded**: Haiku may flag on 48 passes a day, and each flag is a real-time Sonnet call |
| Weekly | 60 × about 400 tok = 24k in + about 20k thinking/out, batch ≈ $0.13 a week | $0.5-1 | ✓ |
| Signals | measured $0.88 | ~$0.90 | ✓ |

**The brief line.** Round 1's formula, per day: input × $1/M + 5,909 × $5/M + 5.23 × $0.01 =
input × 1e-6 + $0.0818, times 30.
- Low: 170k − 23k (removals) + 5k (storyline block) + 0 (unclaimed) = 152k → **$7.02** ✓.
- High, from round 1's stated parts: 170k − 12k + 8k + 60k = 226k → **$9.23**.
- Add revision 2's new inputs (claimed-headline block ≤ 20k, window ≤ 3k) → 249k → **$9.93**.
- The spec's $10.70 corresponds to about 275k, which is 26k above any sum of stated parts. That
  26k is an unexplained residual. It may happen to cover the larger 12-full-thread block, but
  nothing says so. Moot once the plan's first task reads the host's `usage(in=… searches=…)`
  lines, as the spec already requires. Make that task also re-derive the range.

### §5.1 row counts re-checked

| Table | Rows a year | Note |
|---|---|---|
| `storyline_states` | 15 × 365 = 5,475 | ✓ "≈ 5,500" |
| `storyline_timeline` | ≥ 5,475 | ✓ |
| `item_attach_decisions` | 800 × 365 = 292,000 | ✓; 143k-376k over the recorded 392-1,030 a day |
| `item_attachments` | 0 to 3 × 292,000 = 876,000 | ✓ |
| `thesis_evidence` | a few thousand | ✓, but doubled by objection 3's echo |
| Not listed | `storyline_updates` (= states, 5,475), `storyline_proposals` (≤ 1,095), signposts, links, alerts, merge offers | about 10k; immaterial |
| **Sum** | low 5,475 + 5,475 + 292,000 + 0 + ~3,000 ≈ 306k; high 5,475 + 5,475 + 292,000 + 876,000 + ~3,000 ≈ 1,182k | ✓ "0.3-1.2 million" |

At N = 60 the storyline-level tables double, which is still immaterial. The conclusion ("no
retention policy now") stands.

### Residuals, decomposed

| Residual | Parts | Sum |
|---|---|---|
| Baked-in feeds | 7 Google News + 3 Nitter + 17 direct | 27 ✓ (spec and round 1: 10 + 3 + 14, wrong) |
| Archived briefs with the watch heading | 75 "WATCH / FORWARD" + 15 "WATCH LIST" | 90 ✓ |
| Priced budget lines, low / high | as F2 | $16.4 / $20.6 ✓ |
| Brief line, high | 170 − 12 + 8 + 60 (round 1) + 20 + 3 (revision 2) = 249k, against about 275k implied | **26k unexplained** |
| Storage rows | as above | 306k / 1,182k ✓ |

## Smaller points

- **The leak rule and the claimed-headline block are two fixes for one hole, and the leak rule
  brings a repeat path.** With the claimed block listing every attached article under its
  storyline, a static rule would do: attached → that storyline's block, `none` or rejected by
  all → unclaimed. That needs no `consumed`/`landed` routing at submit. As specified, an 18:00
  batch that lands after 20:00 sends its articles to Top Stories on day D. The storyline then
  counts as "moved" on D+1 with the same articles, under 🧵, where no rule checks the 7-day window
  (F1). Batches usually land within minutes (memory note `batch-pipeline-timing`), so this is
  rare. It is still the criterion-1 failure, built in.
- **The brief's length is unspecified.** "Keep the entire brief under 600 words" sits in the
  OUTPUT FORMAT block that §7.1 replaces. Up to 12 full threads, each with what changed, since
  when, whose reading, signposts, thesis and links, either fill about 600 words by themselves or
  get about 25 words each. State the new limit, because it decides how much of objection 1's
  dated recall can reach the reader.
- **The 20:00 cutoff is inherited, not chosen, and it already broke two features.** The
  2026-08-29 log shows `=== SUBMIT ===` at 04:00 and `=== COLLECT ===` at 06:00 on the same day,
  four days running. `scheduler.SCHEDULES` (cutover 2026-09-01) has submit at 20:00 the evening
  before. Its comment says the cutover "changed when nothing", and the log contradicts that.
  Effects:
  - `save_source_index`/`save_claim_evidence` are keyed by the submit day (`brief.py:3092, 3097`),
    and `load_source_index(today)`/`claim_verify.load_evidence(today)` are keyed by the collect
    day (`brief.py:3216`, `claim_verify.py:316`). With submit and collect now on different UTC
    days, the code says both have been on their no-data path since 2026-09-01: the ledger's
    corroboration cue is gone, and `claim_verify` logs "no evidence ... skipping". Host log
    after 2026-08-29: UNKNOWN. Confirm with a host grep for `Claim verify: no evidence for`.
    This is a separate bead, and it bears on §10.3's "`claim_verify` is left as is".
  - For the spec, a 20:00 cutoff means the brief misses the whole Asian working day (three of the
    five `DEFAULT_PINS`) until the following evening. Capture is now continuous and batches
    usually land in minutes, so a 03:00 update with a 04:00 submit is available. The 18:00/20:00
    pair should be a decision, not an inheritance.
- **Merge detection is SQL, not the evening update.** A per-storyline update cannot see
  another storyline's attachments (§9.5). Compute the overlap in code and give the update only
  the flag.
- **Re-run rule**: see objection 2.

## Round balance

This round produced **substantive fixes**:

- the brief gets dated timeline lines, and the window reads 🧵 (objection 1, F1);
- the design is priced at its own bound, with a daily update ceiling and a dormancy rule that can
  actually fire (objection 2);
- one evidence writer per development (objection 3);
- and a side finding (the date-keyed artifacts dark since 2026-09-01) that is a real bead.

None of these re-argues D1-D9 or speculates about a downstream consumer. All of them are small
and local: prompt inputs, a cap, a filter, a date convention. That is the sign the design has
converged. **Apply these fixes and stop red-teaming.** Check them in plan review rather than in a
third round, unless the revision changes the architecture (the data flow, the job set, or the
consumed/unclaimed routing).

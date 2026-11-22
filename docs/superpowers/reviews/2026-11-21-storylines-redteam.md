# Red team: storylines design (`news-brief-al7`)

Reviewed: `docs/superpowers/specs/2026-11-21-storylines-design.md` (draft, 2026-11-21), against
the charter (`2026-11-20-rethink-charter-design.md`), the charter red team, bead `al7`, and the
code at `7ec445c`. Stance: hostile staff engineer arguing this is the wrong thing to build.

Not re-argued: approach A, single user, curated outlets, no weeks-long trial, the £20 floor, and
D1-D8 as directions. Nothing here comes from running the system. The token figures come from the
committed `from-server/newsbrief.log` snapshot, which ends 2026-08-29. Current host values are
UNKNOWN unless stated otherwise.

## Spec claims checked against the code

| Claim | Verdict |
|---|---|
| Capture polls `RSS_FEEDS` and temporary feed sources (`capture.py:293-297`) | **Holds.** `capture_sources()` starts at 272. The cited lines are its temp-feed filter and return. |
| `_handle_callback_query` at `brief.py:1136`, `mode_weekly` at `brief.py:3263` | **Hold.** |
| `submit` 20:00; capture every 30 min; weekly Sun 21:00 | **Hold** (`scheduler.SCHEDULES`). Note that `collect` delivers at 06:00, so the 18:00 storyline state is read by a brief that arrives the next morning. |
| `attach` runs "every 30 min, after `capture`" | **No mechanism for "after".** `Schedule` has no dependency field. Interval schedules anchor to midnight, so `attach` and `capture` share fire times. Harmless (decision rows absorb it), but the sentence describes a feature that does not exist. |
| Alert contract: `(episode_key, message) \| None`, send before storing | **Holds** (`capture.liveness` at `capture.py:541`, `brief.capture_liveness_alert` at 3796). The existing callers remember ONE last key per alert kind. The spec's `storyline_alerts` table correctly widens that to a set. Keep it a set: the single-slot pattern re-rings when two storylines alternate. |
| Prices: Sonnet 5 $2/$10, Haiku 5.5 $0.10/$0.50, batch 50% | **Hold** (`comprehend.py:472-485`, `BATCH_FACTOR = 0.5`). Also there, and not mentioned: Haiku 5.5 bills **every** token at $0.50/$2.50 once a prompt passes 100k. Attach stays far below that. A batched offline-check run could cross it. |
| Five public `TOPICS` | **Holds** (`brief.py:634`; `DEFAULT_PINS` holds the same five labels). |
| "Articles come from the capture database" replaces the live fetch | **Incomplete.** `mode_submit` also scrapes the Mauldin TWIE column (`brief.py:3082`). That column is not captured: a grep of `capture.py` for `twie\|mauldin` found nothing, while the same file has 6 hits for `capture_sources\|load_temp_sources`, so the probe can match. §6.2 keeps the page-type temp sources and drops TWIE without saying so. |
| Evening update reads "first 1,500 chars" and attach "first 600 chars" of each article | **Mostly fictional input.** `items.body` is the RSS summary (`capture.py:123-131`). Capture fetches no article pages. 10 of the 27 baked-in feeds are Google News proxies, **both Reuters feeds among them**, and their "summary" is the title plus the outlet name. In the 2026-08-29 evidence file, the mean summary length per source was 43-262 chars for 25 of 27 sources (n = 5 each, one day, under a 400 cap). So storyline state is written from headlines and ledes. That makes the cost lower than the spec estimates. It also means the quality is not what "1,500 chars" suggests. |

## Objection (a): the simpler thing was already agreed, and this spec dropped it

The charter records an operator decision (§10, §8.1): **approach C's pieces are the first slice
of sub-project 1.** Those pieces are:

- dated ledger rows, with `first_seen` and `resolution_date` rendered;
- a fixed 7-day window in place of `yesterday[:6000]` and `weekly[:1000]`;
- a per-thesis evidence log kept from the brief's own confirm/contradict lines.

Bead `al7` repeats this word for word. The committed memory note does too ("first slice = dated
memory, 7-day window, thesis evidence log").

The spec has no first slice. A search for `first slice|approach C|slice` returns 0 hits in the
spec and 5 in the charter. It goes further than omitting the slice: §6.2 and §9.3 **delete** all
three existing memory inputs (yesterday's brief, the weekly summary, the established-claims
block). Thesis evidence is then written by the new evening update instead of from the brief's
own lines. Nothing in D1-D8 records this as a reversal.

Why this matters beyond procedure:

1. **It is the 80-percent design.** Criterion 1 ("follows from <date>") is impossible today by
   construction: the render drops every date (`brief_memory.render_established_block`, cited by
   the previous round). Criterion 4 is evidence the brief already writes and then throws away.
   The slice costs about $0.01-0.02 a day and needs no new job, no new model tier and no
   attachment.
2. **It is the rollback target the spec lacks.** "Rollback is a row flip" (§9.3) flips back to
   today's brief, the one whose continuity failure caused the rethink. With the slice built
   first, `STORYLINE_BRIEF_ENABLED=false` lands on a brief with a dated 7-day memory.
3. **It is the control.** §15 is judged "by the operator reading the output". Without the slice
   there is nothing to compare against, so a storyline brief that is better only because it
   finally sees dates is indistinguishable from one that is better because of storylines.
   (This is not a request for a trial phase. The slice ships first and storylines stack on it
   within the same build.)

**Fix:** make the slice phase 1 of the plan. Point the 7-day window and the evidence log at the
storyline tables once they exist. Or add a D9 that records the operator dropping the slice, and
the reason.

## Objection (b): assumed, not established — that an attached article reaches the brief

D2 removes claimed articles from the brief's raw input. A claimed article now reaches the brief
**only** through its storyline's evening update. The spec handles attachment *misses* (they stay
unclaimed) but assumes every *attached* article is covered. Its own caps and schedule open four
holes:

1. **The 15-article cap (§4) has no overflow path.** The evening update sees "up to 15 newly
   attached articles". Nothing says what happens to the 16th. Those articles are neither in the
   update nor in the unclaimed pool (§6.2 defines unclaimed as `none` or rejected only). The
   storylines that overflow are, by definition, the ones with the most news that day, and a
   surge (§8.2a) is exactly such a day. Wrong attachments beyond 15 are also never shown to the
   Sonnet check, which is "where Sonnet catches Haiku's mistakes" (§5).
   - Scale: about 800 articles a day, up to 3 labels each, spread over ~15 touched storylines.
     The cap binds whenever one storyline draws more than 15. The attach rate is UNKNOWN until
     §7.2 runs, so the size of this hole is unbounded by anything in the spec.
2. **The 18:00 to 20:00 gap.** An article attached after the 18:00 batch was built, to a
   storyline whose update then lands before 20:00, is not in that update and not unclaimed. It
   reaches the brief through tomorrow's update, in the brief delivered at 06:00 the day after:
   about 36 h after capture, against about 10 h today.
3. **Vantage attribution goes dark for claimed news.** `SYSTEM_PROMPT` relies on per-source
   `kind`, `perspective` and STATE-FUNDED tags ("Beijing's read, via SCMP, is..."; lead with
   ANALYST/REGIONAL/PRIMARY). `outlets` stores all three (`capture.py:72-89`). But none of the
   spec's prompts carries them: attach is "title + first 600 chars", unclaimed is "outlet, title,
   first 400 chars", and the timeline line is "date, one line, article refs". A grep of the spec
   for `perspective|state-funded|ANALYST|vantage` finds nothing. The curated-outlet value the
   charter protects is partly *which vantage said it*, and that is lost at the storyline seam.
4. **TWIE drops** (see the table above).

**Fix (small):** give the brief a compact claimed-article block: outlet, kind/perspective tag and
title, about 25 tokens each, grouped under each moved storyline. About 800 × 25 ≈ 20k tokens
covers a full day. Then add one rule: **any attached article not consumed by a landed update
returns to the unclaimed pool**. That covers overflow, the 18:00 to 20:00 gap and a late batch
with a single mechanism. Carry the vantage tags into the update prompt and into the timeline
line. Keep the TWIE scrape.

## Objection (c): in six months — a storyline set that only grows, measured once at N ≈ 20

Every mechanism in the spec adds storylines. None removes or combines them without a tap:

- **No merge or split.** The lifecycle is proposed/active/dormant/retired/rejected. Proposals are
  read from unclaimed headlines, so a sub-thread of an existing storyline that Haiku failed to
  attach comes back as a *new* proposal. Up to 3 labels per article means overlapping storylines
  get the same articles. The brief then renders the same development in two threads, which is
  the repetition criterion 1 exists to stop.
- **Dormancy needs 14 days with zero attachments**, judged by a model told it may attach up to 3
  storylines per article. Broad storylines will rarely go quiet. Retirement is only *offered*
  after 60 dormant days, and a rejected offer leaves the storyline in the attach prompt
  permanently.
- **Two prompts with no bound.** §4's prompt-bound table lists attach, evening, brief and weekly.
  It does not list `propose`, `burst`, the signpost/surge confirms or the flash brief. `propose`
  reads "the names of rejected proposals", which grows by up to 3 a day forever (about 1,100 a
  year). That is anti-goal 2 ("memory that grows with history") rebuilt in miniature, in the
  spec that exists to state both bounds.
- **The attach check is a one-time measurement at the seeded N (~20).** §7.2 runs once, before
  alerts. The task gets harder as N grows and storylines overlap: "US rates path" against a later
  "Fed independence". Nothing re-runs it. The only size trigger is a log warning at 100, and the
  §7.3 fallback is gated on the one-time check failing, not on N.
- **The check's metric is inflated by easy `none`s.** Exact set agreement with `none` counted as
  a value means a corpus with a high none-rate agrees mostly on articles nobody would attach. The
  relative bar (H ≥ S1/S2 − 10 points) has no absolute floor. At S1/S2 = 70%, Haiku passes while
  differing from Sonnet on 40% of articles. The one prior Haiku-tier measurement had a gap of
  21.5 points (55.1 vs 76.6), so the bar's position matters.

Six months out, with a few proposals accepted a week, N passes 100. Attach then runs at a size
it was never measured at, evening-update spend scales with touched storylines past the 15/day the
budget assumes, and the brief's 12-thread cap pushes most moved storylines into one-liners.

**Fix:**

- State a storyline-count bound, and re-run §7.2 whenever N doubles from the measured value.
- Add a merge operation, offered on Telegram like a retirement.
- Bound propose's rejected list (for example the last 90 days) and accept the occasional
  re-proposal.
- Add rows for propose, burst, confirm and flash to §4's table.
- Report agreement on the articles where **either** run attached something, alongside the
  overall figure, and pre-register an absolute floor for Haiku.

## FIXED CHECK: two facts side by side, and the third claim a reader infers

**F1 (§6.2). FLAGGED: unsupported, and its baseline is stale.**
- The pair: "Ceiling: 60k input tokens" + "the current brief averages ~170k input tokens, so this
  is a saving".
- The inferred claim: the new brief's input will be smaller than today's.
- Reproduced from the log: 65 runs at 06:00, 2026-06-25 to 08-29, mean input 170,217 (min
  86,406, max 314,795), mean 5.23 searches. A least-squares fit of input on search count gives
  **51.5k + 22.7k per search**, so 22.7k × 5.23 = 118.7k of the 170.2k (70%) tracks web searches.
  The parts sum: 51.5 + 118.7 = 170.2 ✓. The spec keeps the web searches unchanged (§6.2, §4).
- **Most favourable fill, on the measured configuration:** remove the *entire* 51.5k non-search
  intercept, send zero unclaimed articles, and add only the storyline block (~5-8k). That gives
  at least ~124k. So **~90k is unreachable** however the gaps are filled.
- **The baseline describes a brief that no longer exists.** Two commits landed on the evening of
  2026-08-29, after the last measured run:
  - `240a4cb` raised `fetch_rss` from 5 to 25 items per feed. Its own message estimates up to
    ~65k more input tokens.
  - `4579910` moved to `web_search_20260209`, whose dynamic filtering trims search results
    before they reach the context.
  The two push in opposite directions, so the current input and its search share are UNKNOWN.
- **Fix:** read the current per-run `usage(in=… searches=…)` lines that `_dump_raw_batch_result`
  already logs on the host, and set the 60k ceiling and the §9.8 brief line from those.

**F2 (§9.8, brief line). FLAGGED: arithmetic.**
- The pair: "input falls from ~170k to ~90k tokens" + "~$4-7.50".
- The inferred claim: about $4 is reachable.
- At $1/$5 batch, 5,909 output tokens and 5.23 searches at $0.01:
  - 90k input gives $0.172 a day, **$5.15 a month**;
  - 170k gives $7.55.
- The $4 end needs about 57k input, which no part of the spec derives. On the measured
  composition, with removals of 12-23k and an unclaimed pool between 0 and the ceiling, the line
  is about **$7.0-10.7**.

**F3 (§9.8, total). FLAGGED: "total" is not the total.**
- The pair: "Total ~$12-16 (about £10-12)" + "Under the £20 floor".
- The inferred claim: the core runs at about £10-12 with about £8-10 of headroom.
- Absent from the table:
  - the signals call, which §6.1 leaves "untouched" ($0.88 per 30 days, measured in August);
  - the watch job's model calls (unmeasured);
  - claim-verify, which is **not** on §9.3's list of what goes away (host state UNKNOWN);
  - flash briefs;
  - the offline check (one-off).
- Corrected with the lines that can be priced (attach 1.95, burst ≤0.5, evening 3.83, brief
  7.0-10.7, proposals 1.5, weekly 1, signals 0.88): **about $16.7-20.4, or £12.8-15.7 at
  $1.30/£**. The floor still holds on these numbers. The headroom is about half what the
  sentence implies.

**F4 (§4, storage). FLAGGED: the count is wrong; the conclusion survives.**
- The pair: "~30 storylines and ~15 updates a day" + "a few thousand small rows a year".
- The inferred claim: total storage is a few thousand rows a year.
- Decomposed per year:

  | table | rows |
  |---|---|
  | `storyline_states` | 15 × 365 = 5,475 |
  | `storyline_timeline` | ≥ 5,475 |
  | `item_attach_decisions` | ~800 × 365 ≈ 292,000 |
  | `item_attachments` | UNKNOWN, attach rate × labels |

  The spec's figure counts only the storyline-level tables. About 300k-600k rows a year is still
  trivial for Postgres, so "no retention policy now" stands. The sentence should give the real
  number.

**F5 (§7.1). Not flagged.**
- "700-900 articles a day across 48 passes, about 18 per pass": 800/48 = 16.7, and 900/48 =
  18.75, so "about 18" is the top of the range.
- The repo's recorded range is wider: 392-1,030 a day (watch spec red team), with a ~670
  non-quote average (gold-set red team) and one surge to ~2,600 Reuters items a day on 2026-09-16.
  None of this changes a verdict.

### Residuals, decomposed

| residual | parts | sum |
|---|---|---|
| brief mean input (65 runs) | 51.5k intercept + 22.7k × 5.23 searches | 170.2k ✓ |
| batch-result log lines | 65 at 06:00 + 38 at other hours (12+5+4+4+3+2+2+2+1+1+1+1) | 103 ✓ |
| baked-in feeds | 10 Google News + 3 Nitter + 14 direct | 27 ✓ |
| §9.8 total, low | 2 + 0.5 + 3.5 + 4 + 1.5 + 1 | 12.5 ✓ ("~12") |
| §9.8 total, high | 2 + 0.5 + 3.5 + 7.5 + 1.5 + 1 | 16 ✓ |
| £ conversion | 12.5-16 / 1.30 | £9.6-12.3 ✓ ("£10-12") |

### §9.8 line by line

| Line | Recomputed | Spec | Verdict |
|---|---|---|---|
| Attach: 450k in, 40k out a day, Haiku real time | 0.045 + 0.020 = $0.065 a day → **$1.95** | ~$2 | ✓ |
| Burst: 8 small Haiku calls, rare confirms | ~200 headlines × 25 tok × 8 ≈ 40k a day → $0.12 + confirms | < $0.50 | plausible |
| Evening: 15 × 5k in / 0.7k out, Sonnet batch | 0.075 + 0.0525 = $0.1275 a day → **$3.83** | ~$3.50 | **9% low** |
| Brief | see F2 | $4-7.50 | **unsupported** |
| Proposals: ~15k in, real time, plus confirms | 0.03 + ~0.01 = $0.04 a day → $1.20 + confirms | ~$1.50 | plausible; confirms unbounded |
| Weekly with thinking | ~15k in + ~20k out, batch → ~$0.12 a week → ~$0.50 | ~$1 | ✓ |

## Smaller points

- **Web search is the largest cost and now duplicates the storylines.** §9.5 seeds storylines
  from `TOPICS`, and §6.2 keeps web-searching the same five topics, without `max_uses`
  (`brief.py:2877-2878`). On the August composition, the searches were about $3.6 of input plus
  $1.6 in fees a month. This is the obvious first dial for `3km`.
- The rewritten `mode_weekly` must keep its paper-book mark-to-market tail (`brief.py:3303-3312`)
  until `d7w` decides the paper sim's fate.
- `fallback if capture.liveness reports stale`: liveness deliberately ignores quality
  (`brief.py:3796` docstring). A day with both Reuters feeds failing is "live" and silently
  thins the brief. Today's live fetch has the same exposure, so this is not a regression, but say
  so.
- §8.2a: the trailing 14-day average is undefined for a storyline younger than 14 days. State the
  cold-start rule.

## Round balance

This round produced **substantive fixes**, not cosmetic ones:

- a recorded charter decision missing from the spec (the first slice);
- a data-flow hole where attached articles reach neither the update nor the unclaimed pool;
- two unbounded prompts and no merge operation;
- an agreement metric that easy `none`s can inflate;
- a brief-cost baseline from a configuration retired on 2026-08-29.

None of these depends on a hypothetical downstream consumer or re-argues D1-D8. **Do not stop
yet.** Revise, then run one more round. If that round finds only wording or downstream
speculation, stop there.

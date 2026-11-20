# Red team: rethink charter (`news-brief-lnr`)

Reviewed: `docs/superpowers/specs/2026-11-20-rethink-charter-design.md` (draft, 2026-11-20).
Stance: hostile staff engineer arguing this is the wrong thing to build. Settled decisions
(approach A picked, single user, curated outlets stay, the one-way boundary) are not re-argued.
Nothing below comes from running code. The cost figures come from the committed
`from-server/newsbrief.log` snapshot, which ends on 2026-08-29. Current host values are UNKNOWN
unless stated.

## Objection (a): a simpler design gets roughly 80 percent of the value

**Before building storylines, give the existing brief call a dated, bounded memory and keep the
thesis evidence it already writes.**

Facts from the repo:

- The brief's only memory inputs are `build_daily_prompt` (`brief.py` ~2312-2403):
  - yesterday's brief, truncated to `[:6000]`;
  - the latest weekly summary, truncated to `[:1000]` characters (about 150 words);
  - the ledger working set: `WORKING_SET_SIZE = 25` claims, TTL 7 days, or 14 for `high`.
- `render_established_block` (`brief_memory.py:525-565`) renders each claim as
  `[topic](cues) claim — driver`. **No date.** Every row carries `first_seen`, and from jx9.6
  on also `horizon_days` and `resolution_date` (`brief_memory.py:257, 333, 626-646`). The render
  drops all three.
- So success criterion 1 ("this follows from what we reported on <date>") **cannot be produced by
  construction**. Apart from yesterday, the model never sees a date for anything it reported.
  The charter diagnoses the re-surprising as a missing world model. The cheaper explanation is a
  one-day dated horizon plus undated background, and nobody has ruled it out.
- Theses already enter the brief prompt. `fetch_portfolio_weights` appends each thesis to its
  line (`brief.py` ~2223-2245), and the prompt says "flag only confirming or contradicting
  evidence". The thesis evidence for criterion 4 is therefore **already written every morning,
  then discarded**.
- `load_last_n_briefs(n=7)` exists (`brief.py:2275`). The forced-tool, separate-call extraction
  pattern exists too (signals, `brief.py` ~2723).

The simpler design, all of it inside the existing brief:

1. Render `first_seen` and `resolution_date` on every ledger row (a one-line change).
2. Feed a fixed window: the last 7 briefs' forward-looking lines, or the last 4 weekly summaries
   untruncated, instead of `[:1000]`. This is bounded by the window, not by history. The charter
   rejects only "feed every past brief back in". A fixed window is never considered.
3. A small post-brief extraction call that logs, per thesis, the dated confirm/erode lines the
   brief already wrote. That log is the thesis evidence log.

Cost: about 10-20k extra input tokens a day at batch rates, about $0.01-0.02 a day.

It covers criterion 1, criterion 4, and much of criterion 2. Links between storylines are found
by a model reading at query time. The Sonnet-with-thinking call that already sees all of these
inputs is that model. Storing links does not find them (see implicature F4). It leaves criterion 3
(event skeletons) to the workbench's interactive sessions. That is where §6 puts the depth anyway.

Even if storylines are built later, two to four weeks of this is the **baseline** they must beat.
Without it, the storylines spec has no control for its main claim.

## Objection (b): an assumed requirement, cheap attachment that makes cost scale with storylines

"Why A" rests on cost that "scales with storylines, not articles" (§6), plus the claim that the
standard pattern is "cheap embedding/trigram clustering first" (§6, supporting research). Both
assume that attaching each new item to a storyline is cheap **and** accurate. The repo measured
the cheap half on an easier task, and it failed.

- **The clustering spike** (`docs/2026-09-26-clustering-recall-spike-result.md`) measured
  cross-outlet same-event pairs captured ≤ 2 h apart. Of 671 usable pairs:

  | title trigram similarity | pairs |
  |---|---|
  | < 0.20 | 218 |
  | 0.20-0.35 | 249 |
  | 0.35-0.50 | 124 |
  | 0.50-0.70 | 46 |
  | ≥ 0.70 | 34 |
  | **total** | **671** |

  So 467/671 = 69.6% of true pairs sit below 0.35, "where unrelated headlines also sit". By
  variant, visibility at W = 2 h:

  | variant | visible |
  |---|---|
  | T (trigram) | 53.7% |
  | TE-cc (trigram + entities) | 65.4% |
  | real time | 97.2% |

  Hub entities form components of up to 223 items. Verdict recorded there: "grouping, as
  designed, cannot be built on this evidence."
- Storyline attachment is **harder** than same-event grouping. The items are different events
  days apart that share a causal thread.
- Embeddings are unmeasured. `bqa.7` (pgvector) is open and not enabled.
- "A small model", the other option in §8.1, is a per-article LLM call. That is the cost shape
  the charter is escaping.
- **The proposed instrument cannot measure attachment either.** §9 reuses the `vd9` labelled
  windows as the attachment test set. Spec `2026-09-28-gold-set-labelling-design.md` §11 records
  what they actually hold:
  - labels for confirmation **within 6 hours only**, by operator ruling;
  - two recorded windows: 85 + 267 = 352 items, 8 + 0 = 8 groups, of them 4 + 0 = 4 multi-outlet;
  - window 2 labelled at about 1.6 s/item, so its 0 is a recorded floor;
  - of the 2,549 merged cross-outlet pairs, 750 + 312 + 164 = 1,226 (48.1%) are more than 6 h
    apart, and 67.1 points of split share in total.

  Whether windows 3 and 4 were labelled is UNKNOWN: nothing is recorded after 2026-10-10, and
  `vd9.14` is open. The charter parks `vd9` by deferring `vd9.14`, which is the gate for
  labelling more.

Before sub-project 1 is chartered as "the core", it needs a pre-registered recall measurement of
cheap attachment on multi-day storyline pairs. That needs a new labelled set, not the census
windows.

## Objection (c): the constraint this collides with in 6 months, the £10/month retail floor

Criterion 6 and §5 require the core to run at or under £10/month at retail prices. The existing
brief already spends most of that before a single storyline exists.

- **Daily brief batch:** 65 runs at 06:00 in the snapshot, 2026-06-25 to 2026-08-29. Means: input
  170,217 tokens (min 86,406, max 314,795), output 5,909, web searches 5.2.

  Re-priced at Sonnet 5 batch, I get $0.252 a day, **$7.56 per 30 days**. Assumptions behind that:
  - Batch is assumed to be 50% of the $2/$10 list price in `comprehend.PRICES_PER_MTOK`, which
    gives $1/$5 per MTok. The code's own Sonnet 4.6 comment uses the same ratio.
  - Searches cost $0.01 each.
- **Signals call:** 59 runs, real-time Sonnet 5, $0.88 per 30 days. It may go if sub-project 0
  parks signals.
- **Weekly summary:** about $0.02 a week.
- **UNKNOWN, not logged with usage in the snapshot:** the ledger reconcile call, claim-verify, and
  watch-job model calls.
- At an assumed $1.30/£, £10 ≈ $13. The remainder is at most $13 − 7.56 − 0.88 − 0.08 ≈
  **$4.5 a month, about $0.15 a day**, before the unknowns. That has to cover storyline updates,
  link-finding, skeletons, and query-time reasoning.

What that remainder buys:

- About 10 storyline updates a day on Sonnet 5 real time, at roughly 4k in / 0.6k out ≈ $0.014
  each. Batch doubles that, but moves link updates hours later.
- On Haiku 5.5 ($0.10/$0.50), volume fits. But the one repo measurement of a Haiku-tier model on
  link decisions is M2 (`y1x`): Haiku 4.5 agreed with Sonnet on **55.1%** of link decisions,
  against Sonnet's **76.6%** agreement with itself. Haiku 5.5 is unmeasured. Which way the
  departures go is bead `sse`, and §9 **supersedes `sse`**.

So the floor pushes links, the capability A is chosen for, onto the model tier the repo measured
worst at links. It also closes the bead that would tell how that tier fails.

The other way out is for the brief to stop reading about 170k tokens of raw material and render
from storyline state. Then orientation, the "keep this" value in §2, depends on attachment recall.
Objection (b) shows that recall is unestablished. §8.1 lists "how the brief consumes storyline
state" as a decision but does not name this coupling.

The trigger in six months is "the free allowance goes, turn the dial down". The charter promises
that is a configuration change, never a redesign (§5), but the brief has no dial today. Nothing in
§8 builds one.

## FIXED CHECK: two facts side by side, and the third claim a reader infers

Each entry gives the pair, the inferred claim, and whether the pair establishes it.

**F1 (§1, bullet 1). FLAGGED.**
- The pair: "It reads every captured item, so its cost grows with news volume" + "there is always
  more news than budget / items age out".
- The inferred claim: per-article reading cannot fit the budget at any price, so it must be
  replaced.
- Repo facts:
  - The £15/day incident was input volume: Reuters quote pages, 78% of the corpus, drained on
    Sonnet. Per item it was $0.0027 (`newsbrief-comprehend-cost`).
  - Haiku 5.5 is priced in code at $0.10/$0.50, 20× below Sonnet 5's $2/$10, but triage was never
    flipped to it (`c5k`, deferred).
- The most favourable fill: at the historical dense rate of about 42 material items an hour
  (about 1,000 a day, inflated by quote pages), 1,000 × $0.0027 / 20 ≈ $0.14 a day ≈ $4 a month.
- **Unsupported on cost.** What is open is quality: link agreement of 55.1% vs 76.6%, measured on
  Haiku 4.5. The sentence should say that.

**F2 (§6 C). FLAGGED.**
- The pair: "the ledger stores facts, not links" + "so it cannot find second-order connections".
- The inferred claim: a store that holds links (storylines) will find them.
- Neither fact establishes that. Connections are found by a model reading the inputs. The brief
  call already reads the ledger, yesterday and the theses. Storage determines what can be
  *recalled* later, not what is *found*. This is the hinge of objection (a).

**F3 (§6, supporting research). FLAGGED.**
- The pair: the per-article KB is "most expensive and most commoditised" + the "standard
  cost-bounded pattern is cheap embedding/trigram clustering first".
- The inferred claim: that pattern works on this corpus.
- **Contradicted for trigram** by the spike (T 53.7% vs RT 97.2%; 69.6% of true pairs below 0.35
  similarity). **UNKNOWN for embeddings.** See objection (b).

**F4 (§7, storylines bullet). FLAGGED.**
- The pair: "storylines with nothing new go quiet" + "Memory is bounded by the number of live
  storylines".
- The inferred claim: memory is bounded.
- "Quiet" is not "retired", and the number of live storylines has no stated bound. The precedent
  in this repo is the claim ledger. Its prompt is bounded (25 claims), but bead `6wc` records that
  "ledger storage now grows without bound: non-standing claims never retire". The prompt bound
  and the storage bound were conflated once already. Anti-goal 2 needs both bounds stated
  separately.

**F5 (§8.0). FACTUALLY WRONG, not merely unsupported.**
- The pair: "The operator's own null result argues against short-horizon news-driven signals,
  and most are shorts an ISA cannot take" + "nobody has looked at the results yet".
- The inferred claim: whether the paper trades work is an open question, so measuring them
  belongs first.
- `docs/2026-08-16-trading-retrospective.md` already did sub-project 0's exact cut on 126 closed
  equity/crypto positions, 2026-06-01 to 2026-08-13:

  | cut | n | hit |
  |---|---|---|
  | all | 126 | 39.7% |
  | bullish | 61 | 41.0% |
  | bearish | 65 | 38.5% |

  Its conclusion: "the direction itself is wrong". Single-leg episodes: hit 50.0%, p = 1.000.
- "Most are shorts" was 65/126 = 51.6% at that date. The current split is UNKNOWN.
- Sub-project 0 is at most a **refresh** on post-2026-08-16 data, with a strong prior already
  recorded. The charter should cite it and pre-register what result would *keep* the signals.

**F6 (§9, `vd9` row). FLAGGED.**
- The pair: "reuse its labelled windows as the test set for storyline attachment" + "The labels
  are an asset".
- The inferred claim: a usable labelled set for attachment exists.
- Recorded: 2 windows, 352 items, 4 multi-outlet groups, all from window 1. Window 2's 0 is a
  floor. Labels cover within-6 h confirmation only, and 48.1% of merged pairs are more than 6 h
  apart. **Unsupported.** See objection (b).

**F7 (§1 bullet 3 with §3.1). Not flagged; the pair holds.**
- The pair: "The brief repeats itself / re-surprises" + "feeding every past brief back in is
  ruled out: it grows without bound".
- The inferred claim: continuity needs a new memory structure.
- The pair does not exclude a fixed window. Both facts are operator testimony and are fine as
  such. The inference is what objection (a) contests.

### Residuals, decomposed

| residual | parts | sum |
|---|---|---|
| spike pairs | 218 + 249 + 124 + 46 + 34 | 671 ✓ |
| census merged pairs | 417 + 906 + 750 + 312 + 164 | 2,549 ✓ |
| census split points | 1.0 + 18.1 + 29.4 + 12.2 + 6.4 | 67.1 ✓ |
| census > 6 h | 750 + 312 + 164 | 1,226 = 48.1% ✓ |
| census items | 85 + 267 | 352 |
| census multi-outlet groups | 4 + 0 | 4 |
| trading closed positions | 61 bullish + 65 bearish | 126 ✓ |

Budget, at an assumed $13:

| line | $/30 d |
|---|---|
| brief batch | 7.56 |
| signals | 0.88 |
| weekly | about 0.08 |
| ledger reconcile, claim-verify, watch-job calls | UNKNOWN |
| remainder | ≤ about 4.48 |

The budget residual is an upper bound, not a sum, until the UNKNOWN lines are measured.

## Smaller points

- §9 supersedes `sse`, but objection (c) shows `sse`'s question is exactly the one the floor
  forces. Keep it, or restate it under sub-project 1.
- §3.6 says "the core". Name which line items are in the core (brief, signals, ledger reconcile,
  watch) so the dial in §5 has something to turn. Today the brief, the largest item, has no dial.

# Red team, round 2: storylines, the editor (2026-11-24 spec, revision 2)

Reviews `docs/superpowers/specs/2026-11-24-storylines-editor-design.md` revision 2 ("the spec"),
against the 11-21 spec (revision 5), the charter, round 1
(`2026-11-24-storylines-editor-redteam.md`), the beads `al7.1`, `al7.3`, `al7.6`, `al7.7`, and the
code. Brief: find why this is the wrong thing to build.

Settled by the operator and not re-argued here: the pull editor with two batched passes, both
feeders from the start, embeddings on Modal with an OpenAI switch, quality first with no trial
steps, and pricing in a pass after approval. Objection 3 touches cost only where the spec's own
premise for that pass is false.

Evidence labels: **measured** (I ran it), **recorded** (a bead, memory or doc says so; not
re-checked), **estimate** (my arithmetic, assumptions stated), **UNKNOWN**.

## The three objections

### 1. The gate assumes "mattered" means "two outlets carried it", and its look-ahead has no selector of its own (b)

Revision 2 fixed round 1's selection problem (the judged pool was the editor's own retrieval) by
swapping in a different selector, outlet count, and left the judge's evidence unspecified.

**The pool leaves out where this outlet set is distinctive.** §15.2's pool is "every group with at
least 2 outlets, plus every primary item", and the replay runs without primary sources (§6.3,
§15.1). So in the gate, the pool is exactly the groups that at least two outlets carried.

- On this outlet set, corroboration is the exception. **Recorded** (memory
  `analysis-stats-traps`, trap 6–7; bead `bqa.19`): 6.6% of comprehension events carried
  assertions from 2 or more outlets (base rate drifting 6.64 → 6.43%). That matcher caught only
  about 12% of true duplicates, and the bead's ceiling estimate is about 9–30%. Embedding grouping
  will do better, but by how much is UNKNOWN. Even at the ceiling, most groups are single-outlet
  and are never judged.
- **Single-outlet by nature.** I count 13 of the 25 outlets as analysts, think tanks or official
  sources (my classification, from `brief.py:150-420`): the three analysts' X feeds, Sinica,
  Un-Diplomatic, Observing Japan, Pinecone, Intersubjectively Transmissible (outlet "Jacob
  Shapiro"), ISW, 38 North, Bank of Japan, EIA, Mining.com and OilPrice. Charter §2 says the
  curated outlet set "is part of the value". A 38 North analysis or a BoJ statement reported by
  nobody else is exactly where a storyline document earns its keep, and the gate can count it
  neither as a miss nor as noise.
- **Every ranker in the design uses outlet count as importance.** The candidate ceiling keeps "by
  outlets, then similarity" (§7.3), Top Stories ranks by outlets (§10), bursts need 3 outlets
  (§11), and the judge pool needs 2. On a busy night the single-outlet specialist group is cut
  first, and the gate cannot see that cut.
- **Noise is computed over an unstated subset.** Cited groups = cited ∩ pool (judged) + cited ∖
  pool (single-outlet, never judged). §15.3 defines noise as "of cited groups, the share ruled
  *didn't matter*". Single-outlet citations have no ruling. Either they leave the denominator (so
  noise covers only corroborated citations) or they count as not-noise (which deflates noise). The
  spec says neither. That matters because §5.2's targets include "a new [open question] raised"
  and any supports/weakens mark. The validator checks that a target is well formed, not that it is
  substantive, so the noise score is the only check on substance, and it does not reach
  single-outlet citations.

**The judge's look-ahead has no selector.** The judge rules after seeing "that storyline's next
21–30 days of news" (§15.2). Nothing in the spec says what picks that news. I grepped for
"look-ahead", "next 21", "storyline's next" and "independent", and the spec's only other mention
(§15.1) gives the look-ahead's length, not its selector. A month of captured news (about 790
items a day, **recorded** from `al7.3`: 5,547 items scored over a 7-day week) does not fit one
call, so something must select it. The candidates are retrieval, attach or the editor's later
citations, and those are the three things the replay scores. If retrieval selects it, a group
whose consequences retrieval does not place near the storyline looks like it "didn't matter". That
is round 1's the-filter-presupposed-the-answer, moved from the pool to the evidence. The heading
"independent of the editor's retrieval" holds for the pool only.

**The storyline set is hindsight-informed (round 1, partly fixed).** The 14-day first run rebuilds
each document's state. But the storylines' names and descriptions, the linked theses and the
watch triggers are today's, re-seeded after the window. A storyline exists in the replay *because*
its situation mattered by 11-24.

**What would answer it.** Put single-outlet groups in the pool, either all of them or a stratified
sample with the share stated, and report recall separately for 1-outlet and ≥2-outlet groups.
Define the look-ahead's selector as something none of the three feeders uses, for example every
group with ≥ 2 outlets in the look-ahead plus the storyline's description, judged by title. Define
noise's denominator. State that the storyline set is post-window, as a known bias.

### 2. The judge is the editor's model, so the regression test's ruler moves with every change it tests (b, with a 6-month collision)

Round 1 raised this. Revision 2 did not fix it. §18's `STORYLINE_EDITOR_MODEL` still covers "both
passes, mid-day runs, the hindsight judge". Revision 2 split the alert model off that row and left
the judge on it.

- **The cost pass's main decision is invisible to the gate.** §7.2 picks the editor's model in
  the cost pass. Comparing Sonnet against Opus on the replay compares a Sonnet-judged score with
  an Opus-judged one. "Opus scored higher" would be partly "the Opus judge rules differently".
- **§15.5 compares across a moving ruler.** "Every change to the editor's prompt, model, feeders
  or retrieval reruns the replay ... and reports before and after." The judge never sees the
  editor's citations (§15.2), so its rulings do not depend on the editor at all. Yet the spec
  re-derives them on every rerun, with a model that changes whenever the editor's does.
- **6 months.** Model churn is already the norm here: `xqr` moved the seed to Sonnet 5.5 this
  week, and `c5k` re-points attach to `claude-haiku-5-5`. If the allowance goes, charter §5 makes
  the dial-down "a configuration change, never a redesign". The cheapest dial-down, a Haiku
  editor, also makes the judge Haiku. And the §15.4 audit (30 taps at ≥ 80%) was earned by a
  different model, and the spec does not say it is redone.
- **The audit's power (round 1, unfixed).** A pass at 24/30 has a Wilson 95% interval of about
  0.63–0.90 (**estimate**, z = 1.96). A judge that is right about two times in three passes.
- **Correlated bias (round 1, unfixed).** The only qualitative evidence in the record (§1: the
  assistant's reads, n = 60) found the stronger model attaching "human-interest pieces, opinion
  and passing mentions". A judge from that family rules those *mattered*, and the noise score
  inherits the editor's own bias.

**The simpler design:** rule once, freeze, reuse. Give the judge its own pinned knob
(`STORYLINE_JUDGE_MODEL`), compute rulings once per replay window, store them with the model and
prompt hash, and score every rerun against the stored rulings. The comparison then holds across
editor changes, the audit holds as long as the judge is unchanged, and the judge's cost is paid
once per window instead of once per rerun. A judge change becomes its own, rare, re-audited event.

### 3. The cost unit is wrong: priced at 20 storylines a night, built to run every live storyline nightly, with the ceiling knob removed (c)

This is not "it is expensive", which the operator settled for a later pass. The premise under that
pass is false. §21 says the pass prices the design "against the floor", using dials. But the run
count the estimate assumes is not one the design produces, and the dial that would produce it has
no knob.

- **The predecessor said the set settles at the bound, and priced it there.** 11-21 §5.2: "Expect
  the set to settle near the bound, since only the operator shrinks it. So the design is **priced
  at the bound**", which is 60, "and the nightly update has its own daily ceiling: **at most 20
  storyline updates a day**" (`STORYLINE_MAX_UPDATES_PER_DAY`). Revision 2 §20 says "the
  20-a-day update ceiling becomes a cost dial (§21)". §18 has no knob for it. I checked every row:
  `STORYLINE_MAX_LIVE` 60 stands, and there is no runs-per-night setting.
- **Every live storyline gets a run every night.** §7.1 runs "every storyline with at least one
  candidate", active **and** dormant. Revision 2 moved dormant storylines from weekly to nightly,
  which fixed round 1's dormancy point. The attach feeder alone puts about 0.35 × 790 ≈ 277
  items a day on some storyline (**recorded** rate `al7.3`, H = 0.35; **estimate** of the
  product). That is about 7 a night each at 40 storylines, before retrieval, which has no stated
  relevance floor. So "with at least one candidate" filters almost nothing.
- **Dormancy rarely fires, so the set ratchets.** Dormant means "no cited group in 14 days"
  (§12), and a citation needs only a well-formed target, a raised question included (§5.2).
  Proposals come from bursts and recurring unclaimed groups. Retirement is only *offered*, and
  only after 60 dormant days. The seed alone may propose up to 40 (`storylines.SEED_MAX = 40`,
  covering every pin and thesis). The live count on the host is UNKNOWN.
- **The arithmetic, using the spec's own per-run figures.** $45 (Sonnet) or $90 (Opus) a month for
  20 storylines is $2.25 or $4.50 per storyline-month, nightly batch only (**estimate**, scaled
  linearly):

  | Live storylines | Sonnet 5.5 | Opus 5.5 |
  |---|---|---|
  | 20 (§21) | $45 | $90 |
  | 40 (`SEED_MAX`) | $90 | $180 |
  | 60 (the bound) | $135 | $270 |

  Not included: mid-day runs (real time, about twice the batch price), alert checks, the brief
  ($9 or $19), embeddings ($10–15, inside a second free allowance, Modal's credit) and replays
  (each about one month of editor spend, §15.5). The floor is £20, about $25 (charter §3.6). Even
  the 20-storyline Sonnet line is 1.8× the floor on its own.
- **The dial, if it existed, fails silently.** A ceiling means some storylines are not updated.
  §7.5 renders them "from [their] last state". Pinned ones go first, so the tail goes stale
  without a line in the brief that says so. Memory `fail-closed-needs-status-not-count`: a skipped
  run needs a status where the operator looks.

**What would answer it:** restore `STORYLINE_MAX_RUNS_PER_NIGHT`, with an order (pinned, then new
mattered-looking candidates) and a "not updated since <date>" line in the brief. Price §21 at the
bound, as 11-21 did. Give retrieval a relevance floor, so that "with at least one candidate"
means something.

### On (a), a simpler design

I found no (a) objection that survives the settled list. Every 80%-for-less design I tried either
re-argues a settled point (a single call instead of two passes; no attach feeder) or is
plan-level detail. One example is the second: storing each run's document as a single versioned
JSONB row plus a citations table, instead of about 15 normalised tables. The two
genuinely simpler moves left are the two fixes above: freeze the judge's rulings (2), and restore
the per-night ceiling (3).

## Fixed check: facts set side by side

Each pair, the third claim a reader will infer, and whether the pair supports it. Residuals are
decomposed until they sum.

**I1. §6.2: "thin | 129 | Reuters 14, NHK, Yonhap, ISW, 38 North, Bank of Japan ..." +
"Consequence: full text helps ... none of the main wires." FLAGGED, and the most consequential
one.** Inferred: these outlets' article pages yield little text. The pair does not establish it,
because for six of these outlets the probe probably never reached the outlet.

- Those six outlets (Reuters, NHK, Yonhap, ISW, 38 North, Bank of Japan) are exactly the Google
  News proxy feeds (`brief.py:174-320`; Reuters has two feeds and one outlet).
- **Measured 2026-11-24, on the dev machine, using the probe's own functions.** I took three
  current links each from the ISW, Reuters World and Yonhap proxy RSS. `google_news_target`
  decoded **0 of 9** (current ids are the opaque kind). Every fetch ended on
  **consent.google.com**, status 200, and `classify` called each one **"thin", at 925
  characters**. `classify` reports `google_unresolved` only when the final host is exactly
  `news.google.com`, so the Google consent wall is counted as the outlet being thin.
- The host's numbers have the same shape: almost every one of those outlets' items is "thin", and
  only 1 is "Google link unresolved". A host outside a consent region would have shown them as
  unresolved instead. The host's location is UNKNOWN.
- So the spec's caveat, "thin is an upper bound on failure (a site that builds paragraphs
  differently reads as thin)", is wrong for these rows: they are not a parse failure. And §6.2's
  remedy ("a real extraction library ... the thin outlets it rescues count as usable") cannot
  rescue a consent page.
- Whether the wires' own pages give text is **UNKNOWN**. Reuters may still block bots. "None of
  the main wires" holds for the *stored links*, not for the wires.

Decomposition of the 286. Of the six outlets with a known count: SCMP 15 and Times of Israel 15
(blocked), IRNA 15 (empty), and Reuters, TASS and IranWire 15 each, which is 90. The other 19
outlets hold 196 items: 104 usable plus 92 thin. Had each of the 19 given 15 items, that would be
285, so the shortfall is 89. Those are low-cadence outlets with fewer than 15 items in 7 days. So
"the sample is equal per outlet" is false: it is *capped* per outlet. Items whose page was never
reached: Reuters 15 plus the five other proxy outlets' items, which are an unstated part of the 89
thin items left after Reuters, TASS, IranWire, Al Jazeera, Meduza and Un-Diplomatic. The fix is to
rerun the probe printing the final hostname per category (counts only, so it stays private).

**I2. §1: "cut disagreements by 120 of 1,050 (11%), all outside the largest bucket (Haiku attached
nothing; 540 both times)." FLAGGED.**

- The arithmetic sums. Run 1: 540 + 390 + 120 = 1,050. Run 2: 540 + 278 + 112 = 930. The change
  is 0 − 112 − 8 = −120.
- **The gloss is wrong.** `storylines.differ_kinds` (`storylines.py:458-467`) defines `less` as
  "h a proper subset of s". That includes Haiku attaching 1 storyline where Sonnet attached 2. It
  is not "Haiku attached nothing".
- **The inference is unsupported.** "540 both times" invites "the same 540 items, so Haiku's
  shortfall does not depend on the seeds". An equal count over different storylines is not an
  equal set, and the check never compared membership.

**I3. §21: "Editor ... about $45 a month on Sonnet 5.5, $90 on Opus 5.5 ... The multi-turn loop
was $186–588 ..., which is why it is out." FLAGGED.** Inferred: the chosen shape fits the budget.
It fits neither bound as stated:

- Against the ~$100 ceiling: Opus $90 + brief $9–19 + embeddings $10–15 = $109–124, over the
  ceiling before mid-day runs, alerts and replays.
- Against the £20 floor (about $25): Sonnet $45 is 1.8× it, editor alone.
- The 20-storyline premise: see objection 3.

Same family, in the header: "the editor's shape sets cost by 10–40×". The spec's own figures give
2.1–6.5× on the same model (Opus: 186/90 to 588/90), or 4.1–13× across models (186/45 to 588/45).
Neither range reaches 40×.

**I4. §7.1 "every storyline with at least one candidate ... gets a run" + §21 "20 storylines a
night". FLAGGED.** Inferred: about 20 runs a night. Neither fact establishes it (objection 3). The
run count is the number of live storylines, active and dormant.

**I5. §15.2: the heading "The hindsight judge is independent of the editor's retrieval" + "that
storyline's next 21–30 days of news". FLAGGED.** Inferred: the judgement is independent of the
system it scores. That holds for the pool only. The look-ahead has no stated selector (objection 1),
and the judge's model is the editor's (objection 2).

**I6. §6.2: "12 [outlets]" usable + "full text helps for about half the outlets".** Not flagged
(minor). 12 of 25 is 48%, but two of the twelve are TASS at 1 of 15 and IranWire at 6 of 15. The
outlets where most items are usable number 10 of 25 (40%).

**I7. §16: "the 11-21 spec's batches have finished in minutes".** Not flagged (minor). The source,
11-21 §6.1, is about the *brief's* batches, one request each. The storyline nightly batch has
never run. Two dependent batch submissions in 3 hours are probably fine, but that has not been
measured.

**I8. §6.1 "no model call per article (the retired comprehension step is not coming back)" vs
§7.3 attach.** Not a pair, a contradiction in wording. The attach feeder is Haiku reading every
article every 30 minutes. That is settled; say "no model call per article *for grouping*".

**I9. §1: "60 of the 1,980 disagreements".** Not flagged (minor). The 1,980 counts the same
week's items twice (1,050 + 930). The per-run shares read are 40/1,050 = 3.8% and 20/930 = 2.2%.

## Did revision 2 fix round 1?

| Round 1 point | Status |
|---|---|
| Obj 1: §1's premise (the gate failed, not push) | **Fixed.** §1 now says so; attach kept as a feeder; the replay scores feeders |
| Obj 2: one-shot pull would do | Overtaken by the operator's two-pass decision (settled) |
| Obj 2: judged pool selected by the editor's own retrieval | **Fixed, and replaced** by an outlet-count selector (objection 1) |
| Obj 2: the judge shares the editor's model | **Not fixed** (§18; objection 2) |
| Obj 2: 24/30 audit, Wilson 0.63–0.90 | **Not fixed** (§15.4 unchanged) |
| Obj 2: storylines know the window | **Partly fixed.** State is rebuilt by a 14-day first run; names, descriptions and theses are still today's |
| Obj 2: dormant storylines are misses by construction | **Fixed** (dormant run nightly), at the cost in objection 3 |
| Obj 3: loop cost | **Fixed** by the two-pass batch shape |
| Obj 3: "price one real editor run on the host before approval" | **Not done.** §21 is still all estimates |
| Obj 3: the replay is unaffordable as a regression test | **Deferred** to the cost pass; the frozen-rulings fix is not adopted |
| Obj 3: wall clock; a silent stale tail | **Partly fixed.** Batching removes the runtime risk; the silent fallback (§7.5) stands |
| F1 hand reads / "Haiku fell behind" | **Fixed** ("the assistant's reads", "Haiku improved by 0.04") |
| F2 "much of the disagreement" | **Fixed** ("unmeasured", the 11%), but see I2 |
| F3 phase-1 baseline | **Fixed** (the briefs actually delivered, stated as pre-phase-1) |
| F4 §20 missing §1–§2, D4 dropped | **Fixed** (D4 kept as consensus) |
| F5 bodies capped; the Google links | **Fixed** in the text; the probe then repeated the Google problem (I1) |
| F6 the design lands between floor and ceiling | **Not fixed** (I3) |
| F7 primary sources absent from the replay | **Fixed** (stated as a known gap) |
| F8 count rows before a drop | **Fixed** |
| F9 `6wc`, the clean-up bead, `uk1` | **Fixed** (§23, §17) |
| Mark counts read as odds | **Fixed** (§8: dated lines, never counts) |
| Dormancy starves itself | **Fixed** |
| §7.4 "429 is a 4xx" | **Fixed** ("any other 4xx") |
| Labelling creep | **Partly fixed.** ≤ 5 a day, but 50 taps before launch, and repeated after each judge fix |

## Other findings

- **Alerts track news volume.** §11 runs a model check on "every group the attach feeder puts on
  a storyline", which is about 277 items a day (**estimate**, as in objection 3). Each check is
  against attach's pick plus the 3 nearest storylines, every 30 minutes, in real time. That is
  charter §1's founding complaint ("its cost tracks news volume") again, in plan 4. Trigger on
  the signpost or calendar hit, not on every attachment.
- **T4 against 4B–8B.** §21 prices Modal on a T4, which has 16 GB. An 8B model in fp16 is about
  16 GB of weights alone. The upper end of §6.1's range needs quantisation or a larger, dearer GPU.
- **The judge sees less than the editor.** It sees titles and outlets. The editor sees summaries
  and full text.
- **The spec's own example primary source** ("BoJ policy statements (RSS)", §5.1) is today a
  Google News proxy (`brief.py:318-323`), so I1 applies to it.

## Round balance

**Stop. Do not run round 3 on the whole spec.** Round 1 found design-level defects: a false
premise, a loop that could not batch, and a gate selected by the editor's own retrieval. This
round finds no reason the design is the wrong thing to build. It finds four specific defects, each
fixable by editing a paragraph:

- **For plan 2's gate (§15):** the pool's outlet filter, the look-ahead selector, the noise
  denominator, and a separate frozen judge.
- **For the cost pass (§21, §18):** price at the bound, restore the runs-per-night knob with a
  stale line in the brief, and a relevance floor on retrieval.
- **For plan 1's probe rerun (§6.2):** record the final host, and resolve the Google links before
  judging the wires.

None of them is a hypothetical downstream consumer or a settled decision re-argued. Two (the judge
and the cost unit) carry over from round 1 unfixed, which is reason to apply them as a checklist
rather than to review again. Apply them to the spec, then check §15's new text in plan 2's own
review.

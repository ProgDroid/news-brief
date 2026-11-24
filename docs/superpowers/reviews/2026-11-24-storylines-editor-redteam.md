# Red team: storylines, the editor (2026-11-24 spec)

Reviews `docs/superpowers/specs/2026-11-24-storylines-editor-design.md` ("the spec"), against
the 11-21 spec (revision 5), the charter, and the record in beads `al7.3`, `al7.6` and `al7.7`.
The brief: find why this is the wrong thing to build. Cost alone is out of scope (§21 defers it);
a cost that makes the design unworkable at any reasonable budget is in.

Evidence labels: **measured** (I ran it or the record holds the number), **recorded** (a bead
note or doc says so, not re-checked), **estimate** (my arithmetic, assumptions stated),
**UNKNOWN**.

## The three objections

### 1. The revision's premise is false: the record shows the gate failed, not the push design (b)

§1 says two things were wrong "and no rerun or bar change fixes either", and that "underneath
both: an attacher ... decides relevance for a consumer it is not. The fix is to let the
consumer decide." The record does not support that diagnosis.

- **The consumer already decided in the push design.** 11-21 §6, "Correction in the nightly
  pass": the nightly update "may reject an attachment"; a rejected article returns to the
  unclaimed pool. D5 itself was "Haiku attaches ... Sonnet ... corrects attachments in the
  nightly pass". The attacher proposed; the consumer disposed. The only thing the push design's
  consumer could not do is see an article Haiku did not propose: a **recall** question.
- **The measured evidence favours the component being retired.** `al7.3` notes: run 1,
  J(S1,S2) 0.83, J(H,S1) 0.61 vs bar 0.73; run 2, J(S1,S2) 0.88, J(H,S1) 0.65 vs 0.78. Hand
  reads: H right 25 / S1 8 / unclear 7 (n = 40), then H 11 / S1 3 / unclear 6 (n = 20, marked
  "agent's reading", not the operator's). Where they disagreed, Haiku's attachment was the better
  one roughly three times in four on the decided samples (25/33, 11/14). Rates: H attached 0.36
  and 0.35 against S1's 0.38 and 0.40, inside the rate bar both times. Nothing in the record
  shows Haiku-push *missing* a development; nothing measured its recall at all.
- **Item 2 is fixed by exactly a rerun.** "The input was known to be bad" (seeds named after
  theses or joined with "and"). The fix is a clean re-seed and a rerun. `al7.6` notes say the
  re-seed was still thesis-shaped ("Needs a prompt fix before plan 2b"); commits `907f172` and
  `27d1f5b` are that prompt fix. **A rerun on clean seeds has never been run**, so "no rerun
  fixes either" is asserted, not observed.

So the evidence establishes one thing: a gate that scored Haiku against a noisier reference was
the wrong gate. The cheap next step it points to (keep Haiku-push, replace §8.2 with a precision
read of Haiku's attachments and a recall probe on a hindsight sample) was skipped for a design
whose three load-bearing parts (the tool loop, `read_article`, the hindsight judge) are all
unmeasured. The operator's choice of pull is not re-argued here on taste; it is argued because
§1's stated reason for it, "a relevance decision made by something that does not use it", does
not describe D5.

What would answer it: run the existing §8.2 machinery once on clean seeds, and score Haiku's
attachments by the same §15.2 hindsight judge the spec proposes. If Haiku-push's recall
against "mattered" is close to the bar the spec proposes (0.8), the pull editor buys little at
a much higher price (objection 3).

### 2. A one-shot pull editor gets most of the value, and the spec's own replay cannot see the difference (a)

The real advance in this spec is §5.2: a citation must name the document target it changed, and
the validator checks it. That, the richer document (§5.1) and pull itself (the consumer sees the
candidates) do not need a 12-turn tool loop. The simpler design missing from §4's table:

> Per storyline, retrieval (the same §7.5 hybrid query over open questions, actors,
> constraints, signposts) returns the top-K groups with their member bodies; **one forced-tool
> call** reads the document plus those groups and calls `write_storyline` with target-required
> citations. Batchable (50% off; the spec rules batch out only because of the loop, §7.2), runs
> only for storylines with new nearby groups, no real-time window to fit.

What the loop adds over that, piece by piece:

- **`search_news`.** The model writes its queries from the same document the starting list was
  retrieved from. Re-querying the same index with paraphrases of the same fields is mostly the
  starting list again. Its distinct value is following a lead the starting list exposed, which
  a larger K approximates.
- **`read_article`.** The one genuinely new input, and for the feeds where text is thinnest it
  probably returns nothing. Capture stores the feed entry's `link` (`brief.py:1835`). For the 7
  Google News proxy entries in `RSS_FEEDS` (`brief.py:174-320`, both Reuters feeds among them)
  that link is a `news.google.com/rss/articles/CBMi...` redirect. **Measured, one sample,
  2026-11-24:** a browser-UA GET of the first Reuters World item returned HTTP 200, 610 KB,
  `<title>Google News</title>`, and zero occurrences of "reuters" (control: "google" on 8
  lines). Reuters' own pages are also bot-protected (the repo already met 403s to non-browser
  agents, `source-fetch-failure-modes`). Decoding Google's redirect is possible but undesigned
  and fragile. So the tool most likely to justify the loop is weakest exactly on the wire
  copy the spec calls thin (§6.2). UNKNOWN for the other feeds until measured.
- **`history(n)`.** Earlier states fit in the one-shot prompt; the document is capped by design.

**The spec's gate cannot measure the loop's marginal value.** §4 sells pull because "misses move
to search, which can be measured". §15.2 judges only "each group near a storyline (vector top-k
over the window)". A group that mattered but that vector retrieval does not place near the
storyline is never judged, so it can never count as a miss. The judged population is selected by
the same retrieval family that feeds the editor (§7.3, §7.5): the-filter-presupposed-the-answer.
Recall as defined is recall *within what retrieval already found*, which is precisely the regime
where a one-shot call over top-K and a 12-turn loop look alike. Compounding it:

- The judge is the editor's model (`STORYLINE_EDITOR_MODEL` covers "editor, alert checks,
  hindsight judge", §18): correlated judgement. The hand reads that triggered this revision found
  the stronger model systematically over-counting relevance (opinion, passing mentions); a judge
  with that bias inflates "mattered" on the editor's own noise.
- The judge audit (§15.4) passes at 24/30 agreements. A Wilson 95% interval on 24/30 is about
  0.63–0.90: a pass is consistent with a judge right two times in three.
- Storylines in the replay are today's, seeded with knowledge of the window's events, and §12's
  first run "reads the last 30 days". Unless the replay rebuilds each document from an empty
  state at day 0, the starting document already knows the window's outcomes. The spec does not
  say which.
- Dormant storylines run weekly (§7.1), yet recall counts a citation "within a day of first
  seen" (§15.3): every judged group near a dormant storyline is a miss by construction, unless
  dormant storylines are excluded, which the spec does not say.

Net: build the one-shot pull editor first, extend §15.2's judged population beyond the
editor's retrieval (for instance, every group with ≥ 3 outlets in the window, judged against
every storyline's description), and add the loop only if that replay shows misses the loop
recovers.

### 3. In six months this collides with the £20 floor and with the charter's "dial, never a redesign" (c)

Not "it is expensive": the claim is that no setting of §21's dials keeps this design while
landing under the floor, and that its own quality gate costs more than the monthly ceiling to run
once.

**Prices** (measured, platform docs 2026-11-24): Opus 5.5 $4 / $20 per MTok, cache write $5,
cache read $0.20; real time only, because the loop cannot batch (§7.2).

**Per editor run** (estimate). Start context ~13k tokens (document, 15 timeline lines, calendar,
theses, triggers, a 60-line index, a starting list, tools and system). Each turn adds tool results
(a group ~1k, an article 2–4k, a search ~2k) and 1–3k of adaptive-thinking output, which is
re-sent as input on the next turn.

| Case | Turns | Final context | Output | Cache reads | Cache writes | Output cost | Per run |
|---|---|---|---|---|---|---|---|
| Low | 6 | 25k | 8k | 110k → $0.02 | 25k → $0.13 | $0.16 | **≈ $0.31** |
| High | 12 | 60k | 30k | 420k → $0.08 | 60k → $0.30 | $0.60 | **≈ $0.98** |

**Per month**, nightly only (mid-day, dormant weekly, first 30-day runs, alert checks every
30 min on the same model, and the brief all excluded):

| Active storylines | Low | High |
|---|---|---|
| 20 | $186 | $588 |
| 60 (`STORYLINE_MAX_LIVE`) | $558 | $1,764 |

Against: the floor, £20 ≈ $25 (charter §3.6, the operator's decision), of which the brief
already takes ~$9 (memory, 2026-11-23) or ~$19/30 d (`al7.3` note: `brief_cost.py` over 82 days,
"contradicts the ~$9/month ... correction pending"; UNKNOWN which). The ceiling is ~$100. Even the
low case at 20 storylines is ~1.9× the ceiling.

**The dials do not close it without a redesign.** Turning model to Sonnet 5.5 ($2/$10) halves
it to ~$93–294 at 20: the whole ceiling at best, before the brief. Haiku 5.5 ($0.10/$0.50, ~1/40 of Opus) gets ~$5–15/month at 20,
the only tier near the floor. But then:

- The design and its gate are calibrated on Opus. §15.5 says every model change reruns the
  replay, so the cost pass's main cut is the one change that invalidates the approval.
- A Haiku editor is the Haiku attacher with more tools, which is objection 1's alternative
  arrived at by the long way round.
- Charter §5: "If the allowance goes, turning the dial down must be a configuration change,
  never a redesign." Anti-goal: "A system that only works at its spend ceiling." This design only
  works above its ceiling.

**The gate is unaffordable as a regression test.** One replay at 20 storylines × 30 days is 600
editor runs (≈ $186–588) plus the hindsight judge, which sees "that storyline's next 30 days of
news" for every judged group. §15.5 runs it on every prompt, model, tool or retrieval change.
At ~$100/month a single replay consumes 2–6 months of allowance.

**Wall clock, a second collision** (UNKNOWN until measured). 60 runs between 02:00 and the
04:30 submit is 150 minutes, 2.5 min per run sequentially. A 6–12-turn Opus loop with adaptive
thinking at "moderate" latency is unlikely to fit that, so the job needs concurrency the spec
does not specify, against rate limits it does not state. `JOB_MAX_RUNTIME_MINUTES` (240,
`common.py:346`) kills the job outright past 4 h (`supervisor.py:438`). §7.5's fallback
("rendered from its last state") makes a slow night silent: pinned storylines land first, the
tail quietly goes stale.

What would answer it: price one real editor run on the host before approval, not after. If one
run is above ~$0.05, the design cannot meet the floor at 20 storylines, and §21 is a redesign,
not a pass.

## Fixed check: facts set side by side

Each pair, and the third claim a reader will infer. Residuals decomposed until they sum.

**F1. §1: "The check failed twice (J(H,S1) 0.65 against a floor of 0.78 ...). Hand reads of the
disagreements favoured Haiku both times." FLAGGED.** Inferred: the push approach failed, hence
the redesign. Neither fact establishes it; the second contradicts it (objection 1). Further:

- "Hand reads" implies the operator. The second read (n = 20) is recorded as "agent's reading".
- Residuals: run 1 disagreements less 540 + more 390 + elsewhere 120 = **1,050**; run 2
  540 + 278 + 112 = **930**. Samples read: 40 + 20 = 60 of 1,980 (3.0%). Run 1 sample:
  25 + 8 + 7 = 40. Run 2 sample: 11 + 3 + 6 = 20. Whether the samples were stratified across
  less / more / elsewhere is UNKNOWN; "less" is 51% and 58% of the disagreements.
- Third claim also inferred: J(S1,S2) rising 0.83 → 0.88 raised the floor 0.73 → 0.78 while
  J(H,S1) rose 0.61 → 0.65, so Haiku "fell further behind". The gap went 0.12 → 0.13: Haiku
  improved by 0.04 on clean-er seeds; the bar moved because Sonnet got more self-consistent.

**F2. §1 item 2: "Both runs scored against seeded storylines named after theses ... so much of
the disagreement measured the storylines" + "no rerun or bar change fixes either". FLAGGED.**
Inferred: the storyline problem is permanent under push. Neither fact shows it. Decomposition of
the change from run 1 to the partial re-seed in run 2: less 540 → 540 (Δ 0), more 390 → 278
(Δ −112), elsewhere 120 → 112 (Δ −8); total Δ −120 of 1,050 (11.4%), all outside "less", the
largest bucket. "Much of" is unmeasured (`al7.6` run-1 read supports "most S1 errors" on
composite storylines, but that is S1's errors on a 40 sample, not a share of 1,050). And a
clean-seed rerun, the test of "no rerun fixes", has not happened.

**F3. §4 + §15.3: "phase 1 already is this, and it is the baseline §15 measures against" +
"Baseline: the share of mattered groups that phase 1's briefs mentioned on the same days".
FLAGGED.** Inferred: the baseline is available for the replay's default 30-day window. It is
not: the `BRIEF_DATED_MEMORY_ENABLED` row went in on 11-23 (`al7.1` notes; step 3 due 11-24), so
any past 30-day window holds at most one phase-1 brief. The gate (plan 2's end) cannot compute
"recall above phase 1's baseline" until about 12-24, or silently substitutes the pre-phase-1
brief. Also "phase 1 already is this" overstates: phase 1 is the existing brief plus a 7-day
headline window and dated claims, not a long-context analyst over grouped news.

**F4. §20 table: 11-21 sections dispositioned. FLAGGED (residual).** The 11-21 spec has 18
top-level sections. The table rows cover §3–§18 (16; §6 and §6.1 share a row). Missing: **§1
and §2** (2). §2 holds the operator's decisions D1–D9. Decomposing: D5 retired (§1 of the spec);
D2, D6, D7, D8 dispositioned via rows §7, §9, §10; D1, D3, D9 implicitly (§12–13, `pinned`, phase
1 stands); **D4 (the consensus-vs-constraints pair, operator's "a") silently dropped**: §5.1 has
constraints and no consensus field; consensus survives only inside calendar set-ups (§9). 9 = 1
retired + 4 via rows + 3 implicit + 1 dropped without record. An operator decision should not
disappear through an omitted row.

**F5. §6.2: "Capture stores the RSS summary only: headlines and ledes, 40–260 characters" +
"The editor gets a `read_article` tool". FLAGGED.** Inferred: stored bodies are 40–260 chars,
and `read_article` closes the gap where it is thinnest. The range was measured in August on the
brief's 400-character-capped text; `items.body` is stored uncapped (11-21 §1; `capture.py`
writes `entry.get("summary")` as is). And for the Google News proxies, where the body is title
plus outlet, the stored URL fetches Google's shell (objection 2, measured once).

**F6. §21: "The floor is £20 a month; the ceiling is the free API-credit allowance."
FLAGGED.** Inferred: the design lands between them once dialled. Neither fact establishes it;
objection 3's estimate puts nightly runs alone above the ceiling at 20 storylines.

**F7. §6.3: "primary sources start empty, so the replay (§15) runs without them".** Inferred:
replay scores transfer to production. Not established: the gated configuration differs from the
shipped one in the input §5.2 most relies on (a primary item is often the development). Minor;
state it as a known gap in the gate.

**F8. §22: "get no new writes; the plan decides whether 0020 drops them (empty on the host while
attach stayed off)".** Inferred: dropping loses nothing. "Attach stayed off" is recorded
(`al7.3`: "Attach NOT switched on", "Attach still OFF"); "empty" is inferred from it, not
counted. resolved-without-a-measurement: the plan should `SELECT count(*)` before any drop (the
down migration already refuses while rows exist). Residual for 0019's tables: 4 created
(`storylines`, `storyline_proposals`, `item_attach_decisions`, `item_attachments`) = 2 kept + 2
no-new-writes. Sums. Also: "`thesis_evidence` gains `storyline_id` as planned" is already done
(0018 column, 0019 FK, `0019_storylines_up.sql:57-58`).

**F9. §23 beads vs the 11-21 §15 disposition.** 11-21 §15 named `c5k`, `sse`, `6wc`, `bqa.7`,
`qap` and one future clean-up bead. §23 covers `c5k`, `sse`, `bqa.7`; `6wc` (was superseded by
plan 2b) is unassigned; the clean-up bead is unmentioned. Also unmentioned: `uk1` (event
skeletons, sub-project 3), which §9's calendar set-ups largely absorb, and whose description asks
for "scenarios with rough probabilities", which this spec's anti-goal forbids. One of the two
needs to change, on the record.

## Other findings (not in the top three)

- **Mark counts will be read as odds.** "14 d: +3 / −1" counts editor citations, so it tracks
  coverage volume (a twelve-outlet group and a one-outlet group each score 1), not evidence
  weight. The anti-goal "scenarios show evidence, never odds" is honoured in form only.
- **Dormancy starves itself.** Dormant = no cited group in 14 days (§12). Under pull, a retrieval
  miss means no citation, which means dormancy, which means weekly runs, which means fewer
  citations. The wake path (§11) needs a group with ≥ 3 outlets; low-cadence storylines fed by
  one or two outlets (38 North, BoJ statements) may never cross it. Push kept dormant storylines
  in the attach prompt precisely to avoid this (11-21 §10.1).
- **§7.4 wording:** "timeout and 429 defer ...; a 4xx charges it" — 429 is a 4xx. Say "other
  4xx".
- **Labelling creep.** 30 + 20 audit taps before launch, repeated after each judge-prompt fix;
  small, but it is a session, which §3 calls an anti-goal.

# news-brief rethink: charter

Bead `news-brief-lnr`. Brainstormed with the operator 2026-11-19/20. **Status: approved
2026-11-20** (decisions in §10; red-team review in
`docs/superpowers/reviews/2026-11-20-rethink-charter-redteam.md`). This is the umbrella document: what the project is for, what success looks like, the
shape it takes, and the order of the sub-projects. Each sub-project gets its own brainstorm and
spec; nothing here is detailed enough to plan code from directly.

The repo is public. Portfolio content (holdings, amounts, thesis text) is deliberately absent; the
operator's private portfolio repo is referred to only as "the workbench".

## 1. Why a rethink

- **Comprehension's shape is wrong for this budget.** It reads every captured item, so its cost
  tracks news volume, and items age out of the queue unprocessed. Raising the age limit or the
  daily spend cap only moves the overflow point. It was switched off on 2026-11-18 as possibly not
  worth it. (Red-team correction: on cost alone, per-item reading on Haiku 5.5 could fit, at
  roughly $4/month at the historical dense rate. The open question is quality: the one
  Haiku-tier measurement, on Haiku 4.5, agreed with Sonnet on 55.1% of link decisions against
  Sonnet's 76.6% with itself. The operator's decision is to stop investing in per-item extraction
  either way.)
- **The spend is only tolerable on a free-credit allowance.** At retail prices the operator would
  never have run it.
- **Output quality, specifically continuity.** The brief repeats itself, and it reports a
  development as surprising when it followed plainly from something it reported days earlier.
  Without continuity it also cannot find connections that are not immediately obvious: second-
  and third-order effects, long-term consequences. Feeding every past brief back in is ruled out:
  it grows without bound.
- **Configuration needs SSH** into the host to change settings rows.
- **Portfolio inputs are awkward.** Theses and watch triggers are authored in the workbench and
  were pushed by hand (SSH, paste file contents into a shell, chmod). Hand-editing JSON is
  awkward, and the seam between the two projects is awkward too.

## 2. What the project is for (the operator's account)

- **Orientation.** It saves googling the basic news to know the direction of things, and it
  filters out bad articles and topics the operator does not care about. The curated outlet set is
  part of the value and is not to be cut.
- **Thesis movement, not prediction.** It has never told the operator something the market had
  not priced, and that is not the ask. Its value is in direction shifts, thesis confirmation, and
  developments that build up or break down a thesis rather than overturn it. The operator tested
  the predictive power of news (historical Bigdata.com sentiment and events against results) and
  found no correlation, so the goal is **material for adjusting analysis frameworks**, not
  forecasts.
- **Thinking about the future**, financially and personally.
- **Offloading reasoning.** The operator works in the Shapiro/Papic style: establish a base case
  from constraints, then adjust the probability space as evidence arrives. The bottleneck is time,
  and judging the quality of information (finding it is easy). Worked example: ahead of an
  announced great-power summit, the operator spent an afternoon on what each actor wanted, could
  gain and was constrained by, and what that meant for investments, security and life, and ran
  out of time before the event with only "probably political theatre" as a starting base case.
- **Portfolio watching** is wanted alongside: thesis health, alerts, and identifying
  opportunities to trim, add, or buy something new that fits the theories behind the portfolio.
  The operator's sense is that this is "only half related" to the news, which the shape in §6
  takes seriously.

## 3. What success looks like

1. The brief says "this follows from what we reported on <date>" instead of re-surprising, and
   stops repeating itself.
2. It surfaces links between storylines, and between storylines and theses, that the operator
   would not have made unprompted.
3. Before a scheduled event that touches a thesis, the operator starts from a prepared skeleton
   (actors, interests, constraints, linked storylines) instead of a blank page: twenty minutes of
   judgement instead of an afternoon of assembly.
4. For each thesis, the operator can see what has built it up or eroded it since the last look.
5. Changing a setting needs no SSH. Theses and triggers reach the host with nothing to remember.
6. The core runs at or under **£20/month at retail prices**.

## 4. Goals, non-goals, anti-goals

**Non-goals**
- Predicting returns or prices from news.
- Reading and extracting every captured article.
- Multi-user. A tool the operator wants to use comes first; making it usable by others is a
  later, separate decision, taken only once it is useful.
- Trading from news-brief (prediction-market trading was retired 2026-09-11; the workbench owns
  decisions and execution).

**Anti-goals** (a failure even if it technically works)
- A system that only works at its spend ceiling.
- Memory that grows with history (the "feed every brief back in" shape).
- Private portfolio data in this repo's git.
- Anything flowing back into the workbench automatically.

## 5. Constraints

- **The budget is a dial, not a number.** Design target: the core is worth having at £20/month
  or less, retail (operator decision 2026-11-20; he would normally pay £10–20, and wants it as
  cheap as possible for now). Red-team estimate for scale: the existing daily brief alone is
  about $7.56 per 30 days at an assumed Sonnet 5 batch rate (from logs ending 2026-08-29; the
  ledger-reconcile, claim-verify and watch calls are unmeasured), so the brief itself needs a
  dial too. Current ceiling: the free API-credit allowance (about $100/month), which the
  operator is willing to allocate entirely to this. If the allowance goes, turning the dial down
  must be a configuration change, never a redesign. Every above-floor capability is a single
  setting with a stated monthly cost.
- **The 2026-11-18 boundary stands** (agreed in the workbench, `docs/2026-11-18-news-brief-boundary.md`
  there): news-brief senses, the workbench decides; data flows one way, workbench → host, only
  what is needed; nothing comes back automatically. This charter adds one thing: the workbench
  may **query** news-brief on demand (§6), which is not an automatic flow back.
- **Public repo.** Private data lives only in the host's data folder.
- **The curated outlet set stays.** Commercial pre-clustered feeds do not carry it.
- Single user; Telegram stays the push channel.

## 6. Approaches

Considered, with what each trades off:

- **A. news-brief watches and remembers; the workbench reasons. PICKED.** news-brief becomes the
  always-on sensor with a storyline memory (continuity, links between storylines, an event
  calendar), the daily brief, alerts, and a read-only query interface. Deep reasoning (thesis
  formation, trim/add/new decisions) happens in the workbench's interactive sessions, with
  news-brief's memory as one of their tools. news-brief prepares cheap skeletons for upcoming
  events; the depth happens with the operator. API spend goes on sensing and the brief only.
  Trade-off: thesis work still needs the operator to sit down; two repos remain.
- **B. One system.** Portfolio review moves into news-brief, which reasons autonomously (full
  event packs, trim/add suggestions). Most hands-off, but private data next to a public codebase,
  needs a real UI (Telegram is awkward for complex interaction), deep reasoning on API credits
  fights the budget floor, by far the biggest build, and discards a workbench that already works.
- **C. Minimal: give the existing brief a dated, bounded memory.** Render `first_seen` /
  `resolution_date` on ledger rows (the render drops them today, `brief_memory.py`
  `render_established_block`), feed a fixed 7-day window instead of yesterday's brief cut to
  6,000 characters plus a 1,000-character weekly slice, and keep the per-thesis confirm/contradict
  lines the brief already writes and discards. About $0.01–0.02 a day. The red team proposed
  running this for 2–4 weeks as a baseline before storylines. **The operator declined a trial
  phase** (it would be forgotten and lead to another rethink); **its pieces become the first
  slice of sub-project 1 instead**. (Correction to an earlier draft: connections are found by a
  model reading its inputs, not by what is stored; storage decides what can be recalled later.)
- **D. A scheduled Claude Code agent writes the brief** over news-brief's query interface. Moves
  spend off API credits if covered by a subscription (unverified, as are its limits); less
  reliable than an owned cron, needs the interface exposed securely, and delivery to Telegram gets
  harder. **Kept as a possible later step inside A**, since A builds the interface it needs.
- **E. Strict split:** everything portfolio-related leaves news-brief. Cleanest privacy, but the
  "news meets holdings" layer needs an always-on machine, which in practice is the host anyway.
- **Buy the firehose** (Kagi News, Event Registry, GDELT Cloud). Dropped: those feeds do not
  carry the curated outlets.

Why A: it fixes the actual letdown (continuity) at a cost that scales with storylines, not
articles; it keeps the reasoning where it is already interactive and working; it respects the
public/private split and the one-way boundary; and it leaves D open.

Supporting research (2026-11-19 landscape scan): the per-article knowledge base is both the most
expensive part of the system and the most commoditised; macro/geopolitical theses as the
*monitored object* are a gap no product fills (2026 "thesis tracker" products are all
single-stock). The standard cost-bounded pattern is cheap embedding/trigram clustering first, LLM
calls per story cluster, only for clusters that matter.

## 7. The shape

```
curated outlets ──► capture (exists) ──► STORYLINES ──────────────► daily brief (orientation +
                                         bounded state per          storyline deltas, "follows
                                         storyline, links           from <date>")
                                         between storylines         
                                         and to theses        ──► event calendar ──► pre-event skeletons
                                              │
workbench ──push──► theses + triggers ────────┼─────────────► thesis evidence log
 (decides)                                    │               (built up / eroded since last look)
    ▲                                         ▼
    └──── on-demand query (read-only) ──── query interface      watch job (exists): alerts
```

- **Storylines are the world model.** The knowledge base tried to build one bottom-up (extract
  every article, hope reasoning emerges), which is why its cost tracked news volume. Storylines
  build it top-down: new items attach to a storyline or open one; only storylines touched today
  get an LLM update; each storyline keeps a short rolling summary and what would come next;
  storylines with nothing new go quiet. **Two bounds, stated separately** (the claim ledger
  conflated them once, bead `6wc`): the *prompt* bound (how many storylines a call sees) and the
  *storage* bound (quiet storylines retire after a stated period; retirement is a date, never a
  DELETE). Both must be explicit in the storylines spec.
- **Attachment is the unproven step.** Cheap attachment is what makes cost scale with
  storylines. The repo's 2026-09-26 spike measured title trigrams on an *easier* task (same
  event, cross-outlet, within 2 h): 467 of 671 true pairs (69.6%) scored below 0.35, and trigram
  grouping saw 53.7% against 97.2% for real time. Embeddings are unmeasured. The storylines spec
  must choose the method with an offline check against existing captured data as part of the
  build (not a weeks-long trial), and must say what the brief does when attachment misses.
- **The two products share the middle.** The brief (news cadence) and portfolio review (holdings
  and prices cadence) are different products. They meet at the theses: news moves storylines,
  storylines move theses, and the workbench turns thesis health plus prices into decisions.
- **Consensus vs constraints.** Markets price narratives; constraints win eventually. Where a
  storyline's consensus reading and the constraint reading differ is where analysis should look.
  That is framework input, never a trade signal. (A proposal to test in the storylines design,
  not a commitment.)

## 8. Sub-projects, in order

0. **Refresh the paper-trade measurement.** `docs/2026-08-16-trading-retrospective.md` already
   cut 126 closed positions (2026-06-01 → 08-13): hit rate 39.7% (bullish 41.0% of 61, bearish
   38.5% of 65), "the direction itself is wrong". Sub-project 0 repeats the cut on positions
   opened **after** the 2026-08-16 fixes, adds a long-only reading (bullish as a buy, bearish on a
   holding as a trim; an ISA cannot short), and pre-registers the bar for "useful" before looking.
   "Self-proposed theses and position signals" here means the forced-tool signals extraction
   call plus the paper sim. **If they are not useful, both are removed** (operator decision
   2026-11-20). The brief's 📌 Position Signals section, which flags news against held positions
   and theses, stays regardless.
1. **Storylines.** The core, and the biggest letdown. Own brainstorm and spec. **First slice:**
   approach C's pieces (dated memory rows, a fixed 7-day window, a per-thesis evidence log
   written from the brief's own confirm/contradict lines). Decisions the spec must make include:
   storylines named by the operator, discovered by the system, or proposed by the system and
   accepted on Telegram; the attachment method and its offline check (§7); the model tier at the
   floor and above it; how the brief consumes storyline state, and what it does when attachment
   misses; how links between storylines are found; both memory bounds (§7); whether the claim
   ledger is absorbed (and with it `6wc`); whether the consensus-vs-constraints pair belongs in
   storyline state.
2. **The seam.** (a) The automatic push of theses and triggers from `/checkin`, which is the
   workbench's own bead, not this repo's. (b) A read-only query interface over storylines and
   theses for workbench sessions (repurposes `nyy.2`). (c) Settings changed from Telegram
   (`/set`, `/settings`) instead of SSH. Telegram stays for simple interactions; structured
   inputs keep arriving from the workbench. **(a) and (c) do not depend on storylines and can
   run alongside sub-project 1; (b) follows it.**
3. **Event skeletons.** Depends on 1. (The thesis evidence log moved into 1's first slice.)
4. **The budget dial.** A per-capability monthly cost table, and a setting for each line,
   including the brief itself (its model and input volume). Can run alongside 1.
5. **Later, optional:** approach D.

## 9. Disposition of existing work (approved 2026-11-20)

| Work | Proposal | Why |
|---|---|---|
| `b42` continuous capture | **Keep**: the foundation storylines read from | Capture is cheap and irreversible; it is what makes a storyline's history exist |
| Watch job (`8lp`, `vap`), theses, Telegram daemon | **Keep** | Already the shape A wants |
| Claim ledger (`brief_memory.py`, `claim_store.py`) | **Keep for now**; the storylines spec decides whether it is absorbed | It is the current anti-repetition mechanism |
| Position signals, paper sim, `7dy` (Stage B) | **Park** until sub-project 0 reports | Fate depends on the measurement |
| Comprehension (`comprehend.py`) | **Leave switched off; code retired once storylines replace it.** Deferred decisions `2r5` and `bqa.11` close as superseded now | Per-article reading is the approach being replaced |
| `c5k` (triage to Haiku), `sse` (which way Haiku's link decisions depart) | **Keep**, re-pointed at sub-project 1 | The £20 floor makes the Haiku tier central to storylines |
| `bqa` KB core and children, `d9i` render path, `nyy.1` KB query API, `1u2` thesis engine and children, `vlg`, `4le`, `115`, and the P3/P4 comprehension/KB bugs | **Supersede** by one rule: *it only serves per-article comprehension or the KB* | All serve the retired path |
| `6jc` confirmation (event coreference) | **Supersede**; "are these items the same event" moves into storylines | Same problem, different unit |
| `vd9` event census | **Drop** (operator, 2026-11-20: labelling stopped about halfway, "very dull and a lot was nonsense"). The code freeze on `census*` lifts. A follow-up bead removes the census and labeller code | Its labels cover within-6 h grouping only and are not a usable attachment test set |
| `nyy` surfaces | `nyy.2` (MCP) **repurposed** in sub-project 2, re-parented out of the `bqa` tree **before** that tree is superseded; `nyy.3`–`nyy.5` **deferred** | Telegram `/set` covers configuration; a web UI is not needed yet |
| `6wc` ledger grows without bound | **Keep**, assigned to sub-project 1 | Collides with anti-goal 2 while the ledger stays |

Superseding is close with a reason naming this charter. Nothing is deleted from git by the
clean-up itself.

## 10. Decisions, 2026-11-20

- Approach A (operator).
- No baseline trial before storylines; build now. Approach C's pieces are folded into sub-project
  1 as its first slice (operator: a trial would be forgotten; the project can be rebuilt as often
  as needed until the right variation is found).
- Floor £20/month retail (operator).
- Paper trading and the signals call are removed if the refresh shows them not useful (operator).
- Census dropped entirely (operator).
- Bead clean-up by the §9 rule (operator: "we should clean up beads for sure").
- Calls made by the assistant, for the operator to check: sub-project 0's long-only reading and
  pre-registered bar; `c5k`/`sse`/`6wc` kept; 2a, 2c and the budget dial run alongside 1; the
  census-code removal as a separate bead.

## 11. Left to the sub-project designs

Storyline data model and lifecycle, attachment thresholds, prompt and model choices, the query
interface's tools and transport, the `/set` command's guard rails, the event calendar's sources,
the skeleton's format, and the per-capability monthly cost table that makes the budget dial real.

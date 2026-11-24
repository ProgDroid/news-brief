# Storylines: the editor (design)

Bead `news-brief-al7.7` (child of `al7`). Brainstormed with the operator 2026-11-24. Revises
`docs/superpowers/specs/2026-11-21-storylines-design.md` (revision 5, "the 11-21 spec" below):
§20 says which of its sections stand, which this replaces and which are retired. Parent charter:
`docs/superpowers/specs/2026-11-20-rethink-charter-design.md`.

**Revision 2** (same day), after a builder check and a red team
(`docs/superpowers/reviews/2026-11-24-storylines-editor-redteam.md`) and the operator's answers:
a two-pass batch editor instead of a multi-turn loop (§7), two candidate feeders (§7.3),
embeddings on Modal with OpenAI as a settings switch (§6.1), a measured article-fetch rate
(§6.2), the consensus field restored (§5.1), and a replay whose judge is independent of the
editor's retrieval (§15). A second red team (`…-redteam-r2.md`) recommended stopping rounds; its
fixes are applied: a judge pool of every group with a pinned, frozen judge (§15.2), a 60-tap audit
(§15.4), runs per night as the cost unit with its own setting (§18, §21), the probe's Google
consent-page error (§6.2), and the cost measures (§21.1).

The repo is public. Nothing in this document, its tests or its fixtures names a holding, an
amount, or the text of an operator thesis. §19 says where that material lives.

**Designed for quality first, priced last** (operator, 2026-11-24: "designing around cost isn't
working"), and **built at the target quality, without trial steps at lower quality** (operator,
same day). One exception was forced by the red team: the editor's shape sets cost by 10–40×, so
it was chosen now (§7.2, §21).

## 1. Why this revision exists

The 11-21 spec attached each article to 0–3 storylines with Haiku, had Sonnet correct the
attachments in the nightly update (D5), and gated the build on an offline check: Haiku's
agreement with Sonnet as attacher (Jaccard, §8.2). Haiku failed it twice: J(H,S1) 0.61 against
a floor of 0.73, then 0.65 against 0.78. The floor rose because Sonnet became more consistent
with itself (J(S1,S2) 0.83 → 0.88); Haiku improved by 0.04. The assistant's reads of 60 of the
1,980 disagreements (40 and 20) favoured Haiku both times, finding Sonnet attaching
human-interest pieces, opinion and passing mentions. The operator did not read them.

What that evidence shows is narrower than "push failed":

- **The gate asked a question D5 never posed.** Agreement with Sonnet-as-attacher tests whether
  Haiku could replace Sonnet in a one-step design, and scores skipping Sonnet's noise the same as
  missing a real development. The evidence is that the gate was wrong, not that attaching was.
- **The storylines were poor input.** Both runs scored against seeded storylines named after
  theses or joining two situations (`al7.6`). How much of the disagreement that explains is
  unmeasured: the partial re-seed between runs cut disagreements by 120 of 1,050 (11%), all
  outside the largest bucket (`less`: Haiku's set a strict subset of Sonnet's; 540 both times).

**Why the redesign anyway.** The operator asked for the best version from scratch, not a fix to
the gate. The editor (§7) decides relevance with the storyline's state in front of it, and gives
"development" a definition the system checks (§5.2). Attaching stays, as one of the editor's two
candidate feeders (§7.3), so nothing the evidence vindicated is thrown away. The replay (§15)
reports what each feeder contributes.

## 2. What it is for

From the charter (§3, §7): **orientation and thesis movement, with continuity the main
letdown.** The operator's own test found news had no predictive power, so the goal is framework
material, not forecasts. The reader reads geopolitics through constraint-based analysis (Papic /
BCA): preferences are optional, constraints are not.

## 3. Goals, non-goals, anti-goals

**Goals**

- Each storyline says where a situation stands, what consensus expects, what binds its actors,
  which outcomes remain possible, what we are waiting to learn, and what is due next, and keeps
  that current.
- A development has a working definition the system enforces (§5.2), not a judgement call.
- The brief follows from the storylines: what moved, the latest evidence for each scenario, what
  is due and what it would mean.
- Quality is measured without a labelling chore (§15).

**Non-goals (this revision):** the analyst-corpus view, the framing map, the query interface
(`nyy.2`) and the thesis stress board are second wave (§17). Multi-user, a web UI.

**Anti-goals** (a failure even if it works):

- **Probabilities or forecasts.** Scenarios show evidence, never odds or tallies that read as
  odds.
- **A labelling chore.** The event census died of it ("very dull, a lot was nonsense").
  Feedback is a tap inside something the operator already reads, never a session.
- **A document that grows without bound.** The state is rewritten each run and capped; history
  lives in dated rows.
- **A relevance decision made by something that does not use it.** Feeders propose candidates;
  only the editor, which writes with them, decides.
- **The brief as a list of everything.** Uncited news reaches Top Stories only on its own merit.

## 4. Approaches considered

| Approach | Trade | Outcome |
|---|---|---|
| **Push** (the 11-21 spec): Haiku attaches every article, Sonnet corrects nightly | cheap; the attacher has no state; its gate measured agreement, not recall | gate retired; Haiku attach kept as a feeder (§7.3) |
| **Pull, multi-turn loop**: each storyline's editor searches and reads over up to 12 turns | most freedom; real time only (cannot batch); the red team's estimate is $0.31–0.98 a run on Opus, $186–588 a month at 20 storylines, above the ~$100 ceiling | rejected on cost at any model that justifies it |
| **Pull, single call**: one call per storyline over a candidate list | batchable, cheap; cannot ask to read an article in full | rejected: the operator wants no lower-quality step first |
| **Pull, two passes** (this spec): pass 1 picks up to 5 articles to read in full, pass 2 writes | most of the loop's value (reading), both passes batch | **chosen, operator 2026-11-24** |
| **One long-context analyst**, no storylines | simplest; weak continuity beyond its window; nothing to query | not built; the briefs actually delivered are the replay's baseline (§15.3) |

## 5. The storyline document

### 5.1 Fields

Example (a public topic):

> **BoJ normalisation** · *active* · pinned
> *Description:* Bank of Japan moving away from ultra-loose policy: rate decisions, yield-curve
> control, intervention, and the wage data that feeds it.
> *Where it stands:* Hiked to 0.75% in Oct. Yen weakness is the pressure, the wage round is the
> gate.
> *Consensus:* one more hike by March; the yen recovers.
> *Actors:* BoJ board (wants: exit without a bond rout) · Ministry of Finance (wants: a stable yen)
> · Rengo (wants: real wage gains)
> *Constraints:* `economic` debt-service cost caps the terminal rate (since 09-02) · `political`
> election year lowers tolerance for yen weakness (since 10-15) · `temporal` the spring wage round
> lands in March
> *Scenarios:* **A** one more hike by March · **B** pause through spring · **C** forced faster by
> the yen. Latest evidence: 11-14 Tokyo CPI above forecast → supports A (Nikkei, 4 outlets) ·
> 11-18 Ueda "no rush" → weakens A (Reuters, 9 outlets)
> *Open questions:* Does the wage round clear 4%? Does MoF intervene below 160?
> *Calendar:* 12-19 BoJ decision. Set up 12-12: consensus hold. Hike → A confirmed, the
> debt-service constraint is tested. Hold with hawkish guidance → A intact. Hold, dovish → B.
> *Signposts:* intervention confirmed → C · wage round under 4% → B
> *Primary sources:* BoJ policy statements (RSS)
> *Links:* storyline "US rates path" (carry-trade unwind) · thesis `<key>`

| Field | Written by | Kept |
|---|---|---|
| name, description | seeding, proposal, operator; edited by the operator only | current; edits are new rows |
| where it stands | editor, every run | one row per run; current = latest; ≤ 600 chars |
| consensus | editor | one row per run, with the state; ≤ 300 chars (D4) |
| actors (name, wants) | editor | current set per run; ≤ 6 |
| constraints (type, actor, text, since) | editor | a change is a dated row; ≤ 8 live |
| scenarios (label, text, what tells it apart) | editor; adding or retiring one is a dated event with a reason | 2–4 live |
| scenario marks (scenario, group, supports/weakens, why) | editor | append-only |
| open questions (text, raised_on, answered_on, answer) | editor | answered ones stay, dated; ≤ 6 open |
| calendar entries (§9) | editor, operator | set-ups immutable once written |
| timeline lines (date, line, group refs, what changed) | editor | append-only |
| signposts (text, points to scenario/constraint) | editor, operator | until hit or retired; hits dated |
| primary sources (source id) | editor proposes, operator accepts | until removed |
| links (storyline or thesis key, reason) | editor, weekly | `first_seen`, `last_affirmed`, `retired_on` |
| thesis evidence (thesis key, date, supports/challenges, line, refs) | editor | append-only, `source='storyline'` |

Constraint types: `economic`, `legal`, `political`, `military`, `physical` (supply, geography,
capacity), `temporal` (deadlines, calendars).

### 5.2 What counts as a development

**A group of news counts for a storyline when it changes the document.** Every group the editor
cites names at least one target, each checkable against the document (§7.4):

- a constraint (added, changed, or retired, naming the constraint);
- a scenario mark (supports or weakens a named live scenario, with the reason), or a scenario
  added or ruled out;
- an open question (answered, naming it, or a new one raised);
- a calendar entry (a date set or moved, or an outcome recorded);
- a signpost hit;
- the consensus (changed, with what it was before).

A change that touches none of these is not a development. The editor may still mention it in
*where it stands*, without citing it. Rhetoric, visits and positioning usually land here.

## 6. Inputs

### 6.1 Groups and embeddings

Articles are grouped by event before anything reads them: twelve outlets on one event become one
**group** with its outlets, vantage tags (`outlets`, `capture.py:72-89`), first and last seen.
Grouping is by embedding similarity within a 72 h window, plus title trigram as a second signal;
no model call per article (the retired comprehension step is not coming back). A group stays
open for new members for 72 h.

**Embeddings run on Modal** (operator, 2026-11-24), on a GPU, with a top open embedding model in
the 4B–8B range (the builder picks it by a retrieval check on captured data; §25). Fly was
considered and dropped: its GPUs were withdrawn on 2026-07-31, which caps it at small CPU models.

- **One interface, two providers.** `EMBEDDING_PROVIDER` is `modal` or `openai`. Switching is a
  settings change plus a re-embed job; it never needs code. Vectors are stored per model, so both
  spaces can coexist while a re-embed runs, and retrieval uses the active model only.
- **Cost is measured, not guessed, so the switch is a recognition decision.** The Modal endpoint
  returns its own runtime and whether the call cold-started; news-brief prices that at the
  configured GPU rate. OpenAI calls are priced from their token usage. The monthly cost line
  (§21) shows the Modal cost next to what the same tokens would have cost on OpenAI.
- **Privacy:** on Modal the text goes to the operator's own deployment. Switching to OpenAI sends
  article text and storyline query text (questions, constraints) to OpenAI; turning that setting
  on is the operator's decision, and §19 records it as the one exception.
- **When the service is down,** `group` defers its work (a decision row, not a silent skip) and
  retrieval falls back to trigram only. Nothing crashes the pipeline.
- Thresholds are calibrated once on captured data and checked by the replay (§15.4).

### 6.2 Article text

Capture stores each feed entry's summary, uncapped (`capture.py`); measured in August at 40–260
characters on the brief's capped text, and for the Google News proxy feeds the title plus the
outlet. **The editor can ask for up to 5 articles a run to be read in full** (§7.2).

Measured 2026-11-24 on the host (`scripts/storylines_fetch_probe.py` at `db80a29`, 15 recent
items per outlet, 25 outlets), second run. The first run counted Google's consent page as a thin
article and never reached the Google News proxy outlets; this run decodes their links.
**131 of 286 items (46%) returned ≥ 1,500 characters of paragraph text.** Google links: 71, of
which 69 reached the publisher.

| Result | Items | Where |
|---|---|---|
| usable | 131 | Kyiv Independent, Mining.com, OilPrice, The Hindu, Observing Japan, Sinica, EIA, Pinecone all or nearly all; Meduza 14, Al Jazeera 10, Yonhap 10 (via Google), IranWire 9, TASS 6; NHK 2, Bank of Japan 2, ISW 1, Jacob Shapiro 1 |
| thin | 71 | NHK 12, the analysts' X feeds, TASS 9, IranWire 6, Yonhap 5, Al Jazeera 4, the rest scattered |
| empty | 18 | IRNA 15, Bank of Japan 3 |
| blocked | 63 | Reuters 14, ISW 14, SCMP 15, Times of Israel 15, 38 North 4, one analyst item |
| http error | 1 | Al Jazeera |
| Google link unresolved | 2 | Reuters 1, NHK 1 |

Sum: 131 + 71 + 18 + 63 + 1 + 2 = 286. (Some rows of the pasted output lost columns; the per-outlet
attributions above are read from the intact rows, and the totals are the script's own.) Caveats:
"thin" counts `<p>` text only, so it is an upper bound on failure; the builder re-runs the probe
with a real extraction library. The sample is equal per outlet, so by volume, where Reuters
dominates, the usable share is lower.

**Consequence:** fetching works for roughly half the outlets, Yonhap included once its Google
links are decoded. **Reuters blocks automated fetches** (14 of 15), as do SCMP, Times of Israel,
ISW and 38 North. Reuters stays headline-only unless a licensed full-text source is added, which
is out of scope here. Each item the editor sees says whether full text is available, and
constraint detail is expected mostly from the outlets that return text and from primary sources
(§6.3).

**Fetch policy** (the decode goes through an undocumented Google endpoint, which rate-limits
heavy use, and capture fetches the same Google News feeds from the same host):

- Fetch only what the editor asks to read (≤ 5 a run), never in bulk; cache the decoded URL and
  the text per item.
- **Skip outlets that block.** A per-outlet record of fetch outcomes; an outlet whose last 10
  attempts were all blocked is not fetched, and is retried once a week. Reuters, SCMP, Times of
  Israel, ISW and 38 North start skipped.
- **Cap Google decodes** at `STORYLINE_GOOGLE_DECODES_PER_NIGHT` (default 30), with a one-second
  pause between them, and stop for the night at the first 429 from Google.
- **Watch capture.** The collect log's RSS failures for the Google News feeds are compared before
  and after the editor starts fetching; a rise switches decoding off (`STORYLINE_GOOGLE_DECODE`
  false) until looked at.

### 6.3 Primary sources

Statements, releases, filings and data are what news reports on. A storyline lists the primary
sources it watches:

- **RSS/Atom first** (central banks, regulators, company filing feeds, official gazettes,
  parliament bill trackers). `page` sources need new capture work (`capture.capture_sources()`
  drops them today) and come in a later plan.
- Items from them are captured like any item, flagged `primary`, and grouped with the news that
  reports them, so a group can say "primary source present".
- The editor proposes a source when it keeps citing news about one; the operator accepts or
  rejects it on Telegram. The operator can also add one by command.
- Primary sources have no history, so the replay (§15) runs without them: a known gap in the
  gate, stated in its report.

## 7. The editor

### 7.1 When it runs

- **Nightly, after the candidate check.** At 01:30 UTC the candidate lists are built for every
  `active` **and** `dormant` storyline (§7.3). Every storyline with at least one candidate, or a
  calendar entry due in 2 days or passed without an outcome, gets a run. Pinned first.
- States land before the 04:30 brief submit; a storyline whose run has not landed is rendered from
  its last state (§7.5).
- **Mid-day runs** follow a confirmed alert (§11), in real time, so the flash brief and the next
  morning read a current state.
- **A new storyline's first run** reads the last 30 days of candidates.

### 7.2 Two passes, batched

Both passes go through the batch API at half price, in two submissions per night.

1. **Pass 1, triage.** The editor sees its document and its candidates (titles, outlets,
   vantages, summaries, and whether full text is available). It returns up to 5 groups to read in
   full, or none.
2. **Fetch.** Code fetches those articles' text (cached per item).
3. **Pass 2, write.** The editor sees the same input plus the fetched text, and calls
   `write_storyline`.

Model: `STORYLINE_EDITOR_MODEL`, chosen in the cost pass (§21), with adaptive thinking set
explicitly and `max_tokens` sized to it. A mid-day run uses the same two passes in real time.

### 7.3 Candidates: two feeders

Each storyline's candidate list for a run is the union of:

- **Retrieval:** groups since its last landed run, by hybrid search (vector + trigram) over its
  open questions, actors, constraints, consensus and signposts.
- **Attach:** the existing `attach` job (plan 2a, Haiku every 30 minutes) proposing up to 3
  storylines per article. Its output becomes a candidate, never a decision: the editor alone
  decides.

Each candidate records which feeder found it, so the replay reports what each contributes
(§15.3). Plus every group with a primary item from the storyline's watched sources. Ceiling per
run: `STORYLINE_EDITOR_MAX_CANDIDATES` (default 60 groups); above it, keep by outlets, then
similarity.

At the start of a run the editor also sees: the last 15 timeline lines whatever their age;
calendar entries due in the next 14 days and those passed without an outcome; linked theses (key
and one line); live watch triggers (one line each); a one-line index of the other storylines.

Groups and storylines appear under per-run opaque labels, mapped back in code; an unknown label
is an error, never a fall-through.

### 7.4 Validation

- Every cited group was in this run's candidates, and each citation names a valid target (§5.2).
- A scenario mark names a live scenario; an answered question names an open one; a retired
  constraint names a live one; a consensus change states the previous consensus.
- Calendar set-ups already written are not changed (§9); a run that tries is rejected for that
  entry, the rest lands.
- Caps in §5.1 hold, or the run fails and the previous state stands.
- A failed run writes a run row with its reason. A timeout, a 429 or a 5xx defers without
  charging a retry budget; any other 4xx charges it.

### 7.5 Claimed and unclaimed

A group is **claimed** when a landed run cites it. Any storyline may cite any group; two citing
the same group is normal. Everything else is **unclaimed** and feeds Top Stories (§10) and burst
detection (§11). A storyline whose run has not landed by submit is rendered from its last state,
and the groups it would have read stay unclaimed.

## 8. Constraints and scenarios

- **Scenarios are 2–4 live outcomes**, each with what would tell it apart from the others.
- **Evidence is shown as dated lines, never counts:** the latest few marks per scenario, each
  with its date, outlets and whether a primary source backs it. A count would track coverage
  volume (a twelve-outlet group and a one-outlet group would each score 1) and read as odds.
- **A scenario set change is an event:** a new scenario, or one retired as ruled out, is dated and
  carries the group that caused it. Ruling one out is itself a development.
- **Constraints carry `since`**, so the brief can say "the debt-service cap has held since 09-02".
  A constraint that changes is a new row; the old one is retired, dated.

## 9. The calendar

An entry: storyline, date or window, event, source (group or primary item), and:

- **Set-up, written before:** consensus, then what each plausible outcome would mean for the
  scenarios and constraints. Locked once written. Written less than 24 h before the event, it is
  marked *late*.
- **Outcome, written after:** what happened, and how it compares with the set-up.

The editor adds entries it finds in the news and primary sources; the operator can add one by
command. An entry past its date with no outcome triggers the next night's run (§7.1) until it is
resolved or marked *cancelled*. The brief's Watch / forward section reads the calendar (§10).

## 10. The brief

The 11-21 spec §7 stands, with these changes:

1. **🧵 Threads that moved:** per moved storyline, what changed and which target it changed (a
   constraint, a scenario, a question answered, the consensus), since when, whose reading, the
   primary source if present, theses touched. Quiet pinned storylines get one line.
2. **🌍 Top stories:** from unclaimed groups only, ranked by outlets, then recency.
3. **👁 Watch / forward:** the calendar's next 7 days with each set-up, plus the last day's
   outcomes against their set-ups ("held as expected", "surprise: …").
4. **The claimed-headline block** lists the groups each run cited, with outlets and vantages.
5. **Feedback** (§14) comes in a short follow-up message, not inside the brief's text.

*Moved* means a landed run since the previous brief's submit that cited at least one group or
recorded a calendar outcome.

## 11. Alerts

The 11-21 spec §9.1–9.3 alert format, Flash brief button and dedupe contract stand. The trigger
changes:

- **Every 30 minutes**, new groups that cross 3 outlets, every new primary item, and every group
  the `attach` feeder puts on a storyline, are matched to their storylines (attach's choice, plus
  the nearest 3 by vector).
- **A model checks each match** against the storyline's signposts, scenarios, constraints and
  calendar: a signpost hit, a scenario ruled out, a constraint broken, or a calendar outcome. A
  confirmed match alerts and queues a mid-day run (§7.1).
- **Bursts:** a group with ≥ 3 outlets in 6 h that matches no storyline alerts and queues a
  proposal.
- **Surges:** candidates for a storyline above `STORYLINE_SURGE_FACTOR` × its trailing 14-day
  average, confirmed the same way.

## 12. Lifecycle, proposals and merges

- **Dormant:** no cited group in 14 days **and** no calendar entry in the next 14 days. A dormant
  storyline stays in the nightly candidate check (§7.1) and in the attach feeder's prompt, so one
  relevant group wakes it, whatever its outlet count.
- **Proposals:** from bursts, and from unclaimed groups recurring on ≥ 3 days across ≥ 3 outlets.
  Accept and reject on Telegram, as today. A new storyline's first run reads the last 30 days.
- **Merges:** two storylines whose runs cited ≥ 50% of the same groups over 14 days are offered
  for merging, as in the 11-21 spec §9.5.
- **The weekly connections pass** (11-21 spec §9.6) stands, reading the new documents; its
  connections include scenarios in different storylines that depend on the same constraint.

## 13. Seeding

- **The storylines live on the host now are all retired and re-seeded** (operator, 2026-11-24),
  once the seed prompt is fixed: they are the thesis-named and composite ones of `al7.6`.
- Seeding proposes **situations**, never theses or trades, and one situation each (`al7.6` folds
  in here). For each pinned subject and thesis it asks which situations would move it; the
  coverage line (commit `27d1f5b`) stays.
- An accepted seed's first run builds its document from the last 30 days (§7.1).

## 14. Feedback from use

- **Thread lines** get *not a development*.
- **Top stories lines** get *belongs to…*, offering the 3 nearest storylines, plus *none*.
- **Calendar outcomes** get *mattered* / *didn't*.

They arrive in one short message after the brief, a button row per line, quoting it. Each tap is
a dated row. Nothing asks for a tap; taps feed §15 as the audit of the judge.

## 15. Measuring quality: hindsight replay

The check that gates the brief, replacing the 11-21 spec §8.2.

### 15.1 The replay

A script replays a past window one day at a time: each night's editor runs see only items
captured by that night, and both feeders run as they would have. Every retrieval goes through the
production functions with an `as_of` argument, never a reimplemented query (a copy drifts from
production). Results go to the host data folder; the replay never writes the live tables.

**Data budget.** Capture began 2026-09-04: about 81 days by 11-24. The replay uses a 14-day first
run (§7.1 shortened for the replay only), a 30-day window, and a look-ahead to the latest captured
day, at least 21 days. Primary sources are absent (§6.3).

### 15.2 What mattered

The hindsight judge is independent of everything it scores: retrieval, attach and the editor.

- **The pool is every group,** single-outlet ones included. Most groups are single-outlet
  (corroboration on this outlet set was measured at 6.6%), and the specialist outlets (38 North,
  ISW, the central bank, the analysts) often carry a development alone.
- **Stage A, per day, all storylines at once:** the judge sees every group's title and outlets
  for that day and every storyline's name, description and opening document, and shortlists
  generously which groups could bear on which storylines. It has its own prompt and does not see
  either feeder's output.
- **Stage B, per storyline and day:** for the shortlisted groups, the judge rules with hindsight:
  did this change where the situation went, its constraints, consensus or options? Its
  look-ahead is the same storyline's stage-A shortlists for the next 21–30 days, the judge's own
  selector, never the editor's citations. A mechanical signal goes with it: whether later groups
  near it kept appearing across outlets and days.
- A group stage A did not shortlist counts as *didn't matter* for that storyline. Every group the
  editor cites is in the pool, so noise is defined for single-outlet citations too.
- **The judge is pinned:** `STORYLINE_JUDGE_MODEL` is its own setting, never the editor's. Its
  rulings are computed once per window, frozen in the data folder, and reused by every later
  replay of that window. Changing the judge model or prompt re-judges the window and resets every
  comparison made against the old rulings; the report says which rulings it used.
- The judge never sees whether the editor cited a group, or which feeder found it.

### 15.3 Scores

- **Recall:** of groups ruled *mattered*, the share the editor cited by the second nightly run
  after the group's first item.
- **Noise:** of cited groups, the share ruled *didn't matter*.
- **Feeders:** recall and noise for groups found by retrieval only, attach only, and both.
- **Calendar:** of scheduled events in the window, the share with a set-up written before.
- **Baseline:** the share of *mattered* groups that the briefs actually delivered on the same days
  mentioned. Those briefs predate phase 1 (on from 2026-11-23), and the report says so.

**Bars are pre-registered by the operator before the first run.** Proposal: recall ≥ 0.8, noise ≤
0.25, recall above the baseline.

### 15.4 Auditing the judge and the grouping

- Judged groups go out as single taps (*agree* / *disagree*), at most 5 a day in the follow-up
  message, until 60 are in (about 12 days), split between *mattered* and *didn't*. The judge
  counts only when the lower bound of the 95% interval on agreement is at least 0.70 (about 50 of
  60 agreeing). At 30 taps the interval was too wide to mean anything (24 of 30 spans 0.63–0.90).
  Below the bar, the judge's prompt is fixed and the window re-judged before any score counts.
- *Same event?* taps on 20 groups check the grouping thresholds (§6.1), the same way.
- After launch, the in-brief taps (§14) take over.

### 15.5 Use

Every change to the editor's prompt, model, feeders or retrieval reruns the replay on the same
window and reports before and after. It is the regression test, and its cost is priced in the
cost pass (§21).

## 16. Schedule

| Job | When | Does |
|---|---|---|
| `group` (new) | every 15 min | embeds new items, joins or opens groups |
| `attach` (built, repurposed) | every 30 min | Haiku proposes candidates (§7.3) |
| `alerts` (new) | every 30 min | §11 |
| `editor` (new) | 01:30 candidates; pass 1 and pass 2 batches through the night; mid-day after a confirmed alert | §7 |
| `submit`, `collect` | 04:30, 06:00 | unchanged schedule; the brief reads §10 |
| `propose` | 07:00 | §12 |
| `weekly` | Sun 21:00 | connections (§12) |

`burst` from the 11-21 spec folds into `alerts`. The nightly window (01:30 to 04:30) must hold two
batch submissions; the 11-21 spec's batches have finished in minutes, and a run not landed by
submit falls back per §7.5.

## 17. Second wave

Designed for, not built now:

- **The analyst corpus:** a `search_podcasts` step over the existing podcast archive, and the
  document notes when the analysts' read shifts.
- **The framing map:** per group, how each vantage frames it, and which side went quiet.
- **The query interface** (`nyy.2`): reads the documents and timelines.
- **The thesis stress board:** per thesis assumption, the evidence for and against across all
  storylines. It collects evidence only; deciding stays with the workbench (charter Approach A).
- **Event skeletons** (`uk1`) are largely absorbed by the calendar and scenarios. Its description
  asks for "scenarios with rough probabilities", which §3 forbids; `uk1` is to be rewritten or
  closed on that point.

## 18. Settings

| Knob | Default | Does |
|---|---|---|
| `STORYLINES_ENABLED` | false | `group`, `attach`, `editor`, `propose` |
| `STORYLINE_BRIEF_ENABLED` | false | §10 |
| `STORYLINE_ALERTS_ENABLED` | false | §11 |
| `STORYLINE_WEEKLY_ENABLED` | false | §12 weekly |
| `STORYLINE_EDITOR_MODEL` | set in the cost pass | both passes, mid-day runs |
| `STORYLINE_JUDGE_MODEL` | set once, then pinned | the hindsight judge (§15.2); changing it re-judges |
| `STORYLINE_MAX_RUNS_PER_NIGHT` | set in the cost pass | nightly runs, pinned first, then calendar due, then most candidates; the rest keep their last state and their groups stay unclaimed |
| `STORYLINE_ALERT_MODEL` | set in the cost pass | §11 checks |
| `STORYLINE_ATTACH_MODEL` | `claude-haiku-5-5` | the attach feeder |
| `STORYLINE_EDITOR_MAX_CANDIDATES` | 60 | §7.3 |
| `STORYLINE_EDITOR_MAX_READS` | 5 | §7.2 |
| `STORYLINE_GOOGLE_DECODE` / `STORYLINE_GOOGLE_DECODES_PER_NIGHT` | true / 30 | §6.2 fetch policy |
| `EMBEDDING_PROVIDER` | `modal` | `modal` or `openai` (§6.1) |
| `EMBEDDING_MODEL` | builder's pick (§25) | per provider |
| `EMBEDDING_MODAL_URL`, `EMBEDDING_GPU_RATE` | — | the endpoint, and the per-second rate it is priced at |
| `STORYLINE_GROUP_WINDOW_HOURS` | 72 | §6.1 |
| `STORYLINE_MAX_LIVE` | 60 | as the 11-21 spec §5.2 |
| `STORYLINE_DORMANT_DAYS` / `STORYLINE_RETIRE_OFFER_DAYS` | 14 / 60 | §12 |
| `STORYLINE_SURGE_FACTOR` | 3 | §11 |

Each is a `KNOBS` entry with its compose anchor line, resolved once per pass. Secrets (the Modal
token, the OpenAI key) go in the environment, never in a settings row.

## 19. Privacy

The 11-21 spec §13 stands, and adds:

- **Never in git:** documents, scenario and constraint text, calendar set-ups, the primary-source
  list (it can name a holding), feedback rows, replay results, the fetch probe's output.
- **External services:** embeddings go to the operator's own Modal deployment. Article fetches
  send only a GET for a public URL. The one exception is `EMBEDDING_PROVIDER=openai`, which sends
  article and storyline query text to OpenAI; it is off by default and only the operator turns it
  on.

## 20. What happens to the 11-21 spec

| 11-21 section | Status |
|---|---|
| §1 what this builds | phase 1 stands (built); phase 2 is this spec |
| §2 decisions | D1–D3, D6–D9 stand; **D4 kept** (consensus, §5.1); **D5 replaced**: attach proposes, the editor decides |
| §3 phase 1 | stands (built) |
| §4 the storyline | replaced by §5 |
| §5.1 storage | stands in principle; tables per §22 |
| §5.2 bounds | 60 live stands; the 20-a-day update ceiling becomes a cost dial (§21) |
| §5.3 prompts | replaced by §7.3 |
| §6 pipeline, §6.1 brief timing | §6 replaced by §16; §6.1 stands |
| §7 brief | stands with §10's changes |
| §8 attachment and offline check | **retired**; attach survives as a feeder (§7.3); the check is replaced by §15 |
| §9 alerts, proposals, merges, weekly | format and contracts stand; triggers per §11–12 |
| §10 defaults | lifecycle per §12; seeding per §13; the rest stands |
| §11 budget | redone in the cost pass (§21) |
| §12 settings | replaced by §18 |
| §13 privacy | stands, plus §19 |
| §14 data model | replaced by §22 |
| §15 bead disposition | replaced by §23 |
| §16 out of scope | fetching article pages moves in (§6.2) |
| §17 success | replaced by §24 |
| §18 build order | replaced by §23 |

## 21. Cost (the pass after approval)

Priced in the next step, against the floor (£20 a month) and the ceiling (the free API-credit
allowance, about $100). **The unit is runs per night, priced at the bound:** the design runs every
storyline with a candidate, dormant ones included, and the attach feeder alone puts about 277
items a day on storylines, so expect most of the 60 to run most nights unless
`STORYLINE_MAX_RUNS_PER_NIGHT` caps it. Rough figures so far, all estimates:

- **Editor, two passes, batch:** about $45 a month on Sonnet 5.5 and $90 on Opus 5.5 at 20 runs a
  night; about $135 and $270 at 60 (the red team's scaling of the same per-run figures). The
  multi-turn loop was $186–588 at 20, which is why it is out.
- **Embeddings:** Modal on a T4 GPU, about $10–15 a month, mostly model loading at each wake-up,
  probably inside Modal's $30 monthly credit; OpenAI, under $1 a month. Measured once running
  (§6.1).
- **The brief** is ~$9 or ~$19 a month (two measurements disagree; `brief_cost.py` settles it).

The dials, roughly in order of size: the editor's model; `STORYLINE_MAX_RUNS_PER_NIGHT`;
candidates per run and reads per run; the alert check's model; how often the replay runs; the
brief's web searches.

### 21.1 Cost measures built in (not dials)

- **Embed each text once:** by the item's content hash, so syndicated copies with identical text
  share one vector.
- **Query vectors are cached** per storyline and recomputed only when the text they come from
  (questions, constraints, consensus, signposts) changes, by hash.
- **Embed short text only:** title plus summary, capped at about 256 tokens; full article text is
  read by the editor, never embedded.
- **Many inputs per embedding call** (up to the provider's limit, 2,048 for OpenAI), never one
  call per item.
- **OpenAI's Batch API (half price) for bulk work:** the backfill, a re-embed after switching
  provider, and replay embeddings. A batch holds at most 50,000 inputs and may take up to 24 h, so
  live items use the normal endpoint. OpenAI gives no caching discount on embeddings; the two
  hash caches above do that job.
- **Modal:** GPU memory snapshots to shorten cold starts (Modal's own examples: about 20 s down to
  about 2 s; marked experimental, so measured before relying on it), and a short idle window. At a
  15-minute cadence a 20-minute window would keep the GPU on all day (roughly $400 a month on a
  T4), so the window stays short and each call pays one cold start. `group` may move to every 30
  minutes, in step with `attach` and `alerts`, to halve the wake-ups.
- **Anthropic:** both editor passes and the hindsight judge go through the batch API. Pass 2
  repeats pass 1's input, and the system prompt and tool definitions are the same for every
  storyline, so both are cached (cache reads cost a tenth of input; batch and cache discounts
  stack, cache hits inside a batch are best-effort). The attach feeder is not cached: at 30-minute
  spacing a 5-minute cache expires between calls and a 1-hour cache costs double to write.
- **Frozen judge rulings** (§15.2): a rerun of the replay pays for the editor only.

## 22. Data model (for the plan to refine)

Migration `0020`. Keeps `storylines` and `storyline_proposals` from `0019`. Every table has
`created_at`; code never DELETEs.

- `item_embeddings` (item_id, model, dim, vector): several models may coexist (§6.1)
- `embedding_calls` (provider, model, items, tokens, runtime_s, cold_start, cost)
- `item_groups` (id, opened_at, closed_at, title, outlet_count, has_primary)
- `item_group_members` (group_id, item_id, joined_at, similarity)
- `storyline_runs` (id, storyline_id, kind `nightly|midday|first|replay`, started_at, landed_at,
  outcome `landed|failed|deferred`, reason, usage)
- `storyline_candidates` (run_id, group_id, feeder `retrieval|attach|primary`, read_in_full)
- `storyline_states` (run_id, storyline_id, where_it_stands, consensus)
- `storyline_actors` (run_id, storyline_id, name, wants)
- `storyline_constraints` (storyline_id, type, actor, text, since, retired_on, run_id)
- `storyline_scenarios` (storyline_id, label, text, tells_apart, added_on, retired_on, reason,
  run_id)
- `storyline_scenario_marks` (scenario_id, group_id, direction, why, run_id)
- `storyline_questions` (storyline_id, text, raised_on, answered_on, answer, run_id)
- `storyline_calendar` (storyline_id, event, on_date | window, source_ref, setup, setup_at, late,
  outcome, outcome_at, status)
- `storyline_timeline` (storyline_id, on_date, line, run_id)
- `storyline_citations` (run_id, group_id, target_kind, target_id)
- `storyline_signposts`, `storyline_links`, `storyline_alerts`, `storyline_merge_offers` as the
  11-21 spec §14
- `storyline_sources` (storyline_id, source_id, proposed_at, accepted_at, removed_on)
- `feedback_taps` (kind, ref, answer, tapped_at)

`thesis_evidence.storyline_id` already exists (0018 column, 0019 foreign key).
`item_attach_decisions` and `item_attachments` keep serving the attach feeder; whatever `0020`
changes in them, the plan counts their rows on the host first.

## 23. Build order and beads

Four plans, each ending in something checkable on the host:

1. **Groups:** `bqa.7` (pgvector), the Modal embedding service and the OpenAI provider behind one
   interface, `embedding_calls`, `group`, hybrid retrieval with `as_of`, threshold calibration and
   the model pick on captured data, the fetch probe re-run with a real extractor.
2. **The editor and the replay:** the seed fix and re-seed (§13), the document tables, candidates
   from both feeders, the two-pass editor, validation, first runs, the replay and hindsight judge,
   the audit taps. **Ends with the replay's scores against the operator's pre-registered bars;
   they gate plan 3.**
3. **The brief:** §10, claimed and unclaimed groups, feedback taps, thesis evidence with the 11-21
   spec §3.3 no-double-counting rule, `/pin` and `/unpin`, primary sources (RSS/Atom) and their
   proposals.
4. **Alerts and the rest:** §11, proposals, lifecycle, merges, the weekly pass, `page` primary
   sources.

Beads:

- `al7.3` closes with plan 2a's surviving parts (attach becomes a feeder); its offline check is
  retired here.
- `al7.4` and `al7.5` are superseded by plans 3 and 4.
- `al7.6` folds into plan 2 (§13).
- `sse` closes as moot (no attacher decides).
- `bqa.7` becomes part of plan 1 and moves to P1.
- `c5k` closes when the attach feeder runs on `claude-haiku-5-5`.
- `6wc` stays superseded, as the 11-21 spec ruled.
- The clean-up bead the 11-21 spec promised (remove the claim-ledger and comprehension code once
  the new brief runs without rollback) is filed with plan 3.
- `uk1` is rewritten or closed (§17).
- `jlo` (retweets with no body) stays open.

## 24. Success

Judged by the operator, against the briefs actually delivered:

1. The replay clears the pre-registered bars (§15.3).
2. The brief says what changed in terms of constraints, consensus, scenarios and questions, with
   earlier dates, not only what happened.
3. Scheduled events arrive with a set-up written before them, and the outcome is compared with it.
4. The threads and the Sunday connections surface links the operator would not have made.
5. For each thesis, `/evidence` shows what built it up or eroded it since a given date.

## 25. Left to the builder

The embedding model (picked by a retrieval check on captured data, 4B–8B, open), grouping
thresholds, retrieval weighting, prompt wording, the extraction library for article text, the
replay window's dates, how pgvector stores vectors of several dimensions, and table details within
§22.

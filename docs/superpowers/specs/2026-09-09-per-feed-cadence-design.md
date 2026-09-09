# Per-feed capture cadence — Design

**Date:** 2026-09-09
**Issue:** `news-brief-b42.5` (Epic 2)
**Parent spec:** `docs/superpowers/specs/2026-09-02-continuous-capture-design.md` §8
**Depends on:** `b42.2` (roll-off measured, closed), `b42.4` (capped proxies, closed)
**Status:** Design, revised after red-team review
**Review:** `docs/superpowers/reviews/2026-09-09-per-feed-cadence-redteam.md`

> **REVISED after red-team review (2026-09-09).** A hostile review with fresh context found
> three objections; all three were verified against the code and all three landed. The
> largest is that **every turnover figure this bead rests on is computed from spans of at
> most 45 minutes** (§3.2), making the original `INTERVAL_HI = 240` a 5.3× extrapolation
> beyond any observation. The design is now **phased** (§2.1), its ceiling lowered (§3.2),
> and a guard that was structurally inert has been deleted (§6.3). §14 records what changed.

---

## 1. The decision, and the recommendation it overrides

`b42.5` carried a standing recommendation to **defer**. That recommendation was
investigated on 2026-09-09 and its two supporting arguments were found to be weaker
than stated, while a third argument — never made — turned out to be the strongest one.
This section records the whole finding, because a spec that silently reverses a
recommendation invites the reversal to be re-litigated later from worse evidence.

### 1.1 What the investigation established

**The request budget is self-set.** `scripts/measure_roll_off.py:116`:

```python
# One user agent's daily request budget across every feed. Below what the
# 30-minute global cadence spends today (26 feeds x 48 = 1248), because the
# point of per-feed intervals is to spend it where it buys something.
BUDGET_PER_DAY = 1200
```

1200 was chosen *deliberately under* current spend so `fit_budget` would be forced to
reallocate rather than approve the status quo. "1248 against a budget of 1200" is
therefore not an overrun. There is no external rate limit being approached.

**The one rate-limit incident in this project's history has a different mechanism.**
`brief.py:1923` blames the documented 429 on a **self-hosted** Nitter throttling
requests that arrive close together, caused by *adjacency* — two X feeds sitting
consecutively in `RSS_FEEDS` — not by volume. Capture already ships the correct fix for
that failure (`order_by_host` plus `HOST_GAP_SECONDS`). The incident does not
generalise into a volume argument, and treating it as one would be reasoning from a
remembered symptom rather than a verified cause.

**The daily brief is not affected either way.** `brief.py:3207` fetches its own feeds at
collect time. Capture writes only to `items`, consumed solely by `comprehend.py`. So
slowing a feed cannot make the brief miss a story; the cost lands on KB freshness and on
items that roll off unseen.

**The feed count is user-growable at runtime.** `capture_sources()` is
`brief.RSS_FEEDS + load_temp_sources()`, so every source added through the Telegram
`/addsource` wizard adds ~48 requests/day with no deploy and no budget check. This looked
like the decisive argument to build — until followed one step further: a newly added feed
has no measurement and takes the default cadence, so **per-feed intervals reduce the fixed
cost and leave the marginal cost of growth exactly where it is.**

### 1.2 Why it is being built anyway

On the merits alone the answer was no: the change is cheap and can be made safe, but
every beneficiary column is zero or negative. The brief is unaffected, the KB gets
marginally *less* fresh, no resource is constrained, and the rate-limit history has a
different cause with a shipped remedy.

The decision to build was taken on a different basis, recorded here so it is not
mistaken for a cost/benefit conclusion: **per-feed policy machinery is wanted as
groundwork.** A subsystem that derives per-feed policy from stored measurement is a seam
that later work — per-feed retry budgets, per-feed capture windows, per-feed staleness
tolerances — can hang off. The request saving is a side effect, not the goal.

That motive changes the design target. Interfaces and auditability matter more than the
saving, which is why §4's provenance columns and §5's anti-drift construction are
load-bearing rather than nice-to-have.

### 1.3 The monitoring hazard this must not ship

`capture.failing_feeds` computes `tolerance = _interval_minutes() * STALE_AFTER_INTERVALS`
= 30 × 3 = **90 minutes**, then filters:

```python
if last_try > cutoff and (last_ok is None or last_ok < cutoff)
```

The `last_try > cutoff` clause is deliberate: its docstring says it exists to stay quiet
about a feed no longer polled at all, because a feed dropped from `RSS_FEEDS` would
otherwise alert forever. **That reasoning is sound only while every feed shares one
interval**, where "not polled recently" can only mean a human removed it.

Move a feed to a 120-minute interval (§3.2's lowered ceiling) and it sits inside that
90-minute window for 75% of its cycle — blind for the other 25%. At the original 240 it
was blind for 63%. Lowering the ceiling shrinks this hazard but does not remove it: a
failing feed still alerts intermittently or not at all, and the
*absence* of an alert would stop carrying information for most of the fleet — which is
"a dropped feed looks identical to a quiet one", relocated one layer up where nothing
about the scheduler looks wrong.

**§8's rework is therefore a precondition of shipping, not a follow-up.** Reworking the
alerting in a later commit means running production with its own detection disabled.

`liveness` is **not** affected: the scheduler tick stays at 30 minutes and `capture_runs`
still gets a row every fire.

---

## 2. Scope

### 2.1 Two phases, because the second is a superset of the first

The red-team's simpler alternative — `poll_every_minutes` as a key on the `RSS_FEEDS`
dicts, the repo's proven per-feed seam (`capture_url`, `outlet`, `kind` all work this way)
— is **not a competing design**. Both versions need the same due-check and the same
`failing_feeds` rework, and they differ in exactly one lookup: where an interval comes
from. So this ships in two phases rather than choosing.

| | Phase 1 | Phase 2 |
|---|---|---|
| interval source | `feed["poll_every_minutes"]`, hand-set | `feed_cadence.interval_minutes`, measured |
| due-check (§7) | yes | unchanged |
| `failing_feeds` rework (§8) | yes | unchanged |
| table, module, job, knobs (§4–§6) | no | yes |
| ships in | one commit | its own |

**Phase 1 delivers the entire behaviour change.** Per-feed polling works, alerting is
correct, requests drop. What it lacks is self-maintenance: the numbers are hand-set and go
stale invisibly, which is §3's option A and the reason Phase 2 exists.

The phase boundary costs one function's lookup line. `due_feeds(conn, feeds, now)` keeps
its signature; only the source of `interval_minutes` changes. Nothing in §4–§8 is
discarded — it is built against a system that already runs, which is the point.

**Phase 1 is what this spec asks approval to build.** Phase 2 is designed here so the
Phase 1 interfaces are shaped to receive it, and is not started until Phase 1 has run.

### 2.2 In and out

**In (Phase 1):** `poll_every_minutes` on feed dicts; a due-check in `capture.run`; the
`failing_feeds` rework.

**In (Phase 2):** a `feed_cadence` table and module; a daily `cadence` job running a probe
rotation; promotion of `measure_roll_off.py`'s computation core into the shared module.

**Out:** changing which feeds exist; changing capture's 30-minute scheduler tick;
`item_drought` (see §8); the brief's own fetch cadence.

**Not depended on:** the target interval table from the 2026-09-08 `b42.2` run. §6.5
starts every feed unmeasured, so no prior number is imported. This is deliberate, and it
also sidesteps a discrepancy worth recording: the bead's prose describes "18 native feeds
slow (16 at the 240 ceiling, Al Jazeera 111, Times of Israel 161), 8 gnews proxies held
at 30, 2 UNKNOWN unchanged", which totals 28 against a 26-feed list. The arithmetic does
not reconcile and the authoritative table is the measurement output, not the prose. **No
number in that summary should be treated as a target.** Every projection in this spec is
illustrative and labelled as such.

---

## 3. The feedback loop, and the control that breaks it

The acceptance criterion asks for intervals "derived from stored measurements rather than
hardcoded". Read literally that means a job periodically recomputing intervals from
`feed_polls` / `feed_sightings`. That design has a loop, and the measurement script names
the mechanism itself:

> `feed_sightings` records an item that was SEEN. An item published and gone between two
> polls leaves no row anywhere — so the current 30-minute cadence bounds what is
> observable about the cadence, and every loss figure here is a FLOOR, never a ceiling.

**Observability is a function of the poll rate, and the poll rate is the output.** Slow a
feed and its turnover is observed more coarsely; items that come and go inside the gap
leave no trace. Measured turnover then looks *longer* than it is, justifying a further
slowdown — a ratchet toward `INTERVAL_HI` whose contradicting evidence is precisely what
the slowdown destroyed. `SAFETY = 4` sets how many iterations it takes; it does not break
the loop. This is `a-detector's-hits-are-a-FLOOR` wired into a control system.

Three ways out were considered:

| | approach | verdict |
|---|---|---|
| A | **Frozen intervals** — measure once, never recompute automatically | No loop, because no loop. But stored numbers go stale invisibly as publishers change cadence. Right answer for a one-off saving; wrong as an end state. **This is Phase 1** (§3A) — shipped knowingly as a stepping stone, not as the design. |
| B | **Fast-probe control** — every feed periodically returns to nominal cadence for a measurement window | **Chosen.** Breaks the loop by construction; every interval is derived from data gathered at nominal cadence. |
| C | **One-directional ratchet** — intervals may only tighten automatically | Safe direction only, but never converges upward, so a feed that genuinely slows is never recognised. |

**B is chosen.** It is the only option where a stored interval still means something a
month later, and the "sample this feed at high rate for a fixed window" primitive is
itself reusable groundwork.

### 3.1 Rotation arithmetic

Only slowed feeds need probing. The Google News proxies are pinned at nominal (§4.3) and
are therefore self-measuring; so is any feed at the default. That leaves the native feeds
in the rotation — 18 today, 20 after the two Nitter additions in §11.

Rotation period is `ceil(rotating_feeds / slots) * probe_days`. At 3 slots and a 4-day
window that is ~24 days for 18 feeds, ~27 days for 20. **The period is derived, never
configured** — adding feeds lengthens it, and §7.4's stalled-rotation alert is derived
from the same expression so it retunes automatically.

A probed feed costs ~48 requests/day instead of ~6, so each slot is ~+42/day. Every
plausible slot count lands far below current spend. **Detection latency is effectively
free here and was chosen on merit, not cost:** 3 slots, because a publisher cadence
change is a slow event, 24–27 days catches one inside a month, and every feed is
re-measured ~14 times a year. One slot would be ~72 days, long enough for a feed to lose
items for two months with no mechanism able to notice.

### 3.2 The ceiling is an extrapolation, and is lowered to 120

**Every turnover figure this bead rests on is computed from spans of at most 45 minutes.**
`scripts/measure_roll_off.py:151` keeps only consecutive successful polls closer together
than `nominal * MAX_GAP_FACTOR`, and `MAX_GAP_FACTOR = 1.5`:

```python
def poll_pairs(polls, nominal_minutes):
    """Consecutive SUCCESSFUL polls close enough together to compare."""
    ok = sorted((p for p in polls if p.failure is None), key=lambda p: p.polled_at)
    limit = timedelta(minutes=nominal_minutes * MAX_GAP_FACTOR)
    return [(a, b) for a, b in zip(ok, ok[1:]) if b.polled_at - a.polled_at <= limit]
```

The original `INTERVAL_HI = 240` is therefore a **5.3× extrapolation beyond any observed
span**. "16 natives at the 240 ceiling" is an inference about a regime for which no
evidence exists. This is a property of the b42.2 measurement, not of this design, and no
architecture repairs it — storing the number differently does not change what backs it.

The settling experiment is direct and was considered: poll one feed at 5 minutes and at
the candidate interval concurrently, and count what the slow reader missed. It was
**deliberately not run** — the project had accumulated more measurement than building, and
the decision was to ship and tune from live signal instead.

**`INTERVAL_HI` is therefore lowered to 120** — a 2.7× extrapolation rather than 5.3×.
The justification is short and follows from §1.2: **the saving has no beneficiary, so
there is no reason to take extrapolation risk to get more of it.** A ceiling of 120 keeps
most of the request reduction and stays far closer to observed ground.

**This remains tunable from a real signal**, which is what makes shipping without the
experiment defensible rather than hopeful. `single_sighting_fraction` rises when items
begin slipping between polls, and `measure_roll_off.py` already computes it. Raising the
ceiling later is a decision with an observable attached; raising it now would not be.

---

## 3A. Phase 1 — intervals as feed dict keys

Phase 1 is §7 and §8 with the interval read from the feed dict instead of a table. Nothing
else in this spec is required to ship it.

**The key.** An optional `poll_every_minutes` on `RSS_FEEDS` entries, absent by default:

```python
{
    "name": "Al Jazeera",
    "url": "https://www.aljazeera.com/xml/rss/all.xml",
    "poll_every_minutes": 111,
    ...
}
```

Absent means nominal, which keeps §4.4's fail-open property for free: a feed nobody has
tuned polls exactly as it does today. Rank-ordered feeds (§4.3) never carry the key, and a
test asserts they never do — a Google News proxy with a `poll_every_minutes` is a
developer error, caught the way `tests/test_capture.py` already catches an outlet
mismatch.

**Where the numbers come from.** The 2026-09-08 `b42.2` run, clamped to the lowered
`INTERVAL_HI = 120` of §3.2. They are hand-set, stale from the moment they land, and
carry no provenance — which is the honest description of option A in §3 and the entire
reason Phase 2 exists. §2's warning applies: the bead's prose summary of that run does not
reconcile arithmetically, so the authoritative source is the measurement output, not the
summary.

**Validation is `single_sighting_fraction` (§3.2), watched after the change.** A rise
means items are slipping between polls and an interval is too long. This is the live
signal that makes shipping hand-set numbers a tunable decision rather than an unobserved
one.

**Rollback is deleting the keys.** No migration, no knob, no table state.

**What Phase 1 does not get:** re-measurement, provenance, staleness detection, and any
alert about the cadence system itself. Those are §5–§7 and they are the difference between
a working feature and a maintained one.

---

## 4. Data model

### 4.1 `feed_cadence`

One row per feed, keyed on `source_name` — matching `feed_polls`, which also uses a bare
`TEXT` with no foreign key, because feeds come from code and a volume file rather than a
table.

| column | meaning |
|---|---|
| `source_name` | primary key |
| `interval_minutes` | NULL = never measured |
| `turnover_minutes` | the measurement the interval was derived from |
| `measured_at` | when the computation ran |
| `window_from`, `window_to` | the observation window the turnover came from |
| `probe_started_at` | non-NULL ⇒ currently probing at nominal cadence |
| `updated_at` | |

Three columns exist purely so the number cannot be read without its precondition.
`a-ledger-dates-what-it-records` cost a day when `schema_migrations.applied_at` was used
to date something it could not date; the countermeasure recorded there was to put the
precondition in the output beside the number. Here it lives in the row: an interval whose
`window_to` is three months old is *visibly* stale rather than silently authoritative,
and §8 can report which tolerance it chose and why.

### 4.2 State is derived, not stored as an enum

`probe_started_at IS NULL` is NORMAL; non-NULL is PROBING. A probe ends when
`now() - probe_started_at >= CADENCE_PROBE_DAYS`, at which point turnover is computed
**over exactly that window**, the interval is written with its provenance, and
`probe_started_at` is cleared.

So `window_from` / `window_to` are the probe's own bounds. The measurement and the
controlled conditions that produced it are the same interval by construction, not by
coincidence — there is no way to write an interval whose provenance describes a different
period.

### 4.3 Pinned feeds are derived, never listed

`rank_ordered(feed)` returns true for `news.google.com` hosts. A rank-ordered feed is
never assigned an interval above nominal, whatever its turnover says.

The reason is structural: Google News returns **relevance-ranked** results under a
100-entry cap, so an item's absence from a poll does not mean it departed. Turnover is
unmeasurable in principle, not merely unmeasured.

Deriving the predicate rather than listing names matters, and a measured correction
motivates it: **the bead's "8 gnews proxies" and `capture.py`'s "four Google News
proxies" are different sets.** Eight of the 26 feeds are Google News proxies by host;
only four carry a `capture_url` override. A predicate built on `capture_url` presence —
the obvious shortcut — would have pinned half the set. A hand-maintained list of eight
names is likewise one new proxy away from being silently wrong, which is the same failure
`tests/test_packaging.py` exists to prevent.

### 4.4 Reads fail open

A missing row, a NULL interval, an unparseable value, or an unreachable table all mean
"poll at nominal". Every failure in this subsystem degrades to today's behaviour, and the
worst case spends requests — the only resource it was ever optimising. The failure mode
is therefore self-limiting, which is what converts "a feed silently stops being polled"
from the default failure into one that has to be written deliberately.

**Reporting does not fail open.** `/capture` and the cadence report render an
unmeasurable or stale interval as UNKNOWN rather than printing the default as though it
were a measurement. Fail open in the poller; refuse in the display.

---

## 5. The module, and the script as a view of it

### 5.1 `feed_cadence.py`, new top-level module

Being top-level triggers the three-part packaging checklist — `Dockerfile` COPY, workflow
paths, workflow ruff file lists — enforced by `tests/test_packaging.py` since `config.py`
broke production by missing all three. A miss fails CI rather than production.

Public surface, deliberately small — three consumers, three entry points, no shared
mutable state:

```python
rank_ordered(feed) -> bool
turnover_minutes(conn, source_name, window_from, window_to) -> float | None
interval_for(turnover, *, rank_ordered) -> int | None
due_feeds(conn, feeds, now) -> list[dict]      # capture's read path
advance(conn, feeds, now) -> CadenceReport     # the cadence job's write path
tolerance_minutes(conn, source_name) -> int    # failing_feeds' read path
```

### 5.2 What moves, and what stays

**Moves out of `scripts/measure_roll_off.py`:** the computation core only — window
reconstruction, turnover estimation, `proposed_interval_minutes`, and the `SAFETY`,
`INTERVAL_LO`, `INTERVAL_HI` constants that shape it.

**Stays in the script:** everything that renders. The flick / ovl / worst / singles
columns and the long explanatory docstring are a human diagnostic with no production
consumer. They sit *on top of* the shared computation rather than beside a second copy.

`reconstruction-drifts-from-production` cost two days because a diagnostic reimplemented
production's query and the two silently diverged — and a control comparing two copies of
the same logic could not notice, because both sides moved together. Here the script calls
the production function; there is no second copy to drift. §9.3 makes that testable rather
than aspirational.

### 5.3 A latent bug fixed in the move

`scripts/measure_roll_off.py:121` hardcodes `NOMINAL_MINUTES = 30`, then line 443 uses it
to compute a prediction:

```python
expected = len(polls) * (1440 / NOMINAL_MINUTES) * span_days
```

That is exactly the defect `capture._interval_minutes()` was written to prevent — a
constant tracking another knob. Retime capture and the script keeps predicting against 30,
printing a wrong expected poll count and a line claiming a feed is "lossless AT 30
MINUTES". It is worse than a stale label because a *generator* emits it: per
`the-prediction-had-a-generator`, the program is the defect and the number only its
symptom. The moved code reads the capture schedule the way `_interval_minutes()` does.

`scheduler.py` imports nothing from the project, so `feed_cadence` can import it at
module level without a cycle.

### 5.4 `fit_budget` is not on the production path

The script slows every feed proportionally until the total fits `BUDGET_PER_DAY`. That
made sense when the script proposed a whole table at once. In production it would mean
**one feed's measurement changing another feed's interval**, at which point the
provenance columns of §4.1 would be false for any feed the fitter touched.

Instead every interval is that feed's own measurement, and the cadence job **checks** the
projected total and alerts if it is unacceptable rather than silently reallocating — a
silent proportional slowdown being precisely the unattributable outcome
`fail-closed-needs-status-not-count` warns about. `fit_budget` stays in the script as a
"what would a budget do to this table" hypothetical, where hypotheticals belong.

`BUDGET_PER_DAY` consequently stops being load-bearing. As an alert threshold it would
need a justification it does not have, so §7.4's alert compares against **nominal cadence
for every feed** — a number the system already knows — rather than a picked constant.

---

## 6. The cadence job

### 6.1 Schedule

```python
Schedule("cadence", "daily", "08:00", None, grace_minutes=240)
```

Daily is sufficient granularity for a 4-day probe: a transition lands within 24 hours of
its true mark. Unlike `monitor`, a stale catch-up is genuinely useful here — a late run
still advances the rotation correctly — so the grace is wide. 08:00 sits clear of collect
(06:00) and backup (07:00, 180 grace).

### 6.2 Per-feed transition, over live feeds only

- `probe_started_at` set and `CADENCE_PROBE_DAYS` elapsed → compute turnover over
  `[probe_started_at, now]`, write `interval_minutes` with provenance, clear the probe.
- `probe_started_at` set, not elapsed → untouched.
- otherwise → eligible for promotion.

Then fill up to `CADENCE_PROBE_SLOTS` from eligible feeds, excluding `rank_ordered` ones,
ordered by `measured_at NULLS FIRST, source_name`.

Oldest-measurement-first rather than round-robin: it self-heals when a probe is missed or
a feed is added mid-cycle, and it makes "how stale is the stalest measurement?" directly
answerable — which is the number §7.4 watches.

### 6.3 Two guards on whether a probe may produce a number

A probe window is not automatically a valid observation.

**Coverage.** Capture can be down, or the feed can fail, and a window with holes yields a
turnover estimated from data that is not there. If successful polls fall below a fraction
of what nominal cadence predicts, the probe **refuses**: `interval_minutes` is left
unchanged and the probe restarts.

The fraction is a guess and is treated as one. It becomes the knob
`CADENCE_MIN_PROBE_COVERAGE` (default 0.8) and the report prints the coverage each probe
actually achieved, per `shared-helper-carries-first-callers-tuning`: make a guessed value
a knob and log the real number.

**A largest-gap cap was specified here and has been DELETED.** It said an interval may not
exceed the largest observation gap inside its own window, computed as
`min(turnover / SAFETY, INTERVAL_HI, largest_gap_minutes)`, and claimed to need no
constant. Both halves were wrong, and the way they were wrong is worth keeping:

- **It was structurally inert.** A probe runs at nominal cadence, and `poll_pairs`
  (§3.2) already discards every pair over 45 minutes — so over the data actually used the
  largest gap is **≤45 by construction**, and the cap would clamp every interval to ≤45,
  silently preventing the feature it was guarding. Computed over *all* polls instead, it
  inverts: a window with holes yields a *longer* permitted interval, rewarding exactly the
  outages it was meant to punish.
- **"Needs no constant" was false.** `MAX_GAP_FACTOR = 1.5` is precisely such a constant,
  and it lives in the file being promoted.

It would also have passed its own pre-registered test, which fed a synthetic gap value to
a pure function and never asked whether the real pipeline could produce one — a vacuous
test authored inside the section arguing against vacuous tests. §9.5's mutation row for
this guard is deleted with it; removing an inert guard makes the system work *better*, so
the test would have been asserting the bug.

Coverage (above) already handles outages, and does so on aggregate evidence rather than on
a single extremal value.

### 6.4 A removed feed must not hold a slot

If a feed leaves `RSS_FEEDS` or `sources.json` mid-probe its row stays — retirement is a
state, not a `DELETE`, following the rule the KB schema already keeps — but it is no
longer live and **must not count toward the slot budget**. Otherwise the rotation runs
silently at reduced concurrency forever with nothing indicating why. §9.4 tests this
specifically.

### 6.5 Bootstrap: start cold

No seeding from the 2026-09-08 run. Those numbers came from a window that was not a
controlled probe, and importing them would mean writing `window_from` / `window_to` values
approximating a run reconstructed from memory — the precise failure the provenance columns
exist to prevent.

Starting cold costs one rotation period of status-quo polling before convergence, which is
free: §1.2 established there is no beneficiary waiting, and it exercises the probe path for
every rotating feed before any of them is trusted. The first slowdowns appear after
`CADENCE_PROBE_DAYS`.

With every feed unmeasured, `measured_at NULLS FIRST` leaves a full tie; `source_name`
breaks it so the rotation is reproducible and testable.

The cold start has a second benefit found after the fact: it protects `item_drought`'s
learned baseline (§8.3).

---

## 7. Capture's due-check

### 7.1 Due-ness is measured from the last attempt

`feed_polls` already carries `(source_name, polled_at DESC)` as an index, so this is one
query.

Keying on last *attempt* rather than last *success* is deliberate. Last-success would
retry a broken feed every 30 minutes until it recovered — the worst possible response to
a 429, and pointless against a 403, which `source-fetch-failure-modes` says must never be
retried. Last-attempt means a feed's request rate equals its interval regardless of
health. Recovery is noticed more slowly, but §8 is what reports it; the retry is not.

### 7.2 A not-due feed writes no `feed_polls` row

It was not polled. Writing a row would corrupt the history the cadence measurement itself
reads — `entries_seen` denominators, poll counts, and §6.3's coverage guard all assume a
row means an attempt.

`Tally` instead gains `feeds_not_due`, so the pass reports what it deliberately skipped.
Silence about a skip would reintroduce exactly the ambiguity `capture_runs` exists to
remove.

### 7.3 Fail open

Any exception, missing row, NULL interval, or unreadable table inside `due_feeds` returns
*all* feeds.

### 7.35 A shrinking due set breaks host interleaving, and the regression test hides it

Found by red-team review and verified by computation over the real feed list. `order_by_host`
can only interleave when the due set is host-diverse, and per-feed cadence makes it
uniform: the 8 Google News proxies are pinned at nominal, so they are due on *every* pass
while natives are due on few.

Best-case consecutive same-host pairs, `max(0, 2·max_host − total − 1)`:

| due set | consecutive same-host pairs |
|---|---:|
| today's full 26-feed pass | **0** |
| 8 pinned proxies + 0 due natives | **7** |
| + 1 / 2 / 3 due natives | **6 / 5 / 4** |

Two things follow, and they differ in severity.

**The spacing itself survives.** `order_by_host` is an optimisation, not the guard —
`capture.run` still sleeps `HOST_GAP_SECONDS = 5` between same-host fetches. Seven
adjacent proxy polls cost 35s against `DEADLINE_SECONDS = 600`, about 6%. The cost is a
slower pass, not an unspaced burst, so this does not reintroduce the 429 condition.

**The regression test does not survive, and that is the real defect.**
`tests/test_capture.py:277` asserts over `capture.order_by_host(list(brief.RSS_FEEDS))` —
the *whole* list, which production would no longer order. It keeps passing while testing
something production stopped doing: a regression test made vacuous by the change it should
be guarding. §9.4 repoints it at the **due set**.

Phase 1 carries this too, since any per-feed cadence shrinks the due set.

### 7.4 What the job reports, and three alerts

`CadenceReport` goes to the log and onto the `/capture` health surface: per feed the
interval, provenance age, probe state and coverage; plus projected requests/day and the
stalest measurement.

Each alert reuses a number the system already has rather than a picked one:

- **Rotation stalled** — the stalest measurement is older than twice the derived rotation
  period of §3.1. Retuning slots or probe days moves the alert automatically; no knob
  tracks another knob.
- **Saving nothing** — projected requests/day equals nominal-for-every-feed, i.e. cadence
  is running and achieving nothing. Replaces `BUDGET_PER_DAY` per §5.4.
- **Probe refused** — coverage below the knob, with the measured coverage in the message.

All three follow the reference alert contract established by `capture.liveness`:
`(episode_key, message) | None`, keyed on the *episode* so the caller dedupes and an
outage produces one message rather than one per run.

### 7.5 New knobs

`CADENCE_ENABLED` (false at first deploy), `CADENCE_PROBE_SLOTS` (3),
`CADENCE_PROBE_DAYS` (4), `CADENCE_MIN_PROBE_COVERAGE` (0.8).

`CADENCE_ENABLED` mirrors `CAPTURE_ENABLED` and is what §10's rollback turns off; an
earlier draft described that rollback without defining the knob it needed.

Each needs a `KNOBS` entry, the constant deleted, and a compose anchor whose name equals
the key — enforced by `tests/test_packaging.py`. Note the trap from
`env-var-needs-compose-passthrough`: on an established host the settings **row** is the
only path, because the anchor merely seeds a row that does not yet exist. These must be
set on the host, not only in the repo's compose file, and the effect verified rather than
the row.

---

## 8. The alerting rework

### 8.1 `failing_feeds` takes a per-feed tolerance

The single scalar `cutoff` becomes per-feed:
`feed_cadence.tolerance_minutes(...) * STALE_AFTER_INTERVALS`, reusing the existing
constant rather than introducing a second. For a 120-minute feed that is 6 hours before
alerting — long in wall-clock terms, correct in the only unit that matters: three missed
polls is three missed polls.

An unknown interval falls back to nominal, i.e. exactly today's 90 minutes.

### 8.2 `liveness` is untouched

The scheduler tick stays at 30 minutes and `capture_runs` still gets a row every fire, so
its tolerance remains correct.

### 8.3 `item_drought` needs no change

Its 7-day learned baseline would step if the whole fleet slowed at once. Under §6.5's cold
start feeds slow one at a time across a rotation period, so the baseline drifts gradually
and re-learns continuously. A bootstrap decision taken for provenance reasons turns out to
protect the alerting baseline as well.

---

## 9. Testing

The dominant defect class in this repository is tests asserting less than their name —
seven in a single run, none caught by anything failing. This section is organised around
what could pass while broken.

### 9.1 Placement

Tests go where their **dependencies** are, not their subject. The comprehend files carry a
module-level DB `pytestmark`, so a non-DB test dropped in one silently never runs in CI.
`tests/test_feed_cadence.py` follows `test_probe_corroboration.py` instead: **no
module-level skipmark**, pure policy tests running in CI unconditionally, DB-backed ones
skipping through a `kb` fixture. The policy functions are pure partly for this reason.

The network guard needs nothing new — `feed_cadence` speaks only to Postgres, and the
`capture.run` tests already stub `brief.fetch_feed_entries`. Knobs are read as
`common.CADENCE_*`, never `from common import`, since a from-copy freezes at import and
defeats both host toggles and `monkeypatch`.

### 9.2 Self-maintaining tests over the real data

`tests/test_scheduler.py:259` is the template — parametrising over `scheduler.SCHEDULES`
so adding a schedule extends coverage without anyone remembering. Two apply:

- **`rank_ordered` over every entry in the real `RSS_FEEDS`**: true iff the host is Google
  News. A ninth proxy tests itself.
- **No Google News feed is ever assigned an interval above nominal**, whatever turnover it
  reports — with the discriminating control in the same test: *a native feed handed the
  identical turnover is slowed*. Without that half the test passes against an
  implementation that never slows anything.

That control pattern is this section's thesis. A test showing only that a guard fires
cannot distinguish a working guard from a dead code path.

### 9.3 The anti-drift test

Monkeypatch `feed_cadence.interval_for` to return a sentinel, run `measure_roll_off`'s
rendering, and assert the sentinel appears in the output.

If the script ever reimplements the computation rather than calling it, the sentinel
vanishes and the test fails. This is the control `reconstruction-drifts-from-production`
identifies as missing — and note its shape: it points at *production's* function, not at a
sibling copy, so the two sides cannot move together.

### 9.4 Behavioural tests, each with a presence sibling

Every absence-assertion is paired, because an inert implementation passes all of them:

| assertion | its sibling |
|---|---|
| a feed inside its interval is **not** due | one past its interval **is** due |
| a not-due feed writes **no** `feed_polls` row | `Tally.feeds_not_due` counts it |
| a 120-min feed silent 3h is **not** alerted | the same feed silent 7h **is** |
| a low-coverage probe writes **no** interval | a good probe writes one, with provenance from its own window bounds |

Plus: a probing feed is always due; every `due_feeds` failure path returns all feeds; a
feed absent from the live list does not consume a probe slot (§6.4); `NULLS FIRST` then
alphabetical actually orders that way; and an unknown interval leaves `failing_feeds` at
exactly today's 90 minutes.

**The interleave test is repointed at the due set (§7.35).** `tests/test_capture.py:277`
currently orders the whole `RSS_FEEDS` list, which production stops doing under either
phase, so it would keep passing while testing nothing production runs. It must order what
`due_feeds` returns.

It must also stop asserting zero same-host adjacency, because that is now arithmetically
unreachable — 8 pinned proxies among 8–11 due feeds cannot be interleaved. The honest
assertion is the one that still discriminates: **every same-host pair in the ordered due
set is separated by a `HostSpacer.wait`**, i.e. the guard that actually protects the host
still fires on every collision. That tests the surviving mechanism rather than restating a
property the design deliberately gave up.

The `failing_feeds` pair is the regression this whole design risks (§1.3) and is the one
place the boundary is tested from both sides rather than trusted.

### 9.5 Pre-registered mutation table

Written before the implementation. Not "does a test fail" but *which* test:

| mutation | must fail |
|---|---|
| delete the `rank_ordered` guard | the pinned-vs-native control |
| `due_feeds` returns `[]` on error | the fail-open test |
| remove capture's not-due skip | the no-row test **and** the tally test |
| remove dead-feed slot exclusion | the slot-accounting test |
| `NULLS FIRST` → `NULLS LAST` | the ordering test |
| `failing_feeds` back to a global tolerance | the 6h/13h pair |
| script reimplements `interval_for` | the sentinel test |
| due comparison `>=` → `>` | *predicted: nothing* |

The last row is the point of the exercise. The boundary mutation is predicted to fail
**zero** tests — a coverage gap rather than a safe operation — so a test sitting exactly on
the boundary goes in before the count is taken.

Per `mutation-diagnostic-demands-a-count`, report *how many* tests failed, not whether one
did; and if a count overshoots, verify the mutation hit only the line named before
concluding anything about the tests. The silence-halves above are excluded from the count,
since an inert implementation passes them.

### 9.6 What is deliberately not tested

No per-entry test for new feeds. Host interleaving, outlet metadata agreement and Google
News freshness windows are already asserted over the real `RSS_FEEDS`; a per-entry test
would restate them for one row and pass because it was written after the code.

---

## 10. Rollout

### 10.1 Phase 1

1. `poll_every_minutes` keys, the due-check (§7) and the `failing_feeds` rework (§8) land
   together. The alerting rework is not separable: §1.3 is a monitoring regression, and
   shipping the due-check first means running production with its own detection disabled.
2. Deploy with no keys set — behaviour is identical to today, and the due-check is
   exercised in its fail-open path across the whole fleet before any feed is slowed.
3. Add keys for a few natives, widest intervals first. Watch `single_sighting_fraction`
   (§3.2) and `failing_feeds` for a week.
4. Fill in the rest if the signal stays flat.

Rollback at any step is deleting keys.

### 10.2 Phase 2

1. Migration and module land with the cadence job **disabled**, so `feed_cadence` is empty
   and every read fails open to nominal. Behaviour is byte-identical to today.
2. Enable the job. The first run promotes `CADENCE_PROBE_SLOTS` feeds to probing; nothing
   slows, because nothing is measured.
3. First intervals appear after `CADENCE_PROBE_DAYS`. Convergence takes one rotation
   period (§3.1).
4. Verification is the `/capture` surface and the cadence report, not the request count:
   the interval, its provenance age and the probe state are the observable state, and
   `env-var-needs-compose-passthrough`'s rule applies — **verify the effect, never the
   row.**

Rollback is setting the job's knob off: reads fail open and the fleet returns to nominal
cadence without touching the table.

---

## 11. Companion change: two Nitter feeds and host-spacing the brief

Independently shippable and specified here only because it touches the same subsystem.
May be carved into its own commit.

**Two feeds added to `RSS_FEEDS`:**

```python
{
    "name": "Chase Taylor (@pineconemacro)",
    "url": f"{NITTER_BASE_URL}/pineconemacro/rss",
    "category": "macro",
    "kind": "analyst",
    "outlet": "Chase Taylor",
},
{
    "name": "@LordPos3idon",
    "url": f"{NITTER_BASE_URL}/LordPos3idon/rss",
    "category": "geo",
    "kind": "analyst",
},
```

Both handles verified by the operator. Nitter feeds cannot cleanly be `/addsource` temp
sources: the wizard stores a literal URL, while `NITTER_BASE_URL` is interpolated at
import from a deploy-scoped env var, so a temp source would freeze the compose service
name into a volume file and break silently if the instance moved.

`@LordPos3idon` carries no explicit `outlet` because `outlet_for` falls back to the feed
name and, for a pseudonymous account, the handle **is** the publisher identity. Chase
Taylor gets one because his name and feed name differ. `outlet` is the KB's corroboration
identity — `corroboration_by_outlet` counts distinct outlets per event — so this also
keeps two surfaces of one person from ever counting as confirmation, the property
`Intersubjectively Transmissible` and `Jacob Shapiro (@jacobshap)` already rely on.

**`outlet_for`'s docstring count is deleted.** It says "Seven are not, and those carry an
explicit `outlet`"; the code already has eight before either addition. Rather than fix it
and let it rot a third time, the number goes: the rule is "name is the publisher unless
`outlet` overrides", and a hand-maintained count in prose is unmaintainable by
construction — nothing breaks when it is wrong.

**Host-spacing moves to `common.py`.** `_host` → `common.feed_host`, `order_by_host` →
`common.order_by_host`, `HOST_GAP_SECONDS` → `common.HOST_GAP_SECONDS`, plus a
`HostSpacer` with a `wait(feed)` method. `capture` imports `brief` at module level, so
`brief` importing `capture` would invert a dependency that has a direction; both already
import `common`.

`HostSpacer` is an object rather than a generator deliberately: `capture.run` checks its
deadline *before* sleeping, and a generator that slept before yielding would burn the gap
on feeds it was about to skip. `gap_seconds` is a keyword defaulting to the incumbent, per
`shared-helper-carries-first-callers-tuning` — 5s was chosen for a 48×/day poller, not for
a once-a-day brief.

**Fetch order changes; emit order must not.** `feed_blocks` becomes `feed_content`, which
feeds the LLM prompt *and* `build_source_index` / `build_source_evidence`. Reordering the
fetch would reorder the model's input for reasons unrelated to the news. The change may
alter only *when* requests leave the box:

```python
sources = RSS_FEEDS + feed_temp
position = {id(f): n for n, f in enumerate(sources)}
spacer = common.HostSpacer()
fetched = []
for f in common.order_by_host(sources):
    spacer.wait(f)
    if c := fetch_rss(f):
        fetched.append((position[id(f)], c))
feed_blocks = [c for _, c in sorted(fetched)]
```

Keyed on `id(f)` rather than `f["name"]`: line 3207 uses `RSS_FEEDS + feed_temp` directly
rather than `all_sources()`, so it does not get the name-collision overwrite, and a
name-keyed dict could silently drop a source. `order_by_host` returns the same objects, so
identity is exact.

This retires the placement workaround the feed additions would otherwise have needed:
with ordering host-interleaved, declaration order no longer determines request order.

**Tests:** the existing real-list interleave test repointed from `capture.` to `common.`;
and one new test asserting emitted block order equals declaration order **while** the
host-spaced fetch order differs — both halves in one test, since without the second
assertion it passes trivially if ordering did nothing. Plus one assertion that
`spacer.wait` is not called for a feed capture skipped on deadline, which is the reason
the generator was rejected and which nothing else would catch.

---

## 12. What this deliberately does not do

- **No production `fit_budget`** (§5.4). Intervals are always the feed's own measurement.
- **No seeding from prior measurements** (§6.5).
- **No change to the 30-minute scheduler tick.** Per-feed intervals select *which* feeds a
  pass polls, never whether a pass happens — which is why `liveness` survives untouched.
- **No cap on growth.** §1.1 established that a new feed takes the default either way;
  budgeting the `/addsource` path is a separate concern and is not smuggled in here.

---

## 13. What the red-team review changed

Full review: `docs/superpowers/reviews/2026-09-09-per-feed-cadence-redteam.md`. All three
objections were verified against the code before being acted on; one had its severity
corrected in the other direction.

| objection | verdict | change |
|---|---|---|
| A simpler `poll_every_minutes` dict key gets most of the value | **Upheld**, and it is a subset rather than an alternative | §2.1 phasing, §3A |
| `INTERVAL_HI = 240` is assumed, not established — all evidence spans ≤45 min | **Upheld**, verified at `measure_roll_off.py:105,151` | §3.2, ceiling → 120; §6.3 guard deleted |
| Per-feed cadence collapses host diversity in the due set | **Upheld with severity corrected** | §7.35, §9.4 |

**One claim I checked and would not repeat as stated.** The review presents the due-set
collapse as reintroducing the 429 condition. The adjacency arithmetic is exactly right —
0 same-host pairs today against 7/6/5/4 projected — but `HOST_GAP_SECONDS = 5` still
sleeps between same-host fetches, so the consequence is ~35s of extra sleeping against a
600s deadline, not an unspaced burst. The genuinely serious half is the regression test
going vacuous, which the review also found and which nothing else would have caught.

**One correction the review did not make, found in self-review**: §10's rollback referenced
a `CADENCE_ENABLED` knob that §7.5 never defined. Now defined.

**The pattern worth carrying forward.** Two guards and a ceiling were built on
`INTERVAL_HI = 240`, a number never traced to its evidence — treated as measured because
it appeared in a measurement script. Increasing care was applied on top of an unexamined
input, and the care made the result *look* more rigorous rather than less. The largest-gap
guard is the sharpest instance: deriving a bound from the observation reads as more
principled than picking a constant, which is exactly why nobody checked whether it could
fire.

---

## 14. Follow-ups

- **`b42.5` closes on Phase 1**: per-feed intervals in effect, `failing_feeds` reworked,
  and `single_sighting_fraction` flat for a week after the last interval was set. Phase 2
  gets its own bead rather than holding this one open.
- **Phase 2's bead should carry §3.2's open question**, because it is the thing Phase 1
  will not answer: whether 120 is safe, and by how much it could rise. The settling
  experiment — a shadow reader polling one feed fast and slow concurrently — is cheap and
  is also the primitive the probe rotation needs, so it is not throwaway work if wanted.
- The `/addsource` growth path remains unbudgeted (§1.1). Worth its own bead if the feed
  count grows.
- `uk5` (sizing `JOB_MAX_RUNTIME_MINUTES` from observed durations) gains a new job in the
  `job_runs` ledger and should be measured after `cadence` has run for a while.

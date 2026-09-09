# Red-team review — per-feed capture cadence design

**Reviewing:** `docs/superpowers/specs/2026-09-09-per-feed-cadence-design.md`
**Date:** 2026-09-09
**Stance:** hostile. Brief was to find why this is the wrong thing to build.
**Base:** `fd9d574` (clean tree)

Every code claim below was executed or read, not recalled. Line numbers were
re-derived from the working tree; where the spec's differ, that is noted in §4.

---

## Objection (a) — a simpler design that gets ~80% of the value

### Build instead: `poll_every_minutes` as a key in the `RSS_FEEDS` dict

**The repository already has a per-feed policy table, and it is not a database
table.** It is the feed dict. Verified by enumeration over the real list:

| per-feed policy | where it lives | shipped |
|---|---|---|
| which URL capture polls (narrower window) | `capture_url` key, `capture.py:285` | yes, `b42.4` |
| corroboration identity | `outlet` key, `brief.outlet_for` (`brief.py:532`) | yes |
| source classification | `kind`, `category`, `perspective` keys | yes |

`capture_sources()` (`capture.py:261-286`) is already a per-feed policy
application layer: it reads a key off the feed dict and substitutes it into a
copy. Adding `poll_every_minutes` is the same move, one key over.

**What I would build:**

1. `poll_every_minutes` as an optional key on `RSS_FEEDS` entries, seeded from
   the 2026-09-08 `b42.2` run. Absent ⇒ nominal.
2. The due-check of §7.1–7.3, unchanged: one query against
   `feed_polls_source_time`, fail open, `Tally.feeds_not_due`.
3. The `failing_feeds` rework of §8.1, unchanged. This is a **precondition of
   any per-feed cadence at all**, not of this particular design, and §1.3 is
   right that it cannot be deferred.
4. Nothing else.

**What is deleted relative to the spec:**

- migration `0013` and the `feed_cadence` table (§4)
- `feed_cadence.py`, a new top-level module and its three-part packaging
  checklist (§5.1) — `Dockerfile:39` COPY line, workflow paths filter, workflow
  ruff file list
- the `cadence` scheduler job (§6.1), which also needs a `brief.py` `MODES`
  entry (`brief.py:4247`), a `JOB_MODES` entry (`brief.py:4268`), a compose
  service, and a `supervisor` fire path (`supervisor.py:586`)
- three knobs (§7.5), each needing a `KNOBS` entry, a deleted constant, a
  compose anchor whose name equals the key, **and a settings row created by
  hand on the established host** — the spec correctly cites
  `env-var-needs-compose-passthrough` and then signs up for three instances of it
- the probe state machine, the coverage guard, the gap cap, the rotation-stall
  alert, the "saving nothing" alert (§6.2, §6.3, §7.4)
- roughly half of §9's test surface, including §9.3's anti-drift sentinel test,
  which only exists because §5.2 moves code out of the script in the first place

**What is genuinely lost:** automatic re-measurement. Stored intervals go stale
if a publisher changes cadence.

**Why that loss is smaller than §3 claims.** `scripts/measure_roll_off.py`
already computes the entire proposed table, is already tested
(`tests/test_measure_roll_off.py`), and already prints its own liveness caveat
before the table. Re-running it is one command. The spec's §3 table dismisses
frozen intervals as "Right answer for a one-off saving; wrong for groundwork" —
so the whole delta between (a) and the spec rests on the groundwork claim, which
is the next thing to attack.

### The groundwork claim does not survive contact with the three consumers it names

§1.2 justifies the entire build with: "per-feed retry budgets, per-feed capture
windows, per-feed staleness tolerances". Taken one at a time:

- **Per-feed capture windows — already shipped, by the simpler mechanism.**
  `capture_url` is a per-feed capture window. It landed in `b42.4`, it works, and
  it is a dict key. This is direct evidence *against* the seam the spec proposes:
  when this project actually needed per-feed policy, it put it in the feed dict
  and shipped.
- **Per-feed staleness tolerances — created by this design, not enabled by it.**
  §8.1 exists only because §7 slows feeds. Today's single tolerance
  (`_interval_minutes() * STALE_AFTER_INTERVALS`) is correct, derived, and needs
  no per-feed anything. Counting the work you generate as the benefit you deliver
  is circular.
- **Per-feed retry budgets — shares no column.** Retry policy lives in
  `brief.fetch_feed_entries`, keyed on *failure kind*:
  `RSS_RETRY_STATUSES = {429, 500, 502, 503, 504}` and `RSS_MAX_ATTEMPTS = 3`
  (`brief.py:1880-1881`). A per-feed retry budget would key on failure history —
  `feed_polls.failure`, which already exists — not on `turnover_minutes`,
  `probe_started_at`, `window_from` or `window_to`. Nothing in `feed_cadence`'s
  schema serves it.

So: one is already built by the rejected mechanism, one is self-created, one
shares no column. **"Groundwork" is the only stated justification and none of its
three instances holds.** If the seam is the point, the seam that this repo's own
history validates is the feed dict.

---

## Objection (b) — a requirement assumed rather than established

### The claim: "a 4-day probe at nominal cadence can establish that 240 minutes is safe"

This is the load-bearing assumption of the whole design. §3 asserts the
fast-probe control "breaks the loop by construction; every interval is derived
from data gathered at nominal cadence." Breaking the *recursion* is not the same
as making the *number valid*, and the spec never separates the two.

**Four pieces of verified evidence that the instrument cannot support the claim:**

**1. The measurement is floor-bounded by its own poll rate — the spec quotes this
and then reasons past it.** §3 reproduces the script's own warning that "every
loss figure here is a FLOOR, never a ceiling." An item published and gone inside
a 30-minute gap leaves no row. That bias is *directional*: unobserved churn makes
measured turnover look **longer** than true turnover, which pushes intervals
**up**. Probing at nominal does not remove that bias — nominal is where the bias
lives.

**2. Turnover is only ever measured over spans of ≤45 minutes, then extrapolated
5.3×.** `poll_pairs` (`scripts/measure_roll_off.py:151-155`):

```python
limit = timedelta(minutes=nominal_minutes * MAX_GAP_FACTOR)   # 30 * 1.5 = 45
return [(a, b) for a, b in zip(ok, ok[1:]) if b.polled_at - a.polled_at <= limit]
```

Every overlap that feeds `full_turnover_minutes` comes from a pair spanning at
most 45 minutes. `INTERVAL_HI = 240` is a claim about a span **5.3× longer than
any span the instrument ever observed**, and `INTERVAL_HI` itself is an
undefended constant in the script (`:114`, no comment, no derivation).

**3. §6.3's gap cap is inverted on both available readings, and the spec does not
say which it means.**

```
interval = min(turnover / SAFETY, INTERVAL_HI, largest_gap_minutes)
```

- *Reading 1 — `largest_gap` over the pairs the measurement actually uses.* Those
  pairs are ≤45 minutes by construction (point 2). So `min(...)` can never exceed
  45, `INTERVAL_HI = 240` is unreachable, and **the feature is inert in
  production**. Worse, it is inert *silently*: §5.1 makes `interval_for` a pure
  function taking `largest_gap_minutes` as a keyword, so every §9.2 and §9.4 test
  passes a synthetic large value and passes. The §9.5 mutation "drop
  `largest_gap` from the `min()`" also fails the right test. Nothing in the
  pre-registered table runs the real probe-window computation.
- *Reading 2 — `largest_gap` over all successful polls in the window.* Then the
  guard **rewards holes**. A spotless 4-day probe has a largest gap of ~30
  minutes and caps at 30. A probe containing one 4-hour capture outage caps at
  240. And §6.3's *other* guard admits exactly that: `CADENCE_MIN_PROBE_COVERAGE
  = 0.8` permits ~19 hours of missing polls inside a 4-day window. **The two
  guards that are described as reinforcing each other anti-correlate: the
  cleaner the probe, the tighter the cap.**

**4. "This one needs no constant" is contradicted by the file being promoted.**
§6.3 presents the gap cap as constant-free and self-limiting. `MAX_GAP_FACTOR =
1.5` exists in `measure_roll_off.py:105` with the comment *"1.5 admits ordinary
jitter and the occasional late pass while excluding a skipped one"* — i.e. the
codebase already established that distinguishing a normal gap from a hole
**requires a constant**. The spec claims a property the code it is promoting
disproves.

### Supporting: the mechanism is scoped away from the only feeds with measured loss

`is_proxy`'s docstring (`scripts/measure_roll_off.py`) states that for the 8
Google News proxies "the loss is truncation INSIDE a single poll at the
100-entry cap, not roll-off between polls." §4.3 pins them at nominal for exactly
that reason. Verified by enumeration: **8 of 26 feeds are `news.google.com` by
host** — 384 of 1248 daily requests, 31% of the fleet — permanently outside the
mechanism. The feeds with demonstrated item loss are the ones cadence can never
touch or measure.

### What evidence would settle it

**A shadow reader, and nothing less.** Poll one candidate feed at 240 minutes and
the *same* feed at 5 minutes for one probe window, then count items the 240-minute
poller never saw. That is the only probe where a correct and an incorrect answer
disagree — `the-rule-exempts-its-own-origin`'s control requirement. Every probe in
this spec is run *at the cadence whose adequacy is the question*, which is
`the-probe-measured-the-wrong-layer`: a well-formed number answering an adjacent
question. Cost is one feed's requests for four days, against a fleet that is
3.8% under a self-set budget (§1.1).

Absent that, the honest reading is: nothing here establishes 240 is safe, and
§4.4's "the worst case spends requests" is false — the worst case silently loses
items, which is the failure `feed_polls` was created to make impossible.

---

## Objection (c) — the constraint this collides with

### Host concentration. `order_by_host`'s protection is a function of the DUE set, and this design shrinks the due set to one host.

This is not "complexity". It is a measured, mechanical regression, and it
re-creates the exact failure §1.1 uses to dismiss the volume argument.

**Measured, today, against the real `RSS_FEEDS`:**

```
capture.order_by_host(RSS_FEEDS)  ->  26 feeds, 0 consecutive same-host pairs
```

**Measured, against the spec's own projected steady state** — 8 Google News
proxies pinned at nominal and therefore due on *every* pass (§4.3), plus the
handful of native feeds due on a given pass:

| due set | feeds | consecutive same-host pairs |
|---|---|---|
| today (all 26) | 26 | **0** |
| 8 pinned + 0 natives | 8 | **7** |
| 8 pinned + 1 native | 9 | **6** |
| 8 pinned + 2 natives | 10 | **5** |
| 8 pinned + 3 natives | 11 | **4** |

`order_by_host` (`capture.py:294-313`) dilutes a heavy host **with the other
hosts in the same pass**. Its fallback is explicit:

```python
placed = next(
    (h for h in candidates if not ordered or _host(ordered[-1]) != h),
    candidates[0],          # <- no other host available: place same-host
)
```

Per-feed intervals remove the other hosts from most passes. The design converts a
26-feed host-interleaved pass into a ~9-feed pass that is ~89% one host,
separated only by `HOST_GAP_SECONDS = 5` (`capture.py:258`). Google News receives
**8 requests in ~40 seconds, every 30 minutes, from one user agent** — on the
host whose 4 narrowed feeds already sit at the 100-entry cap.

### Why it is stable rather than transient: phase-locking

Due-ness is `now - last_poll >= interval`, evaluated on a **fixed 30-minute
tick** that §12 explicitly refuses to change. So a 240-minute feed occupies one
of 8 phase slots, and **two feeds polled in the same pass with the same interval
are due in the same pass forever** — co-scheduling is an attractor, not a
coincidence.

The bootstrap maximises the alignment:

- §6.5 starts every feed unmeasured ⇒ every feed at nominal ⇒ every feed in
  phase.
- §6.2 writes intervals in batches of `CADENCE_PROBE_SLOTS = 3` from a **daily**
  job. A cohort's first post-slowdown poll therefore lands on the same capture
  pass, and locks there.
- §3.1's own projection concentrates 16 of 18 native feeds on the single value
  `INTERVAL_HI = 240`, collapsing the phase space further.

A decision taken for provenance reasons (§6.5) turns out to hurt here, which is
the mirror of the happy accident §8.3 claims for it.

### §11 makes it worse on the specific host with the documented failure

§11 raises the Nitter host from **2 feeds to 4** — verified: `nitter:8080` × 2
today. That is the host whose 429 is documented at `brief.py:1920-1927` as caused
by "requests that arrive close together". Meanwhile §8.1 raises that host's
`failing_feeds` alert threshold from 90 minutes to 12 hours. The design increases
the collision probability and lengthens the time to notice it, in the same commit.

### The control cannot fail — and the spec leans on it

`tests/test_capture.py:277`:

```python
def test_the_real_feed_list_never_polls_one_host_back_to_back():
    """Written against the real RSS_FEEDS as well as a synthetic list, because
    this is the regression test for a production failure."""
    ordered = capture.order_by_host(list(brief.RSS_FEEDS))
```

It asserts the invariant over **the whole feed list**. After this change,
production never orders the whole list — it orders the due subset. §11 proposes
only *repointing* this test from `capture.` to `common.`, preserving the vacuity,
and §9.6 explicitly declines further coverage because "Host interleaving ... [is]
already asserted over the real `RSS_FEEDS`."

So the regression test for a real production failure keeps passing against a
list production no longer uses. That is precisely the defect class §9 opens by
naming — *tests asserting less than their name* — sitting inside the section
written to prevent it. And none of §7.4's three alerts watches per-pass host
concentration, so the first signal would be a 429 arriving as an ordinary
`failing_feeds` line 12 hours later.

**If this is built anyway, the minimum fix:** assert the interleave invariant over
the **due set** under the spec's own projected interval table, not over
`RSS_FEEDS`; and make `order_by_host`'s same-host fallback branch observable
(count it into `Tally`) so the degradation is a number rather than an inference.

---

## §4. Factual errors and internal contradictions in the spec

Verified against the working tree at `fd9d574`.

1. **§1.1 cites `scripts/measure_roll_off.py:116`** for `BUDGET_PER_DAY`. The
   assignment is at **line 119**; 116 is the first line of its comment. Cosmetic,
   listed for completeness.

2. **§3.1 calls the Google News proxies "self-measuring". §4.3 says their
   turnover is "unmeasurable in principle".** Both cannot be true. `is_proxy`'s
   docstring agrees with §4.3. They are not self-measuring; they are permanently
   exempt. This matters because §13's close condition ("every rotating feed
   carries an interval with provenance") and §7.4's saving alert both depend on
   which feeds the rotation covers.

3. **§7.4's "Saving nothing" alert fires from day one, for the whole cold
   start.** §6.5 starts every feed unmeasured, so projected requests/day *equals*
   nominal-for-every-feed **exactly** until the first interval is written — at
   minimum `CADENCE_PROBE_DAYS` = 4 days after enablement, and near-equal for the
   24–27-day convergence §10 step 3 describes. §10's rollout and §7.4's alert
   contradict each other; the alert's inaugural state is a false positive lasting
   most of a month.

4. **§7.1's "last attempt" is not the same as "an attempt was made".** The
   deadline path writes a `feed_polls` row for a feed that was never fetched —
   `record_poll(conn, run_id, feed["name"], "deadline", 0)` (`capture.py:344`),
   documented in `migrations/0008_capture_telemetry_up.sql` as *"'deadline' for a
   feed the pass ran out of time to reach"*. Under per-feed cadence that row
   **resets the feed's due clock for a full interval**: a feed skipped for time is
   then skipped by policy for up to 240 minutes. It also depresses §6.3's coverage
   ratio (which counts *successful* polls) toward the 0.8 refusal threshold, so a
   deadline-prone feed becomes a probe-refusing feed. §7.2 reasons carefully about
   not writing a row for a not-due feed and does not revisit the row that is
   written for a not-fetched one.

5. **§5.3 understates its own finding.** `NOMINAL_MINUTES` is not only used for a
   printed prediction at `:443`. It is passed into `feed_stats` at `:454`, which
   passes it to `poll_pairs` at `:154`, where it sets the `MAX_GAP_FACTOR` window
   that **selects which poll pairs enter the turnover estimate at all**. Retiming
   capture changes the measurement, not just the caption — a stronger version of
   the spec's own argument, and one that raises the stakes on objection (b)
   point 3.

6. **§11's `outlet_for` claim is correct.** The docstring says "Seven are not";
   enumeration gives **8** feeds carrying an explicit `outlet`. Deleting the count
   rather than fixing it is the right call.

7. **§4.3's proxy arithmetic is correct and is the spec's best verified
   observation.** 8 feeds on `news.google.com` by host; exactly 4 carry a
   `capture_url` (`Reuters Markets`, `Reuters World`, `Kyiv Independent`,
   `Yonhap (English)`), all overriding to `news.google.com`. A `capture_url`-based
   predicate would indeed have pinned half the set.

8. **§1.3's `failing_feeds` analysis is correct**, including the 37% figure:
   `_interval_minutes() * STALE_AFTER_INTERVALS` = 30 × 3 = 90 minutes
   (`capture.py:416, 413, 499`), the `last_try > cutoff` clause and its docstring
   rationale are as quoted, and 90/240 = 37.5% of a slowed feed's cycle. This is
   the spec's strongest section and its "precondition, not follow-up" conclusion
   is right.

9. **`scheduler.py` imports nothing from the project — verified.** §5.3's cycle
   claim holds. `test_scheduler.py:259` is indeed the `SCHEDULES`-parametrised
   template §9.2 describes.

---

## Summary

The spec is unusually careful and most of its code citations check out. Its
weakness is not carelessness — it is that §1.2 openly concedes there is no
beneficiary and substitutes "groundwork", and then the groundwork claim is never
tested against the three consumers it names. All three fail: one is already
shipped by the mechanism the spec rejects, one is work this design creates, one
shares no column with the schema proposed.

Underneath that, the central technical claim — that a probe at the current
cadence can license a 4× slowdown — rests on an instrument the spec itself quotes
as floor-bounded, extrapolates 5.3× beyond any span it observes, and guards with a
cap that is inverted under both readings of its own text.

And the concrete operational cost is measurable today: **0 → 4-7 consecutive
same-host fetches per pass**, with the regression test for that exact production
failure structurally unable to see it.

If the decision to build stands, the two changes that would most improve it are
(i) run the shadow-reader validation on one feed before trusting any interval
above nominal, and (ii) move the interleave invariant onto the due set. Both are
cheap; neither is in the spec.

# Red team: watch-file event triggers (news-brief-8lp)

Date: 2026-11-18. Spec under review:
`docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md`.
Settled and not re-argued: (1) the judge reads the day's captured items; (2) one
alert per row, re-alerting only on edited trigger text or a stronger verdict.

Code read for this review: `brief.py` (`run_job`, `_alert_once`, `MODES`,
`JOB_MODES`, `build_source_index`), `common.py` (`telegram_send`,
`telegram_send_long`, `telegram_alert`, `KNOBS`), `db.py` (`start_run`,
`finish_run`), `supervisor.py` (`_closing_status`), `scheduler.py`, `capture.py`,
`migrations/0006_knowledge_base_up.sql`. Host data was not queried. Every claim
about the live corpus below is marked UNKNOWN where it rests on that.

## The three objections

### (a) Simpler design: a fixed trailing window, with the alert state doing the dedup

The window section builds catch-up machinery: `since` comes from the last
finished `watch` run's `scheduled_for`, capped at 72 h, with a 24 h default.
The design already needs per-row alert state for decision (2), and that state
makes re-judging an item harmless: a repeated verdict at the same strength sends
nothing. So a **fixed trailing window** (say 48 h every night, no `job_runs`
lookup) gives the same alerts. It also deletes the three defects the derived
window brings with it:

1. **"Finished" includes crashed runs.** `db.finish_run` defaults
   `status='finished'`, and `supervisor._closing_status` returns `finished` for
   every exit code except `EX_ALREADY_RUNNING`, including 1 and -1 (OOM/SIGKILL).
   `run_job._finish_quietly` does the same for manual runs. A night that crashes
   on a model 5xx, a timeout or a `WatchFileError` therefore moves `since`
   forward, and the next good run never sees that night's items. This
   contradicts the spec's own claim that "a missed night is then covered by the
   next one". The pre-registered test ("since is derived from the last finished
   run") would pass with a fixture where exit_code is 0 and never catch it.
2. **A failed send loses the alert for good.** `telegram_send_long` returns
   `False` and never raises (common.py:536-553). Send-before-store means the
   state is not written. But the run still finishes and the window moves past
   the evidence item, so nothing re-judges it the next night. The alert comes
   back only if a new headline happens to repeat the story. With a fixed
   overlapping window, the next night retries for free. That is what
   send-before-store assumes happens.
3. **The 72 h cap leaves a silent gap.** After an outage longer than the cap,
   items older than 72 h are never judged and nothing reports it. That breaks
   the spec's own rule: "never a silent skip". (Manual `/run` rows also carry
   `scheduled_for = NULL`, which the "last finished run" query has to handle.)

The fixed window costs only coverage of outages longer than 48 h. The 72 h cap
already gives up most of that, and capture's own liveness and drought alerts
report the outage anyway. It removes a DB query, a cap, a fallback and a test
group, and it makes "send before store" do what the spec says it does.

### (b) Assumed, not established: that the judge finds the trigger

Every pre-registered test checks plumbing: loader, window, validator, state.
Nothing measures the success criterion in the spec's first paragraph, "a real
trigger reaches Telegram within a day". The spec makes a quiet night
"provably a judged night". But judged is not the same as judged correctly, and a
missed trigger looks exactly like a clean `not`. The input is one forced call
over 392-1,030 headlines a day, and up to about 3,000 on a 72 h catch-up. The
whole value of the feature rests on that call's recall, and the spec takes it on
faith. "A titles-only judge's doubt resolves to `unclear`" is a hope about how
the model behaves. A headline the model passes over produces `not`, not doubt.

The KB has held captured items since 2026-09-04, so a measurement is close to
free and should be a gate before shipping, not something left to the builder.
Pick 3-5 past events visible in `items` (any signed deal, ceasefire or rate
decision from Sept-Nov). Write made-up rows for them and replay the judge on the
historical windows that contain them, plus the same number of windows that do
not. Pre-register the expected hits. Recall below the expected count means the
design (one call, titles only, a list this long) is wrong before any plumbing
exists. Feed coverage is a related unestablished requirement: the rows live in a
private repo, so nobody here has checked that the captured feed set covers the
topics they name. A row about a topic no feed carries gets a `not` built from
news the judge never saw, which is the spec's first anti-goal arriving by
another route.

### (c) Six-month collision: whole-file rejection across two independently deployed repos

The loader rejects the **whole file** on an unknown `kind`, on `version != 1`,
or on any invalid price row. It is shared with news-brief-vap. The file is
produced by a private repo that ships on its own schedule. The theses precedent
the spec cites does not fit: there "one bad row means a generator bug". Here an
unknown kind or a new price `condition` (`below_ma50`, a percent drop) means a
**newer** generator, which is the expected state of affairs once vap is live and
the operator starts extending the watchlist. Each such extension then turns both
jobs red every night until this public repo deploys a loader change. That
change has to name the new kind or condition in public code, which leaks the
watchlist's structure into public code by the back door. It also links the
failure domains: a typo in a price row (vap's concern) blinds event judging.
This job doesn't use those rows, and the spec says so. Cheaper and just as loud:
reject a malformed row of a known kind, and report (once) and skip a row of an
unknown kind. Gate the whole file only on top-level structure and `version`.
Each job then fails only on rows it owns.

A second collision is close behind: input growth has no tripwire. "If that grows
~10x, revisit" has no detector wired to this job. The system has already shown
it can surge: Reuters went from ~300 to ~2,600 items a day on 2026-09-16
(comment above `capture.SURGE_FACTOR`). Recall will drop before the context
limit is reached, and the drop will be silent (see b). Log the per-run line
count and input tokens to the run record, and fail loud above a fixed line
budget.

## Fixed check: sentences that pair two facts

| # | Sentence (spec section) | Pair | Implied third claim | Supported? |
|---|---|---|---|---|
| F1 | "392–1,030 items/day, so all titles fit one call (~20k input tokens). No cut is needed." (Approaches) | measured count/day + token figure | Every run's input is about 20k tokens or less, and that size is judged well | **Unsupported.** See the decomposition below. |
| F2 | Approach 1 "only sees what the brief chose to cover, and the brief and its stored source index carry no URLs" | brief is a subset + no URLs | Captured items are a superset of what the brief saw | **Unsupported.** `capture_sources()` takes feeds only (`source_type == "feed"`). Page sources and the brief's `web_content` never reach `items`, some feeds use a narrower `capture_url`, and the brief model web-searches. The two sets overlap; neither contains the other. The decision stands, but the comparison overstates it. |
| F3 | "Titles only, no bodies: bodies multiply the input many times, and a titles-only judge's doubt resolves to `unclear`, which alerts" (Judge input) | bodies are costly + doubt → alert | Dropping bodies costs no recall, because failure becomes an alert | **Unsupported.** A missed headline produces `not`, not doubt. `unclear` alerts only with evidence. "Doubt resolves to unclear" is unmeasured model behaviour. |
| F4 | "A clean night sends nothing. `job_runs` records it, and that is the proof it ran" + Success: "a quiet night is provably a judged night (`/jobs`)" | silence + a green ledger row | A green `watch` row means every event row was judged against the night's news | **Unsupported.** The zero-event-rows path is green with no model call. A window from 1 of N live feeds is green. The `/jobs` verdict summary that would tell these apart is "nice to have, not required". |
| F5 | "`since` is the `scheduled_for` of the last *finished* `watch` run, capped at 72 h … A missed night is then covered by the next one, not lost" (Window) | catch-up rule + cap | No captured item ever goes unjudged | **Unsupported.** `finished` includes exit 1 and -1 (`supervisor._closing_status`, `db.finish_run`), and the cap drops anything older than 72 h without a report. See (a). |
| F6 | "Zero items in the window raises … It is never judged as `not`" (Window) | zero → raise + non-zero → judged | A non-empty window is a complete one | **Unsupported.** A partial capture outage gives a small non-empty window and confident `not`s. That is the first anti-goal. `capture.failing_feeds` alerts separately, but nothing links that alert to the verdicts. |
| F7 | "Send **before** storing, the `_alert_once` order" + test "a failing send stores nothing" (Alerting, Tests) | precedent's order + test | A failed send is retried | **Unsupported.** `_alert_once` sends through `telegram_alert`, which ignores the HTTP status and never raises, so in the precedent the order protects nothing. Here the window has moved on, so nothing retries even when the state is not stored. See (a). |
| F8 | "06:30 puts it just after the 06:00 collect, so an alert lands with the morning brief" (Where it runs) | schedule order | The operator gets notified when it lands | **Partly unsupported.** `telegram_send` (the route `telegram_send_long` takes) sets `disable_notification: True` (common.py:523), so a TRIGGERED alert arrives **silently** in the brief chat. Collect's 120 min grace also means the alert can arrive before the brief. |

### F1 decomposed

- **The 20k figure is an estimate, not a measurement.** "Left to the builder"
  asks for the budget to be "sized from one measured call", so no call has been
  measured. 20,000 / 1,030 = 19.4 tokens per line. That has to cover the label
  (about 2-3 tokens), the outlet (about 2-5) and the title (about 12-20). It is
  plausible **only if no URL goes to the model**. "Judge input" lists
  `title, url and outlet name`. `brief.py` has 11 `news.google.com` references,
  and Google News redirect URLs run to hundreds of characters, so with URLs the
  figure would be several times larger. Labels already map to URLs in code. The
  spec should say outright that URLs are **not** sent.
- **Window ≠ day.** The window runs up to 72 h, so the worst case is about
  3 × 1,030 = 3,090 lines before syndication collapse, not 1,030.
- **What the measured range covers: UNKNOWN.** 2026-11-11..18 is 8 calendar
  days. The 18th is today and only partial, and the 14th and 15th are a
  weekend. Whether the 392 low is a partial day was not checked; the host was
  not queried. The range also sits below a single-outlet day of about 2,600
  already recorded in this system (2026-09-16).

### Verdict residual that doesn't sum

"Every event row must get exactly one verdict. A missing row, an unknown
`row_id`, or an unknown evidence label makes **that row's** verdict `invalid`."
Split the returned verdicts: verdicts for known rows (valid, or invalid because
of an unknown label or a duplicate), plus verdicts with an **unknown** `row_id`.
The last group has no "that row" to invalidate, so the partition leaves a part
with nowhere to go. The duplicate-row_id case is only implied by "exactly one".
Whether an `invalid` report is itself once-per-row or nightly is also not
specified.

## Smaller findings (not in the three)

- `items.created_at` is the capture-insert time, not when the story ran.
  Adding an outlet or changing a `capture_url` inserts that feed's backlog with
  `created_at = now()`, so months-old news (a ceasefire signed and since broken)
  enters tonight's window as new. `items.published_at` exists, and the window
  could require it (when present) to fall inside some recent bound.
- "Each verdict links at most 3": syndication collapse puts several URLs behind
  one label, and the spec doesn't say whether the 3 counts labels or URLs.

## Are more red-team rounds worth it?

**One narrow round, then stop.** The objections above are concrete defects
against code in this repo: the `finished` status semantics, the
non-raising send, the silent notification flag. One more is an unmeasured
property that decides whether the feature works at all. None of them concerns a
hypothetical downstream consumer or re-argues the two settled decisions. After
the spec is revised, run one round scoped **only** to the Window and Alerting
sections and the recall-replay gate, since that is where the fixes land. If that
round produces only wording, labelling or vap-side objections, stop and build.

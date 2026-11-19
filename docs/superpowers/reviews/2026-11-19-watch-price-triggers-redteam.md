# Red team: watch-file price triggers (news-brief-vap)

Date: 2026-11-19. Reviewed: `docs/superpowers/specs/2026-11-19-watch-price-triggers-design.md`
(untracked draft). Context read: the step-1 spec (8lp), `watch.py`, `brief.py`
`mode_watch` / `_watch_post` / `mode_monitor` / `MODES` / `JOB_MODES`, `trading.py`
`_yahoo_fetch` / `_yahoo_closes` / `_YAHOO_HEADERS`, `scheduler.py` `SCHEDULES` and
`weekdays`, `supervisor.py` shutdown-budget comment, `bd show news-brief-vap`, and the
locally installed yfinance 1.4.1 source (`yfinance.utils.parse_quotes`,
`fix_Yahoo_returning_live_separate`, `yfinance.scrapers.history`). No network calls were
made.

Out of bounds, per the brief: alert-on-crossing with re-arm, FIRED rings, code (not the
model) judges price rows. Nothing below argues those.

Caveat on the yfinance evidence: I read yfinance **1.4.1** as installed on the dev machine.
The version `watch_check.py` runs in the private repo is UNKNOWN. Each claim below about
yfinance behaviour holds for 1.4.1 and should be checked against the private repo's pin.

---

## Objection (a): fold price rows into the existing 06:30 `watch` job instead of a new 22:15 job

**The simpler design.** Add a price stage to `mode_watch`, run first and guarded on its own,
so one daily job loads the file once and judges both kinds. No new schedule.

**Why it gets most of the value.** The spec's only stated reason for 22:15 is "a level
crossing reaches Telegram on the day it happens." It never says why that has to happen at
22:15 rather than 06:30. The 8lp Intent says the operator "act[s] on it in the private
repo": a human step comes between the alert and any trade, and the account is an ISA. Every
listing class in the file opens after 06:30 UTC: LSE, Xetra, Euronext Paris and Helsinki
open at 08:00 UTC, the US cash market at 14:30 UTC. So a 06:30 alert reaches the operator
before he could act on any of them. The thing 22:15 actually changes is when it rings: late
evening UK time, instead of alongside the morning brief, which is the reason 8lp gave for
picking 06:30.

**What the fold-in removes, each of which is a live defect in the current draft:**

1. **The futures-final argument is only true in winter** (see Fixed check F2). From
   mid-March to early November the CME daily halt is 21:00 to 22:00 UTC, so at 22:15 the
   next session is already trading. So 22:15 does not give final futures bars for about
   eight months of the year. At 06:30 futures and FX are mid-session too. The difference is
   that a 06:30 design does not claim otherwise. The honest framing either way: like
   `watch_check.py` run by hand during the day, both times judge 4 symbols (3 futures, 1 FX)
   on whatever bar is live.
2. **The skipped-row report is borrowed from a job that can fail before sending it** (see
   F3). The Non-goals rely on the 06:30 job to report skipped price rows "every run". In
   `brief.py` `mode_watch`, `format_problems` runs only after `watch.judge_watch` returns.
   An empty capture window (`WatchFileError`) or a `_post_messages` failure raises first, and
   that day's skip report is never sent. If both kinds live in one job, one problems message
   at the end can carry skipped rows, price UNKNOWNs and event `invalid`s, with each stage
   caught and the raise deferred to the end, as `failed` already is.
3. **The supervisor shutdown budget.** `supervisor.py` lines 70 to 91 carry a hand-written
   worst case: "8 schedules x 2 statements x 0.5s". It says this is the one row that scales
   with `len(scheduler.SCHEDULES)` and must be kept in step. `SCHEDULES` has 8 entries today;
   `watch_prices` makes 9. The spec does not mention the table. No test enforces it (a grep
   for `8 schedules` finds only the comment).
4. **Two actors clearing state from one file.** Each job clears stale keys under its own
   prefix (`watch.alerted.` and `watch.price.`), so the prefixes do not collide. But two
   jobs each derive "the live rows" from separate loads of a file that can be replaced
   between 06:30 and 22:15. One job is one snapshot.
5. **The 22:15 + 120 min grace window crosses midnight.** A catch-up can run at 00:10 on the
   next UTC day, the case the `weekly` schedule's comment explicitly avoids. It is harmless
   here because the detail line carries the bar date, but it is one more thing to reason
   about.

**Cost of the fold-in, stated fairly.** It couples two pipelines: an event-stage bug can
mask the price stage unless each stage gets its own try block and the price stage runs
first. The `job_runs` row also stops telling you which stage failed. If the operator does
want the late-evening ring, this objection falls. **Ask him; don't assume either answer.**

---

## Objection (b): "the same verdict `watch_check.py` gives on the same data" is assumed, and the planned tests cannot detect a miss

The spec's central goal is parity with the private rule. The evidence is that `adjclose` is
**present** for 2 symbols (SPY, VOD.L) at `range=1y`. Parity, though, is a property of the
whole series that reaches `mean(last 200)`, and yfinance does four things to that series
that `_yahoo_adj_closes` as specified does not:

1. **Fallback when `adjclose` is absent.** `yfinance.utils.parse_quotes` (1.4.1) sets
   `adjclose = closes` and overrides it only `if "adjclose" in data["indicators"]`. The spec
   says the fetch "takes `adjclose` ... and raises on failure". Any symbol whose payload has
   no `adjclose` block gets a verdict from `watch_check.py` and UNKNOWN forever here. Whether
   any of the 24 symbols lacks it is UNKNOWN: no futures, FX, `.DE`, `.PA` or `.HE` payload
   was examined (decomposed under F1).
2. **The same-day live-row merge.** `fix_Yahoo_returning_live_separate` (1.4.1), for
   `interval == "1d"`, drops the second-to-last row when the last two rows fall on the same
   exchange-local date. Its own comment says Yahoo returns today's bar as a separate row
   while the market is open. A raw v8 reader that only drops `None` keeps both rows. Today's
   bar is then counted twice in the 200-bar window and the oldest bar falls out. That shifts
   the MA slightly, and an MA200 row is decided exactly at the crossing, where a slight shift
   is the verdict. At 22:15 Monday to Thursday, FX is open, and so are futures from March to
   November, so this is not a corner case for those 4 symbols.
3. **Duplicate-timestamp drop** (`df[~df.index.duplicated(keep='first')]` in
   `scrapers/history.py`).
4. **Transport.** yfinance 1.4.1 fetches through `curl_cffi`, which impersonates a browser.
   The host would use plain `requests` with a fixed Mozilla UA from a datacenter IP. The
   SPY/VOD.L measurement does not say which machine it ran from. When Yahoo rate-limits a
   plain client and not the impersonating one, the two disagree at the fetch layer, not the
   rule layer.

**Why the planned tests cannot catch it.** The "Rule parity" fixtures are "computed by hand".
Their generator is the spec author's reading of the rule, so they encode the same omissions
as the code (memory `the-prediction-had-a-generator`: call the real predicate, never
re-derive it). A canned payload with no `adjclose`, or with a same-day duplicate last row,
would pass every listed test.

**What would establish the requirement, cheaply:**

- Generate golden fixtures with the real predicate. yfinance is already installed on the dev
  machine. Take canned v8 payloads (made up, or class proxies, never the operator's
  symbols), run them through yfinance's own parse and fix path, and commit the resulting
  close series as the expected output for `_yahoo_adj_closes`. Include one payload without
  `adjclose` and one with a same-date duplicate last row.
- Run a one-off probe from the **host**, not the dev box, with one proxy per unmeasured
  class: a futures, an FX, a `.DE`, a `.PA` and a `.HE` symbol that are not in the file.
  Pre-register that `adjclose` is present, there are at least 200 non-null bars, the
  `currency` field is recorded, and the last-two-rows dates differ. Do this before writing
  the fetcher, not after.

Secondary, also unmeasured: **alert volume**. Rings are tolerable only while they are rare.
With 18 of the 29 rows on `above_ma200` and crossing semantics, a symbol chopping around its
MA200 crosses repeatedly. The first run alone rings once for every row already fired. The
private repo can count historical crossings per row offline, from data it already fetches,
before deploy. That checks that ringing is livable without re-arguing the decision.

---

## Objection (c): in 6 months the rule lives in two repositories with nothing keeping them equal

`watch_check.py` (private, yfinance) stays the operator's tool. This spec re-implements its
rule in the public repo over a different client. Nothing in the design notices when they
diverge, and divergence is the expected path:

- **The rule will grow.** A rebuy list with 10 `below` rows and 18 `above_ma200` rows is one
  edit away from `below_ma200`, `above_ma50`, or a changed window. Each new condition must
  then be implemented in both repos and deployed here. Until it is, the host loader skips
  the row as `unknown price condition`. That skip is reported only by the 06:30 event job,
  and only on days its judge succeeds (F3). Meanwhile `watch_prices` clears the skipped
  row's key, because the row is not in `wf.prices`, so it silently re-arms and rings as a
  "crossing" once fixed. The spec's "rows no longer in the file are cleared" does not say
  whether a skipped row counts as gone. The builder will copy `plan_alerts`, which counts
  only parsed rows.
- **The data path will drift.** yfinance changes its fixups and transport on its own
  schedule. Version 1.4.1 already does four things the spec omits (objection b). Every
  upgrade in the private repo moves the reference, and nothing here moves with it.
- **Yahoo will tighten again.** This repo has already lost one free price source (memory
  `stooq-dead-provider-replacement`). This is the third hand-rolled v8 client, beside
  `_yahoo_fetch` and `_yahoo_closes`, and it has no failover. A partial block
  (429s after the first few of 24 back-to-back requests) is the likely failure shape. The
  spec turns the job red only when **every** symbol fails, so 23 of 24 failing is a green
  `/jobs` row plus one silent message. That message arrives every run, so it becomes
  wallpaper. A row can then sit UNKNOWN for weeks, and a cross-and-uncross during that time
  is lost for good, because UNKNOWN freezes state.

**The fix that makes "same verdict" a measured property rather than an intent:** have the
private generator write, per price row, the verdict `watch_check.py` computed when it
pushed, with that run's last-bar date and last close. The file already carries `generated`,
and none of this is a reason, size or amount. On the first run after each push, the host
recomputes the same rows **as of that bar date** from its own 1y series and reports any
disagreement loudly. That turns cross-repo drift, fetch-layer divergence and unit mistakes
into an alert on the next push instead of a silent wrong verdict. It costs a few fields and
one comparison, and keeps working whichever side changes. Also make the red threshold a
fraction (for example more than a quarter of symbols failing), not "all".

---

## Fixed check: paired facts and the claim a reader infers

| # | Sentence (spec) | The pair | What a reader infers | Established? |
|---|---|---|---|---|
| F1 | "Measured 2026-11-19: `adjclose` is present for SPY and VOD.L at `interval=1d&range=1y` (about 251 bars, enough for MA200)." | present for 2 symbols + about 251 bars, at least 200 | `adjclose` is present, with at least 200 usable bars, for **all 24** of the operator's symbols | **No: unsupported implicature.** |
| F2 | "22:15 UTC is after the US close (21:00 UTC in winter) and after the CME futures daily halt starts (22:00 UTC), so the futures bars ... are final." | halt starts 22:00 UTC + 22:15 is later | futures bars are final at 22:15 **year-round** | **No: unsupported.** The 22:00 UTC start is the winter (EST) figure. From mid-March to early November the halt is 21:00 to 22:00 UTC and trading has resumed by 22:15. The probe date (2026-11-19, after DST ended) sits on the winter side, so a measurement taken today would pass (`the-oracle-held-one-axis-fixed`). Whether Yahoo then shows the reopened session as a separate live daily row is UNKNOWN. yfinance's own fixup assumes it can. |
| F3 | Non-goal: "Re-reporting row-level loader problems. The 06:30 `watch` job already reports skipped rows of both kinds every run." | the 06:30 job reports skipped rows + this job does not | every skipped price row is reported every day | **No: unsupported, and contradicted by code.** `mode_watch` sends `format_problems` only after `judge_watch` returns. A raise from an empty window or a failed post skips the report. The 06:30 job is also provisional, pending gate `8lp.1`. |
| F4 | "29 rows, 24 symbols" | a total + a distinct count | 5 rows share a symbol with another row | The residual is decomposed: 29 − 24 = 5 extra rows sitting on shared symbols. Their spread (five pairs, or one symbol carrying six rows) is not given. Harmless: it only motivates fetching each symbol once. Supplied condition split: 18 + 10 + 1 = 29, which sums. |
| F5 | Approach 1: "reads raw closes and divides pence by 100" vs "`auto_adjust=True` closes in native units" | two differences | both differences change verdicts for every row | **Overstated.** Adjustment factors apply to bars **before** an ex-date, so the last bar's adjusted close equals its raw close. For the 11 level rows (10 `below` + 1 `above`), only the /100 matters. Adjustment matters only for the 18 MA200 rows. This does not rescue approach 1 (it is still wrong for MA200 and pence), but the rejection reads stronger than its evidence. |
| F6 | "If every symbol fails to fetch, the job raises" beside "One silent UNKNOWN report per run" | total failure is red + partial failure is a silent message | a degraded fetch shows up in `/jobs` | **No.** 1 to 23 failures out of 24 give a green row. This is a design gap rather than a fact pair, so it is folded into objection (c). |

**F1, decomposed until the parts sum.** 24 symbols = 3 futures (`BZ=F`, `GC=F`, `HG=F`) +
1 FX (`USDBRL=X`) + 20 across US equities/ETFs and `.DE`/`.PA`/`.HE`/`.L`. The supplied
facts do not split the 20. The two measured symbols cover two classes, a US ETF and an LSE
equity. Whether SPY or VOD.L is one of the 24 is UNKNOWN, so 0 to 2 of the operator's own
symbols were measured. Classes with **zero** measurement: futures (3 symbols), FX (1),
`.DE`, `.PA` and `.HE`. Each of those suffixes appears at least once, so that is at least 3
symbols. So **at least 7 of 24 symbols are in classes nothing was measured on.** The other
17 or fewer sit in measured classes but were not measured individually. "Enough for MA200"
also assumes no listing younger than a year and few `None` bars; the margin is 51.

F1 and F2 are what objection (b) and the timing half of objection (a) rest on, so I kept
them as evidence rather than letting them displace an objection. F3 is verified from code
and is the strongest single flag. It supports both (a) and (c).

---

## Smaller points (not objections)

- **Name collision.** `/watch` is already a Telegram command ("Track an instrument for volume
  alerts", `brief.py` around line 3652), separate from the `watch` job. `watch_prices` adds
  a third "watch". Worth one sentence in the spec so `/jobs` and `/watch` aren't confused.
- **Bar dates.** For FX the exchange timezone is London. Taking the UTC date of a bar
  timestamp at local midnight in summer gives the previous day. Derive bar dates in the
  exchange timezone (`meta.exchangeTimezoneName`), as yfinance does, or the 5-day
  staleness check and the detail line's date can be off by one.
- **`_yahoo_adj_closes` placement.** The spec puts it in `brief.py`, beside two Yahoo
  clients that already live in `trading.py`. Put the shared parse step (timestamps, meta,
  the series) in one place, or the three copies drift.

## Is another red-team round worth it?

**Probably not.** What is left is not design argument. It is two measurements (a host-side
proxy probe per unmeasured class, and golden fixtures generated by yfinance) and two
decisions for the operator (06:30 fold-in or 22:15; adding the per-push parity snapshot to
the file contract). Another round would mostly re-find these. Following the
`red-team-saturation` lesson: make the decisions, run the probe, record the numbers in the
bead, and go to the plan.

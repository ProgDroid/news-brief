# Watch-file price triggers — design (news-brief-vap)

Date: 2026-11-19. Status: APPROVED and BUILT 2026-11-19 (parity 29/29 on real data;
final review fixes: exchange-timezone bar dating, per-row crash isolation). Revised after one red-team round
(`docs/superpowers/reviews/2026-11-19-watch-price-triggers-redteam.md`). Step 2 of the watch
file. Step 1 (event rows, news-brief-8lp) is
`docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md`, and this reuses its
loader, file contract, job and conventions.

## Intent

The operator's `kind: "price"` rows (29 rows, 24 symbols on 2026-11-19) are checked today by
`watch_check.py` in the private portfolio repo, which only runs when his PC is on. This moves
the same check onto the always-on host, so a level crossing reaches Telegram the morning after
it happens, before any market in the file opens. **Code judges price rows, never the model.**

## Goals

- **Checked by the existing 06:30 UTC `watch` job** (user, 2026-11-19, the red-team's
  simpler design), as a second stage after the event rows. No new job.
- The same verdict `watch_check.py` gives on the same data:
  - `below`: last close < level.
  - `above`: last close > level.
  - `above_ma200`: last close > the mean of the last 200 closes.
  - `above_ma200` with fewer than 200 closes: UNKNOWN. A level row needs one recent close.
    (`watch_check.py` makes ANY row with under 200 closes UNKNOWN; the narrower rule here is
    an executor ruling, so a level row on a young symbol gets a verdict.)
- **Alert on crossing, then re-arm** (user, 2026-11-19). A row alerts the day it starts
  firing, stays quiet while it stays fired, and re-arms when it stops. Editing a row's
  signal, condition or level re-arms it too.
- **A FIRED alert rings** (user, 2026-11-19), like a TRIGGERED event.
- **UNKNOWN is reported every run**, in one silent message, never as "not fired".
- A clean run sends nothing; the job's `job_runs` row is the proof it ran.

## Non-goals

- Changing event judging.
- Trading, sizing, or any £ amounts.
- Having the private generator write its own verdicts into `watch.json` for the host to
  cross-check. The red-team proposed it; declined because it needs a private-repo change, and
  the pre-deploy parity run below measures drift directly.

## Approaches considered

1. **Reuse `trading._yahoo_closes`.** Rejected for parity. It reads raw closes and divides
   pence by 100, while `watch_check.py` uses yfinance `auto_adjust=True` closes in native
   units (pence for `GBp`). Measured: VOD.L is `currency=GBp`, close 111.9.
2. **Add yfinance to the image.** It matches exactly, but adds a heavy dependency for one call.
3. **Read Yahoo v8 chart `indicators.adjclose` in native units, reproducing yfinance's
   cleanup.** **Picked.** Measured: `adjclose` is present for SPY and VOD.L at
   `interval=1d&range=1y` (about 251 bars).
4. **A separate 22:15 UTC job.** Rejected after the red-team. Nothing in the file can be acted
   on before markets open, and it brought a summer-time futures caveat, a second stale-state
   writer, a dependency on another job's report, and a shutdown-budget change.

## Design

- **Stage 2 of `mode_watch`.** The stage runs only when price rows exist. Each stage is
  isolated: its failure is collected, the other stage still runs, and the job raises at the
  end naming every failed stage. So a Yahoo outage cannot block event alerts, or the reverse.
- **Fetch (`brief.py`):** `_yahoo_adj_closes(symbol) -> list[tuple[date, float]]` from
  `v8/finance/chart/{symbol}?interval=1d&range=1y`, with the existing `_YAHOO_HEADERS`. It
  raises on failure. It reproduces yfinance's cleanup:
  - `adjclose`, falling back to `close` where `adjclose` is absent.
  - `None` bars dropped.
  - Duplicate dates collapsed, keeping the last.
  - No unit conversion.

  Each symbol is fetched once, even when several rows share it.
- **Only completed bars:** bars dated on or after the run's UTC date are dropped. A forming
  futures or FX bar is never judged, and neither is yfinance's same-day live row.
- **Judge (`watch.py`, pure):** `evaluate_price(row, bars, as_of) -> PriceVerdict(row_id,
  state: "fired"|"not"|"unknown", detail)`. The detail reads like `watch_check.py`:
  `SPY 512.30 vs below 500 (+2.5%) @ 2026-11-18`.

  A row is UNKNOWN when:
  - its symbol failed to fetch;
  - no completed bars remain;
  - it is an `above_ma200` row with fewer than 200 completed bars;
  - the last completed bar is more than 5 days before `as_of` (a stale or delisted symbol).
- **State:** one `runtime_state` key per row, `watch.price.<id>` =
  `{"hash": <signal|condition|level>, "fired": bool}`.
  - It alerts when the row is fired AND (there is no key, the hash differs, or the stored
    `fired` is false).
  - `fired: true` is stored only after a successful send.
  - A `not` verdict stores `fired: false`, which re-arms the row.
  - UNKNOWN leaves the state unchanged.
- **Stale-key cleanup, for both stages:** a key is cleared only when its row id is absent from
  the file. A row the loader **skipped** as malformed keeps its key, so fixing a typo does not
  re-ring an old crossing. The red-team found this; it applies to event keys too.
- **Messages:**
  - A FIRED alert rings: `📈 Watch p1: <name>. FIRED` plus the detail line.
  - UNKNOWN rows join the silent rows-not-judged report, which already exists and is sent
    every run.
- **Failure:**
  - **The price stage fails (job red) when more than 25% of distinct symbols failed to
    fetch,** not only when all failed. As first written, a 23-of-24 outage showed green.
  - A failed send stores nothing and fails the job.
  - A missing or broken file raises through the shared loader.
- **First run:** every row already fired that day alerts once. Expected; note it at deploy.

## Pre-deploy parity run (the real oracle)

Hand-computed tests cannot see yfinance behaviour the fetcher fails to reproduce. So before
deploy, on the operator's PC (which already queries Yahoo for these symbols daily), run the new
fetcher and `evaluate_price` against the portfolio repo's `watch_check.evaluate` for all open
price rows, on the same day. **Print only counts of agree, disagree and unknown, plus the
`row_id`s that disagree; no names or reasons.**

Pass: zero disagreements. Exception: an `above_ma200` row whose close sits within 0.5% of its
MA, where the completed-bar rule can legitimately differ for a day. Any other disagreement
blocks deploy.

## Tests (pre-registered)

- **Rule parity on fixtures:**
  - `below`/`above` just either side of the level.
  - `above_ma200` with 199 and with 200 completed bars.
  - A bar dated on the run day is ignored.
  - A stale last bar gives UNKNOWN.
  - GBp values are compared with no /100 conversion.
- **Fetch, on a canned payload:**
  - `adjclose` is used, with `close` as the fallback.
  - `None` is dropped.
  - Duplicate dates keep the last.
  - No conversion is applied.
- **State:**
  - It fires on crossing, stays silent while fired, re-arms on `not`, and re-fires on a
    re-cross.
  - An edited level re-arms the row.
  - UNKNOWN changes nothing.
  - A removed row's key is cleared, and a **skipped** row's key is kept (both stages).
  - A failed send stores nothing.
- **Job:**
  - The stages are isolated: a price fetch failure still sends event alerts, and the job
    exits non-zero.
  - More than 25% of symbols failing turns the job red; 25% or fewer reports them silently.
  - Shared symbols are fetched once.
  - Only FIRED rings.
  - A file with no price rows makes no Yahoo call.

## Calls made by the agent

The 5-day staleness limit; the 25% failure threshold; dropping run-day bars; the 0.5%
parity tolerance for MA-straddling rows; UNKNOWN joining the existing silent report; the fix
for keeping skipped rows' keys applying to event rows as well.

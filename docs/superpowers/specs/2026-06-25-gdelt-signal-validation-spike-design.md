# GDELT Signal-Validation Spike — Design

**Date:** 2026-06-25
**Status:** Approved (brainstorming) — pending implementation plan
**Backlog item:** External geo-dashboards borrow-backlog #2 (GDELT 2.0 as a trading-signal feeder)

## Purpose

Answer one question cheaply before building anything for production:

> **Does GDELT-measured geopolitical conflict tone carry tradeable signal at our daily cadence?**

The backlog flagged GDELT 2.0 as the single highest-novel-value borrow item (3 of 5 evaluated
dashboards independently chose it) but explicitly said *"prove signal with a spike before trusting
it."* GDELT is a noisy firehose (CAMEO coding is imperfect); dumping it into the curated brief would
fight our deliberate curation. So it can only ever be a **trading-side** input, and only if a signal
actually exists. This spike is the go/no-go gate.

This is a **throwaway validation spike**, not a production feature. No brief wiring, no signal
injection, no paper-trading hookup.

## Hypothesis (the one thing we test)

> Rising GDELT conflict intensity in the Middle East / energy theatre predicts near-term (t+1 to t+5)
> gains in oil and gold, and is a risk-off tell for broad equities.

This maps directly onto plays the brief already trades (Hormuz / energy / Middle East), so a positive
result would be immediately actionable and a negative result is a clean, defensible reason to drop the
item.

- **Signal (independent variable):** a daily scalar "conflict pressure" derived from GDELT **Events**,
  using the conflict-coded CAMEO root codes (18–20: assault / fight / mass violence) plus
  `GoldsteinScale` magnitude and `AvgTone`, restricted to events geolocated to a Middle-East country
  set (Iran, Iraq, Israel, Saudi Arabia, Syria, Yemen, and neighbours as appropriate).
- **Targets (dependent variable), daily historical series via yfinance:**
  - `USO` (oil) and `GLD` (gold) — the two thesis-aligned longs.
  - `SPY` — risk-off control, expected **negative** relationship.
  - `ITA` (defense) — optional secondary long.
- **Direction priors:** conflict ↑ → USO ↑, GLD ↑, SPY ↓. A signal that fires with the **wrong sign**
  counts as a failure, not a success.

## Data source decision

The backlog recipe targets GDELT **2.0** (15-minute real-time files, −20 min lag) — correct for an
eventual production feeder that needs low latency, but the wrong tool for validation (6 months ≈ 17k
zip downloads).

**Decision: validate on GDELT 1.0 daily Event files.**

- URL pattern: `http://data.gdeltproject.org/events/YYYYMMDD.export.CSV.zip` (one file per day).
- ~180 files for a 6-month window vs ~17k for 2.0 at 15-min.
- Same Events schema we need: `GoldsteinScale`, `AvgTone`, actor/action geo country codes, CAMEO
  `EventRootCode`, `NumMentions`.
- Daily resolution matches our daily paper-trade marks.
- Exact URL, column order, and schema version verified at implementation time (GDELT 1.0 Events has a
  fixed tab-delimited, header-less column layout — confirm against the GDELT codebook before parsing).

**Consequence:** only if this spike returns a "go" do we build the 2.0 real-time 15-minute puller (the
floored-timestamp `{YYYYMMDDHHmm00}.export.CSV.zip` recipe from the backlog). Building the expensive
real-time pipeline before knowing the signal exists is exactly the trap the backlog warns against.

## Method

Reuse the existing `backtest/` machinery; write almost no new statistics code.

1. **New code — GDELT puller + daily aggregator** (`backtest/gdelt/`):
   - Download + unzip + parse each day's Events CSV (tab-delimited, header-less).
   - Filter to Middle-East-geolocated conflict events.
   - Aggregate to one scalar `conflict_score` per calendar day (e.g. mention-weighted count of
     conflict-coded events, or mean negative Goldstein; exact aggregation chosen during
     implementation and noted in FINDINGS).
   - Emit a `date -> conflict_score` series.
2. **Reuse — `backtest/`:**
   - `prices_yf.py` for daily price history of the target instruments.
   - `returns.py` / `align.py` for forward returns (t+1..t+5) and date alignment (GDELT calendar days
     vs trading days).
   - `metrics.py` for **Information Coefficient** (Spearman rank correlation), **hit-rate** (sign
     agreement), and quantile spread.
3. **Output:** a small report (printed + JSON) of IC, hit-rate, sample count, and sign-vs-prior check
   for each (instrument × horizon) cell.

## Scope boundaries (YAGNI)

- **Location:** nested under existing `backtest/gdelt/` — **not** a new top-level module. This avoids
  the Dockerfile-COPY / GitHub-workflow path allowlist updates a new top-level package would require,
  and it is correct because the spike is manual and never runs in production cron.
- **Out of scope:** brief wiring, signal injection into `signals-{date}.json`, paper-trading hookup,
  the 2.0 real-time puller, any production config/feature flag.
- **Tests:** light. Unit-test the CSV parse + daily aggregation against a tiny committed fixture
  (fixture + monkeypatch pattern, per `tests/test_enrichment_providers.py`); do **not** test the live
  network pull. Guard any pandas/polars-dependent tests with `importorskip` (CI has no pandas).
- **Pre-push gate:** `ruff check` + `ruff format --check` + `pytest` (stage all reformatted files).

## Go / no-go criteria

- **GO** (worth building the 2.0 real-time feeder): IC materially > 0 with the **correct sign** on at
  least oil **and** gold, hit-rate > ~53%, across a meaningful sample (~120+ trading days).
- **NO-GO / SKIP:** weak, insignificant, or wrong-sign IC → document as a (near-)null and close the
  backlog item.

## Honest expectation

Given the prior **confident null on sentiment-sizing** (memory `sentiment-sizing-null-decided`,
n≈7k, sentiment does not size positions), the base rate of a clean "go" here is modest — roughly
25–35%. A null is a **successful** spike outcome: it cheaply prevents building a real-time pipeline
for noise. The session is allowed to end in SKIP per the backlog's one-item-per-session protocol.

## Deliverables

- `backtest/gdelt/` spike code (puller + aggregator + runner).
- Light unit tests with a tiny fixture.
- `backtest/gdelt/FINDINGS.md` — the go/no-go result with the actual numbers.
- Backlog memory STATUS update for item #2 (GO → next-step build, or NO-GO → closed).
- Captured learning.

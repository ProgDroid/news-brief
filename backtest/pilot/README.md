# MCP-approx sentiment backtest pilot (Task 8)

Operator-run, offline R&D. Decides — **directionally only** — whether an entity
sentiment signal predicts forward returns, as a precursor to the faithful
**Bigdata.com** ([https://bigdata.com](https://bigdata.com)) RavenPack run (Task 9,
gated on the business-email REST key).

> **APPROXIMATION CAVEAT.** This pilot substitutes **Claude's headline tone
> scoring** for RavenPack's sentiment dataset (the MCP path surfaces no numeric
> sentiment — see `capture_windows.md`). A positive result says *a* sentiment
> signal has edge; it does **not** establish that **Bigdata.com's** sentiment does.
> Never wire this into `trading.py` sizing. The deliverable is a go/no-go report.

## Pipeline

```
bigdata_search (capture_windows.md)        →  raw/<TICKER>/<YYYY-MM>.json   (headlines)
score_window  (scorer_llm, Claude)         →  sentiment_<TICKER>.json       (tone series)
fetch_price_series (prices_yf, yfinance)   →  PriceSeries per name
run_backtest + report_markdown (run.py)    →  RESULT-approx-<date>.md       (go/no-go)
```

`backtest/` is **not** in the cron image and needs no Dockerfile/CI change. Live
calls (`bigdata_search`, Claude scoring, yfinance) happen **only** here, never in CI.

## Prereqs

- Run Python via **PowerShell** on this host (the Bash tool errors `stdin is not a
  tty`; memory `python-via-powershell`).
- `pip install yfinance` if missing (it is **not** in the brief image's
  `requirements.txt`; it stays local — do not add it).
- Anthropic creds available to the existing brief client/config (reused by
  `score_window`).

## Step 1 — capture headlines

Follow `capture_windows.md`. Start with **5–10 liquid names**. Result tree:
`backtest/pilot/raw/<TICKER>/<YYYY-MM>.json`.

## Step 2 — score each window into a sentiment series

For each name, score every captured window and emit the Task-5 fixture shape
(`sentiment_<TICKER>.json`, consumed by `FixtureSentimentSource`). Date each point
to a consistent in-window day (e.g. last captured trading day) so it aligns to a
real close — keep the convention identical across names.

```python
import json, pathlib, anthropic
from backtest.scorer_llm import score_window

client = anthropic.Anthropic()  # reuse the brief's config/creds
raw_root = pathlib.Path("backtest/pilot/raw")
out_dir = pathlib.Path("backtest/pilot/series"); out_dir.mkdir(parents=True, exist_ok=True)

for tdir in sorted(raw_root.iterdir()):
    ticker = tdir.name
    points = []
    for wfile in sorted(tdir.glob("*.json")):
        w = json.loads(wfile.read_text(encoding="utf-8"))
        if not w["headlines"]:
            continue  # empty window — skip, don't fabricate
        score = score_window(client, ticker, w["window"], w["headlines"])
        points.append({"date": f"{w['window']}-28", "value": score})  # month-end convention
    (out_dir / f"sentiment_{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "points": points}, indent=2), encoding="utf-8")
```

`FixtureSentimentSource(str(out_dir)).series(ticker)` then reads these back if you
want to inspect them.

## Step 3 — fetch prices

```python
from backtest.prices_yf import fetch_price_series
# yf symbol may differ from the signal symbol (e.g. LSE: "RR.L"). Cover the full
# date span of the captured windows plus the longest horizon's forward window.
prices = {t: fetch_price_series(yf_symbol_for[t], "2023-01-01", "2025-12-31")
          for t in tickers}
```

## Step 4 — run the backtest + write the report

The runner pools `(date, sentiment, fwd_return)` across tickers per horizon, splits
**temporally** (earlier `split_frac` = discovery, later = held-out), selects the
horizon by arg-max |IC| on **discovery only**, and reports the full metric suite on
the **held-out** set. Read the held-out confirmation row, not the discovery table.

```python
import json, pathlib
from backtest.series import load_sentiment_series
from backtest.run import run_backtest, report_markdown

sent = {t: load_sentiment_series(json.loads(
            (pathlib.Path("backtest/pilot/series") / f"sentiment_{t}.json").read_text("utf-8")))
        for t in tickers}

res = run_backtest(
    sent, prices, horizons=[1, 5, 21, 63],
    mode="level",        # or "delta" for day-over-day change in tone
    q=5,
    split_frac=0.5,
    standardize=True,    # z-score sentiment WITHIN each ticker before pooling —
)                        # recommended for cross-ticker pools (removes level offsets)

md = report_markdown(res)
pathlib.Path("backtest/pilot/RESULT-approx-<date>.md").write_text(md, encoding="utf-8")
print(md)
```

## Step 5 — read it as DIRECTIONAL-ONLY

- **held-out Rank IC** ≈ 0 across horizons → no usable directional signal from
  *approximate* tone; weak evidence against pursuing the faithful run, but not
  decisive (the approximation could be washing out a real RavenPack signal).
- **held-out Rank IC** consistently signed with monotone quantile buckets →
  *a* sentiment signal has directional edge; **justifies** the faithful Task-9 run
  to test whether Bigdata.com's sentiment specifically carries it.
- Either way: **directional only.** No sizing decision off this pilot. Record the
  universe, window count, and per-window cost so the go/no-go is auditable.

Save the rendered report to `backtest/pilot/RESULT-approx-<date>.md` and note the
sample size / cost alongside it.

## Known limits

- One sentiment point per monthly window → coarse; small held-out `n` with a tiny
  universe. Widen names before trusting a near-zero IC.
- `event_filtered` (event-conditioned slice) is built but not wired into
  `run_backtest` yet — it needs event dates that only land with Task-8 capture or
  Task-9 data (memory `bigdata-next-steps`, open Minor (b)).
- yfinance single-symbol downloads can return MultiIndex columns;
  `prices_yf._closes_from_frame` tolerates this, but eyeball `len(prices[t].closes)`
  is non-zero before running.

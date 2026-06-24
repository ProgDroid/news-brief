# AV sentiment backtest — operator runbook

Offline research tool (NOT in the cron image / CI). Needs Alpha Vantage
**premium** for the bulk pull (free tier = ~95 days; premium 75 req/min ≈ 35 min).

## 1. Pull (one premium month, ~35 min)

```powershell
cd G:\pythonDev\news-brief
$env:ALPHAVANTAGE_API_KEY = "YOUR_PREMIUM_KEY"   # never commit this
python -m backtest.avpull.pull
```

Writes raw JSON + a manifest to `./av_data/` (gitignored). **Resumable:** re-run
after any interruption — completed units in `av_data/manifest.json` are skipped.
Downgrade/cancel the AV plan once the pull is complete.

Optional ~10-sec smoke before committing to the full pull (also reveals the
transcript response shape):

```powershell
python -c "from backtest.avpull import pull; import os; k=os.environ['ALPHAVANTAGE_API_KEY']; tx=pull.fetch_transcript('MU','2024Q1',k); print(list(tx.keys()), list(tx['transcript'][0].keys()))"
```

## 2. Run the backtest (offline; yfinance for prices; no key)

```powershell
python -c "from backtest.avpull import run_av_backtest as r; r.main('./av_data', './av_data/results')"
```

## 3. Read the results

`av_data/results/RESULT-av-news.md` and `RESULT-av-transcript.md`. The **held-out
confirmation** row (not the discovery sweep) is the go/no-go: positive rank IC +
monotone quantiles + adequate n ⇒ a tradeable sentiment factor; flat/insignificant
⇒ sentiment-for-sizing unsupported for that factor.

## Caveats (see spec §6, §10)

- **Survivorship bias:** today's large-caps, not point-in-time membership (cross-sectional rank IC mitigates).
- **Transcript dating** is a quarter-label anchor (lookahead-safe; noisy for off-calendar-fiscal names like MU — see spec §10.3).
- **Transcript sentiment** may be `[0,1]` positivity, not signed — interpret rank IC as positivity-ranking accordingly (spec §10 risk 5).

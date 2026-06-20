# Pilot capture runbook — windowed `bigdata_search` → raw headlines

Operator-run, in a Claude session with the **Bigdata.com** ([https://bigdata.com](https://bigdata.com))
MCP connector attached. This produces the raw per-window headline files the LLM
scorer (`backtest/scorer_llm.py`) turns into a sentiment series.

> **Why this exists (read first):** the 2026-06-20 probe proved `bigdata_search`
> exposes **no numeric sentiment** — only chunk text + relevance — and the
> connector has no historical-sentiment time-series tool. So the pilot
> *reconstructs* an approximate tone series: retrieve each window's headlines,
> then have Claude score the media tone. This is **DIRECTIONAL ONLY** — it tests
> whether *a* sentiment signal predicts returns, not whether **Bigdata.com's**
> does. The faithful run is Task 9 (REST, gated on the business-email key).

## Output layout

```
backtest/pilot/raw/<TICKER>/<YYYY-MM>.json
```

Each file:

```json
{
  "ticker": "MU",
  "window": "2025-09",
  "rp_entity_id": "49BBBC",
  "headlines": ["Micron beats on AI memory demand", "DRAM pricing surges", "..."]
}
```

## Per-name, per-window loop

1. **Resolve the entity id once per name** with `find_securities` (one focus per
   call). Cached ids confirmed live 2026-06-20 (memory `bigdata-enrichment-validated-live`):
   `CVX=D54E62  MU=49BBBC  RGLD=263216  ESLT=0401A0  AVAV=F1EB39  NEE=2CB4C9  RR=947B28`
   (RR = Rolls-Royce LSE, **not** Richtech Robotics). Do **not** re-resolve an id
   already confirmed in this thread.

2. **For each monthly window** call `bigdata_search`:
   - natural-language query naming the company (full sentence, not keywords) —
     e.g. *"news about Micron Technology during September 2025"*;
   - scope to the entity (`rp_entity_id`) so off-entity chunks are excluded;
   - timestamp window = that month only (**one time period per call** — never
     fold multiple months into one search);
   - fast mode, `max_chunks` ≈ 12.

3. **Extract headlines** from each chunk's document metadata (headline/title; fall
   back to the first sentence of the chunk text). Dedupe near-identical strings.
   Write the file above. An empty month → write `"headlines": []` (the scorer
   skips empty windows; do not fabricate).

## Budget discipline

- ≈ **1.2 query units per windowed search** (probe-measured).
- 100 names × ~30 monthly windows ≈ 3,000 searches ≈ **~3,600 units** — do **not**
  fan out blind.
- **Start with 5–10 names** to confirm wiring and per-window cost, eyeball a few
  `raw/*.json` files, then decide whether to widen. Log what you capture vs. skip;
  a silently-truncated universe reads as "covered everything" when it wasn't.

## Window/horizon sizing note

The engine measures forward returns by **positional (trading-day) offset** over the
price index, and aligns sentiment to returns on **matching dates**. Use **one
sentiment point per window** dated to a consistent day (e.g. month-end
`YYYY-MM-28`/last captured trading day) so it aligns to a real close. Monthly
windows pair naturally with horizons like `[1, 5, 21, 63]` (≈ 1d / 1w / 1mo / 3mo).
Keep the dating convention identical across all names or the cross-ticker pool
misaligns.

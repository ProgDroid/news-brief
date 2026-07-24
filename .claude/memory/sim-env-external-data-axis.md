---
name: sim-env-external-data-axis
description: How external market/news feeds behave in this simulated-2026 environment — pick pre-2025 windows for real data; GDELT must be dated by modal SQLDATE
metadata: 
  node_type: memory
  type: reference
  originSessionId: 4c2fff72-fde8-4474-928a-21842bee9965
---

This dev environment runs a **simulated 2026 clock** (system date 2026-06-25) over external data feeds. Discovered 2026-06-25 during the GDELT signal-validation spike ([[external-geo-dashboards-backlog]] item #2).

**Behavior of external feeds here:**
- **yfinance** honors the REQUESTED date axis: a 2026 request returns 2026-dated prices, 2025→2025, 2024→2024. Data exists through sim-present (~2026-06).
- **GDELT 1.0 daily** (`data.gdeltproject.org/events/YYYYMMDD.export.CSV.zip`): continuous daily coverage 2015 → sim-present (2026-06), correctly dated; one ~11-day gap at 2025-06-20..07-01; 404 after ~2026-06.
- **Unknown if 2025–2026 data is real or synthetic.** Pre-2025 (e.g. 2022–2024) is unambiguously REAL historical data on both feeds and axis-aligned.

**How to apply:** For any backtest / external-data validation in this repo, prefer a **pre-2025 window** (e.g. 2022–2024) so the data is unambiguously real and both feeds share one date axis. Avoid windows straddling the sim boundary unless you've confirmed the 2025–2026 data is real.

**GDELT gotcha (cost me a false alarm):** date a GDELT file by the **MODAL** SQLDATE, never row 0. Row 0 is often a straggler / late-re-added old event (e.g. file `20260601` had a row-0 SQLDATE of `20250601` among 897 stragglers, but its modal date is correctly `20260601` with ~100k events). Reading row 0 looked like a bogus "1-year shift". `backtest/gdelt/aggregate.fold_daily` already groups by per-event SQLDATE, which is correct once you ignore the row-0 noise.

See [[brief-sources-and-edge-latency-thread]], [[multi-asset-trading-build]].

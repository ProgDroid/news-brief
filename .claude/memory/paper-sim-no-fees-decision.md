---
name: paper-sim-no-fees-decision
description: Why the paper trade simulation is frictionless (no fees) and why we rejected using a demo T212 account
metadata: 
  node_type: memory
  type: project
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
---

The paper trade tracker is **frictionless by design** and we decided (2026-05-31) to keep it that way — no fees, no demo T212 account.

**What it actually models:** `_signal_return` = `sign * (price/entry - 1)`, priced off a single Stooq daily **close** (`fetch_stooq_price`, column 6) for both entry and exit. `paper_scorecard` reports equal-weighted hit-rate + mean return, percentages only. So there is **no commission, spread, slippage, FX fee, or position sizing**. It measures *directional signal quality* (was the 1w/2w/4w bullish/bearish call right), not a tradeable net P&L.

**Why:** Why: For signal-quality measurement, fees (~0.1–0.3% round trip) are noise against 1–4 week moves and won't flip whether a call was right — excluding them keeps the measurement clean.

**Why NOT a demo T212 account:** it would add real spread/FX/fills but fights the system's design — requires order placement (write scope; current key is read-only by design), depends on market hours (collect runs ~6am UTC), demo accounts reset/expire (wipes a long-running scorecard), adds order-rejection/partial-fill/rate-limit failure modes, and loses the clean offline 1w/2w/4w checkpoint model.

**If realism is ever wanted:** the proportionate fix is a modeled haircut — a `PAPER_FEE_BPS` constant (~20–30 bps ≈ spread + T212's ~0.15% FX fee) subtracted at close, defaulting to 0. No external account. Not implemented; revisit only if the goal shifts from "is the signal right?" to "what's the net tradeable return?".

See [[google-news-rss-recipe]] and [[formatter-owns-style]] for other news-brief notes.

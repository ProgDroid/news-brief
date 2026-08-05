---
name: fail-closed-needs-status-not-count
description: "A fail-closed path whose normal output is 'did nothing' must return a STATUS (which gate fired, with numbers) and surface it where the operator looks — a count of 0 is unattributable, and a swallowed exception recreates the ambiguity one level up"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0020b8b4-ded9-4def-919b-be8744df2379
  modified: 2026-08-05T22:29:41.134Z
---

Any function with several independent fail-closed gates whose ordinary outcome is "nothing happened" must return **why**, not just how many things happened. `-> int` is the wrong return type: `0` collapses "no candidate qualified" (working as designed) and "the venue read failed" (broken) into the same observable, and the gates are only distinguishable at the instant they fire.

**Why:** news-brief's `open_sleeve_a_live` returned a count. Nine days of zero real-money trades could not be attributed — flags-off, unfunded wallet, unreadable orderbook, missing eventId and "nothing in band" all looked identical. It now returns a status dict (`state`, candidates/matches/opened, a per-reason skip tally, the wallet balance the bot can actually READ, and a few near-miss markets with their prices) which `validation.daily_trade_message` renders into the daily Telegram message. See [[polygram-live-trading-spec]].

**How to apply:**
- Return a status dict from the silent path; keep the count as one field of it.
- **Split any lumped reason where one branch is design and the other is a fault** — `entry_gate` became `out_of_band` (fine) vs `spread_or_book` (fault); `unreadable_or_closed` became `market_closed` (ordinary) vs `unreadable` (fault). Mark the fault branches in the rendered output (⚠️) so a broken venue read can't hide behind "nothing qualified".
- Include the *numbers*, not just the verdict: "missed the band by a cent" and "miles off" imply different fixes.
- Render it **where the operator actually looks**. A container log nobody greps is not observability; this user reads Telegram.
- **A `try/except` that swallows the failure must record a `crashed` state**, not leave the status `None`. Wrapping the live path so it can't break the paper run was correct, but the handler only logged — so a crashed sleeve rendered no status block at all and looked exactly like one that declined every market. That reintroduced the same ambiguity one level up (fixed 0ca9286). Use `log.exception` there, not `log.warning(f"{e}")`.

Relates to [[http-error-body-is-the-diagnosis]] (same failure of nerve, one layer down) and [[signals-parse-error-is-truncation]] (another "the log didn't say enough" recurrence).

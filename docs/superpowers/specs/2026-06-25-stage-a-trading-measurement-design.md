# Stage A — Trading Measurement & Attribution (Design)

**Date:** 2026-06-25
**Parent:** [Self-Improving Trading — Staged-Autonomy Roadmap](2026-06-25-self-improving-trading-roadmap.md)
**Status:** Approved for implementation planning.

## Goal

Make the trading side *self-aware before self-improving*: measure what is actually working,
at geo-aware (`kind`+`perspective`) granularity, and present it honestly. **Zero autonomy** —
every change to the system remains a human decision. This is Stage A of the roadmap; it builds
the measurement that later stages' autonomy must earn against.

One-line framing: *"Measure the trades you make, at geo-aware granularity, honestly."*

## Context (what already exists)

`validation.py` already computes net-of-cost return, hit-rate, and edge-over-benchmark by
`asset_class` / `confidence` / `play_type` / `thesis_ref` (`aggregate_performance`), renders a
weekly `performance_report`, and feeds a track-record block into the daily prompt
(`performance_prompt_block`). The signal schema (`brief.py` `_EMIT_SIGNALS_TOOL`) carries a
free-text `provenance` field. The source registry tags every feed/web source with `kind`
(`wire`/`analyst`/`regional`/`primary`) and an optional `perspective` (`ARAB`, `UKRAINIAN`,
`KOREAN`, …) via `_source_header`.

The gap: `perspective` is a *registry* property, not inferable from finished-brief prose; and
positions today drop `provenance` entirely, so realized P&L cannot be attributed to a source.

## Decisions (from brainstorming)

- **Q1** — Primary focus: source-trust (A) + calibration (B); shaped by the rest.
- **Q2** — Source attribution granularity: **`kind` + `perspective`** (not `kind`-only, not raw
  free-text).
- **Q3** — Resolution mechanism: **Opt 2, upstream pick-from-list**. The post-delivery signal
  extractor is given the day's tagged source list and names the registry source it cited; code
  derives `kind`+`perspective` from the registry (single source of truth).
- **Q4** — Declined signals: **lightweight leakage counts** now; full counterfactual scoring
  parked (see roadmap "Deferred / parked ideas").
- **Q5** — Surfacing: **human-facing only**. Extend `performance_report` (rendered on demand by
  the existing `/performance` command and pushed weekly). `performance_prompt_block` is **left
  untouched** — the firewall between "measured" and "acted on" is what keeps Stage A from
  quietly becoming Stage B and overfitting on unproven dimensions.

## Architecture

Five units, each independently testable:

### 1. Source registry resolver  *(new, single source of truth)*
- The source universe is the union `RSS_FEEDS` (hardcoded, lines ~137-252) `+
  load_temp_sources()` (volume `sources.json`), each entry a dict with `name` / `kind` /
  optional `perspective`. A small helper composes and indexes this union by `name` (the
  `source_id`). No such accessor exists today; the plan adds one.
- A function `resolve_source_tags(source_id) -> {"kind": str, "perspective": str | None}` looks
  the id up in that index. Unresolved or `None` → `{"kind": "unknown", "perspective": None}`.
- Pure, never raises. The *only* place `kind`/`perspective` are derived, so the registry stays
  the single source of truth and no caller invents tag values.
- `source_id` = the registry `name`. Names are assumed unique; if a temp source duplicates a
  hardcoded name, the hardcoded entry wins (deterministic precedence, noted for the plan).

### 2. Extraction call — capture `source_id`  *(brief.py)*
- `build_signals_request(brief_text, sources)` additionally receives the day's tagged source
  list (each source's registry `name`; `kind`/`perspective` may be included for the model's
  disambiguation but are **not** trusted back — code re-derives them via the resolver).
- `_EMIT_SIGNALS_TOOL` gains an optional `source_id` property: *"the source from the provided
  list that the brief cites for this signal; omit if none."* Free-text `provenance` is retained
  as a diagnostic fallback.
- The user-template lists the available sources so the model picks from a closed set.
- `normalize_signals` keeps `source_id` (nulled when absent/unknown), preserving its existing
  drop rules. No new required fields — a signal with no `source_id` is still valid.
- Token cost lands entirely on the post-delivery extraction call (latency-free for the user).

### 3. Position stamping  *(trading.py `mode_paper`)*
- On opening an equity/crypto position, resolve the signal's `source_id` via unit 1 and stamp
  `source_id`, `source_kind`, `source_perspective` onto the position dict — **as-of-open**,
  mirroring how `benchmark_entry`/`haircut` are stamped. Registry drift never rewrites history.
- Prediction positions (opened by the matcher, many-signals→one-market) are **out of scope**:
  they carry `source_kind="unknown"`, `source_perspective=None`, and are excluded from source
  attribution. Documented, not silently dropped.

### 4. Aggregation & views  *(validation.py)*
- Add `source_kind` and `source_perspective` to `_DIMENSIONS` so `aggregate_performance` slices
  them automatically. Positions without the field (legacy/prediction) fall into no bucket or
  `unknown`, consistent with the existing `None`-key skip.
- **Calibration block:** a dedicated rendering of the existing `confidence` dimension —
  low/medium/high → hit-rate, mean edge, `n` — plus a **monotonicity check** that flags
  *inversions* (a lower confidence band realizing higher mean edge than a higher one). The
  inversion is the actionable insight.
- **Sample-awareness:** define `_REPORT_MIN_N`; any bucket below it is rendered with a
  `⚠ thin — not yet meaningful` marker. `n` is always shown. v1 uses n-gating only (no Wilson
  intervals; trivial to add later if bands are wanted).

### 5. Leakage tally  *(trading.py + validation.py)*
- `mode_paper` already determines every skip reason. Capture a per-run tally into a rolling
  `paper/leakage-log.json` (date-keyed): `traded` count plus dropped-by-reason —
  `not_actionable` (neutral / low-confidence / no ticker), `no_instrument` (resolve failed),
  `no_price` (pricing failed), `unpriced_reversal`, prediction `below_floor` / `no_match`.
- `performance_report` gains a **"signal leakage (last 7 days)"** section summing the rolling
  log over the trailing window, e.g. *"34 signals → 11 traded; dropped: 14 no-ticker,
  6 no-instrument, 3 low-confidence."* Surfaces coverage gaps; scores nothing.

## Data flow

```
brief (finished prose) ─┐
day's tagged sources ───┼─► extract_signals ─► signals[].source_id (from closed set)
                        │
mode_paper ─► resolve_source_tags(source_id) ─► position{source_kind, source_perspective}
          └─► leakage tally ─► leakage-log.json
                        │
close (existing) ─► net_return / edge (unchanged)
                        │
aggregate_performance ─► dims incl. source_kind / source_perspective / confidence
                        │
performance_report ─► by source_kind · by source_perspective · calibration(+inversion)
                      · leakage(7d)   [/performance on demand · weekly push]
performance_prompt_block ─► UNCHANGED (Stage-B firewall)
```

## Error handling & safety
- Resolver and stamping never raise; a missing/garbled `source_id` → `unknown` bucket.
- Leakage logging is best-effort: a write failure logs and is skipped, never aborts a collect
  run (matches the codebase's corruption-resilient posture).
- No component changes trade selection, sizing, or live-trading enablement. Pure measurement.
- Fail-safe default everywhere: on any doubt, contribute nothing rather than a guessed value.

## Testing (TDD, pandas-free → CI-safe)
- Resolver: known id → tags; unknown/`None` → `unknown`/`None`.
- `normalize_signals`: `source_id` kept when present, nulled when absent; existing drop rules
  intact.
- `mode_paper`: equity/crypto positions stamped with resolved tags as-of-open; prediction
  positions get `unknown`.
- `aggregate_performance`: new dimensions bucket correctly; `None`/legacy skipped.
- Calibration: monotonicity flag fires on an inverted ordering, silent when monotonic; thin
  marker on small `n`.
- Leakage: per-run tally accumulates by reason; 7-day window sums correctly.
- `performance_report`: renders the new sections; thin-sample marker present; prediction
  positions absent from source sections.

## Out of scope (explicit)
- Prompt-block expansion (Stage B).
- Counterfactual scoring of declined signals (parked C — roadmap).
- Prediction-position source attribution.
- Any autonomous parameter tuning or auto-enable of live trading.

## Exit gate → unlock Stage B
Per the roadmap: attribution stable week-over-week **and** at least one dimension
(`source_kind` / `source_perspective` / `confidence`) shows a persistent signed effect across
the sustained-eval window before any of it is fed into the prompt.

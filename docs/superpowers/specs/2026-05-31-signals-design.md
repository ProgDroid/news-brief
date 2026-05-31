# Signals feature — design

**Date:** 2026-05-31
**Scope:** Complete the existing (WIP) signals pipeline in `brief.py`. Single file, no new dependencies.
**Out of scope:** The paper-trade tracker (separate design — price resolution to be settled first).

## Background

The daily brief already instructs the model to emit a JSON array of position-relevant
signals after a `---SIGNALS---` marker. Plumbing already present in `brief.py`:

- `---SIGNALS---` marker + signal schema in the daily prompt.
- `split_brief_and_signals(raw)` — separates prose from the trailing JSON array.
- `mode_collect` — splits, delivers prose to Telegram, persists signals.
- `save_signals(signals, date_str)` — writes a dated snapshot + appends to a rolling `.jsonl`.

Signals are **file-only**. The human-readable form is the prose `📌 POSITION SIGNALS`
section in the Telegram brief; the JSON is a machine artifact for the (future) paper
tracker and review tooling.

A signal record has 7 fields:
`ticker` (nullable — null = macro-level), `topic`, `direction`, `confidence`,
`thesis_ref` (nullable), `rationale`, `provenance`.

## Problem

The model emits the JSON freehand, so the pipeline can't be trusted as-is:

1. **No validation.** Bad enums (`long` vs `bullish`, `med` vs `medium`), missing keys,
   or invented fields are persisted verbatim. `mode_paper` filters on exact enum strings,
   so malformed signals are silently persisted but never paper-traded — silent data erosion.
2. **No audit trail on quiet days.** `save_signals` early-returns on an empty list, so a
   day with zero signals leaves no file — indistinguishable from "brief never ran" or
   "signals failed to parse."
3. **`mode_run` is inconsistent.** It delivers the *raw* model output, so the
   `---SIGNALS---` JSON block leaks into Telegram as prose and no signals are saved — the
   testing path never exercises the signals pipeline.

## Design

Three changes, structured as **extract → normalize → persist** so the LLM-output-cleaning
logic is isolated and independently testable, and the Trading212 privacy boundary stays
plainly elsewhere (`normalize_signals` only ever touches tickers/enums, never monetary data).

### 1. `split_brief_and_signals(raw) -> (prose, raw_signals, status)`

Extend the existing function to also report *why* it produced the signals it did, so the
snapshot can be a single source of truth. `status` is one of:

- `ok` — marker present, JSON parsed (the array may be a genuine empty `[]`).
- `parse_error` — marker present, but no JSON array found / `json.loads` failed.
- `no_marker` — `---SIGNALS---` absent entirely (model format failure).

In every case the prose is still returned for delivery (for `no_marker`, prose is the whole
output). Existing `log.warning` on parse failure is retained.

### 2. `normalize_signals(raw_signals) -> (clean, dropped_count)`

New function next to `split_brief_and_signals`. For each item:

- Coerce `direction` to `{bullish, bearish, neutral}` via a synonym map
  (`long→bullish`, `buy→bullish`, `short→bearish`, `sell→bearish`, `flat→neutral`,
  `hold→neutral`, case-insensitive). Unresolvable → **drop the signal**.
- Coerce `confidence` to `{low, medium, high}` (`med→medium`, `hi→high`, case-insensitive).
  Unresolvable → **drop**.
- Require a non-empty string `topic`. Missing/empty → **drop**.
- Normalize null-ish `ticker` / `thesis_ref` (`""`, `"null"`, `"none"`, case-insensitive) → `None`.
- Default missing `rationale` / `provenance` to `""`.
- Keep **only the 7 known fields**; discard any invented extras.

Returns the cleaned list and the count of dropped items.

### 3. Persist + wire in

**`save_signals(signals, date_str, status="ok", dropped=0)`** — drop the empty-list
early-return. Always write the dated snapshot:

```json
{
  "date": "YYYY-MM-DD",
  "generated_at": "<iso8601 UTC>",
  "status": "ok | parse_error | no_marker",
  "dropped": 0,
  "signals": [ ... ]
}
```

Append to the rolling `signals-log.jsonl` **only when `signals` is non-empty**.

**`mode_collect`** — split → normalize → deliver prose → `save_signals(..., status, dropped)`.
Log a warning when `dropped > 0` or `status != "ok"`.

**`mode_run`** — mirror `mode_collect` (split → normalize → deliver prose → save) instead of
delivering raw output, so the testing path exercises the full signals pipeline.

## Deliberately excluded (scope control)

- No cross-validation of `thesis_ref` against `theses.json` — marginal value, adds coupling.
- No change to the prompt schema field set (the 7 fields are settled).
- No Telegram delivery of the raw JSON — signals stay file-only by design.

## Verification

No test suite exists in the repo. Verify via the same stubbed-exec harness used for the
`build_daily_prompt` fix (stub `feedparser`/`requests`, redirect `/app/logs` paths):

- `normalize_signals`: synonym coercion maps correctly; malformed/extra-field/missing-topic
  signals are dropped and counted; null-ish tickers become `None`; only the 7 fields survive.
- `split_brief_and_signals`: returns the right `status` for each of ok / parse_error / no_marker.
- `save_signals`: writes a snapshot on an empty list with `status` + `dropped`; skips the
  rolling log when empty; appends when non-empty.
- `py_compile` clean after changes.

## Privacy boundary

Unaffected. Signals carry only tickers and enums (tickers already appear in the prompt via
portfolio weights). No absolute monetary value enters any signal record, snapshot, or log.

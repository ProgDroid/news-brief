# Severity-weighted retention for the standing-claim ledger (#5b)

**Date:** 2026-06-26
**Status:** Approved — ready for implementation plan
**Backlog item:** external-geo-dashboards-backlog #5b ("Severity-weighted retention: importance buys extra TTL — complements the ledger's flat 7-day aging")
**Scope:** `brief_memory.py` only. Descriptive-only, fail-safe, flag-gated by the existing `BRIEF_MEMORY_ENABLED`. Does not touch trading.

## Problem

The standing-claim ledger (`brief_memory.py`) gives the daily brief multi-day memory of facts it has already established, so it stops re-explaining them. Today every claim ages out on a **flat 7-day TTL**: `merge_ledger` retires any claim where `_days_between(last_reaffirmed, today) > RETIRE_AFTER_DAYS` (7), then keeps the 25 most-recent under `MAX_CLAIMS`.

That flat rule is blind to importance. A hugely consequential fact reported once (e.g. *"Country X invaded Country Y"*) is retired at day 7 exactly like a minor durable detail — and the brief then re-explains it as if new. Importance should buy a claim a longer life.

## Decisions (from brainstorm)

1. **Severity source:** model-assigned at reconcile time (the existing Haiku call), *not* derived from `source_count`/`restate_count`. Rationale: importance is an editorial judgment that corroboration count does not capture — a single-source exclusive can be major. Severity tiering is a coarser task than the source-counting Haiku already does, so it does not justify a model upgrade.
2. **Model stays Haiku** (`RECONCILE_MODEL`). The ledger only went live 2026-06-26, so there is no production evidence yet; the feature is fail-safe and reversible, and the model is a one-line constant swap if the first live ledgers show strain. Upgrading would also drag in a timeout re-tune (per the e255436 Sonnet-latency regression) that does not belong in this feature. Out of scope.
3. **Both eviction gates honor severity** (the TTL filter *and* the 25-claim cap).
4. **3-tier, additive-only TTL:** `low` / `normal` / `high`. Severity only ever *extends* life; it never culls early. Effective TTLs: **7d / 7d / 14d**. `low` and `normal` are identical for TTL but `low` ranks below `normal` under cap pressure.

### Consequence of (3)+(4): two derived keys, not one

Additive-only TTL means `low` and `normal` share a TTL bonus of 0, so a single shared "effective age" number cannot also make `low` rank below `normal` under the cap. Severity therefore enters the two gates differently:

- **TTL filter** uses a **high-only bonus**.
- **Cap sort** uses the **full `low < normal < high` ordering**.

Both are small lookups off one stored `severity` value.

## Design

All changes are confined to `brief_memory.py`.

### Data model

Each claim gains one optional field: `severity ∈ {"low", "normal", "high"}`.

Pre-feature claims (and any claim where the model omitted/garbled the field) carry **no** `severity` and are treated as `"normal"` everywhere via a default in the lookups. Fully back-compat; no migration of `brief_memory.json` required.

### New constants / helpers

```
RETIRE_AFTER_DAYS = 7          # unchanged — the baseline floor
HIGH_SEVERITY_BONUS_DAYS = 7   # high claims survive 7 extra days (=> 14d TTL)
_SEVERITY_RANK = {"low": 0, "normal": 1, "high": 2}   # cap ordering
_VALID_SEVERITY = frozenset(_SEVERITY_RANK)

_ttl_bonus(severity) -> int        # high -> HIGH_SEVERITY_BONUS_DAYS, else 0
_severity_rank(severity) -> int    # _SEVERITY_RANK.get(severity, 1)  (default normal)
_coerce_severity(v) -> str | None  # validated lowercase enum, else None (omit)
```

### `merge_ledger` changes

**Severity lifecycle:**
- *New* claim → `severity = _coerce_severity(mc.get("severity")) or "normal"`.
- *Reaffirmed* claim (echoed id) → `new = _coerce_severity(mc.get("severity"))`; set `base["severity"] = new if new else base.get("severity", "normal")`. Latest-valid-else-keep: importance can move **both** directions, but a single omission won't silently demote a claim. Deliberately **not** peak-max like `source_count` (peak-max would ratchet everything to `high` within a week).

**TTL filter** — replace the flat filter with an effective-age filter:
```
effective_age = _days_between(c["last_reaffirmed"], today) - _ttl_bonus(c.get("severity"))
keep if effective_age <= retire_after_days
```
⇒ high retires at actual age > 14; normal/low at > 7.

**Cap sort** — replace the pure-recency sort with a (severity, recency) sort:
```
result.sort(key=lambda c: (_severity_rank(c.get("severity")), c["last_reaffirmed"]), reverse=True)
return {"version": 1, "claims": result[:cap]}
```
⇒ under the 25-cap, high beats normal beats low; recency breaks ties within a tier.

**Bounded risk (intended behavior, on record):** a live high-severity claim (≤14d) outranks every normal/low claim for cap slots regardless of their recency. This is the intended "importance buys life"; it is safe because high-severity standing facts are rare (won't approach 25 live) and self-heal at 14d. A mis-tiered `high` over-suppresses a fact for up to two weeks — cosmetic only, since the ledger is descriptive.

### Prompt change (`_RECONCILE_TEMPLATE`)

Add one rule instructing the model to set `"severity"` per claim, with crisp criteria:
- **high** = a major standing development the reader must not have re-explained — wars, leadership/regime changes, major policy regime shifts, market-structural events.
- **normal** = a typical durable fact (the default).
- **low** = a true but minor durable detail, low-stakes.
- *When unsure, use `normal`.*

Extend the per-item JSON schema line to include `"severity": "<low|normal|high>"`.

### Parsing (`parse_reconcile_response`)

Add `_coerce_severity` and include `severity` in the extracted entry when valid; omit otherwise (merge then defaults to `normal`). Mirrors the existing `_coerce_source_count` pattern.

### Render — unchanged

`render_established_block` is **not** touched. Severity is an internal retention signal, not reader-facing. The ESTABLISHED block already means "facts to NOT re-explain"; surfacing "high severity" there risks the model re-featuring them and overlaps the why-it-matters lens. YAGNI.

## Testing (TDD)

- `_ttl_bonus` / `_severity_rank` / `_coerce_severity` mapping + defaults (incl. bool/None/garbage → omit-or-normal).
- Effective-age TTL filter: high survives to 14d and retires at 15d; normal/low retire at 8d.
- Cap ordering: high > normal > low under the cap; recency tiebreak within a tier; verify a stale-but-high claim survives the cap over fresh normals.
- Lifecycle: new claim stores severity; reaffirm updates in both directions; omitted-on-reaffirm keeps existing; invalid → normal default.
- Back-compat: a claim with no `severity` is treated as normal (7d TTL, normal cap rank).
- Parse coercion: valid/invalid/missing/typed severity.
- Prompt-substring regression: `_RECONCILE_TEMPLATE` mentions severity criteria (mirrors existing prompt-substring tests).
- Fail-safe path (`reconcile_ledger` swallows errors, keeps prior ledger) still holds.

## Out of scope

- Model upgrade (stays Haiku).
- Any reader-facing severity display.
- Any change to trading, signals, or the corroboration `source_count` tag.
- Migration of existing `brief_memory.json` (back-compat default handles it).

## Activation

Pure addition behind the already-live `BRIEF_MEMORY_ENABLED` flag (on in prod). Takes effect on the next `mode_collect` reconcile after deploy; existing claims acquire `severity` as they are reaffirmed.

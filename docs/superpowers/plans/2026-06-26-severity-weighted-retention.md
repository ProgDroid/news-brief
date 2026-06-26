# Severity-weighted Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the standing-claim ledger a model-assigned `severity` so important facts survive longer than the flat 7-day TTL and resist cap eviction.

**Architecture:** All changes are confined to `brief_memory.py` (logic) and `tests/test_brief_memory.py` (tests). A new `severity ∈ {low,normal,high}` field is assigned by the existing Haiku reconcile call, stored on each claim, and read by two retention gates: a high-only TTL bonus (effective age) and a severity-aware cap sort. Descriptive-only, fail-safe, no trading impact.

**Tech Stack:** Python 3, pytest, ruff. No new dependencies.

## Global Constraints

- Touch only `brief_memory.py` and `tests/test_brief_memory.py`. No changes to `brief.py`, trading, signals, or `render_established_block`.
- Back-compat: a claim with **no** `severity` field (legacy / pre-feature) is treated as `"normal"` everywhere. No migration of `brief_memory.json`.
- Fail-safe: `reconcile_ledger` already swallows all errors and returns the prior ledger — do not weaken this.
- `RETIRE_AFTER_DAYS` stays `7` (the baseline floor). High severity adds `HIGH_SEVERITY_BONUS_DAYS = 7` ⇒ TTLs 7d / 7d / 14d.
- Severity scale is exactly `{"low", "normal", "high"}`; default and fallback is `"normal"`.
- Stay on Haiku (`RECONCILE_MODEL` unchanged). No timeout changes.
- Severity is internal — it is NOT rendered into the ESTABLISHED block.
- Pre-push gate (per repo convention): `ruff check . && ruff format --check . && pytest` must all pass; stage every file ruff reformats.
- Run Python via the PowerShell tool (the Bash tool errors "stdin is not a tty"); make git commits via the Bash tool.

---

### Task 1: Severity constants and helper functions

**Files:**
- Modify: `brief_memory.py` (add constants after `RETIRE_AFTER_DAYS`; add three helpers near `_coerce_source_count`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `HIGH_SEVERITY_BONUS_DAYS: int = 7`, `_SEVERITY_RANK: dict[str,int]`, `_VALID_SEVERITY: frozenset[str]`, `_DEFAULT_SEVERITY: str = "normal"`
  - `_coerce_severity(v) -> str | None` — canonical lowercase severity, or `None` to omit/default
  - `_ttl_bonus(severity) -> int` — extra retention days (only `"high"` extends)
  - `_severity_rank(severity) -> int` — cap ordering; unknown/missing → normal rank

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("low", "low"),
        ("normal", "normal"),
        ("high", "high"),
        ("HIGH", "high"),
        ("  High ", "high"),
        ("medium", None),
        ("", None),
        (None, None),
        (2, None),
        (True, None),
    ],
)
def test_coerce_severity(raw, expected):
    assert bm._coerce_severity(raw) == expected


@pytest.mark.parametrize(
    "sev,expected",
    [("high", 7), ("normal", 0), ("low", 0), (None, 0), ("bogus", 0)],
)
def test_ttl_bonus(sev, expected):
    assert bm._ttl_bonus(sev) == expected


@pytest.mark.parametrize(
    "sev,expected",
    [("high", 2), ("normal", 1), ("low", 0), (None, 1), ("bogus", 1)],
)
def test_severity_rank(sev, expected):
    assert bm._severity_rank(sev) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell tool): `python -m pytest tests/test_brief_memory.py -k "coerce_severity or ttl_bonus or severity_rank" -v`
Expected: FAIL with `AttributeError: module 'brief_memory' has no attribute '_coerce_severity'`.

- [ ] **Step 3: Add constants**

In `brief_memory.py`, immediately after the line `RETIRE_AFTER_DAYS = 7`, add:

```python
HIGH_SEVERITY_BONUS_DAYS = 7  # extra retention days a "high" claim earns (=> 14d TTL)
_SEVERITY_RANK = {"low": 0, "normal": 1, "high": 2}  # cap-eviction ordering
_VALID_SEVERITY = frozenset(_SEVERITY_RANK)
_DEFAULT_SEVERITY = "normal"
```

- [ ] **Step 4: Add helper functions**

In `brief_memory.py`, add these three functions just above `_coerce_source_count`:

```python
def _coerce_severity(v) -> str | None:
    """Canonical severity ('low'/'normal'/'high'), or None to omit/default to normal.
    Case-insensitive; anything non-string or outside the enum returns None."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VALID_SEVERITY:
            return s
    return None


def _ttl_bonus(severity) -> int:
    """Extra retention days a claim's severity buys. Only 'high' extends life."""
    return HIGH_SEVERITY_BONUS_DAYS if severity == "high" else 0


def _severity_rank(severity) -> int:
    """Cap-eviction ordering: high > normal > low; unknown/missing -> normal."""
    return _SEVERITY_RANK.get(severity, _SEVERITY_RANK[_DEFAULT_SEVERITY])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_brief_memory.py -k "coerce_severity or ttl_bonus or severity_rank" -v`
Expected: PASS (21 cases).

- [ ] **Step 6: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): add severity constants + helpers to claim ledger

_coerce_severity / _ttl_bonus / _severity_rank, low/normal/high scale.
Pure additions; no behavior change yet.
EOF
```

---

### Task 2: Store severity on new and reaffirmed claims in `merge_ledger`

**Files:**
- Modify: `brief_memory.py` — `merge_ledger`, the reaffirm branch (`if cid and cid in by_id:`) and the new-claim branch (`elif mc.get("claim"):`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `_coerce_severity`, `_DEFAULT_SEVERITY` (Task 1).
- Produces: every merged claim carries a `severity` key. Reaffirm uses latest-valid-else-keep (NOT peak-max). New claim defaults `"normal"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_merge_new_claim_takes_observed_severity():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "war broke out", "topic": "geo", "severity": "high"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"


def test_merge_new_claim_defaults_severity_normal():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "minor fact", "topic": "x"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"


def test_merge_new_claim_invalid_severity_defaults_normal():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "fact", "topic": "x", "severity": "spicy"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"


def _high_prior():
    return {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "fact",
                "topic": "x",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 5,
                "severity": "high",
            }
        ],
    }


def test_merge_reaffirm_updates_severity_downward():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x", "severity": "normal"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"  # importance can fade


def test_merge_reaffirm_keeps_severity_when_omitted():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x"}],  # no severity
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"  # omission must not demote


def test_merge_reaffirm_keeps_severity_when_invalid():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x", "severity": "???"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"  # garbage must not demote
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_brief_memory.py -k "merge_new_claim_takes_observed_severity or merge_new_claim_defaults_severity or merge_new_claim_invalid_severity or merge_reaffirm_updates_severity or merge_reaffirm_keeps_severity" -v`
Expected: FAIL with `KeyError: 'severity'`.

- [ ] **Step 3: Update the reaffirm branch**

In `brief_memory.py` `merge_ledger`, inside `if cid and cid in by_id:`, after the existing `base["source_count"] = max(...)` assignment and before `result.append(base)`, add:

```python
            new_sev = _coerce_severity(mc.get("severity"))
            base["severity"] = new_sev or base.get("severity", _DEFAULT_SEVERITY)
```

- [ ] **Step 4: Update the new-claim branch**

In the `elif mc.get("claim"):` branch, add a `severity` key to the appended dict (place it after `"source_count": ...,`):

```python
                    "severity": _coerce_severity(mc.get("severity"))
                    or _DEFAULT_SEVERITY,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_brief_memory.py -k "severity" -v`
Expected: PASS (all Task 1 + Task 2 severity tests).

- [ ] **Step 6: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): store severity on ledger claims (latest-valid-else-keep)

New claims default normal; reaffirm updates both directions but an omitted
or invalid severity never demotes an existing claim. Not peak-max.
EOF
```

---

### Task 3: Severity-aware retention — effective-age TTL filter + cap sort

**Files:**
- Modify: `brief_memory.py` — `merge_ledger`, the retire-filter list comprehension and the `result.sort(...)` call
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `_ttl_bonus`, `_severity_rank` (Task 1); `_days_between` (existing).
- Produces: TTLs of 7d/7d/14d; cap evicts by `(severity_rank, last_reaffirmed)` descending.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_high_severity_survives_to_14_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "war",
                "topic": "geo",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-10",  # exactly 14 days before 06-24
                "restate_count": 1,
                "severity": "high",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_high_severity_retires_after_14_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "war",
                "topic": "geo",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-09",  # 15 days before 06-24
                "restate_count": 1,
                "severity": "high",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_normal_severity_still_retires_at_7_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "minor",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-16",  # 8 days before 06-24
                "restate_count": 1,
                "severity": "normal",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_missing_severity_treated_as_normal_for_retention():
    # legacy claim with no severity field retires at the normal 7-day TTL
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "legacy",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-16",  # 8 days -> past normal TTL
                "restate_count": 1,
            }  # no severity field
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_cap_keeps_high_severity_over_fresher_normal():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "old major war",
                "topic": "geo",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-20",  # older
                "restate_count": 1,
                "severity": "high",
            },
            {
                "id": "c-0002",
                "claim": "fresh trivia",
                "topic": "x",
                "first_seen": "2026-06-24",
                "last_reaffirmed": "2026-06-24",  # fresher
                "restate_count": 1,
                "severity": "normal",
            },
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24", cap=1)
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_cap_orders_by_severity_then_recency():
    def mk(cid, day, sev):
        return {
            "id": cid,
            "claim": cid,
            "topic": "x",
            "first_seen": day,
            "last_reaffirmed": day,
            "restate_count": 1,
            "severity": sev,
        }

    prior = {
        "version": 1,
        "claims": [
            mk("c-low", "2026-06-24", "low"),  # freshest but lowest rank
            mk("c-normA", "2026-06-22", "normal"),
            mk("c-normB", "2026-06-23", "normal"),
            mk("c-high", "2026-06-20", "high"),  # oldest but highest rank
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24", retire_after_days=999)
    assert [c["id"] for c in out["claims"]] == [
        "c-high",
        "c-normB",
        "c-normA",
        "c-low",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_brief_memory.py -k "high_severity or normal_severity_still_retires or missing_severity_treated or cap_keeps_high or cap_orders_by_severity" -v`
Expected: FAIL — high claims get retired at 7d (current flat TTL) and the cap sorts by recency only, so `c-0001`/`c-high` are dropped or mis-ordered.

- [ ] **Step 3: Replace the TTL filter with an effective-age filter**

In `brief_memory.py` `merge_ledger`, replace this block:

```python
    result = [
        c
        for c in result
        if _days_between(c["last_reaffirmed"], today) <= retire_after_days
    ]
    result.sort(key=lambda c: c["last_reaffirmed"], reverse=True)
    return {"version": 1, "claims": result[:cap]}
```

with:

```python
    result = [
        c
        for c in result
        if _days_between(c["last_reaffirmed"], today) - _ttl_bonus(c.get("severity"))
        <= retire_after_days
    ]
    # Cap eviction honors severity first (high > normal > low), then recency.
    result.sort(
        key=lambda c: (_severity_rank(c.get("severity")), c["last_reaffirmed"]),
        reverse=True,
    )
    return {"version": 1, "claims": result[:cap]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_brief_memory.py -k "high_severity or normal_severity_still_retires or missing_severity_treated or cap_keeps_high or cap_orders_by_severity" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole file to confirm no regression**

Run: `python -m pytest tests/test_brief_memory.py -v`
Expected: PASS — in particular `test_merge_retires_stale_claims`, `test_merge_caps_to_most_recent` (all-normal rank → recency tiebreak preserves prior behavior), and `test_save_then_load_roundtrip` still pass.

- [ ] **Step 6: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): severity-weighted ledger retention (TTL + cap)

High claims get a 7-day TTL bonus (effective age) and outrank normal/low
under the 25-cap; recency breaks ties within a tier. Legacy no-severity
claims behave as normal. Replaces the flat 7-day filter and recency sort.
EOF
```

---

### Task 4: Parse severity out of the reconcile response

**Files:**
- Modify: `brief_memory.py` — `parse_reconcile_response`
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `_coerce_severity` (Task 1).
- Produces: parsed entries include a validated `"severity"` key when present and valid; omitted otherwise (merge then defaults to normal). Mirrors the existing `source_count` handling.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_parse_extracts_severity():
    text = '[{"claim":"x","topic":"a","severity":"high"}]'
    assert bm.parse_reconcile_response(text) == [
        {"claim": "x", "topic": "a", "severity": "high"}
    ]


def test_parse_tolerates_bad_severity():
    text = (
        '[{"claim":"a"},'
        '{"claim":"b","severity":"medium"},'
        '{"claim":"c","severity":5},'
        '{"claim":"d","severity":"HIGH"}]'
    )
    out = bm.parse_reconcile_response(text)
    assert out[0] == {"claim": "a"}  # absent -> omitted
    assert out[1] == {"claim": "b"}  # invalid enum -> omitted
    assert out[2] == {"claim": "c"}  # non-string -> omitted
    assert out[3] == {"claim": "d", "severity": "high"}  # case-normalized
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_brief_memory.py -k "parse_extracts_severity or parse_tolerates_bad_severity" -v`
Expected: FAIL — `severity` is dropped by the current parser (test_parse_extracts_severity sees `{"claim":"x","topic":"a"}`).

- [ ] **Step 3: Extract severity in `parse_reconcile_response`**

In `brief_memory.py` `parse_reconcile_response`, inside the `for item in data:` loop, after the existing `source_count` block and before `out.append(entry)`, add:

```python
            sev = _coerce_severity(item.get("severity"))
            if sev is not None:
                entry["severity"] = sev
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_brief_memory.py -k "parse_extracts_severity or parse_tolerates_bad_severity" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): parse severity from reconcile response

Validated low/normal/high passes through; invalid/missing omitted so merge
defaults it to normal. Mirrors source_count coercion.
EOF
```

---

### Task 5: Teach the reconcile prompt to assign severity

**Files:**
- Modify: `brief_memory.py` — `_RECONCILE_TEMPLATE` (add a rule bullet + extend the per-item JSON schema line)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: the reconcile prompt instructs the model to emit `"severity"`. End-to-end severity now flows model → parse → merge → retention.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brief_memory.py`:

```python
def test_reconcile_prompt_teaches_severity():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief")
    assert "severity" in p
    assert '"high"' in p
    assert "when unsure" in p.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py::test_reconcile_prompt_teaches_severity -v`
Expected: FAIL — `severity` not yet in the template.

- [ ] **Step 3: Add the severity rule to `_RECONCILE_TEMPLATE`**

In `brief_memory.py`, in `_RECONCILE_TEMPLATE`, insert this bullet immediately after the existing `source_count` rule (the bullet ending `...when you are unsure.`) and before the `- Return at most {max_claims}...` bullet:

```
- For each fact, set "severity" to one of "low", "normal", or "high". "high" =
  a major standing development the reader must not have re-explained (wars,
  leadership or regime changes, major policy-regime shifts, market-structural
  events); "normal" = a typical durable fact (use this by default); "low" = a
  true but minor, low-stakes detail. When unsure, use "normal".
```

- [ ] **Step 4: Extend the per-item JSON schema line**

In the same template, replace this line:

```
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>", "source_count": <integer>}}.
```

with:

```
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>", "source_count": <integer>, "severity": "<low|normal|high>"}}.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py::test_reconcile_prompt_teaches_severity -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): teach reconcile prompt to assign claim severity

Crisp low/normal/high criteria + schema field. Completes the
severity-weighted retention path end-to-end.
EOF
```

---

### Task 6: Full-suite + lint verification

**Files:** none (verification only).

- [ ] **Step 1: Run ruff lint**

Run (PowerShell tool): `ruff check .`
Expected: no errors. If any, fix and re-run.

- [ ] **Step 2: Run ruff format check**

Run: `ruff format --check .`
Expected: "N files already formatted". If it reports files to reformat, run `ruff format .`, then `git add` the reformatted files (the formatter owns style — do not hand-fix).

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest -q`
Expected: all tests pass (prior count was 443 for the perspective-matrix work; this adds ~20 new tests, all green).

- [ ] **Step 4: Commit any formatting fixups (only if Step 2 reformatted anything)**

```bash
git add -A
git commit -F - <<'EOF'
style(brief): ruff format fixups for severity-weighted retention
EOF
```

---

## Notes for the implementer

- This is a descriptive, fail-safe feature behind the already-live `BRIEF_MEMORY_ENABLED` flag — there is no new flag and nothing to enable. It activates on the next `mode_collect` reconcile after deploy; existing claims acquire `severity` as they are reaffirmed.
- Do NOT modify `render_established_block` — severity is intentionally invisible to the reader.
- Pushing this (= a Docker deploy) is the user's call, separate from implementation. Per repo memory, two other features (#5a why-it-matters lens, #4 source mining) are already built-but-unpushed and batched for one deploy; confirm with the user whether this rides the same batch.

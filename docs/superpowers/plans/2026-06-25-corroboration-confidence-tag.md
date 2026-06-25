# Corroboration Confidence Tag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each durable claim in the standing-claim ledger a peak corroboration count (distinct outlets that carried it) and render it back into the brief as a coarse confidence cue (`widely corroborated` / `corroborated` / `single-source`).

**Architecture:** A titles-only, source-labelled headline index is built and persisted at `mode_submit` time (when the source-labelled feeds exist) to `source_index-{today}.json`. At `mode_collect`/reconcile (hours later, in a different process), that index is loaded and passed into the existing Haiku reconcile call, which tags each durable claim with a best-effort `source_count`. `merge_ledger` keeps the peak; `render_established_block` derives and shows the bucket. Descriptive only, fail-safe, gated by the existing `BRIEF_MEMORY_ENABLED`.

**Tech Stack:** Python 3.14, `requests` (Anthropic Messages API), `pytest`, `ruff`. No new dependencies.

## Global Constraints

- **Feature flag:** rides under the existing `BRIEF_MEMORY_ENABLED` env var (no new flag). Index is built/persisted only when enabled.
- **Descriptive only:** never influences trading/sizing. No retention/TTL weighting — corroboration must NOT change which claims survive the cap or the 7-day retirement.
- **Fail-safe:** any error in index build/persist/load leaves the brief and the prior ledger intact. Missing index file ⇒ reconcile behaves exactly as today (claims get no `source_count`, render omits the cue).
- **Thresholds (verbatim):** `source_count == 1` → `single-source`; `2–3` → `corroborated`; `≥ 4` → `widely corroborated`; `0`/missing → no cue.
- **Peak rule:** `merge_ledger` stores `source_count = max(prior, observed)`; carried-over (unreturned) claims keep their prior value unchanged; the value never decays.
- **Coarse only:** the integer is best-effort; the reader only ever sees the derived bucket (bucket is NOT stored).
- **Tooling discipline (this machine):** run Python/pytest/ruff via the **PowerShell** tool; make git commits via the **Bash** tool (PowerShell prepends a UTF-8 BOM to commit subjects). Pre-push gate is all three: `ruff check .` + `ruff format --check .` + `pytest`. Stage every file ruff reformats or CI fails.

---

### Task 1: `source_count` peak in `merge_ledger` + lenient parse

**Files:**
- Modify: `brief_memory.py` (`merge_ledger`, `parse_reconcile_response`; add `_coerce_source_count`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: existing `merge_ledger(prior, model_claims, today, *, cap, retire_after_days)`, `parse_reconcile_response(text) -> list[dict]`.
- Produces: claims may carry `source_count: int` (peak); `parse_reconcile_response` extracts `source_count` when present and valid (coerced to a non-negative int), otherwise omits it. New helper `_coerce_source_count(v) -> int | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_merge_keeps_peak_source_count_on_reaffirm():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "BOJ at 1.0%",
                "topic": "japan",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 5,
                "source_count": 6,
            }
        ],
    }
    # observed today is LOWER (story aged out) -> peak must hold at 6
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "BOJ at 1.0%", "topic": "japan", "source_count": 2}],
        "2026-06-24",
    )
    assert out["claims"][0]["source_count"] == 6
    # observed today is HIGHER -> peak rises
    out2 = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "BOJ at 1.0%", "topic": "japan", "source_count": 9}],
        "2026-06-24",
    )
    assert out2["claims"][0]["source_count"] == 9


def test_merge_new_claim_takes_observed_source_count():
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(
        prior, [{"claim": "new fact", "topic": "x", "source_count": 4}], "2026-06-24"
    )
    assert out["claims"][0]["source_count"] == 4


def test_merge_missing_source_count_defaults_zero():
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(prior, [{"claim": "no count", "topic": "x"}], "2026-06-24")
    assert out["claims"][0]["source_count"] == 0


def test_merge_unreturned_claim_preserves_source_count():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "kept",
                "topic": "a",
                "first_seen": "2026-06-22",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 2,
                "source_count": 5,
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"][0]["source_count"] == 5


def test_parse_extracts_source_count():
    text = '[{"claim":"x","topic":"a","source_count":3}]'
    assert bm.parse_reconcile_response(text) == [
        {"claim": "x", "topic": "a", "source_count": 3}
    ]


def test_parse_tolerates_bad_source_count():
    # missing, non-numeric string, bool, and negative are all dropped/clamped
    text = (
        '[{"claim":"a"},'
        '{"claim":"b","source_count":"two"},'
        '{"claim":"c","source_count":true},'
        '{"claim":"d","source_count":"5"},'
        '{"claim":"e","source_count":-2}]'
    )
    out = bm.parse_reconcile_response(text)
    assert out[0] == {"claim": "a"}                       # absent -> omitted
    assert out[1] == {"claim": "b"}                       # "two" -> omitted
    assert out[2] == {"claim": "c"}                       # bool -> omitted
    assert out[3] == {"claim": "d", "source_count": 5}    # numeric string -> int
    assert out[4] == {"claim": "e", "source_count": 0}    # negative -> clamped
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell tool): `pytest tests/test_brief_memory.py -k "source_count" -v`
Expected: FAIL (`KeyError`/assert mismatches — `source_count` not yet handled).

- [ ] **Step 3: Implement the minimal code**

In `brief_memory.py`, add the helper above `parse_reconcile_response`:

```python
def _coerce_source_count(v) -> int | None:
    """Best-effort non-negative int, or None to omit. bool is rejected (it is an
    int subclass but never a real count)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, str) and v.strip().lstrip("+").isdigit():
        return max(0, int(v.strip()))
    return None
```

In `parse_reconcile_response`, replace the append loop body:

```python
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("claim"):
            entry = {k: item[k] for k in ("id", "claim", "topic") if k in item}
            sc = _coerce_source_count(item.get("source_count"))
            if sc is not None:
                entry["source_count"] = sc
            out.append(entry)
    return out
```

In `merge_ledger`, the reaffirmed branch (`if cid and cid in by_id:`) — after `base["restate_count"] = ...`, add:

```python
            base["source_count"] = max(
                base.get("source_count", 0) or 0,
                mc.get("source_count", 0) or 0,
            )
```

In `merge_ledger`, the new-claim branch (`elif mc.get("claim"):`) — add to the dict literal (e.g. after `"restate_count": 1,`):

```python
                    "source_count": mc.get("source_count", 0) or 0,
```

(Carried-over prior claims are copied via `dict(c)`, so their `source_count` is preserved automatically — no change needed there.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brief_memory.py -k "source_count or merge or parse" -v`
Expected: PASS (new tests + all existing merge/parse tests still green).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -m "feat(brief-memory): carry peak source_count through merge and parse"
```

---

### Task 2: Corroboration cue rendering

**Files:**
- Modify: `brief_memory.py` (`render_established_block`; add `_corroboration_cue`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: claim dicts that may carry `source_count: int`.
- Produces: `_corroboration_cue(n) -> str` ("" | "single-source" | "corroborated" | "widely corroborated"). `render_established_block` prefixes the cue in parentheses per claim and its instruction paragraph references corroboration.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
import pytest


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, ""),
        (1, "single-source"),
        (2, "corroborated"),
        (3, "corroborated"),
        (4, "widely corroborated"),
        (12, "widely corroborated"),
        (None, ""),
        ("bad", ""),
    ],
)
def test_corroboration_cue_buckets(n, expected):
    assert bm._corroboration_cue(n) == expected


def test_render_includes_corroboration_cue():
    ledger = {
        "version": 1,
        "claims": [
            {"id": "c-1", "claim": "Widely known fact", "topic": "macro",
             "first_seen": "2026-06-20", "last_reaffirmed": "2026-06-24",
             "restate_count": 3, "source_count": 7},
            {"id": "c-2", "claim": "Thin rumor", "topic": "tech",
             "first_seen": "2026-06-24", "last_reaffirmed": "2026-06-24",
             "restate_count": 1, "source_count": 1},
        ],
    }
    block = bm.render_established_block(ledger)
    assert "(widely corroborated) Widely known fact" in block
    assert "(single-source) Thin rumor" in block
    # instruction teaches the model how to use the cue
    assert "single-source" in block.lower()


def test_render_omits_cue_when_no_source_count():
    ledger = {
        "version": 1,
        "claims": [
            {"id": "c-1", "claim": "Legacy claim", "topic": "macro",
             "first_seen": "2026-06-20", "last_reaffirmed": "2026-06-24",
             "restate_count": 3},  # no source_count
        ],
    }
    block = bm.render_established_block(ledger)
    assert "Legacy claim" in block
    assert "corroborated" not in block.split("Legacy claim")[0].split("\n")[-1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brief_memory.py -k "corroboration or render" -v`
Expected: FAIL (`AttributeError: _corroboration_cue` / missing cue in output).

- [ ] **Step 3: Implement the minimal code**

In `brief_memory.py`, add above `render_established_block`:

```python
def _corroboration_cue(source_count) -> str:
    """Coarse, reader-facing confidence cue derived from the peak source_count.
    Thresholds: 1 single-source / 2-3 corroborated / >=4 widely corroborated."""
    try:
        n = int(source_count)
    except (TypeError, ValueError):
        return ""
    if n >= 4:
        return "widely corroborated"
    if n >= 2:
        return "corroborated"
    if n == 1:
        return "single-source"
    return ""
```

Replace the body of `render_established_block`:

```python
def render_established_block(ledger: dict) -> str:
    claims = ledger.get("claims", [])
    if not claims:
        return ""
    rows = []
    for c in claims:
        cue = _corroboration_cue(c.get("source_count"))
        tag = f" ({cue})" if cue else ""
        rows.append(f"  • [{c.get('topic') or 'general'}]{tag} {c['claim']}")
    lines = "\n".join(rows)
    return (
        "## ESTABLISHED — THE READER ALREADY KNOWS THESE\n"
        "Reference each in at most one clause, and only if still relevant. Do NOT "
        "re-explain or restate them as news. Lead every section with what has "
        "CHANGED since. The parenthetical is how broadly the fact was corroborated "
        "across outlets: lean on 'widely corroborated' facts with confidence and "
        "treat 'single-source' ones more tentatively.\n\n" + lines + "\n"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brief_memory.py -k "corroboration or render" -v`
Expected: PASS (including existing `test_render_*`).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -m "feat(brief-memory): render corroboration confidence cue in established block"
```

---

### Task 3: Thread the source index into the reconcile call

**Files:**
- Modify: `brief_memory.py` (`_RECONCILE_TEMPLATE`, `build_reconcile_prompt`, `reconcile_ledger`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `merge_ledger`/`parse_reconcile_response` from Task 1, a plain-text `source_index` string (built in Task 4).
- Produces: `build_reconcile_prompt(ledger, brief_text, source_index="")`; `reconcile_ledger(prior, brief_text, today, *, call=None, source_index="")`. Reconcile output schema now includes `source_count` per claim.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_build_reconcile_prompt_includes_source_index():
    p = bm.build_reconcile_prompt(
        {"version": 1, "claims": []},
        "brief text",
        "SOURCE: Reuters\n- OPEC extends cut\nSOURCE: AP\n- OPEC extends cut",
    )
    assert "SOURCE: Reuters" in p
    assert "source_count" in p


def test_build_reconcile_prompt_placeholder_when_no_index():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief text")
    assert "no source index" in p.lower()


def test_reconcile_passes_index_and_records_count(monkeypatch):
    captured = {}

    def fake_call(system, user):
        captured["user"] = user
        return '[{"claim":"OPEC extends cut","topic":"oil","source_count":2}]'

    out = bm.reconcile_ledger(
        {"version": 1, "claims": []},
        "OPEC extended cuts.",
        "2026-06-24",
        call=fake_call,
        source_index="SOURCE: Reuters\n- OPEC extends cut",
    )
    assert "SOURCE: Reuters" in captured["user"]
    assert out["claims"][0]["source_count"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brief_memory.py -k "reconcile or prompt" -v`
Expected: FAIL (`build_reconcile_prompt` takes 2 args / `reconcile_ledger` has no `source_index` kwarg / index not in prompt).

- [ ] **Step 3: Implement the minimal code**

In `brief_memory.py`, replace `_RECONCILE_TEMPLATE`:

```python
_RECONCILE_TEMPLATE = """Below is the CURRENT memory (JSON), TODAY'S BRIEF, and
TODAY'S SOURCE HEADLINES (the outlets that ran each story today, grouped by SOURCE).

Return ONLY a JSON array of the durable facts the reader now knows after today's
brief. Rules:
- A durable fact is something that should NOT be re-explained tomorrow unless it
  materially changes: one-time events already reported (e.g. a rate hike), and
  standing analytical frames/theses. NOT ephemeral daily price moves.
- For a fact already in CURRENT memory that is still relevant, include it and
  ECHO its existing "id". You may reword its "claim" if today refined it.
- For a genuinely NEW durable fact, include it with NO "id".
- Omit facts that are no longer relevant.
- For each fact, set "source_count" to the number of DISTINCT outlets in TODAY'S
  SOURCE HEADLINES (the "SOURCE:" blocks) whose headline supports that fact. Count
  outlets, not headlines. Use 0 when the fact is not covered in today's headlines,
  when no source headlines are provided, or when you are unsure.
- Return at most {max_claims} items — keep only the most important durable facts,
  and keep each "claim" to one terse sentence (no more than ~30 words).
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>", "source_count": <integer>}}.
Output the JSON array and nothing else.

CURRENT memory:
{current}

TODAY'S BRIEF:
{brief}

TODAY'S SOURCE HEADLINES:
{source_index}
"""
```

Replace `build_reconcile_prompt`:

```python
def build_reconcile_prompt(ledger: dict, brief_text: str, source_index: str = "") -> str:
    current = json.dumps(ledger.get("claims", []), indent=2)
    return _RECONCILE_TEMPLATE.format(
        current=current,
        brief=brief_text,
        max_claims=MAX_CLAIMS,
        source_index=source_index.strip() or "(no source index available)",
    )
```

Replace `reconcile_ledger`:

```python
def reconcile_ledger(
    prior: dict, brief_text: str, today: str, *, call=None, source_index: str = ""
) -> dict:
    caller = call or _messages_call
    try:
        text = caller(
            _RECONCILE_SYSTEM,
            build_reconcile_prompt(prior, brief_text, source_index),
        )
        return merge_ledger(prior, parse_reconcile_response(text), today)
    except Exception as e:
        log.warning(f"Brief-memory reconcile failed; keeping prior ledger: {e}")
        return prior
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brief_memory.py -v`
Expected: PASS (new tests + existing `test_build_reconcile_prompt_contains_ledger_and_brief`, `test_reconcile_prompt_bounds_output_size`, `test_reconcile_success_merges` all still green via the `source_index=""` default).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -m "feat(brief-memory): feed source-headline index into reconcile for source_count"
```

---

### Task 4: `build_source_index` + persist/load helpers

**Files:**
- Modify: `brief.py` (add `build_source_index`, `save_source_index`, `load_source_index`, `_source_index_path`)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: the `feed_content` / `web_content` text blobs built in `mode_submit` (each source is a `### NAME [...] (CATEGORY)` header followed by `- title (pubdate)` items).
- Produces: `build_source_index(feed_content: str, web_content: str) -> str` (lines: `SOURCE: <name>` and `- <title>`); `save_source_index(index: str, day: str) -> None` → `DATA_DIR / f"source_index-{day}.json"` (`{"date", "index"}`); `load_source_index(day: str) -> str` ("" if missing/unreadable).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brief_memory.py`:

```python
def test_build_source_index_extracts_sources_and_titles():
    import brief

    feed = (
        "\n### Reuters [WIRE] (WORLD)\n"
        "- OPEC+ extends production cut (Mon, 24 Jun)\n"
        "  long summary body that must be dropped\n"
        "- Fed holds rates (Mon)\n"
        "\n### Al Jazeera [REGIONAL · ARAB · STATE-FUNDED] (MIDEAST)\n"
        "- OPEC+ extends production cut (Tue)\n"
    )
    idx = brief.build_source_index(feed, "")
    assert "SOURCE: Reuters" in idx
    assert "SOURCE: Al Jazeera" in idx
    assert "- OPEC+ extends production cut" in idx
    assert "- Fed holds rates" in idx
    # summaries and pub dates are stripped
    assert "long summary body" not in idx
    assert "(Mon, 24 Jun)" not in idx


def test_build_source_index_handles_empty():
    import brief

    assert brief.build_source_index("(no RSS content)", "(no web content)") == ""


def test_source_index_save_load_roundtrip(tmp_path, monkeypatch):
    import brief

    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    brief.save_source_index("SOURCE: Reuters\n- Big news", "2026-06-25")
    assert "SOURCE: Reuters" in brief.load_source_index("2026-06-25")


def test_load_source_index_missing_returns_empty(tmp_path, monkeypatch):
    import brief

    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    assert brief.load_source_index("2099-01-01") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_brief_memory.py -k "source_index or build_source" -v`
Expected: FAIL (`AttributeError: module 'brief' has no attribute 'build_source_index'`).

- [ ] **Step 3: Implement the minimal code**

In `brief.py`, add near the other source helpers (after `fetch_web_source`, around line 1361). `re`, `Path`, `DATA_DIR`, `_write_json_atomic`, `json`, `log` are already imported in this module.

```python
def build_source_index(feed_content: str, web_content: str) -> str:
    """Compact, titles-only, source-labelled index of today's headlines so the
    brief-memory reconcile call can count how many distinct outlets carried each
    durable claim. Parsed from the already-built feed/web blobs: each source is a
    '### NAME [...] (CATEGORY)' header followed by '- title (pubdate)' items;
    indented summary lines and web-source body text are dropped."""
    out: list[str] = []
    for raw in f"{feed_content}\n{web_content}".splitlines():
        line = raw.strip()
        if line.startswith("### "):
            name = line[4:].split(" [")[0].split(" (")[0].strip()
            if name:
                out.append(f"SOURCE: {name}")
        elif line.startswith("- "):
            title = re.sub(r"\s*\([^()]*\)\s*$", "", line[2:].strip()).strip()
            if title:
                out.append(f"- {title}")
    return "\n".join(out)


def _source_index_path(day: str) -> Path:
    return DATA_DIR / f"source_index-{day}.json"


def save_source_index(index: str, day: str) -> None:
    _write_json_atomic(_source_index_path(day), {"date": day, "index": index})


def load_source_index(day: str) -> str:
    try:
        p = _source_index_path(day)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("index", ""))
    except Exception as e:
        log.warning(f"Source index unreadable for {day}; reconciling without it: {e}")
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_brief_memory.py -k "source_index or build_source" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_brief_memory.py
git commit -m "feat(brief): build and persist titles-only source-headline index"
```

---

### Task 5: Wire persist-at-submit and load-at-collect

**Files:**
- Modify: `brief.py` (`mode_submit` after `web_content` is built ~line 2252; `mode_collect` reconcile call ~line 2354)

**Interfaces:**
- Consumes: `build_source_index`, `save_source_index`, `load_source_index` (Task 4); `reconcile_ledger(..., source_index=...)` (Task 3); existing `brief_memory_enabled()`.
- Produces: end-to-end behaviour — index persisted at submit (flag-gated), loaded and used at collect.

- [ ] **Step 1: Add the submit-side persist**

In `brief.py` `mode_submit`, immediately after the `web_content = (...)` assignment (~line 2252) and before `fb = load_feedback()`:

```python
    if brief_memory_enabled():
        try:
            save_source_index(build_source_index(feed_content, web_content), today)
        except Exception as e:
            log.warning(f"Source index persist skipped (brief unaffected): {e}")
```

- [ ] **Step 2: Update the collect-side reconcile call**

In `brief.py` `mode_collect`, replace the reconcile line (~2354):

```python
                save_ledger(
                    reconcile_ledger(
                        load_ledger(), brief, today, source_index=load_source_index(today)
                    )
                )
```

- [ ] **Step 3: Run the full pre-push gate**

Run (PowerShell tool):

```
ruff check .
ruff format --check .
pytest -q
```

Expected: ruff clean (stage any reformatted files), all tests pass. If `ruff format` rewrites files, `git add` them before committing.

- [ ] **Step 4: Manual fail-safe sanity check (no network)**

Run (PowerShell tool) to confirm the collect path degrades cleanly when no index exists for a day:

```
python -c "import brief; print(repr(brief.load_source_index('2099-01-01')))"
```

Expected output: `''` (empty string — reconcile would proceed with the `(no source index available)` placeholder, i.e. today's behaviour).

- [ ] **Step 5: Commit**

```bash
git add brief.py
git commit -m "feat(brief): wire source index into submit/collect reconcile"
```

---

## Self-Review

**Spec coverage:**
- Data model `source_count` (peak, int) → Task 1. ✓
- Bucket derived at render time, thresholds 1/2-3/≥4 → Task 2 + Global Constraints. ✓
- Submit builds & persists titles-only source index → Task 4 + Task 5 Step 1. ✓
- Collect/reconcile joins breadth via existing Haiku call, new `source_count` field → Task 3 + Task 5 Step 2. ✓
- Render cue + instruction → Task 2. ✓
- Fail-safe / back-compat (missing index, old claims, lenient parse, no new flag) → Task 1 (lenient parse), Task 3 (placeholder default), Task 4 (load returns ""), Task 5 (flag-gated persist, try/except). ✓
- Non-goal: no retention weighting → no task touches `cap`/`retire_after_days` logic; Global Constraints states it. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows full code. ✓

**Type consistency:** `source_count: int` everywhere; `_coerce_source_count -> int | None`; `_corroboration_cue(source_count) -> str`; `build_source_index(feed_content, web_content) -> str`; `save_source_index(index, day)`; `load_source_index(day) -> str`; `reconcile_ledger(..., source_index="")` / `build_reconcile_prompt(ledger, brief_text, source_index="")` consistent across Tasks 1–5. ✓

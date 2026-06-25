# Stage A — Trading Measurement & Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paper-trading side self-aware — attribute realized P&L to source `kind`+`perspective`, show a sample-aware confidence-calibration view, and count declined-signal leakage — all human-facing, with zero autonomy.

**Architecture:** Resolve a model-chosen `source_id` (picked from the day's tagged source list during the post-delivery signals extraction) into `kind`/`perspective` at signal-save time in `brief.py` (the resolver is the single source of truth; `trading.py` cannot import `brief.py`, so it only *copies* pre-resolved tags). `trading.py` stamps those tags onto opened positions and records a per-run leakage tally. `validation.py` slices the new dimensions, renders a calibration block with an inversion flag, and sums leakage over a 7-day window. The daily-prompt feedback block (`performance_prompt_block`) is deliberately left untouched (Stage-B firewall).

**Tech Stack:** Python (stdlib + `requests`); pytest; ruff. No pandas (CI has none). JSON files on the deploy volume via `common._write_json_atomic` / `_load_json_or` / `file_lock`.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-25-stage-a-trading-measurement-design.md`. **Roadmap:** `docs/superpowers/specs/2026-06-25-self-improving-trading-roadmap.md`.
- **Zero autonomy / descriptive-only:** nothing here may change trade selection, sizing, or live-trading enablement. Measurement and presentation only.
- **Firewall:** do NOT modify `performance_prompt_block` or its hardcoded dimension tuple `("asset_class", "confidence", "thesis_ref")`. New dimensions stay human-facing until Stage B.
- **Fail-safe everywhere:** a missing/garbled `source_id` resolves to `kind="unknown"`, `perspective=None`; any file-write for leakage is best-effort (log + skip, never abort a run). Never raise out of the collect/paper path.
- **Scope:** source attribution + leakage cover equity/crypto (direct signal→position) only. Prediction positions carry `source_kind="unknown"`, `source_perspective=None` and are excluded.
- **Run Python via the PowerShell tool** (the Bash tool errors `stdin is not a tty`); PowerShell wraps Python stderr/logging as a scary `NativeCommandError` even on success — not a failure.
- **Make git commits via the Bash tool, not PowerShell** (PowerShell prepends a UTF-8 BOM to the commit subject). Use `git commit -F -` with a single-quoted heredoc when a message has backticks/`$`.
- **Pre-push gate:** `ruff check . && ruff format --check . && pytest -q`. ruff reflows on save — stage all reformatted files or CI fails. No new top-level module is added, so no Dockerfile/workflow path changes are needed.

---

### Task 1: Source-tag resolver + registry index (brief.py)

The single place `kind`/`perspective` are derived from a source name. Pure, never raises.

**Files:**
- Modify: `brief.py` (add `all_sources`, `_source_tag_index`, `resolve_source_tags` near `load_temp_sources`, ~line 314)
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `RSS_FEEDS` (list of dicts with `name`/`kind`/optional `perspective`), `load_temp_sources() -> list[dict]`.
- Produces:
  - `all_sources() -> list[dict]` — `RSS_FEEDS + load_temp_sources()`.
  - `_source_tag_index() -> dict[str, dict]` — `{name: {"kind": str, "perspective": str | None}}`; hardcoded `RSS_FEEDS` wins on name collision.
  - `resolve_source_tags(source_id: str | None, index: dict) -> dict` — `{"kind": str, "perspective": str | None}`; unknown → `{"kind": "unknown", "perspective": None}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_signals.py`, append:

```python
# ── source-tag resolver ───────────────────────────────────────────────────────
def test_source_tag_index_includes_hardcoded_feeds():
    index = brief._source_tag_index()
    # every RSS feed name resolves to its own kind
    for f in brief.RSS_FEEDS:
        assert index[f["name"]]["kind"] == f.get("kind", "wire")


def test_resolve_known_source_returns_kind_and_perspective():
    index = {"Al Jazeera": {"kind": "regional", "perspective": "ARAB"}}
    tags = brief.resolve_source_tags("Al Jazeera", index)
    assert tags == {"kind": "regional", "perspective": "ARAB"}


def test_resolve_unknown_source_is_unknown():
    assert brief.resolve_source_tags("Nonesuch", {}) == {
        "kind": "unknown",
        "perspective": None,
    }


def test_resolve_none_source_is_unknown():
    assert brief.resolve_source_tags(None, {"X": {"kind": "wire"}}) == {
        "kind": "unknown",
        "perspective": None,
    }


def test_hardcoded_feed_wins_over_temp_on_name_collision(monkeypatch):
    monkeypatch.setattr(
        brief,
        "load_temp_sources",
        lambda: [{"name": brief.RSS_FEEDS[0]["name"], "kind": "regional"}],
    )
    index = brief._source_tag_index()
    assert index[brief.RSS_FEEDS[0]["name"]]["kind"] == brief.RSS_FEEDS[0].get(
        "kind", "wire"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run (PowerShell tool): `python -m pytest tests/test_signals.py -k "source_tag or resolve_ or collision" -v`
Expected: FAIL with `AttributeError: module 'brief' has no attribute '_source_tag_index'`.

- [ ] **Step 3: Implement the resolver**

In `brief.py`, immediately after `load_temp_sources` (ends ~line 349), add:

```python
def all_sources() -> list[dict]:
    """The full source universe: always-on RSS_FEEDS plus volume temp sources."""
    return RSS_FEEDS + load_temp_sources()


def _source_tag_index() -> dict[str, dict]:
    """Index every source by name -> {"kind", "perspective"}.

    Temp sources are added first; the hardcoded RSS_FEEDS overwrite on a name
    collision so the baked-in registry is authoritative. The single place
    kind/perspective are looked up, so callers never invent tag values.
    """
    index: dict[str, dict] = {}
    for src in load_temp_sources():
        index[src["name"]] = {
            "kind": src.get("kind", "regional"),
            "perspective": src.get("perspective"),
        }
    for f in RSS_FEEDS:
        index[f["name"]] = {
            "kind": f.get("kind", "wire"),
            "perspective": f.get("perspective"),
        }
    return index


def resolve_source_tags(source_id: str | None, index: dict) -> dict:
    """Resolve a source name to {"kind", "perspective"}; unknown -> ("unknown", None).

    Pure, never raises. `index` comes from _source_tag_index(). A None/empty/
    unrecognised source_id yields the unknown bucket so attribution always has a
    well-defined value.
    """
    if source_id and source_id in index:
        tags = index[source_id]
        return {"kind": tags.get("kind", "unknown"), "perspective": tags.get("perspective")}
    return {"kind": "unknown", "perspective": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_signals.py -k "source_tag or resolve_ or collision" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(trading): source-tag resolver + registry index (Stage A unit 1)

Single place kind/perspective are derived from a source name; hardcoded
RSS_FEEDS win over temp sources; unknown/None -> unknown bucket. Pure.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 2: Extraction emits `source_id` (brief.py)

Give the latency-free signals extractor the day's source list and a schema slot to name the source it cited; keep it through normalization.

**Files:**
- Modify: `brief.py` — `_EMIT_SIGNALS_TOOL` (~1886), `_SIGNALS_USER_TEMPLATE` (~1961), `build_signals_request` (~1973), `extract_signals` (~2031), `normalize_signals` (~1859 clean-dict).
- Test: `tests/test_signals.py` (incl. updating `test_normalize_strips_unknown_fields`).

**Interfaces:**
- Consumes: `all_sources()` (Task 1).
- Produces:
  - `build_signals_request(brief_text: str, sources: list[dict] | None = None) -> dict`
  - `extract_signals(brief_text: str, sources: list[dict] | None = None, *, call=None) -> tuple[list, str]`
  - normalized signal dict gains key `source_id: str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_signals.py`, append and update:

```python
def test_normalize_keeps_source_id():
    s = dict(SIGNAL, source_id="Al Jazeera")
    clean, _ = brief.normalize_signals([s])
    assert clean[0]["source_id"] == "Al Jazeera"


def test_normalize_nulls_missing_source_id():
    clean, _ = brief.normalize_signals([dict(SIGNAL)])  # no source_id key
    assert clean[0]["source_id"] is None


def test_signals_request_lists_sources_for_the_model():
    req = brief.build_signals_request(
        "BRIEF", sources=[{"name": "Kyiv Independent", "kind": "regional"}]
    )
    user_text = req["messages"][0]["content"]
    assert "Kyiv Independent" in user_text
```

Update the existing `test_normalize_strips_unknown_fields` expected set to include `"source_id"`:

```python
def test_normalize_strips_unknown_fields():
    s = dict(SIGNAL, price_target=120, note="extra")
    clean, _ = brief.normalize_signals([s])
    assert set(clean[0]) == {
        "ticker",
        "topic",
        "direction",
        "confidence",
        "thesis_ref",
        "rationale",
        "provenance",
        "asset_class",
        "source_id",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_signals.py -k "source_id or lists_sources or strips_unknown" -v`
Expected: FAIL (`source_id` not in normalized dict; `build_signals_request` rejects `sources=`).

- [ ] **Step 3: Implement**

In `normalize_signals`, add `source_id` to the appended clean dict (after `provenance`, ~line 1867):

```python
                "provenance": str(item.get("provenance", "")).strip(),
                "source_id": _nullish(item.get("source_id")),
```

In `_EMIT_SIGNALS_TOOL`'s `properties` (after the `provenance` property, ~line 1939), add:

```python
                        "source_id": {
                            "type": "string",
                            "description": (
                                "Exact source name, copied verbatim from the "
                                "SOURCES list provided, that the brief cites for "
                                "this signal. Omit if no listed source clearly "
                                "backs it."
                            ),
                        },
```

Replace `_SIGNALS_USER_TEMPLATE` (~1961) with a version that lists the sources:

```python
_SIGNALS_USER_TEMPLATE = """Read the entire brief below and call emit_signals with \
every position-relevant signal it contains.

Include macro-level signals (no single ticker — omit the ticker field) as well as \
named-instrument signals. Set provenance from a source the brief cites, or omit it. \
Set source_id to the exact name (from the SOURCES list) of the source the brief cites \
for the signal, or omit it if none clearly applies. Return an empty list if the brief \
contains no actionable signals.

SOURCES (choose source_id from these exact names):
{sources}

BRIEF:
{brief}
"""
```

Replace `build_signals_request` (~1973):

```python
def build_signals_request(brief_text: str, sources: list[dict] | None = None) -> dict:
    """Build the Anthropic Messages payload for the forced-tool signals call.

    `sources` (defaults to all_sources()) is listed by name so the model can set
    source_id from a closed set; code re-derives kind/perspective from the registry.
    """
    if sources is None:
        sources = all_sources()
    source_names = "\n".join(f"- {s['name']}" for s in sources) or "(none)"
    return {
        "model": SIGNALS_MODEL,
        "max_tokens": 2048,
        "system": _SIGNALS_SYSTEM,
        "tools": [_EMIT_SIGNALS_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_signals"},
        "messages": [
            {
                "role": "user",
                "content": _SIGNALS_USER_TEMPLATE.format(
                    sources=source_names, brief=brief_text
                ),
            }
        ],
    }
```

Update `extract_signals` (~2031) to thread `sources`:

```python
def extract_signals(
    brief_text: str, sources: list[dict] | None = None, *, call=None
) -> tuple[list, str]:
    """Extract position signals from a finished brief via a forced-tool call.

    Returns (raw_signals, status) where status is "ok" on success or
    "extract_error" on any failure (HTTP, missing tool block, malformed input).
    Fail-safe: never raises; the brief is already delivered and unaffected.
    """
    caller = call or _post_messages
    try:
        resp = caller(build_signals_request(brief_text, sources))
        return parse_signals_response(resp), "ok"
    except Exception as e:
        log.warning(f"Signals extraction failed; no signals this run: {e}")
        return [], "extract_error"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_signals.py -v`
Expected: PASS (existing + new; `test_build_signals_request_forces_emit_signals_tool` still passes — one-arg call uses the default sources).

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(trading): extractor names cited source_id from a closed list (Stage A unit 2)

emit_signals gains an optional source_id; the user template lists source
names so the model picks from the registry. normalize_signals keeps it.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 3: Annotate signals with resolved tags + wire into collect/run (brief.py)

Resolve `source_id` to tags at save-time and stamp them on each signal in the snapshot, so `trading.py` only copies.

**Files:**
- Modify: `brief.py` — add `annotate_signal_sources` (near `normalize_signals`); call it in `mode_collect` (~2387) and `mode_run` (~2498), before `save_signals`.
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `_source_tag_index`, `resolve_source_tags` (Task 1); normalized signals with `source_id` (Task 2).
- Produces: `annotate_signal_sources(signals: list[dict]) -> list[dict]` — each signal gains `source_kind: str` and `source_perspective: str | None` (mutates in place and returns the list).

- [ ] **Step 1: Write the failing test**

```python
def test_annotate_signal_sources_sets_kind_and_perspective(monkeypatch):
    monkeypatch.setattr(
        brief,
        "_source_tag_index",
        lambda: {"Al Jazeera": {"kind": "regional", "perspective": "ARAB"}},
    )
    sigs = [
        {"topic": "t", "source_id": "Al Jazeera"},
        {"topic": "u", "source_id": "Nonesuch"},
        {"topic": "v"},  # no source_id
    ]
    out = brief.annotate_signal_sources(sigs)
    assert out[0]["source_kind"] == "regional"
    assert out[0]["source_perspective"] == "ARAB"
    assert out[1]["source_kind"] == "unknown"
    assert out[2]["source_kind"] == "unknown"
    assert out[2]["source_perspective"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_signals.py -k annotate_signal_sources -v`
Expected: FAIL (`annotate_signal_sources` not defined).

- [ ] **Step 3: Implement and wire**

In `brief.py`, after `normalize_signals` (~line 1876), add:

```python
def annotate_signal_sources(signals: list[dict]) -> list[dict]:
    """Stamp resolved source_kind / source_perspective onto each signal.

    Runs at save-time (brief.py owns the registry; trading.py cannot import it),
    so the paper tracker copies pre-resolved tags. Mirrors annotate_signals
    (enrichment). Builds the index once. Mutates and returns `signals`.
    """
    index = _source_tag_index()
    for s in signals:
        tags = resolve_source_tags(s.get("source_id"), index)
        s["source_kind"] = tags["kind"]
        s["source_perspective"] = tags["perspective"]
    return signals
```

In `mode_collect`, after the enrichment-annotation `try/except` and before `save_signals` (~line 2397), insert:

```python
        signals = annotate_signal_sources(signals)
        save_signals(signals, today, status=status, dropped=dropped)
```

(Replace the existing bare `save_signals(signals, today, status=status, dropped=dropped)` line so annotation precedes it.)

In `mode_run`, before its `save_signals` (~line 2501), insert the same annotation line:

```python
            signals = annotate_signal_sources(signals)
            save_signals(signals, today, status=status, dropped=dropped)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_signals.py -k annotate_signal_sources -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(trading): annotate signals with resolved source tags at save-time (Stage A unit 3)

annotate_signal_sources resolves source_id -> kind/perspective and stamps
them on each snapshot signal; wired into collect + run before save_signals.
Import-safe: trading.py copies, never resolves.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 4: Stamp source tags onto opened positions (trading.py)

`mode_paper` copies the pre-resolved tags onto equity/crypto positions; prediction positions get the unknown bucket.

**Files:**
- Modify: `trading.py` — the equity/crypto position dict in `mode_paper` (~1455-1479) and the prediction position dict in `_open_prediction_positions` (~1331-1361).
- Test: `tests/test_trading.py`

**Interfaces:**
- Consumes: snapshot signals carrying `source_id` / `source_kind` / `source_perspective` (Task 3).
- Produces: opened position dicts gain `source_id: str | None`, `source_kind: str`, `source_perspective: str | None`.

- [ ] **Step 1: Write the failing test**

In `tests/test_trading.py`, append (follow the file's existing monkeypatch style for `mode_paper`; the snippet below shows the assertions — adapt the setup to the file's existing `mode_paper` test harness, e.g. writing a signals snapshot and stubbing `fetch_price`/`resolve_symbol`):

```python
def test_paper_position_carries_source_tags(monkeypatch, tmp_path):
    import trading
    # minimal signal snapshot with source tags already annotated
    sig = {
        "ticker": "SHEL",
        "topic": "hormuz",
        "direction": "bullish",
        "confidence": "high",
        "asset_class": "equity",
        "source_id": "Al Jazeera",
        "source_kind": "regional",
        "source_perspective": "ARAB",
    }
    book = _run_mode_paper_with_signals(monkeypatch, tmp_path, [sig])  # test helper
    pos = book["positions"][-1]
    assert pos["source_kind"] == "regional"
    assert pos["source_perspective"] == "ARAB"
    assert pos["source_id"] == "Al Jazeera"
```

> Note: reuse the existing `mode_paper` test scaffolding in `tests/test_trading.py` (signals dir, `refresh_instruments_cache`/`resolve_symbol`/`fetch_price` stubs, book lock pointed at `tmp_path`). If no shared helper exists, inline the setup mirroring the nearest existing `mode_paper` test.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trading.py -k source_tags -v`
Expected: FAIL (`KeyError: 'source_kind'`).

- [ ] **Step 3: Implement**

In `mode_paper`, in the equity/crypto `book["positions"].append({...})` dict (after `"rationale": s.get("rationale"),`, ~line 1468), add:

```python
                        "source_id": s.get("source_id"),
                        "source_kind": s.get("source_kind", "unknown"),
                        "source_perspective": s.get("source_perspective"),
```

In `_open_prediction_positions`, in its `book["positions"].append({...})` dict (after `"rationale": ...`, ~line 1351), add:

```python
                "source_id": None,
                "source_kind": "unknown",
                "source_perspective": None,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_trading.py -v`
Expected: PASS (new test + existing trading tests).

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_trading.py
git commit -F - <<'EOF'
feat(trading): stamp source tags onto opened positions (Stage A unit 4)

mode_paper copies source_id/kind/perspective off the annotated signal onto
equity/crypto positions; prediction positions get the unknown bucket.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 5: Signal-leakage tally (trading.py)

Count *why* directional signals didn't become trades, per run, into a rolling log.

**Files:**
- Modify: `trading.py` — add `LEAKAGE_LOG_FILE` const (~line 54 area) and `_record_leakage`; tally inside `mode_paper`.
- Test: `tests/test_trading.py`

**Interfaces:**
- Produces:
  - `LEAKAGE_LOG_FILE = PAPER_DIR / "leakage-log.json"` — keep this literal in sync with `validation.LEAKAGE_LOG_FILE` (Task 8).
  - `_record_leakage(date_str: str, tally: dict) -> None` — best-effort merge of `{date_str: tally}` into the log.
  - Leakage tally keys: `traded`, `no_ticker`, `low_confidence`, `neutral`, `no_instrument`, `no_price`, `unpriced_reversal`.

- [ ] **Step 1: Write the failing test**

```python
def test_record_leakage_merges_by_date(monkeypatch, tmp_path):
    import trading
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    trading._record_leakage("2026-06-25", {"traded": 2, "no_ticker": 3})
    trading._record_leakage("2026-06-26", {"traded": 1})
    data = trading._load_json_or(tmp_path / "leak.json", {})
    assert data["2026-06-25"]["no_ticker"] == 3
    assert data["2026-06-26"]["traded"] == 1


def test_paper_tallies_leakage_for_undirectional_and_unresolved(
    monkeypatch, tmp_path
):
    import trading
    monkeypatch.setattr(trading, "LEAKAGE_LOG_FILE", tmp_path / "leak.json")
    sigs = [
        {"topic": "a", "direction": "neutral", "confidence": "high",
         "ticker": "X", "asset_class": "equity"},          # neutral
        {"topic": "b", "direction": "bullish", "confidence": "low",
         "ticker": "Y", "asset_class": "equity"},           # low_confidence
        {"topic": "c", "direction": "bullish", "confidence": "high",
         "ticker": None, "asset_class": "equity"},          # no_ticker
    ]
    _run_mode_paper_with_signals(monkeypatch, tmp_path, sigs)  # test helper
    data = trading._load_json_or(tmp_path / "leak.json", {})
    day = next(iter(data))
    assert data[day]["neutral"] == 1
    assert data[day]["low_confidence"] == 1
    assert data[day]["no_ticker"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_trading.py -k leakage -v`
Expected: FAIL (`LEAKAGE_LOG_FILE` / `_record_leakage` undefined).

- [ ] **Step 3: Implement**

In `trading.py`, near the other `PAPER_DIR` paths (~line 54), add:

```python
LEAKAGE_LOG_FILE = PAPER_DIR / "leakage-log.json"  # Stage A: declined-signal counts
```

Add the recorder (near `save_book`, ~line 1041):

```python
def _record_leakage(date_str: str, tally: dict) -> None:
    """Best-effort merge of one run's directional-signal leakage tally into the log.

    Keyed by date so the weekly report can sum a trailing window. A write/parse
    failure is logged and skipped — leakage accounting never aborts a paper run.
    """
    try:
        data = _load_json_or(LEAKAGE_LOG_FILE, {})
        if not isinstance(data, dict):
            data = {}
        data[date_str] = tally
        _write_json_atomic(LEAKAGE_LOG_FILE, data)
    except Exception as e:
        log.warning(f"Leakage log write skipped: {e}")
```

In `mode_paper`, initialise a tally and replace the `actionable = [...]` comprehension (~1392-1398) with a classifying loop:

```python
    leakage = {
        "traded": 0,
        "no_ticker": 0,
        "low_confidence": 0,
        "neutral": 0,
        "no_instrument": 0,
        "no_price": 0,
        "unpriced_reversal": 0,
    }
    actionable = []
    for s in signals:
        if s.get("direction") not in ("bullish", "bearish"):
            leakage["neutral"] += 1
        elif s.get("confidence") not in ("medium", "high"):
            leakage["low_confidence"] += 1
        elif not s.get("ticker"):
            leakage["no_ticker"] += 1
        else:
            actionable.append(s)
```

Inside the `with file_lock(...)` block, increment in-loop reasons at the existing skip/append points:
- where the unpriced reversal `continue` is (~line 1437): add `leakage["unpriced_reversal"] += 1` before that `continue`.
- where `if not symbol:` skips (~line 1448): add `leakage["no_instrument"] += 1` before that `continue`.
- where `if price is None:` skips (~line 1452): add `leakage["no_price"] += 1` before that `continue`.
- where a position is successfully appended and `opened += 1` (~line 1482): add `leakage["traded"] += 1`.

After `save_book(book)` (~line 1488), record the tally:

```python
        _record_leakage(today, leakage)
```

(The dedup `continue` at "already open" is intentionally NOT counted — it is not leakage.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_trading.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trading.py tests/test_trading.py
git commit -F - <<'EOF'
feat(trading): per-run directional-signal leakage tally (Stage A unit 5)

mode_paper classifies why each directional signal did/didn't trade
(no_ticker/low_confidence/neutral/no_instrument/no_price/unpriced_reversal/
traded) into a date-keyed leakage-log.json. Best-effort, never aborts a run.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 6: Source-attribution dimensions + thin-sample marker (validation.py)

Slice the new dimensions automatically and mark small-n buckets so they never read as fact.

**Files:**
- Modify: `validation.py` — `_DIMENSIONS` (~22), add `_REPORT_MIN_N`, update `_fmt` (~111).
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: closed positions carrying `source_kind` / `source_perspective` (Task 4).
- Produces: `aggregate_performance` and `performance_report` cover `source_kind` and `source_perspective`; `_fmt` appends a thin-sample marker below `_REPORT_MIN_N`.

- [ ] **Step 1: Write the failing tests**

```python
def test_aggregate_includes_source_dimensions():
    book = {
        "positions": [
            dict(_closed("equity", 0.10, edge=0.04), source_kind="regional",
                 source_perspective="ARAB"),
            dict(_closed("equity", -0.05, edge=-0.02), source_kind="wire",
                 source_perspective=None),
        ]
    }
    agg = validation.aggregate_performance(book)
    assert agg["dimensions"]["source_kind"]["regional"]["n"] == 1
    assert agg["dimensions"]["source_kind"]["wire"]["n"] == 1
    assert agg["dimensions"]["source_perspective"]["ARAB"]["n"] == 1


def test_fmt_marks_thin_samples():
    thin = {"n": 1, "hit_rate": 100.0, "mean_net": 0.1, "median_net": 0.1,
            "mean_edge": 0.05, "n_edge": 1}
    assert "thin" in validation._fmt(thin)
    fat = dict(thin, n=validation._REPORT_MIN_N)
    assert "thin" not in validation._fmt(fat)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_validation.py -k "source_dimensions or thin" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `validation.py`, extend `_DIMENSIONS` (~line 22):

```python
_DIMENSIONS = (
    "asset_class",
    "confidence",
    "play_type",
    "thesis_ref",
    "source_kind",
    "source_perspective",
)
```

Add a constant near `_PROMPT_MIN_N` (~line 161) — keep it above the existing one or grouped with module constants:

```python
_REPORT_MIN_N = 5  # below this, a report bucket is flagged thin (not yet meaningful)
```

Update `_fmt` (~line 111):

```python
def _fmt(s: dict) -> str:
    edge = f"{100 * s['mean_edge']:+.1f}%" if s["mean_edge"] is not None else "n/a"
    out = (
        f"{s['hit_rate']:.0f}% hit · net {100 * s['mean_net']:+.1f}% "
        f"· edge {edge} (n={s['n']})"
    )
    if s["n"] < _REPORT_MIN_N:
        out += " ⚠thin"
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_validation.py -v`
Expected: PASS (existing report tests still pass — they assert substrings unaffected by the marker).

- [ ] **Step 5: Commit**

```bash
git add validation.py tests/test_validation.py
git commit -F - <<'EOF'
feat(trading): source_kind/source_perspective dims + thin-sample marker (Stage A unit 6)

aggregate_performance/performance_report now slice source attribution; _fmt
flags any bucket below _REPORT_MIN_N so small-n never reads as fact. The
prompt-block dimension tuple is untouched (Stage-B firewall).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 7: Calibration block with inversion flag (validation.py)

Render low/medium/high realized performance and flag when higher confidence underperforms lower.

**Files:**
- Modify: `validation.py` — add `_CONF_ORDER`, `_calibration_block`; call it in `performance_report` (~before the go-live section, ~line 152).
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: `aggregate_performance(book)["dimensions"]["confidence"]`.
- Produces: `_calibration_block(agg: dict) -> list[str]` — Telegram-HTML lines; flags inversions by edge (falling back to net when edge is None).

- [ ] **Step 1: Write the failing tests**

```python
def test_calibration_block_flags_inversion():
    # high realizes LESS edge than medium -> inverted
    book = {
        "positions": [
            *[dict(_closed("equity", 0.02, edge=0.05), confidence="medium")
              for _ in range(3)],
            *[dict(_closed("equity", 0.01, edge=0.01), confidence="high")
              for _ in range(3)],
        ]
    }
    agg = validation.aggregate_performance(book)
    lines = validation._calibration_block(agg)
    joined = "\n".join(lines)
    assert "Calibration" in joined
    assert "inverted" in joined.lower()


def test_calibration_block_silent_when_monotonic():
    book = {
        "positions": [
            *[dict(_closed("equity", 0.01, edge=0.01), confidence="medium")
              for _ in range(3)],
            *[dict(_closed("equity", 0.05, edge=0.06), confidence="high")
              for _ in range(3)],
        ]
    }
    agg = validation.aggregate_performance(book)
    joined = "\n".join(validation._calibration_block(agg))
    assert "inverted" not in joined.lower()


def test_calibration_block_empty_without_confidence_data():
    assert validation._calibration_block({"dimensions": {}, "overall": None}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_validation.py -k calibration -v`
Expected: FAIL (`_calibration_block` undefined).

- [ ] **Step 3: Implement**

In `validation.py`, add near the other helpers (after `_fmt`):

```python
_CONF_ORDER = ("low", "medium", "high")


def _calibration_block(agg: dict) -> list[str]:
    """Confidence -> realized performance, with an inversion flag.

    Lists low/medium/high (those present) via _fmt, then flags any adjacent pair
    where the higher confidence band realized LESS than the lower — the
    actionable miscalibration signal. Scored by mean_edge, falling back to
    mean_net when edge is unavailable. Returns [] when no confidence data exists.
    """
    conf = agg.get("dimensions", {}).get("confidence", {})
    present = [(c, conf[c]) for c in _CONF_ORDER if c in conf]
    if not present:
        return []
    lines = ["<b>🎯 Calibration (confidence → realized)</b>"]
    for c, s in present:
        lines.append(f"  – {c}: {_fmt(s)}")

    def _score(s: dict) -> float:
        return s["mean_edge"] if s["mean_edge"] is not None else s["mean_net"]

    inversions = [
        f"{hi_c}&lt;{lo_c}"
        for (lo_c, lo_s), (hi_c, hi_s) in zip(present, present[1:])
        if _score(hi_s) < _score(lo_s)
    ]
    if inversions:
        lines.append(
            "  ⚠ inverted: " + ", ".join(inversions)
            + " (higher confidence underperforming)"
        )
    return lines
```

In `performance_report`, insert the block before the go-live gate section (before `lines.append("<b>🚦 Go-live gate</b>")`, ~line 152):

```python
    lines.extend(_calibration_block(agg))

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_validation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add validation.py tests/test_validation.py
git commit -F - <<'EOF'
feat(trading): calibration block with inversion flag (Stage A unit 7)

performance_report renders low/medium/high realized edge and flags when a
higher confidence band underperforms a lower one — the actionable
miscalibration cue. Scored by edge, falling back to net.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

---

### Task 8: Leakage summary section (validation.py)

Sum the rolling leakage log over a trailing window and render it in the report.

**Files:**
- Modify: `validation.py` — add `LEAKAGE_LOG_FILE`, `leakage_summary`, `_leakage_block`; call `_leakage_block` in `performance_report`.
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: `paper/leakage-log.json` written by Task 5.
- Produces:
  - `LEAKAGE_LOG_FILE = DATA_DIR / "paper" / "leakage-log.json"` — must equal `trading.LEAKAGE_LOG_FILE`.
  - `leakage_summary(window_days: int = 7) -> dict` — summed reason→count over the most recent `window_days` date-keys.
  - `_leakage_block() -> list[str]` — Telegram-HTML line(s), `[]` when nothing logged.

- [ ] **Step 1: Write the failing tests**

```python
def test_leakage_summary_sums_recent_window(monkeypatch, tmp_path):
    f = tmp_path / "leak.json"
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", f)
    validation._write_json_atomic(f, {
        "2026-06-20": {"traded": 1, "no_ticker": 2},
        "2026-06-21": {"traded": 3, "no_ticker": 1, "no_price": 1},
    })
    totals = validation.leakage_summary(window_days=7)
    assert totals["traded"] == 4
    assert totals["no_ticker"] == 3
    assert totals["no_price"] == 1


def test_leakage_summary_respects_window(monkeypatch, tmp_path):
    f = tmp_path / "leak.json"
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", f)
    validation._write_json_atomic(f, {
        "2026-06-01": {"traded": 99},
        "2026-06-24": {"traded": 1},
        "2026-06-25": {"traded": 2},
    })
    assert validation.leakage_summary(window_days=2)["traded"] == 3


def test_leakage_block_empty_when_no_log(monkeypatch, tmp_path):
    monkeypatch.setattr(validation, "LEAKAGE_LOG_FILE", tmp_path / "absent.json")
    assert validation._leakage_block() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_validation.py -k leakage -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `validation.py`, add the path constant near `GATE_HISTORY_FILE` (~line 21):

```python
LEAKAGE_LOG_FILE = DATA_DIR / "paper" / "leakage-log.json"  # written by trading._record_leakage
```

Add the summary + block (after `_calibration_block`):

```python
def leakage_summary(window_days: int = 7) -> dict:
    """Sum directional-signal leakage counts over the most recent window_days.

    Reads the date-keyed log trading._record_leakage writes. Returns {} when the
    log is missing/empty. Non-integer values are skipped defensively.
    """
    data = _load_json_or(LEAKAGE_LOG_FILE, {}) or {}
    if not isinstance(data, dict):
        return {}
    totals: dict = {}
    for day in sorted(data.keys())[-window_days:]:
        for reason, n in (data.get(day) or {}).items():
            try:
                totals[reason] = totals.get(reason, 0) + int(n)
            except (TypeError, ValueError):
                continue
    return totals


def _leakage_block() -> list[str]:
    """One-line directional-signal leakage summary for the report ([] when empty)."""
    totals = leakage_summary()
    grand = sum(totals.values())
    if grand == 0:
        return []
    traded = totals.get("traded", 0)
    drops = {k: v for k, v in totals.items() if k != "traded" and v > 0}
    line = f"<b>🚰 Signal leakage (7d)</b>: {grand} directional → {traded} traded"
    if drops:
        parts = ", ".join(
            f"{v} {k}" for k, v in sorted(drops.items(), key=lambda kv: -kv[1])
        )
        line += f"; dropped: {parts}"
    return [line]
```

In `performance_report`, add the leakage block after the calibration block and before the go-live gate (~line 152):

```python
    lines.extend(_leakage_block())

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_validation.py -v`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

```bash
ruff check . && ruff format --check . && python -m pytest -q
git add validation.py tests/test_validation.py
git commit -F - <<'EOF'
feat(trading): 7-day signal-leakage summary in performance report (Stage A unit 8)

leakage_summary sums the rolling log over a trailing window; performance_report
renders "N directional -> K traded; dropped: ..." surfacing coverage gaps.
Completes Stage A measurement.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01AGst6UihQnUKhuq2m7L4jt
EOF
```

> If `ruff format --check` reports changes, run `ruff format .`, re-stage, and amend before pushing.

---

## Self-Review

**Spec coverage:**
- Unit 1 (resolver) → Task 1. Unit 2 (extraction `source_id`) → Task 2. Unit 3 (annotation + position stamping) → Tasks 3 (annotate, brief.py) + 4 (stamp, trading.py). Unit 4 (aggregation + source views + calibration + thin marker) → Tasks 6 (dims + marker) + 7 (calibration). Unit 5 (leakage) → Tasks 5 (tally) + 8 (summary/render). Surfacing via existing `/performance`/weekly `performance_report` → Tasks 6-8 (no new command, per Q5). Firewall (`performance_prompt_block` untouched; its hardcoded tuple, not `_DIMENSIONS`) → Global Constraints + verified in Task 6. Prediction out-of-scope → Task 4. Counterfactual scoring parked → not in plan (roadmap). ✔ All covered.

**Placeholder scan:** No TBD/TODO; every code step shows full code. The one soft spot — Task 4/5 reuse the file's existing `mode_paper` test scaffolding (`_run_mode_paper_with_signals`) — is called out explicitly with fallback instructions, since the exact harness lives in `tests/test_trading.py` and must be matched, not invented.

**Type consistency:** `source_id`/`source_kind`/`source_perspective` names consistent across Tasks 2-6. `resolve_source_tags` returns `{"kind", "perspective"}`; `annotate_signal_sources` maps those to signal keys `source_kind`/`source_perspective` (Task 3) which Task 4 copies and Task 6 aggregates. Leakage tally keys identical between Task 5 (writer) and Task 8 (`leakage_summary` reader). `LEAKAGE_LOG_FILE` defined in both modules with matching path literal (`PAPER_DIR/"leakage-log.json"` == `DATA_DIR/"paper"/"leakage-log.json"`), flagged in both interfaces. `_fmt` thin-marker (Task 6) is reused by `_calibration_block` (Task 7) — consistent. ✔

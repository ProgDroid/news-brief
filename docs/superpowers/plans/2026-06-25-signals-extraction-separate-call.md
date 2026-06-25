# Signals Extraction Separate-Call Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move position-signal JSON extraction out of brief generation into a separate Sonnet 4.6 forced-tool-use call that reads the finished brief.

**Architecture:** The brief Batch API call returns prose only. A new post-gen call (`extract_signals`) reads the delivered brief and emits signals via a forced `emit_signals` tool (schema-enforced JSON — no marker, no truncation). The existing `normalize_signals` / `save_signals` pipeline is unchanged. The legacy delimiter parser is deleted. Mirrors the shipped `brief_memory.reconcile_ledger` separate-call pattern.

**Tech Stack:** Python 3, `requests` (raw Anthropic Messages API), pytest. No new dependencies, no new top-level module.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-25-signals-extraction-separate-call-design.md`.
- Extraction model: `claude-sonnet-4-6` (constant `SIGNALS_MODEL`). Reconcile stays Haiku.
- All new code lives in `brief.py` — NOT a new module (avoids the Dockerfile-COPY / CI-paths triple-update).
- `requests` and `ANTHROPIC_HEADERS` are already imported in `brief.py` (lines 30, 43) — no import edits.
- Fail-safe everywhere: any extraction error → `([], "extract_error")`, logged at warning, brief unaffected.
- Signal schema (8 fields) is unchanged from today's inline JSON — do NOT add/remove fields (paper-tracker consumer contract).
- `normalize_signals` and `save_signals` stay exactly as-is (defense-in-depth).
- Pre-push gate (run before every commit): `ruff check . && ruff format --check . && pytest -q`. Stage every reformatted file or CI fails.
- Commit via the Bash tool, never PowerShell (PowerShell prepends a BOM to the commit subject).

---

### Task 1: Pure request builder, response parser, and tool schema

**Files:**
- Modify: `brief.py` — add constants + two pure functions near the existing signals code (after `normalize_signals`, before `save_signals` at `brief.py:1770`).
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib + module constants).
- Produces:
  - `SIGNALS_MODEL: str = "claude-sonnet-4-6"`
  - `_EMIT_SIGNALS_TOOL: dict` — Anthropic tool definition.
  - `build_signals_request(brief_text: str) -> dict` — full Messages API payload (model, max_tokens, system, tools, tool_choice, messages).
  - `parse_signals_response(resp: dict) -> list` — pulls `input.signals` from the `emit_signals` tool_use block; raises `ValueError` if absent/malformed.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_signals.py`:

```python
import brief


def test_build_signals_request_forces_emit_signals_tool():
    req = brief.build_signals_request("PROSE BRIEF TEXT")
    assert req["model"] == "claude-sonnet-4-6"
    assert req["tool_choice"] == {"type": "tool", "name": "emit_signals"}
    assert req["tools"][0]["name"] == "emit_signals"
    # brief text is carried into the user message
    assert "PROSE BRIEF TEXT" in req["messages"][0]["content"]
    # enum-constrained fields are real JSON-Schema enums
    item = req["tools"][0]["input_schema"]["properties"]["signals"]["items"]
    assert item["properties"]["direction"]["enum"] == ["bullish", "bearish", "neutral"]
    assert item["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert item["properties"]["asset_class"]["enum"] == ["equity", "crypto"]
    # nullable fields are NOT required; core fields are
    assert set(item["required"]) == {
        "asset_class", "topic", "direction", "confidence", "rationale",
    }


def test_parse_signals_response_extracts_signal_list():
    resp = {
        "content": [
            {
                "type": "tool_use",
                "name": "emit_signals",
                "input": {"signals": [{"topic": "x", "direction": "bullish"}]},
            }
        ]
    }
    assert brief.parse_signals_response(resp) == [
        {"topic": "x", "direction": "bullish"}
    ]


def test_parse_signals_response_raises_when_no_tool_block():
    resp = {"content": [{"type": "text", "text": "no tool call here"}]}
    try:
        brief.parse_signals_response(resp)
        assert False, "expected ValueError"
    except ValueError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signals.py -k "build_signals_request or parse_signals_response" -v`
Expected: FAIL with `AttributeError: module 'brief' has no attribute 'build_signals_request'`

- [ ] **Step 3: Write the implementation**

Insert into `brief.py` immediately after `normalize_signals` returns (before `def save_signals`):

```python
# ── Signals extraction (separate post-gen call) ───────────────────────────────
# The brief no longer emits a trailing @@@SIGNALS@@@ JSON block; a dedicated
# Sonnet call reads the finished brief and emits signals via a forced tool, so
# the JSON is schema-guaranteed (no delimiter to mangle, no shared token budget
# to truncate). Mirrors brief_memory.reconcile_ledger.
SIGNALS_MODEL = "claude-sonnet-4-6"

_EMIT_SIGNALS_TOOL = {
    "name": "emit_signals",
    "description": (
        "Record every position-relevant signal found in the brief. Return an "
        "empty list if the brief contains none."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": (
                                "primary listing symbol — e.g. SHEL, BP, BTC, "
                                "ETH. Omit for macro-level signals with no single "
                                "tradable instrument."
                            ),
                        },
                        "asset_class": {
                            "type": "string",
                            "enum": ["equity", "crypto"],
                            "description": "equity for stocks/ETFs, crypto for major coins",
                        },
                        "topic": {
                            "type": "string",
                            "description": "short topic label, e.g. hormuz-disruption",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["bullish", "bearish", "neutral"],
                        },
                        "thesis_ref": {
                            "type": "string",
                            "description": "the held thesis this bears on. Omit if none.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "rationale": {
                            "type": "string",
                            "description": "one sentence, no more",
                        },
                        "provenance": {
                            "type": "string",
                            "description": (
                                "which source the brief cites for this. Omit if "
                                "the brief does not name one."
                            ),
                        },
                    },
                    "required": [
                        "asset_class",
                        "topic",
                        "direction",
                        "confidence",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["signals"],
    },
}

_SIGNALS_SYSTEM = (
    "You extract position-relevant trading signals from a finished daily market "
    "brief. You do not add analysis of your own — you only record signals the "
    "brief itself supports."
)

_SIGNALS_USER_TEMPLATE = """Read the entire brief below and call emit_signals with \
every position-relevant signal it contains.

Include macro-level signals (no single ticker — omit the ticker field) as well as \
named-instrument signals. Set provenance from a source the brief cites, or omit it. \
Return an empty list if the brief contains no actionable signals.

BRIEF:
{brief}
"""


def build_signals_request(brief_text: str) -> dict:
    """Build the Anthropic Messages payload for the forced-tool signals call."""
    return {
        "model": SIGNALS_MODEL,
        "max_tokens": 2048,
        "system": _SIGNALS_SYSTEM,
        "tools": [_EMIT_SIGNALS_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_signals"},
        "messages": [
            {"role": "user", "content": _SIGNALS_USER_TEMPLATE.format(brief=brief_text)}
        ],
    }


def parse_signals_response(resp: dict) -> list:
    """Pull the signals list from the emit_signals tool_use block.

    Raises ValueError if no emit_signals tool_use block is present or its input
    has no 'signals' list — the fail-safe wrapper turns that into extract_error.
    """
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_signals":
            signals = block.get("input", {}).get("signals")
            if isinstance(signals, list):
                return signals
            raise ValueError("emit_signals input missing 'signals' list")
    raise ValueError("no emit_signals tool_use block in response")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signals.py -k "build_signals_request or parse_signals_response" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(signals): forced-tool request builder + response parser

Pure build_signals_request / parse_signals_response + emit_signals tool schema
(enum-constrained direction/confidence/asset_class; nullable ticker/thesis_ref/
provenance non-required). No wiring yet.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `extract_signals` fail-safe wrapper + HTTP helper

**Files:**
- Modify: `brief.py` — add `_post_messages` and `extract_signals` directly after `parse_signals_response`.
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `build_signals_request`, `parse_signals_response` (Task 1); `ANTHROPIC_HEADERS`, `requests`, `log` (already imported).
- Produces:
  - `extract_signals(brief_text: str, *, call=None) -> tuple[list, str]` — returns `(raw_signals, status)`, `status ∈ {"ok", "extract_error"}`. `call` is an injectable seam taking the request payload dict and returning the response JSON dict; defaults to `_post_messages`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_signals.py`:

```python
def test_extract_signals_ok_path_returns_signals_and_ok():
    def fake_call(payload):
        assert payload["tool_choice"]["name"] == "emit_signals"
        return {
            "content": [
                {
                    "type": "tool_use",
                    "name": "emit_signals",
                    "input": {"signals": [{"topic": "oil", "direction": "bullish"}]},
                }
            ]
        }

    raw_signals, status = brief.extract_signals("BRIEF", call=fake_call)
    assert status == "ok"
    assert raw_signals == [{"topic": "oil", "direction": "bullish"}]


def test_extract_signals_failsafe_on_call_exception():
    def boom(payload):
        raise RuntimeError("HTTP 529 overloaded")

    raw_signals, status = brief.extract_signals("BRIEF", call=boom)
    assert raw_signals == []
    assert status == "extract_error"


def test_extract_signals_failsafe_on_missing_tool_block():
    def no_tool(payload):
        return {"content": [{"type": "text", "text": "sorry"}]}

    raw_signals, status = brief.extract_signals("BRIEF", call=no_tool)
    assert raw_signals == []
    assert status == "extract_error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_signals.py -k extract_signals -v`
Expected: FAIL with `AttributeError: module 'brief' has no attribute 'extract_signals'`

- [ ] **Step 3: Write the implementation**

Insert into `brief.py` after `parse_signals_response`:

```python
def _post_messages(payload: dict) -> dict:
    """Raw Anthropic Messages API call (not unit-tested; the seam in
    extract_signals is the test boundary)."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def extract_signals(brief_text: str, *, call=None) -> tuple[list, str]:
    """Extract position signals from a finished brief via a forced-tool call.

    Returns (raw_signals, status) where status is "ok" on success or
    "extract_error" on any failure (HTTP, missing tool block, malformed input).
    Fail-safe: never raises; the brief is already delivered and unaffected.
    """
    caller = call or _post_messages
    try:
        resp = caller(build_signals_request(brief_text))
        return parse_signals_response(resp), "ok"
    except Exception as e:
        log.warning(f"Signals extraction failed; no signals this run: {e}")
        return [], "extract_error"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_signals.py -k extract_signals -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
feat(signals): extract_signals fail-safe wrapper + HTTP helper

Sonnet forced-tool call reading the finished brief; any failure -> ([], "extract_error").
Injectable call seam mirrors brief_memory.reconcile_ledger.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Remove the `@@@SIGNALS@@@` block from the brief prompt

**Files:**
- Modify: `brief.py:1567-1585` (the delimiter + JSON schema block inside `DAILY_SYSTEM_PROMPT`).
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a `DAILY_SYSTEM_PROMPT` that no longer instructs the model to emit signals JSON, but still contains the human-readable `📌 POSITION SIGNALS` prose section.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signals.py`:

```python
def test_daily_prompt_drops_signals_json_but_keeps_prose_section():
    assert "@@@SIGNALS@@@" not in brief.DAILY_SYSTEM_PROMPT
    assert "JSON array" not in brief.DAILY_SYSTEM_PROMPT
    # human-readable section stays
    assert "POSITION SIGNALS" in brief.DAILY_SYSTEM_PROMPT
    # word-limit instruction is preserved
    assert "under 600 words" in brief.DAILY_SYSTEM_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_signals.py -k daily_prompt_drops -v`
Expected: FAIL (`@@@SIGNALS@@@` still present in the prompt)

- [ ] **Step 3: Edit the prompt**

In `brief.py`, delete this block (everything from the line after the WATCH/FORWARD bullet up to but NOT including `Keep the entire brief under 600 words.`):

Remove exactly:
```
After the WATCH / FORWARD section and a blank line, output the delimiter token below on its
own line, exactly as written — it is a literal parsing marker, NOT a section divider, so
reproduce it verbatim and do not shorten, restyle, or drop it:
@@@SIGNALS@@@
Then output a JSON array (and nothing else after it) capturing any position-relevant signals.
Empty array if none. Schema:
[
  {{
    "ticker": "the primary listing symbol — e.g. SHEL or BP for equities, BTC or ETH for crypto; null only for macro-level signals with no single tradable instrument",
    "asset_class": "equity | crypto — equity for stocks/ETFs, crypto for major coins; default to equity if unsure",
    "topic": "short topic label, e.g. hormuz-disruption",
    "direction": "bullish | bearish | neutral",
    "thesis_ref": "the held thesis this bears on, or null",
    "confidence": "low | medium | high",
    "rationale": "one sentence, no more",
    "provenance": "which source/feed/search this came from"
  }}
]

```

So that the WATCH / FORWARD section is immediately followed (after one blank line) by:
```
Keep the entire brief under 600 words."""
```

Leave the `📌 POSITION SIGNALS` section (above WATCH/FORWARD) untouched.

- [ ] **Step 4: Run tests to verify the suite is green**

Run: `pytest tests/test_signals.py -v && ruff check brief.py && ruff format --check brief.py`
Expected: PASS; the new prompt test passes, no lint/format drift.

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
refactor(brief): drop @@@SIGNALS@@@ JSON tail from the brief prompt

Brief generation now produces prose only; the trailing signals JSON is emitted
by the separate extract_signals call. The human-readable POSITION SIGNALS
section stays.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Wire `extract_signals` into the collect and run paths

**Files:**
- Modify: `brief.py:2093-2118` (`mode_collect`) and `brief.py:2196-2210` (`mode_run`).

**Interfaces:**
- Consumes: `extract_signals` (Task 2), `normalize_signals` / `save_signals` (existing), `deliver` (existing), the now-prose-only `raw`.
- Produces: both modes deliver the brief first, then extract → normalize → annotate (collect only) → save.

- [ ] **Step 1: Rewrite the `mode_collect` body**

Replace `brief.py:2095-2118` (the `if raw:` block through `clear_batch_state()`) with:

```python
    if raw:
        brief = raw.strip()
        deliver(
            brief,
            header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
            archive_path=BRIEFS_DIR / f"brief-{today}.md",
        )
        raw_signals, status = extract_signals(brief)
        signals, dropped = normalize_signals(raw_signals)
        if dropped or status != "ok":
            log.warning(f"Signals: status={status}, dropped={dropped}")
        try:
            enr_path = DATA_DIR / "enrichment" / f"enrichment-{today}.json"
            if enr_path.exists():
                enr_raw = json.loads(enr_path.read_text(encoding="utf-8"))
                signals = annotate_signals(signals, bundles_from_dict(enr_raw))
        except Exception as e:
            log.error(f"Signal annotation skipped (signals unaffected): {e}")
        save_signals(signals, today, status=status, dropped=dropped)
        if brief_memory_enabled():
            try:
                save_ledger(reconcile_ledger(load_ledger(), brief, today))
            except Exception as e:
                log.error(f"Brief-memory reconcile skipped (brief unaffected): {e}")
        clear_batch_state()
```

(The trading-stage block below `clear_batch_state()` is unchanged.)

- [ ] **Step 2: Rewrite the `mode_run` body**

Replace `brief.py:2197-2210` (the inner `if raw:` block) with:

```python
        if raw:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            brief = raw.strip()
            deliver(
                brief,
                header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
                archive_path=BRIEFS_DIR / f"brief-{today}.md",
            )
            raw_signals, status = extract_signals(brief)
            signals, dropped = normalize_signals(raw_signals)
            if dropped or status != "ok":
                log.warning(f"Signals: status={status}, dropped={dropped}")
            save_signals(signals, today, status=status, dropped=dropped)
            mode_paper()
            clear_batch_state()
```

- [ ] **Step 3: Verify nothing else references the old split flow**

Run: `grep -n "split_brief_and_signals" brief.py`
Expected: now only the definition (and its tests) remain — no call sites. (Removed in Task 5.)

- [ ] **Step 4: Run the gate**

Run: `ruff check . && ruff format --check . && pytest -q`
Expected: PASS. (No unit test drives `mode_collect`/`mode_run` directly; `extract_signals` is covered by Task 2. Verification is the green suite + lint.)

- [ ] **Step 5: Commit**

```bash
git add brief.py
git commit -F - <<'EOF'
feat(brief): deliver-first, then extract signals via the separate call

mode_collect / mode_run now deliver the prose brief, then run extract_signals ->
normalize -> annotate (collect) -> save. A slow/failed extraction never delays
the brief. Enrichment annotation moves after extraction.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Delete the dead delimiter parser

**Files:**
- Modify: `brief.py` — remove `split_brief_and_signals`, `_find_trailing_json_array`, `_SIGNAL_MARKERS` and the explanatory comment above `_SIGNAL_MARKERS` (`brief.py:1628-1690`).
- Modify: `tests/test_signals.py` — remove tests that exercise `split_brief_and_signals` / `_find_trailing_json_array`.

**Interfaces:**
- Consumes: nothing.
- Produces: a smaller `brief.py` with no inline-signals parsing surface.

- [ ] **Step 1: Find the dead-code tests**

Run: `grep -n "split_brief_and_signals\|_find_trailing_json_array\|_SIGNAL_MARKERS\|---SIGNALS---\|@@@SIGNALS@@@" tests/test_signals.py`
Expected: a list of test functions and assertions referencing the removed format. (The Task 3 prompt test asserts `@@@SIGNALS@@@` is ABSENT — keep that one.)

- [ ] **Step 2: Delete the dead tests**

Remove every test function whose body calls `brief.split_brief_and_signals(...)` or `brief._find_trailing_json_array(...)`. Keep `test_daily_prompt_drops_signals_json_but_keeps_prose_section` (Task 3) and all `normalize_signals` / `save_signals` / `extract_signals` / `build_signals_request` / `parse_signals_response` tests.

- [ ] **Step 3: Run tests to confirm only dead-code tests are gone**

Run: `pytest tests/test_signals.py -q`
Expected: PASS — the suite is green with the dead tests removed (the production functions still exist at this point).

- [ ] **Step 4: Delete the production dead code**

In `brief.py`, remove the comment block + the three definitions (`brief.py:1628-1690`):
- the two-line comment beginning `# Delimiters the model may emit before the signals JSON, primary first.`
- `_SIGNAL_MARKERS = ("@@@SIGNALS@@@", "---SIGNALS---")`
- `def _find_trailing_json_array(...)` (entire function)
- `def split_brief_and_signals(...)` (entire function)

Leave `normalize_signals`, the synonym maps (`_DIRECTION_MAP` etc.), and everything from Task 1/2 intact.

- [ ] **Step 5: Run the full gate**

Run: `ruff check . && ruff format --check . && pytest -q`
Expected: PASS. Confirm no `NameError` (nothing references the removed symbols):
Run: `grep -rn "split_brief_and_signals\|_find_trailing_json_array\|_SIGNAL_MARKERS" .`
Expected: no matches outside `docs/`.

- [ ] **Step 6: Commit**

```bash
git add brief.py tests/test_signals.py
git commit -F - <<'EOF'
refactor(signals): remove dead @@@SIGNALS@@@ delimiter parser

split_brief_and_signals / _find_trailing_json_array / _SIGNAL_MARKERS existed
only to recover the inline marker/array format, which forced tool-use replaced.
Net code reduction.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Self-Review

**1. Spec coverage:**
- Brief prompt JSON tail removed, prose kept → Task 3. ✓
- `extract_signals` forced-tool Sonnet call, fail-safe, `(raw_signals, status)` → Tasks 1+2. ✓
- Forced tool-use, enum constraints, required-vs-nullable → Task 1 schema + test. ✓
- Delete dead parser → Task 5. ✓
- `normalize_signals`/`save_signals` unchanged; `extract_error` flows to snapshot → Tasks 1–5 leave them untouched; status passed through in Task 4. ✓
- Deliver-first call-site reorder, enrichment annotation moved after extraction (collect) → Task 4. ✓
- Two separate calls (reconcile stays its own, unchanged) → Task 4 leaves the reconcile block intact. ✓
- Testing list (ok / fail-safe / missing-tool / normalize-still-applies / remove split tests) → Tasks 1,2,5. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**3. Type consistency:** `extract_signals -> (list, str)` consumed in Task 4 as `raw_signals, status`; `normalize_signals(raw_signals) -> (signals, dropped)` matches existing signature; `save_signals(signals, today, status=, dropped=)` matches existing signature; `build_signals_request`/`parse_signals_response` names consistent across Tasks 1–2. ✓

**Note for executor:** if the Anthropic API rejects a tool `input_schema` property with no `type` constraint, this plan never produces one — every property has an explicit `type`. The model may still occasionally omit a `required` enum field; `normalize_signals` drops such a signal (its existing behavior), which is the intended defense-in-depth.

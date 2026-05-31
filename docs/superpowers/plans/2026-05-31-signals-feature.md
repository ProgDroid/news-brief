# Signals Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing WIP signals pipeline in `brief.py` trustworthy — validated/normalised model output, a per-day audit snapshot that distinguishes empty from failed, and a consistent collect/run path.

**Architecture:** Three-stage flow inside the single file `brief.py`: `split_brief_and_signals` (extract + report status) → `normalize_signals` (clean to the 7-field schema, drop junk) → `save_signals` (always write a dated snapshot; append rolling log only when non-empty). `mode_collect` and `mode_run` both drive this flow.

**Tech Stack:** Python 3 stdlib + `requests` + `feedparser` (no additions). No test framework — verification uses an ephemeral stubbed-exec harness (stubs `feedparser`/`requests`, redirects `/app/logs` paths) run via `python - <<'PY'`.

**Spec:** `docs/superpowers/specs/2026-05-31-signals-design.md`

**Verification harness preamble** (reused in every task's verify step — load `brief.py` without secrets, network, or `/app/logs`):

```python
import sys, types, os
for name in ("feedparser", "requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py", "r", encoding="utf-8").read()
src = src.replace('"/app/logs/newsbrief.log"', '"_t/newsbrief.log"').replace('Path("/app/logs/', 'Path("./_t/')
os.makedirs("_t", exist_ok=True)
ns = {}
exec(compile(src, "brief.py", "exec"), ns)
```

(Each task's harness ends by cleaning up: `import shutil; shutil.rmtree("_t", ignore_errors=True)`.)

---

### Task 1: `normalize_signals` — clean model output to the 7-field schema

**Files:**
- Modify: `brief.py` — add module-level maps + `_nullish` + `normalize_signals`, immediately after `split_brief_and_signals` (currently ends ~`brief.py:766`, in the `# ── Prompts ──` section).

This task adds a pure function with no call-site changes — `brief.py` still compiles and runs as before (the function is simply unused until Task 3).

- [ ] **Step 1: Write the failing verification**

```python
# verify_normalize.py content — run inline (see harness preamble above), then:
norm = ns["normalize_signals"]

# Coercion of synonyms
clean, dropped = norm([
    {"ticker": "SHEL_US_EQ", "topic": "hormuz", "direction": "long", "confidence": "med",
     "thesis_ref": "oil", "rationale": "r", "provenance": "reuters"},
    {"ticker": "null", "topic": "macro", "direction": "BEARISH", "confidence": "High",
     "rationale": "r", "provenance": "p", "extra_field": "should be dropped"},
])
assert dropped == 0, dropped
assert clean[0]["direction"] == "bullish" and clean[0]["confidence"] == "medium", clean[0]
assert clean[1]["direction"] == "bearish" and clean[1]["confidence"] == "high", clean[1]
assert clean[1]["ticker"] is None, "null-ish ticker -> None"
assert "extra_field" not in clean[1], "invented fields discarded"
assert set(clean[0]) == {"ticker","topic","direction","confidence","thesis_ref","rationale","provenance"}

# Drops: bad enum, missing topic, non-dict
clean2, dropped2 = norm([
    {"topic": "x", "direction": "sideways", "confidence": "high"},   # bad direction
    {"topic": "x", "direction": "bullish", "confidence": "vibes"},   # bad confidence
    {"direction": "bullish", "confidence": "high"},                  # no topic
    "not a dict",
])
assert clean2 == [] and dropped2 == 4, (clean2, dropped2)

# Defaults for missing optional fields
clean3, _ = norm([{"topic": "x", "direction": "neutral", "confidence": "low"}])
assert clean3[0]["rationale"] == "" and clean3[0]["thesis_ref"] is None, clean3[0]
print("TASK1_PASS")
```

- [ ] **Step 2: Run to verify it fails**

Run:
```bash
python - <<'PY'
import sys, types, os
for name in ("feedparser", "requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py","r",encoding="utf-8").read()
src = src.replace('"/app/logs/newsbrief.log"','"_t/newsbrief.log"').replace('Path("/app/logs/','Path("./_t/')
os.makedirs("_t", exist_ok=True)
ns = {}
exec(compile(src, "brief.py", "exec"), ns)
assert "normalize_signals" in ns, "EXPECTED FAIL: normalize_signals not defined yet"
PY
```
Expected: `AssertionError: EXPECTED FAIL: normalize_signals not defined yet`

- [ ] **Step 3: Implement `normalize_signals`**

Insert after the end of `split_brief_and_signals` (before `save_signals`):

```python
# Synonym maps for coercing free-form model output to the known enums.
_DIRECTION_MAP = {
    "bullish": "bullish", "long": "bullish", "buy": "bullish", "positive": "bullish", "up": "bullish",
    "bearish": "bearish", "short": "bearish", "sell": "bearish", "negative": "bearish", "down": "bearish",
    "neutral": "neutral", "flat": "neutral", "hold": "neutral",
}
_CONFIDENCE_MAP = {
    "low": "low", "lo": "low",
    "medium": "medium", "med": "medium", "moderate": "medium",
    "high": "high", "hi": "high",
}
_NULLISH = {"", "null", "none", "n/a", "na"}


def _nullish(value) -> str | None:
    """Map null-ish model values ('', 'null', 'none', ...) to None, else a stripped string."""
    if value is None or str(value).strip().lower() in _NULLISH:
        return None
    return str(value).strip()


def normalize_signals(raw_signals: list) -> tuple[list, int]:
    """Coerce free-form model signal output to the known 7-field schema.

    Validates/normalises the direction and confidence enums, requires a non-empty
    topic, nulls empty tickers/thesis_refs, and keeps only the known fields.
    Returns (clean_signals, dropped_count). A signal is dropped when its direction
    or confidence cannot be resolved, when it has no topic, or when it is not a dict.
    """
    clean, dropped = [], 0
    for item in raw_signals:
        if not isinstance(item, dict):
            dropped += 1
            continue
        direction  = _DIRECTION_MAP.get(str(item.get("direction", "")).strip().lower())
        confidence = _CONFIDENCE_MAP.get(str(item.get("confidence", "")).strip().lower())
        topic      = str(item.get("topic", "")).strip()
        if direction is None or confidence is None or not topic:
            dropped += 1
            continue
        clean.append({
            "ticker":     _nullish(item.get("ticker")),
            "topic":      topic,
            "direction":  direction,
            "confidence": confidence,
            "thesis_ref": _nullish(item.get("thesis_ref")),
            "rationale":  str(item.get("rationale", "")).strip(),
            "provenance": str(item.get("provenance", "")).strip(),
        })
    return clean, dropped
```

- [ ] **Step 4: Run the Step 1 verification to confirm it passes**

Run the full harness preamble + the Step 1 assertions + `shutil.rmtree("_t", ignore_errors=True)`.
Expected: prints `TASK1_PASS`, exit 0.

- [ ] **Step 5: Compile check**

Run: `python -m py_compile brief.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 6: Commit** (only if the user has authorised committing — they chose "stay on main, commit when asked")

```bash
git add brief.py
git commit -m "feat(signals): add normalize_signals to coerce model output to schema"
```

---

### Task 2: `save_signals` — always write a status-bearing snapshot

**Files:**
- Modify: `brief.py` — replace the body of `save_signals` (currently ~`brief.py:769-789`).

Signature gains defaulted params (`status="ok"`, `dropped=0`), so the existing
`mode_collect` call `save_signals(signals, today)` keeps working until Task 3.

- [ ] **Step 1: Write the failing verification**

```python
# (after harness preamble; SIGNALS_DIR is redirected under ./_t/signals)
import json, glob
save = ns["save_signals"]

# Empty list still writes a snapshot, with status, and does NOT touch the rolling log
save([], "2026-05-31", status="parse_error", dropped=0)
snap = json.loads(open("_t/signals/signals-2026-05-31.json").read())
assert snap["status"] == "parse_error" and snap["signals"] == [] and snap["dropped"] == 0, snap
assert not glob.glob("_t/signals/signals-log.jsonl"), "empty day must not append to rolling log"

# Non-empty writes snapshot AND appends rolling log
save([{"ticker": "X", "topic": "t", "direction": "bullish", "confidence": "high",
       "thesis_ref": None, "rationale": "r", "provenance": "p"}], "2026-06-01", status="ok", dropped=2)
snap2 = json.loads(open("_t/signals/signals-2026-06-01.json").read())
assert snap2["status"] == "ok" and snap2["dropped"] == 2 and len(snap2["signals"]) == 1, snap2
log_lines = open("_t/signals/signals-log.jsonl").read().strip().splitlines()
assert len(log_lines) == 1 and json.loads(log_lines[0])["date"] == "2026-06-01", log_lines
print("TASK2_PASS")
```

- [ ] **Step 2: Run to verify it fails**

Run the harness + Step 1 assertions.
Expected: `FileNotFoundError` opening `signals-2026-05-31.json` (current `save_signals` early-returns on empty list, writing nothing).

- [ ] **Step 3: Implement the new `save_signals`**

Replace the whole function with:

```python
def save_signals(signals: list, date_str: str, status: str = "ok", dropped: int = 0):
    """Persist signals as a dated snapshot and append to the rolling log.

    The dated snapshot is ALWAYS written (even for an empty signals list) so a quiet
    day is distinguishable from a missing run; `status` records whether the signals
    block parsed cleanly ('ok' | 'parse_error' | 'no_marker') and `dropped` how many
    malformed signals were discarded. The rolling log is appended only when signals exist.
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "dropped": dropped,
        "signals": signals,
    }
    (SIGNALS_DIR / f"signals-{date_str}.json").write_text(json.dumps(snapshot, indent=2))

    if signals:
        rolling = SIGNALS_DIR / "signals-log.jsonl"
        with rolling.open("a") as f:
            for s in signals:
                f.write(json.dumps({**s, "date": date_str}) + "\n")

    log.info(f"Saved {len(signals)} signals for {date_str} (status={status}, dropped={dropped})")
```

- [ ] **Step 4: Run the Step 1 verification to confirm it passes**

Run harness + assertions + `shutil.rmtree("_t", ignore_errors=True)`.
Expected: prints `TASK2_PASS`, exit 0.

- [ ] **Step 5: Compile check**

Run: `python -m py_compile brief.py && echo COMPILE_OK`
Expected: `COMPILE_OK`

- [ ] **Step 6: Commit** (if authorised)

```bash
git add brief.py
git commit -m "feat(signals): always write a status-bearing daily snapshot"
```

---

### Task 3: `split_brief_and_signals` reports status + wire the flow into `mode_collect`

**Files:**
- Modify: `brief.py` — `split_brief_and_signals` (~`brief.py:749-766`) and `mode_collect` (~`brief.py:985-1006`).

- [ ] **Step 1: Write the failing verification**

```python
# (after harness preamble)
split = ns["split_brief_and_signals"]

prose, sigs, status = split('Brief body.\n---SIGNALS---\n[{"topic":"t","direction":"bullish","confidence":"high"}]')
assert status == "ok" and len(sigs) == 1 and prose == "Brief body.", (status, sigs, prose)

prose2, sigs2, status2 = split("Brief only, model forgot the block.")
assert status2 == "no_marker" and sigs2 == [] and prose2 == "Brief only, model forgot the block.", (status2,)

prose3, sigs3, status3 = split("Body.\n---SIGNALS---\nnot json at all")
assert status3 == "parse_error" and sigs3 == [], (status3, sigs3)

prose4, sigs4, status4 = split("Body.\n---SIGNALS---\n[]")
assert status4 == "ok" and sigs4 == [], "genuine empty array is ok, not parse_error"
print("TASK3_PASS")
```

- [ ] **Step 2: Run to verify it fails**

Run harness + assertions.
Expected: `ValueError: not enough values to unpack (expected 3, got 2)` (current function returns a 2-tuple).

- [ ] **Step 3: Implement the status-returning `split_brief_and_signals`**

Replace the whole function with:

```python
def split_brief_and_signals(raw: str) -> tuple[str, list, str]:
    """Separate the prose brief from the trailing ---SIGNALS--- JSON block.

    Returns (prose, raw_signals, status) where status is one of:
      "ok"          — marker present and a JSON array parsed (the array may be empty)
      "parse_error" — marker present but no parseable JSON array followed it
      "no_marker"   — the ---SIGNALS--- marker was absent entirely (model format failure)
    """
    marker = "---SIGNALS---"
    if marker not in raw:
        return raw.strip(), [], "no_marker"
    prose, _, signal_part = raw.partition(marker)
    match = re.search(r"\[.*\]", signal_part, re.DOTALL)
    if not match:
        return prose.strip(), [], "parse_error"
    try:
        signals = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("Could not parse signals JSON; delivering brief without signals")
        return prose.strip(), [], "parse_error"
    if not isinstance(signals, list):
        return prose.strip(), [], "parse_error"
    return prose.strip(), signals, "ok"
```

- [ ] **Step 4: Update `mode_collect` to drive split → normalize → save**

In `mode_collect`, replace this block:

```python
    raw = poll_batch(batch_id)
    if raw:
        brief, signals = split_brief_and_signals(raw)
        deliver(
            brief,
            header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
            archive_path=BRIEFS_DIR / f"brief-{today}.md",
        )
        save_signals(signals, today)
        clear_batch_state()
    else:
        log.error("Could not retrieve brief — will retry next collect run")
```

with:

```python
    raw = poll_batch(batch_id)
    if raw:
        brief, raw_signals, status = split_brief_and_signals(raw)
        signals, dropped = normalize_signals(raw_signals)
        if dropped or status != "ok":
            log.warning(f"Signals: status={status}, dropped={dropped}")
        deliver(
            brief,
            header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
            archive_path=BRIEFS_DIR / f"brief-{today}.md",
        )
        save_signals(signals, today, status=status, dropped=dropped)
        clear_batch_state()
    else:
        log.error("Could not retrieve brief — will retry next collect run")
```

- [ ] **Step 5: Run the Step 1 verification to confirm it passes**

Run harness + Step 1 assertions + `shutil.rmtree("_t", ignore_errors=True)`.
Expected: prints `TASK3_PASS`, exit 0.

- [ ] **Step 6: End-to-end check (split → normalize → save) + compile**

```bash
python - <<'PY'
import sys, types, os, json, shutil
for name in ("feedparser", "requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py","r",encoding="utf-8").read()
src = src.replace('"/app/logs/newsbrief.log"','"_t/newsbrief.log"').replace('Path("/app/logs/','Path("./_t/')
os.makedirs("_t", exist_ok=True)
ns = {}; exec(compile(src,"brief.py","exec"), ns)
raw = 'Body.\n---SIGNALS---\n[{"ticker":"SHEL","topic":"hormuz","direction":"long","confidence":"med","rationale":"r","provenance":"p"},{"topic":"x","direction":"bad","confidence":"high"}]'
prose, rs, status = ns["split_brief_and_signals"](raw)
sig, dropped = ns["normalize_signals"](rs)
ns["save_signals"](sig, "2026-05-31", status=status, dropped=dropped)
snap = json.loads(open("_t/signals/signals-2026-05-31.json").read())
assert snap["status"]=="ok" and snap["dropped"]==1 and len(snap["signals"])==1, snap
assert snap["signals"][0]["direction"]=="bullish" and snap["signals"][0]["confidence"]=="medium", snap
shutil.rmtree("_t", ignore_errors=True)
print("E2E_PASS")
PY
python -m py_compile brief.py && echo COMPILE_OK
```
Expected: `E2E_PASS` then `COMPILE_OK`.

- [ ] **Step 7: Commit** (if authorised)

```bash
git add brief.py
git commit -m "feat(signals): report parse status and wire normalize into collect"
```

---

### Task 4: Make `mode_run` mirror `mode_collect`

**Files:**
- Modify: `brief.py` — `mode_run` (~`brief.py:1043-1058`).

Currently `mode_run` delivers the raw model output, leaking the `---SIGNALS---` JSON into
Telegram and saving no signals. Make the testing path exercise the real pipeline.

- [ ] **Step 1: Implement the change**

In `mode_run`, replace this block:

```python
    if batch_id:
        brief = poll_batch(batch_id, max_wait_secs=3600)
        if brief:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            deliver(
                brief,
                header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
                archive_path=BRIEFS_DIR / f"brief-{today}.md",
            )
            clear_batch_state()
```

with:

```python
    if batch_id:
        raw = poll_batch(batch_id, max_wait_secs=3600)
        if raw:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            brief, raw_signals, status = split_brief_and_signals(raw)
            signals, dropped = normalize_signals(raw_signals)
            if dropped or status != "ok":
                log.warning(f"Signals: status={status}, dropped={dropped}")
            deliver(
                brief,
                header=f"🌐 <b>Morning Brief — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>",
                archive_path=BRIEFS_DIR / f"brief-{today}.md",
            )
            save_signals(signals, today, status=status, dropped=dropped)
            clear_batch_state()
```

- [ ] **Step 2: Verify no raw signals leak into the delivered prose**

```bash
python - <<'PY'
import sys, types, os, re
for name in ("feedparser", "requests"):
    sys.modules[name] = types.ModuleType(name)
os.environ.update(ANTHROPIC_API_KEY="x", TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="x")
src = open("brief.py","r",encoding="utf-8").read()
# Confirm both modes call the same three functions
collect = re.search(r"def mode_collect.*?(?=\ndef )", src, re.DOTALL).group(0)
run     = re.search(r"def mode_run.*?(?=\ndef )", src, re.DOTALL).group(0)
for name, body in (("collect", collect), ("run", run)):
    assert "split_brief_and_signals" in body and "normalize_signals" in body and "save_signals" in body, name
print("MODE_PARITY_PASS")
PY
python -m py_compile brief.py && echo COMPILE_OK
```
Expected: `MODE_PARITY_PASS` then `COMPILE_OK`.

- [ ] **Step 3: Commit** (if authorised)

```bash
git add brief.py
git commit -m "feat(signals): make mode_run exercise the signals pipeline like collect"
```

---

## Notes for the implementer

- **Privacy boundary:** none of these changes touch Trading212 data. Signals carry only tickers + enums + free-text rationale; no absolute monetary value enters any signal, snapshot, or log. Do not add fields that could carry amounts.
- **Commits:** the user chose "stay on main, commit when asked." Treat every commit step as gated on explicit user authorisation; otherwise implement and leave staged/unstaged per their direction.
- **Formatter:** a PostToolUse formatter hook reformats `brief.py` after edits. Re-read regions before subsequent edits if line numbers shift.
- **`mode_paper` is still unreachable** (not in the dispatch dict) and `get_price` is still a stub — both belong to the *paper tracker* work, which is a separate design and explicitly out of scope here.

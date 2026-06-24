# Brief Claim-Memory (anti-repetition) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the daily brief re-explaining facts the reader already holds, by (A) feeding yesterday's brief whole instead of truncated, and (B) maintaining a model-maintained "standing-claim" ledger injected into the prompt and reconciled after each brief.

**Architecture:** A new top-level module `brief_memory.py` holds a JSON ledger of durable facts (on the deploy-host volume). At `submit`, the active ledger renders into the prompt as an "ESTABLISHED" block; at `collect`, a small synchronous Claude call reconciles the ledger against the just-generated brief. Pure logic (merge, render, parse) is CI-tested; the one network call is a fail-safe shell that returns the prior ledger on any error.

**Tech Stack:** Python 3 stdlib, `requests` (raw Anthropic HTTP, already used by `brief.py`), pytest. No new dependencies.

## Global Constraints

- **Commit through the Bash tool, never PowerShell** — PowerShell prepends a UTF-8 BOM to the commit subject. Use `git commit -F -` with a single-quoted heredoc.
- **Formatter owns style:** ruff reflows on save. After edits, run `ruff check` + `ruff format` and stage every reformatted file, or CI fails.
- **Pre-push gate:** `ruff check brief.py common.py trading.py brief_memory.py enrichment tests` + `ruff format --check ...` + `pytest`. All three must pass.
- **Run Python via the PowerShell tool** (the Bash tool errors `stdin is not a tty`); PowerShell wraps Python stderr/logging as a scary `NativeCommandError` even on success — that is not a failure.
- **New first-party module must be added to the Dockerfile COPY allowlist AND the CI workflow paths** (Task 7) or it ModuleNotFounds at runtime despite green CI.
- **State lives on the deploy-host volume** (`DATA_DIR`, default `/app/logs`), never the dev repo. `brief_memory.json` is runtime state — do not commit it.
- **Claim dict shape (canonical):** `{"id": str, "claim": str, "topic": str, "first_seen": "YYYY-MM-DD", "last_reaffirmed": "YYYY-MM-DD", "restate_count": int}`.
- **Model-returned claim shape (from reconcile):** `{"claim": str, "topic": str, "id"?: str}` — `id` present iff the model is reaffirming an existing claim.

---

## File Structure

- **Create `brief_memory.py`** (repo root, sibling of `brief.py`) — flag, ledger I/O, pure merge/render/parse, fail-safe reconcile shell, raw-HTTP Claude call.
- **Create `tests/test_brief_memory.py`** — CI-safe unit tests (pure functions + stubbed `call`).
- **Modify `brief.py`** — Part A (un-truncate yesterday + strengthen instruction); Part B wiring (`established_block` param + `mode_submit`/`mode_collect` hooks + import).
- **Modify `Dockerfile`** and **`.github/workflows/docker-publish.yml`** — add the new module to COPY/paths/ruff lists.

---

### Task 1: Module scaffold — flag, ledger model, load/save

**Files:**
- Create: `brief_memory.py`
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `common.DATA_DIR`, `common.log`, `common._write_json_atomic`.
- Produces: `is_enabled() -> bool`; `empty_ledger() -> dict`; `load_ledger(path: Path = BRIEF_MEMORY_FILE) -> dict`; `save_ledger(ledger: dict, path: Path = BRIEF_MEMORY_FILE) -> None`; constants `BRIEF_MEMORY_FILE`, `MAX_CLAIMS = 25`, `RETIRE_AFTER_DAYS = 7`, `RECONCILE_MODEL = "claude-haiku-4-5-20251001"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brief_memory.py
import json
from pathlib import Path

import brief_memory as bm


def test_is_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("BRIEF_MEMORY_ENABLED", raising=False)
    assert bm.is_enabled() is False
    monkeypatch.setenv("BRIEF_MEMORY_ENABLED", "1")
    assert bm.is_enabled() is True


def test_empty_ledger_shape():
    assert bm.empty_ledger() == {"version": 1, "claims": []}


def test_load_missing_returns_empty(tmp_path):
    assert bm.load_ledger(tmp_path / "nope.json") == {"version": 1, "claims": []}


def test_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert bm.load_ledger(p) == {"version": 1, "claims": []}


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "ledger.json"
    ledger = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "BOJ at 1.0% since 2026-06-16",
                "topic": "japan",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 7,
            }
        ],
    }
    bm.save_ledger(ledger, p)
    assert bm.load_ledger(p) == ledger
    assert json.loads(p.read_text(encoding="utf-8")) == ledger
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'brief_memory'`.

- [ ] **Step 3: Write minimal implementation**

```python
# brief_memory.py
"""Standing-claim ledger: gives the daily brief multi-day memory of facts it has
already established, so it stops re-explaining them. DESCRIPTIVE only — never
affects trading. Flag-gated by BRIEF_MEMORY_ENABLED; fail-safe (any error leaves
the brief unaffected and the prior ledger intact)."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests

from common import ANTHROPIC_HEADERS, DATA_DIR, _write_json_atomic, log

BRIEF_MEMORY_FILE = DATA_DIR / "brief_memory.json"
MAX_CLAIMS = 25
RETIRE_AFTER_DAYS = 7
RECONCILE_MODEL = "claude-haiku-4-5-20251001"


def is_enabled() -> bool:
    return os.environ.get("BRIEF_MEMORY_ENABLED", "0") == "1"


def empty_ledger() -> dict:
    return {"version": 1, "claims": []}


def load_ledger(path: Path = BRIEF_MEMORY_FILE) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("claims"), list):
                return data
        return empty_ledger()
    except Exception as e:
        log.warning(f"Brief-memory ledger unreadable ({path}); starting empty: {e}")
        return empty_ledger()


def save_ledger(ledger: dict, path: Path = BRIEF_MEMORY_FILE) -> None:
    _write_json_atomic(path, ledger)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief-memory): module scaffold — flag + ledger load/save

New top-level brief_memory.py: BRIEF_MEMORY_ENABLED flag, empty/load/save
of the standing-claim ledger JSON (fail-safe: missing/corrupt -> empty).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: `merge_ledger` — id-stamping, retirement, cap

**Files:**
- Modify: `brief_memory.py`
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: claim dicts (Global Constraints), `RETIRE_AFTER_DAYS`, `MAX_CLAIMS`.
- Produces: `merge_ledger(prior: dict, model_claims: list[dict], today: str, *, cap: int = MAX_CLAIMS, retire_after_days: int = RETIRE_AFTER_DAYS) -> dict`. Reaffirmed (id echoed) → carry `first_seen`, set `last_reaffirmed=today`, `restate_count+=1`, accept reworded text; new (no id) → next `c-NNNN`, dates=today, count=1; prior id absent from return → untouched; then drop claims older than `retire_after_days`; cap to most-recently-reaffirmed.

- [ ] **Step 1: Write the failing test**

```python
def test_merge_reaffirm_carries_first_seen_and_increments():
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
            }
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "BOJ still at 1.0% (Himino: more to come)", "topic": "japan"}],
        "2026-06-24",
    )
    c = out["claims"][0]
    assert c["id"] == "c-0001"
    assert c["first_seen"] == "2026-06-18"
    assert c["last_reaffirmed"] == "2026-06-24"
    assert c["restate_count"] == 6
    assert c["claim"] == "BOJ still at 1.0% (Himino: more to come)"


def test_merge_new_claim_gets_next_id_and_today():
    prior = {
        "version": 1,
        "claims": [
            {"id": "c-0003", "claim": "x", "topic": "a",
             "first_seen": "2026-06-24", "last_reaffirmed": "2026-06-24", "restate_count": 1}
        ],
    }
    out = bm.merge_ledger(prior, [{"claim": "China may miss 4.5-5% growth", "topic": "china"}], "2026-06-24")
    new = [c for c in out["claims"] if c["claim"].startswith("China")][0]
    assert new["id"] == "c-0004"
    assert new["first_seen"] == new["last_reaffirmed"] == "2026-06-24"
    assert new["restate_count"] == 1


def test_merge_unreturned_prior_claim_is_kept():
    prior = {
        "version": 1,
        "claims": [
            {"id": "c-0001", "claim": "kept", "topic": "a",
             "first_seen": "2026-06-22", "last_reaffirmed": "2026-06-23", "restate_count": 2}
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert [c["id"] for c in out["claims"]] == ["c-0001"]
    assert out["claims"][0]["last_reaffirmed"] == "2026-06-23"  # untouched


def test_merge_retires_stale_claims():
    prior = {
        "version": 1,
        "claims": [
            {"id": "c-0001", "claim": "old", "topic": "a",
             "first_seen": "2026-06-01", "last_reaffirmed": "2026-06-10", "restate_count": 1}
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")  # 14 days > 7
    assert out["claims"] == []


def test_merge_caps_to_most_recent():
    claims = [
        {"id": f"c-{i:04d}", "claim": str(i), "topic": "a",
         "first_seen": "2026-06-24", "last_reaffirmed": f"2026-06-{10 + i:02d}", "restate_count": 1}
        for i in range(1, 6)
    ]
    prior = {"version": 1, "claims": claims}
    out = bm.merge_ledger(prior, [], "2026-06-24", cap=2, retire_after_days=999)
    kept = [c["last_reaffirmed"] for c in out["claims"]]
    assert kept == ["2026-06-15", "2026-06-14"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -k merge -v`
Expected: FAIL with `AttributeError: module 'brief_memory' has no attribute 'merge_ledger'`.

- [ ] **Step 3: Write minimal implementation**

Append to `brief_memory.py`:

```python
def _max_id_num(ledger: dict) -> int:
    nums = []
    for c in ledger.get("claims", []):
        m = re.match(r"c-(\d+)$", str(c.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0)


def _days_between(d_old: str, d_new: str) -> int:
    try:
        a = datetime.strptime(d_old, "%Y-%m-%d")
        b = datetime.strptime(d_new, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return 0  # unparseable date -> never retire on this basis


def merge_ledger(
    prior: dict,
    model_claims: list[dict],
    today: str,
    *,
    cap: int = MAX_CLAIMS,
    retire_after_days: int = RETIRE_AFTER_DAYS,
) -> dict:
    by_id = {c["id"]: c for c in prior.get("claims", []) if "id" in c}
    next_num = _max_id_num(prior) + 1
    returned = set()
    result = []
    for mc in model_claims:
        cid = mc.get("id")
        if cid and cid in by_id:
            base = dict(by_id[cid])
            base["claim"] = mc.get("claim", base.get("claim", ""))
            base["topic"] = mc.get("topic", base.get("topic", ""))
            base["last_reaffirmed"] = today
            base["restate_count"] = base.get("restate_count", 0) + 1
            result.append(base)
            returned.add(cid)
        elif mc.get("claim"):
            result.append({
                "id": f"c-{next_num:04d}",
                "claim": mc["claim"],
                "topic": mc.get("topic", ""),
                "first_seen": today,
                "last_reaffirmed": today,
                "restate_count": 1,
            })
            next_num += 1
    for c in prior.get("claims", []):
        if c.get("id") not in returned:
            result.append(dict(c))
    result = [c for c in result if _days_between(c["last_reaffirmed"], today) <= retire_after_days]
    result.sort(key=lambda c: c["last_reaffirmed"], reverse=True)
    return {"version": 1, "claims": result[:cap]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py -k merge -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief-memory): merge_ledger — id-stamp, retire, cap

Deterministic reconciliation by id (no fuzzy text matching): reaffirmed
claims carry first_seen + increment, new claims get the next c-NNNN, stale
claims (>RETIRE_AFTER_DAYS) drop, ledger capped to most-recently-reaffirmed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Render + prompt + parse (pure)

**Files:**
- Modify: `brief_memory.py`
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Produces: `render_established_block(ledger: dict) -> str` (empty string when no claims); `build_reconcile_prompt(ledger: dict, brief_text: str) -> str`; `parse_reconcile_response(text: str) -> list[dict]` (raises `ValueError` when no JSON array; filters to items with a `claim`, keeping only `id`/`claim`/`topic`).

- [ ] **Step 1: Write the failing test**

```python
def test_render_empty_ledger_is_blank():
    assert bm.render_established_block({"version": 1, "claims": []}) == ""


def test_render_lists_claims_with_instruction():
    ledger = {"version": 1, "claims": [
        {"id": "c-0001", "claim": "BOJ at 1.0% since 2026-06-16", "topic": "japan",
         "first_seen": "2026-06-18", "last_reaffirmed": "2026-06-24", "restate_count": 7},
    ]}
    block = bm.render_established_block(ledger)
    assert "ESTABLISHED" in block
    assert "BOJ at 1.0% since 2026-06-16" in block
    assert "japan" in block
    assert "one clause" in block.lower()


def test_build_reconcile_prompt_contains_ledger_and_brief():
    ledger = {"version": 1, "claims": [
        {"id": "c-0001", "claim": "BOJ at 1.0%", "topic": "japan",
         "first_seen": "2026-06-18", "last_reaffirmed": "2026-06-24", "restate_count": 7}]}
    p = bm.build_reconcile_prompt(ledger, "Today the BOJ left rates unchanged.")
    assert "c-0001" in p
    assert "Today the BOJ left rates unchanged." in p


def test_parse_extracts_array_and_filters():
    text = 'Here you go:\n[{"id":"c-0001","claim":"x","topic":"a"},{"claim":"y"},{"topic":"no-claim"}]'
    out = bm.parse_reconcile_response(text)
    assert out == [{"id": "c-0001", "claim": "x", "topic": "a"}, {"claim": "y"}]


def test_parse_raises_without_array():
    import pytest
    with pytest.raises(ValueError):
        bm.parse_reconcile_response("no array here")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -k "render or reconcile_prompt or parse" -v`
Expected: FAIL with `AttributeError` on `render_established_block`.

- [ ] **Step 3: Write minimal implementation**

Append to `brief_memory.py`:

```python
_RECONCILE_SYSTEM = (
    "You maintain a compact memory of durable facts a daily market brief has "
    "already told its reader, so tomorrow's brief stops re-explaining them."
)

_RECONCILE_TEMPLATE = """Below is the CURRENT memory (JSON) and TODAY'S BRIEF.

Return ONLY a JSON array of the durable facts the reader now knows after today's
brief. Rules:
- A durable fact is something that should NOT be re-explained tomorrow unless it
  materially changes: one-time events already reported (e.g. a rate hike), and
  standing analytical frames/theses. NOT ephemeral daily price moves.
- For a fact already in CURRENT memory that is still relevant, include it and
  ECHO its existing "id". You may reword its "claim" if today refined it.
- For a genuinely NEW durable fact, include it with NO "id".
- Omit facts that are no longer relevant.
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>"}}.
Output the JSON array and nothing else.

CURRENT memory:
{current}

TODAY'S BRIEF:
{brief}
"""


def render_established_block(ledger: dict) -> str:
    claims = ledger.get("claims", [])
    if not claims:
        return ""
    lines = "\n".join(
        f"  • [{c.get('topic') or 'general'}] {c['claim']}" for c in claims
    )
    return (
        "## ESTABLISHED — THE READER ALREADY KNOWS THESE\n"
        "Reference each in at most one clause, and only if still relevant. Do NOT "
        "re-explain or restate them as news. Lead every section with what has "
        "CHANGED since.\n\n" + lines + "\n"
    )


def build_reconcile_prompt(ledger: dict, brief_text: str) -> str:
    current = json.dumps(ledger.get("claims", []), indent=2)
    return _RECONCILE_TEMPLATE.format(current=current, brief=brief_text)


def parse_reconcile_response(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"no JSON array in reconcile response: {text[:200]!r}")
    data = json.loads(m.group())
    if not isinstance(data, list):
        raise ValueError("reconcile response is not a JSON list")
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("claim"):
            out.append({k: item[k] for k in ("id", "claim", "topic") if k in item})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py -k "render or reconcile_prompt or parse" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief-memory): render block + reconcile prompt/parse (pure)

render_established_block (the ESTABLISHED prompt section), build_reconcile_prompt
(memory + brief -> instruction), parse_reconcile_response (tolerant JSON-array
extraction, id/claim/topic only).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: `reconcile_ledger` fail-safe shell + raw-HTTP call

**Files:**
- Modify: `brief_memory.py`
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `build_reconcile_prompt`, `parse_reconcile_response`, `merge_ledger`, `common.ANTHROPIC_HEADERS`, `requests`, `RECONCILE_MODEL`.
- Produces: `reconcile_ledger(prior: dict, brief_text: str, today: str, *, call=None) -> dict` (on any exception returns `prior` unchanged); `_messages_call(system: str, user: str) -> str` (raw sync POST to `/v1/messages`; not exercised in CI).

- [ ] **Step 1: Write the failing test**

```python
def test_reconcile_success_merges(monkeypatch):
    prior = {"version": 1, "claims": []}

    def fake_call(system, user):
        return '[{"claim":"BOJ at 1.0% since 2026-06-16","topic":"japan"}]'

    out = bm.reconcile_ledger(prior, "BOJ held rates.", "2026-06-24", call=fake_call)
    assert out["claims"][0]["claim"] == "BOJ at 1.0% since 2026-06-16"
    assert out["claims"][0]["id"] == "c-0001"


def test_reconcile_failure_returns_prior_unchanged():
    prior = {"version": 1, "claims": [
        {"id": "c-0001", "claim": "x", "topic": "a",
         "first_seen": "2026-06-24", "last_reaffirmed": "2026-06-24", "restate_count": 1}]}

    def boom(system, user):
        raise RuntimeError("network down")

    out = bm.reconcile_ledger(prior, "brief", "2026-06-25", call=boom)
    assert out is prior  # untouched, memory never lost


def test_reconcile_bad_json_returns_prior():
    prior = {"version": 1, "claims": []}
    out = bm.reconcile_ledger(prior, "brief", "2026-06-24", call=lambda s, u: "garbage")
    assert out == prior
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -k reconcile_success -v`
Expected: FAIL with `AttributeError` on `reconcile_ledger`.

- [ ] **Step 3: Write minimal implementation**

Append to `brief_memory.py`:

```python
def _messages_call(system: str, user: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": RECONCILE_MODEL,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    blocks = resp.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def reconcile_ledger(prior: dict, brief_text: str, today: str, *, call=None) -> dict:
    caller = call or _messages_call
    try:
        text = caller(_RECONCILE_SYSTEM, build_reconcile_prompt(prior, brief_text))
        return merge_ledger(prior, parse_reconcile_response(text), today)
    except Exception as e:
        log.warning(f"Brief-memory reconcile failed; keeping prior ledger: {e}")
        return prior
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add brief_memory.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief-memory): fail-safe reconcile_ledger + raw-HTTP Claude call

reconcile_ledger builds the prompt, calls Claude (injectable for tests),
parses and merges; ANY error returns the prior ledger unchanged so memory is
never lost and the brief is never affected. _messages_call mirrors brief.py's
raw Anthropic HTTP style (Haiku 4.5, sync /v1/messages).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Part A — un-truncate yesterday + strengthen instruction

**Files:**
- Modify: `brief.py:1469-1474` (the `yesterday_block` f-string)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: existing `build_daily_prompt(...)` signature (unchanged in this task).
- Produces: yesterday's brief now fed up to 6000 chars (was 2000); instruction covers standing frames.

- [ ] **Step 1: Write the failing test**

```python
def test_part_a_feeds_back_beyond_2000_chars():
    import brief
    fb = {}
    yesterday = "HEAD " + ("x" * 2800) + " MARKER-3000 " + ("y" * 200)
    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", yesterday, "", fb, "", "", "", ""
    )
    assert "MARKER-3000" in prompt  # was truncated away at [:2000]


def test_part_a_instruction_mentions_standing_frames():
    import brief
    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", "yesterday text", "", {}, "", "", "", ""
    )
    assert "standing analytical frames" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -k part_a -v`
Expected: FAIL — `MARKER-3000` absent (truncated at 2000) and the new instruction string missing.

- [ ] **Step 3: Make the change**

In `brief.py`, replace the `yesterday_block` assignment (currently lines 1469-1474):

```python
        yesterday_block = f"""
## YESTERDAY'S BRIEF
For any section where the situation is materially unchanged from yesterday, replace the paragraph with a single sentence: "No significant change — [one-line summary]." This applies to standing analytical frames too — named theses, recurring podcast framings, and one-time events already reported: state them in at most one clause and never re-explain them. Only write a full paragraph when something new or materially different has occurred.

{yesterday_brief[:6000]}
"""
```

(Only two substantive changes: the added "standing analytical frames" sentence, and `[:2000]` → `[:6000]`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_brief_memory.py -k part_a -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): feed yesterday whole + cover standing frames (Part A)

yesterday_block was truncated to 2000 chars (~TOP STORIES only), blinding the
model to the back-half sections where standing facts repeat daily. Feed up to
6000 chars and extend the no-change instruction to standing analytical frames.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Part B wiring — prompt param + submit/collect hooks

**Files:**
- Modify: `brief.py` (import block ~84-92; `build_daily_prompt` signature 1443 + body 1496 + return f-string 1524; `mode_submit` ~2009 + call ~2049; `mode_collect` ~2099)
- Test: `tests/test_brief_memory.py`

**Interfaces:**
- Consumes: `brief_memory.is_enabled`, `load_ledger`, `save_ledger`, `render_established_block`, `reconcile_ledger`.
- Produces: `build_daily_prompt(..., enrichment_block: str = "", established_block: str = "")` — the established block renders into the prompt; `mode_submit` injects the active ledger, `mode_collect` reconciles after delivery.

- [ ] **Step 1: Write the failing test**

```python
def test_established_block_injected_into_prompt():
    import brief
    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", "y", "", {}, "", "", "", "",
        established_block="## ESTABLISHED — THE READER ALREADY KNOWS THESE\n  • [japan] BOJ at 1.0%",
    )
    assert "BOJ at 1.0%" in prompt
    assert "ESTABLISHED" in prompt


def test_no_established_block_when_empty():
    import brief
    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", "y", "", {}, "", "", "", "", established_block=""
    )
    assert "ESTABLISHED — THE READER ALREADY KNOWS" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_brief_memory.py -k established_block -v`
Expected: FAIL with `TypeError: build_daily_prompt() got an unexpected keyword argument 'established_block'`.

- [ ] **Step 3: Make the changes**

(a) Add the import after the `enrichment` import block (`brief.py` ~line 92):

```python
from brief_memory import (
    is_enabled as brief_memory_enabled,
    load_ledger,
    reconcile_ledger,
    render_established_block,
    save_ledger,
)
```

(b) Add the parameter to `build_daily_prompt` (after `enrichment_block: str = "",`, line 1443):

```python
    enrichment_block: str = "",
    established_block: str = "",
```

(c) After the `enrichment_section` line (1496), add:

```python
    established_section = f"\n{established_block}\n" if established_block else ""
```

(d) In the return f-string (line 1524), inject `{established_section}` immediately before `{yesterday_block}`:

```python
{established_section}{yesterday_block}{weekly_block}{portfolio_block}{enrichment_section}
```

(e) In `mode_submit`, after `yesterday_brief = load_yesterday_brief()` (line 2009), add:

```python
    established_block = (
        render_established_block(load_ledger()) if brief_memory_enabled() else ""
    )
```

and add `established_block` as the final argument to the `build_daily_prompt(...)` call (after `enrichment_block,` at line 2059):

```python
        enrichment_block,
        established_block,
    )
```

(f) In `mode_collect`, after `save_signals(signals, today, status=status, dropped=dropped)` (line 2099), add:

```python
        if brief_memory_enabled():
            try:
                save_ledger(reconcile_ledger(load_ledger(), brief, today))
            except Exception as e:
                log.error(f"Brief-memory reconcile skipped (brief unaffected): {e}")
```

- [ ] **Step 4: Run tests + full suite**

Run: `python -m pytest tests/test_brief_memory.py -v && python -m pytest -q`
Expected: PASS (new tests green; full suite still green).

- [ ] **Step 5: Commit**

```bash
git add brief.py tests/test_brief_memory.py
git commit -F - <<'EOF'
feat(brief): wire standing-claim memory into submit/collect (Part B)

build_daily_prompt gains an established_block param rendered into the prompt;
mode_submit injects the active ledger (flag-gated), mode_collect reconciles it
against the delivered brief and saves. Reconcile is isolated: a failure logs and
leaves the already-delivered brief untouched.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 7: Deployment — Dockerfile + CI workflow

**Files:**
- Modify: `Dockerfile:13`
- Modify: `.github/workflows/docker-publish.yml` (paths ~7-11; ruff lines 47-48)

**Interfaces:** none (build/CI config). Verified by lint + a clean import.

- [ ] **Step 1: Add the module to the Docker COPY allowlist**

In `Dockerfile`, line 13, add `brief_memory.py`:

```dockerfile
COPY common.py trading.py validation.py brief.py brief_memory.py .
```

- [ ] **Step 2: Add to CI trigger paths and ruff targets**

In `.github/workflows/docker-publish.yml`, under `paths:` (after `- 'brief.py'`):

```yaml
      - 'brief_memory.py'
```

And in both ruff lines (47-48), add `brief_memory.py` to the file list:

```yaml
          ruff check brief.py brief_memory.py common.py trading.py enrichment tests
          ruff format --check brief.py brief_memory.py common.py trading.py enrichment tests
```

- [ ] **Step 3: Verify lint + import + full suite**

Run:
```
ruff check brief.py brief_memory.py common.py trading.py enrichment tests
ruff format --check brief.py brief_memory.py common.py trading.py enrichment tests
python -c "import brief_memory, brief"
python -m pytest -q
```
Expected: ruff clean, import OK, full suite PASS. (If ruff reformats, stage the reformatted files.)

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .github/workflows/docker-publish.yml
git commit -F - <<'EOF'
build(brief-memory): add brief_memory.py to Docker COPY + CI

New first-party module must be in the COPY allowlist and the workflow paths/ruff
targets, or it ModuleNotFounds at runtime despite green CI.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Rollout note (post-implementation, not a code task)

`BRIEF_MEMORY_ENABLED` defaults **off**. Part A ships live immediately (unconditional). To enable Part B on the deploy host: set `BRIEF_MEMORY_ENABLED=1`, let one `collect` run write `brief_memory.json` on the volume, eyeball that file plus the next brief, then leave it on. Manual reset = delete `brief_memory.json` (it self-rebuilds). The ledger is runtime state on the volume — never commit it.

---

## Self-Review

**Spec coverage** (against `2026-06-24-brief-claim-memory-design.md`):
- §4 Part A (un-truncate + instruction; trade-tail strip dropped as unnecessary — `deliver` archives the brief only, tags stripped, no trade tail) → Task 5. ✓
- §5.1 state shape + `id` → Global Constraints + Task 1/2. ✓
- §5.2 inject/reconcile/decay → Tasks 3 (render), 4 (reconcile), 2 (retire). ✓
- §5.3 guardrails (code-stamped dates, cap, schema, retire) → Task 2 + Task 1 (load validates shape). ✓
- §5.4 separate post-gen call, structured, Haiku → Task 4. ✓
- §7 error handling (missing/corrupt/call-fail/bad-JSON all fail-safe) → Tasks 1 + 4. ✓
- §8 CI-safe pure tests + injectable call → Tasks 1-6. ✓
- §9 Dockerfile/workflow + flag gating → Task 7 + wiring flag in Task 6. ✓
- §10 success criteria → rollout note (operational, not code). ✓

**Placeholder scan:** no TBD/TODO/"handle errors"/"similar to"; every code step shows complete code. ✓

**Type consistency:** `merge_ledger`, `reconcile_ledger`, `render_established_block`, `build_reconcile_prompt`, `parse_reconcile_response`, `load_ledger`, `save_ledger` signatures match between their defining task, the Interfaces blocks, and call sites in Task 6. Claim dict / model-claim shapes match Global Constraints throughout. ✓

**Note vs spec:** retirement implemented by **calendar days** (`RETIRE_AFTER_DAYS=7`) rather than literal "N briefs" — a faithful, more-robust realization (survives a skipped day); same default magnitude.

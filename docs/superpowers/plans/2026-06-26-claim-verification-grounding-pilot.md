# Claim-Verification Grounding Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a flag-gated, log-only Sonnet grounding check that measures how often the brief's TOP STORIES make claims unsupported by the source material we fed the model, accumulating shadow logs for a ~14-day pilot.

**Architecture:** A new top-level module `claim_verify.py` mirrors `brief_memory.py`'s shape (pure functions + an injectable model call + fail-safe orchestrator). Three touch-points: `mode_submit` persists a richer source-evidence blob; `mode_collect` (after `deliver()`) runs a forced-tool Sonnet check against it and writes `verification-{day}.json`; a `summarize_verifications` aggregator produces the decision-ready report. Everything is gated by `CLAIM_VERIFY_ENABLED` (default off) and never affects the delivered brief.

**Tech Stack:** Python 3, `requests` (Anthropic Messages API, forced tool use), `pytest`, `ruff`. No new dependencies.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-26-claim-verification-grounding-pilot-design.md`.
- **Fail-safe always:** no new step may raise out of `mode_submit`/`mode_collect`; the delivered brief is never delayed or altered. Every touch-point is wrapped in `try/except` that logs and continues.
- **Flag:** `CLAIM_VERIFY_ENABLED` env var; enabled only when `== "1"`. Default off. Independent of `BRIEF_MEMORY_ENABLED`.
- **Judge model:** `claude-sonnet-4-6`, exposed as `VERIFY_MODEL` (one-line swap). Timeout 90s + 1 retry (post-delivery latency is free — lesson `e255436`).
- **No external/web calls in the verify step** — grounds the brief only against persisted feed text.
- **Scope:** TOP STORIES section only.
- **License:** Pharos is AGPL-3.0 — idea reimplemented from concept, no code lifted. Do not copy any Pharos source.
- **Commit style:** conventional commits; commit via the **Bash tool** (PowerShell prepends a BOM to commit subjects). End commit messages with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- **Test gate (per `brief-local-run`):** `ruff check` + `ruff format --check` + `pytest` must all pass; stage every reformatted file or CI fails.

## File Structure

- **Create `claim_verify.py`** — the whole feature: flag, evidence builder/persistence, TOP STORIES extractor, grounding tool/request/parser, model caller, record assembly, fail-safe orchestrator, aggregator. One module, one responsibility (claim grounding), mirrors `brief_memory.py`.
- **Create `tests/test_claim_verify.py`** — unit tests for every pure function + fail-safe paths (model call injected via `call=`).
- **Modify `brief.py`** — import from `claim_verify`; add gated evidence persistence in `mode_submit`; add gated `run_verification` in `mode_collect`.
- **Modify `Dockerfile`** — add `claim_verify.py` to the `COPY` allowlist.
- **Modify `.github/workflows/docker-publish.yml`** — add `claim_verify.py` to the path trigger and both ruff file lists.

---

### Task 1: Module scaffold, flag, and source-evidence persistence

**Files:**
- Create: `claim_verify.py`
- Test: `tests/test_claim_verify.py`

**Interfaces:**
- Consumes: `common.DATA_DIR`, `common._write_json_atomic`, `common.log`.
- Produces:
  - `is_enabled() -> bool`
  - `build_source_evidence(feed_content: str, web_content: str) -> str`
  - `_evidence_path(day: str) -> Path`
  - `save_evidence(evidence: str, day: str) -> None`
  - `load_evidence(day: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claim_verify.py
import json

import claim_verify as cv


def test_build_source_evidence_keeps_summaries_and_labels():
    feed = (
        "### Reuters [wire] (WORLD)\n"
        "- Central bank holds rates (Mon)\n"
        "  The bank kept its policy rate unchanged at 4.5 percent.\n"
        "### AlJazeera [regional · arab · state-funded] (WORLD)\n"
        "- Talks resume in Geneva (Mon)\n"
        "  Negotiators returned to the table after a week-long pause.\n"
    )
    web = "### SomePage [regional] (ANALYSIS)\nA short meta description of the page.\n"
    out = cv.build_source_evidence(feed, web)
    assert "SOURCE: Reuters" in out
    assert "SOURCE: AlJazeera" in out
    assert "SOURCE: SomePage" in out
    # headline retained
    assert "- Central bank holds rates (Mon)" in out
    # summary retained (this is what build_source_index drops)
    assert "kept its policy rate unchanged at 4.5 percent" in out
    # web body retained
    assert "short meta description" in out


def test_build_source_evidence_caps_long_detail_lines():
    feed = "### X [wire] (WORLD)\n- A title (Mon)\n  " + ("z" * 900) + "\n"
    out = cv.build_source_evidence(feed, "")
    # each detail line capped at 400 chars
    detail = [ln for ln in out.splitlines() if ln.startswith("  ")][0]
    assert len(detail.strip()) == 400


def test_build_source_evidence_handles_empty_placeholders():
    out = cv.build_source_evidence("(no RSS content)", "(no web content)")
    assert "SOURCE:" not in out


def test_is_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("CLAIM_VERIFY_ENABLED", raising=False)
    assert cv.is_enabled() is False
    monkeypatch.setenv("CLAIM_VERIFY_ENABLED", "1")
    assert cv.is_enabled() is True


def test_save_and_load_evidence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: Reuters\n- A title (Mon)\n  detail", "2026-06-26")
    assert "SOURCE: Reuters" in cv.load_evidence("2026-06-26")


def test_load_evidence_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    assert cv.load_evidence("2026-06-26") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_verify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claim_verify'`.

- [ ] **Step 3: Write the module scaffold + evidence functions**

```python
# claim_verify.py
"""Claim-verification grounding pilot: a flag-gated, log-only check that measures
how often the daily brief's TOP STORIES make claims unsupported by the source
material we fed the model. SHADOW only — never affects the delivered brief or
trading. Gated by CLAIM_VERIFY_ENABLED; fail-safe (any error leaves the brief
unaffected). Idea reimplemented from the Pharos verify pattern (AGPL); no code lifted."""

import json
import os
import re
from pathlib import Path

import requests

from common import ANTHROPIC_HEADERS, DATA_DIR, _write_json_atomic, log

VERIFY_MODEL = "claude-sonnet-4-6"
VERIFY_TIMEOUT = 90  # generous: runs AFTER delivery, so latency is free (lesson e255436)
VERIFY_MAX_ATTEMPTS = 2
VERIFY_MAX_TOKENS = 4096
_DETAIL_CAP = 400  # per evidence detail line (fetch_rss already caps summaries at 400)

_VALID_VERDICTS = frozenset(
    {"supported", "unsupported", "contradicted", "overstated", "unverifiable"}
)
# Verdicts that count as a flag in the pilot. `unverifiable` and `supported` do not.
_FLAG_VERDICTS = ("unsupported", "contradicted", "overstated")


def is_enabled() -> bool:
    return os.environ.get("CLAIM_VERIFY_ENABLED", "0") == "1"


def build_source_evidence(feed_content: str, web_content: str) -> str:
    """Source-labelled evidence blob that RETAINS the per-item summary lines that
    build_source_index strips. Line-by-line transform of the already-built feed/web
    blobs: '### NAME [...] (CAT)' -> 'SOURCE: NAME'; '- title (date)' kept as a
    bullet; any other non-empty line is an indented detail (RSS summary or web body),
    capped at _DETAIL_CAP chars."""
    out: list[str] = []
    for raw in f"{feed_content}\n{web_content}".splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            name = stripped[4:].split(" [")[0].split(" (")[0].strip()
            if name:
                out.append(f"SOURCE: {name}")
        elif stripped.startswith("- "):
            out.append(f"- {stripped[2:].strip()}")
        else:
            out.append(f"  {stripped[:_DETAIL_CAP]}")
    return "\n".join(out)


def _evidence_path(day: str) -> Path:
    return DATA_DIR / f"claim_evidence-{day}.json"


def save_evidence(evidence: str, day: str) -> None:
    _write_json_atomic(_evidence_path(day), {"date": day, "evidence": evidence})


def load_evidence(day: str) -> str:
    try:
        p = _evidence_path(day)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("evidence", ""))
    except Exception as e:
        log.warning(f"Claim evidence unreadable for {day}; skipping verify: {e}")
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_verify.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add claim_verify.py tests/test_claim_verify.py
git commit -F - <<'EOF'
feat(verify): module scaffold + source-evidence persistence

claim_verify.py with CLAIM_VERIFY_ENABLED flag and build_source_evidence
(retains the per-item summaries build_source_index drops), plus dated
claim_evidence-{day}.json save/load. Fail-safe load.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: TOP STORIES section extractor

**Files:**
- Modify: `claim_verify.py`
- Test: `tests/test_claim_verify.py`

**Interfaces:**
- Produces: `extract_top_stories(brief_text: str) -> str`

The brief uses HTML bold section headings (a heading line is `<b>…</b>` on its own line). The TOP STORIES section runs from the `<b>…TOP STORIES…</b>` heading to the next heading line. Operates on the raw delivered `brief` (HTML intact), the same value `mode_collect` feeds `reconcile_ledger`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_claim_verify.py
BRIEF = (
    "<b>🌍 TOP STORIES</b>\n"
    "- Central bank held rates at 4.5%.\n"
    "- Geneva talks resumed after a pause.\n"
    "<b>📈 MARKET PULSE — WHAT MOVED</b>\n"
    "- Oil up 2% on the news.\n"
    "<b>👁 WATCH / FORWARD</b>\n"
    "- Fed minutes due Thursday.\n"
)


def test_extract_top_stories_returns_section_only():
    out = cv.extract_top_stories(BRIEF)
    assert "Central bank held rates" in out
    assert "Geneva talks resumed" in out
    # stops at the next heading
    assert "Oil up 2%" not in out
    assert "MARKET PULSE" not in out
    # keeps its own heading
    assert "TOP STORIES" in out


def test_extract_top_stories_absent_returns_empty():
    assert cv.extract_top_stories("<b>📈 MARKET PULSE</b>\n- x\n") == ""


def test_extract_top_stories_runs_to_end_when_last_section():
    brief = "<b>🌍 TOP STORIES</b>\n- Only section here.\n"
    assert "Only section here" in cv.extract_top_stories(brief)


def test_extract_top_stories_ignores_inline_bold_in_bullets():
    brief = (
        "<b>🌍 TOP STORIES</b>\n"
        "- A bullet with <b>inline</b> emphasis stays in.\n"
        "<b>📈 MARKET PULSE</b>\n- out\n"
    )
    out = cv.extract_top_stories(brief)
    assert "inline" in out
    assert "out" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_verify.py -k extract_top_stories -v`
Expected: FAIL — `AttributeError: module 'claim_verify' has no attribute 'extract_top_stories'`.

- [ ] **Step 3: Implement the extractor**

```python
# add to claim_verify.py (after load_evidence)
def _is_heading(line: str) -> bool:
    """A section heading is a line that is exactly a single bold span, e.g.
    '<b>📈 MARKET PULSE — WHAT MOVED</b>'. Distinguishes headings from bullets
    that merely contain inline <b>…</b>."""
    s = line.strip()
    return s.startswith("<b>") and s.endswith("</b>")


def extract_top_stories(brief_text: str) -> str:
    lines = brief_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _is_heading(line) and "TOP STORIES" in line.upper():
            start = i
            break
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if _is_heading(line):
            break
        out.append(line)
    return "\n".join(out).strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_verify.py -k extract_top_stories -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add claim_verify.py tests/test_claim_verify.py
git commit -F - <<'EOF'
feat(verify): extract TOP STORIES section from the delivered brief

Anchors on the <b>…TOP STORIES…</b> bold heading, ends at the next
heading line; tolerates inline bold inside bullets.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: Grounding tool schema, request builder, and response parser

**Files:**
- Modify: `claim_verify.py`
- Test: `tests/test_claim_verify.py`

**Interfaces:**
- Produces:
  - `build_verify_request(top_stories: str, evidence: str) -> dict`
  - `_coerce_verdict(v) -> str | None`
  - `parse_verify_response(resp: dict) -> list[dict]` — each entry has `claim`, `verdict`, and (when present) `evidence`, `reason`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_claim_verify.py
def _tool_resp(claims):
    return {"content": [{"type": "tool_use", "name": "emit_claim_checks",
                         "input": {"claims": claims}}]}


def test_build_verify_request_forces_the_tool():
    req = cv.build_verify_request("<b>🌍 TOP STORIES</b>\n- x", "SOURCE: R\n- x")
    assert req["model"] == cv.VERIFY_MODEL
    assert req["tool_choice"] == {"type": "tool", "name": "emit_claim_checks"}
    assert req["tools"][0]["name"] == "emit_claim_checks"
    body = req["messages"][0]["content"]
    assert "TOP STORIES" in body and "SOURCE: R" in body


def test_parse_verify_response_keeps_valid_claims():
    resp = _tool_resp([
        {"claim": "Rates held at 4.5%", "verdict": "supported",
         "evidence": "- Central bank holds rates", "reason": "matches headline"},
        {"claim": "War declared", "verdict": "unsupported", "evidence": "", "reason": "no source"},
    ])
    out = cv.parse_verify_response(resp)
    assert len(out) == 2
    assert out[0]["verdict"] == "supported"
    assert out[1]["verdict"] == "unsupported"
    assert out[0]["evidence"] == "- Central bank holds rates"


def test_parse_verify_response_drops_invalid_verdict_and_empty_claim():
    resp = _tool_resp([
        {"claim": "ok claim", "verdict": "made-up"},      # bad verdict -> drop
        {"claim": "", "verdict": "supported"},            # empty claim -> drop
        {"verdict": "supported"},                         # no claim -> drop
        {"claim": "good", "verdict": "CONTRADICTED"},     # case-insensitive -> keep
    ])
    out = cv.parse_verify_response(resp)
    assert len(out) == 1
    assert out[0] == {"claim": "good", "verdict": "contradicted"}


def test_parse_verify_response_no_tool_block_raises():
    import pytest
    with pytest.raises(ValueError):
        cv.parse_verify_response({"content": [{"type": "text", "text": "hi"}]})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_verify.py -k verify_request or parse_verify -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Implement tool, request, and parser**

```python
# add to claim_verify.py (after extract_top_stories)
_VERIFY_TOOL = {
    "name": "emit_claim_checks",
    "description": (
        "Record every significant factual claim in the brief section, each with a "
        "grounding verdict judged ONLY against the provided sources."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": "the factual assertion, quoted or closely "
                            "paraphrased from the brief section",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": [
                                "supported",
                                "unsupported",
                                "contradicted",
                                "overstated",
                                "unverifiable",
                            ],
                            "description": "supported = a source line backs it; "
                            "unsupported = no related source line at all; "
                            "contradicted = a source says the opposite; "
                            "overstated = a source backs the gist but not the "
                            "specifics (magnitude/precision); unverifiable = "
                            "analytical or forward-looking, not a hard factual claim",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "the source line that supports or "
                            "contradicts the claim; empty string if none",
                        },
                        "reason": {"type": "string", "description": "one terse clause"},
                    },
                    "required": ["claim", "verdict"],
                },
            }
        },
        "required": ["claims"],
    },
}

_VERIFY_SYSTEM = (
    "You verify whether each significant factual claim in a daily market brief's "
    "lead section is supported by the source material the brief was written from. "
    "You are NOT checking against the outside world or your own knowledge — only "
    "against the provided sources. Judge conservatively and assign a verdict to "
    "every claim."
)

_VERIFY_USER_TEMPLATE = """Below is the TOP STORIES section of today's brief and the \
SOURCE MATERIAL it was written from (outlet-labelled headlines with summaries).

Call emit_claim_checks with every significant factual claim in the TOP STORIES \
section. For each claim, assign a verdict judged ONLY against the SOURCE MATERIAL \
below — do not use outside knowledge. Mark analytical or forward-looking statements \
(predictions, "may", "could", framing) as "unverifiable" rather than forcing a \
supported/unsupported call.

TOP STORIES:
{top_stories}

SOURCE MATERIAL:
{evidence}
"""


def build_verify_request(top_stories: str, evidence: str) -> dict:
    return {
        "model": VERIFY_MODEL,
        "max_tokens": VERIFY_MAX_TOKENS,
        "system": _VERIFY_SYSTEM,
        "tools": [_VERIFY_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_claim_checks"},
        "messages": [
            {
                "role": "user",
                "content": _VERIFY_USER_TEMPLATE.format(
                    top_stories=top_stories, evidence=evidence
                ),
            }
        ],
    }


def _coerce_verdict(v) -> str | None:
    """Canonical verdict, or None if missing/unknown. Case-insensitive."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VALID_VERDICTS:
            return s
    return None


def parse_verify_response(resp: dict) -> list[dict]:
    """Pull the claims list from the emit_claim_checks tool_use block. Drops items
    with no claim text or an unknown verdict. Raises ValueError if the tool block is
    absent (the fail-safe wrapper turns that into a skipped record)."""
    for block in resp.get("content", []):
        if (
            block.get("type") == "tool_use"
            and block.get("name") == "emit_claim_checks"
        ):
            claims = block.get("input", {}).get("claims")
            if not isinstance(claims, list):
                raise ValueError("emit_claim_checks input missing 'claims' list")
            out = []
            for item in claims:
                if not isinstance(item, dict):
                    continue
                claim = item.get("claim")
                verdict = _coerce_verdict(item.get("verdict"))
                if not (isinstance(claim, str) and claim.strip()) or verdict is None:
                    continue
                entry = {"claim": claim.strip(), "verdict": verdict}
                for k in ("evidence", "reason"):
                    if isinstance(item.get(k), str) and item[k].strip():
                        entry[k] = item[k].strip()
                out.append(entry)
            return out
    raise ValueError("no emit_claim_checks tool_use block in response")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_verify.py -k "verify_request or parse_verify" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add claim_verify.py tests/test_claim_verify.py
git commit -F - <<'EOF'
feat(verify): grounding tool schema, request builder, response parser

Forced-tool emit_claim_checks (5-verdict taxonomy incl. the unverifiable
noise valve); parser coerces verdicts case-insensitively and drops empty
or unknown-verdict claims.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 4: Model caller, record assembly, and fail-safe orchestrator

**Files:**
- Modify: `claim_verify.py`
- Test: `tests/test_claim_verify.py`

**Interfaces:**
- Consumes: `build_verify_request`, `parse_verify_response`, `extract_top_stories`, `load_evidence`.
- Produces:
  - `_post_verify(payload: dict) -> dict`
  - `_verification_record(today: str, top_stories_present: bool, claims: list[dict]) -> dict`
  - `_verification_path(day: str) -> Path`
  - `save_verification(record: dict, day: str) -> None`
  - `verify_claims(brief_text: str, evidence: str, today: str, *, call=None) -> dict | None`
  - `run_verification(brief_text: str, today: str, *, call=None) -> None` — fail-safe entry used by `mode_collect`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_claim_verify.py
def test_verification_record_counts_verdicts():
    claims = [
        {"claim": "a", "verdict": "supported"},
        {"claim": "b", "verdict": "unsupported"},
        {"claim": "c", "verdict": "contradicted"},
    ]
    rec = cv._verification_record("2026-06-26", True, claims)
    assert rec["date"] == "2026-06-26"
    assert rec["model"] == cv.VERIFY_MODEL
    assert rec["top_stories_present"] is True
    assert rec["n_claims"] == 3
    assert rec["counts_by_verdict"]["supported"] == 1
    assert rec["counts_by_verdict"]["unsupported"] == 1
    assert rec["counts_by_verdict"]["contradicted"] == 1
    assert rec["counts_by_verdict"]["overstated"] == 0


def test_verify_claims_absent_top_stories_records_empty():
    rec = cv.verify_claims("<b>📈 MARKET PULSE</b>\n- x", "SOURCE: R", "2026-06-26",
                           call=lambda payload: (_ for _ in ()).throw(AssertionError("should not call")))
    assert rec["top_stories_present"] is False
    assert rec["n_claims"] == 0


def test_verify_claims_happy_path_uses_injected_call():
    def fake_call(payload):
        return _tool_resp([{"claim": "Rates held", "verdict": "supported"}])

    rec = cv.verify_claims("<b>🌍 TOP STORIES</b>\n- Rates held.", "SOURCE: R\n- Rates held",
                           "2026-06-26", call=fake_call)
    assert rec["top_stories_present"] is True
    assert rec["n_claims"] == 1
    assert rec["claims"][0]["claim"] == "Rates held"


def test_verify_claims_returns_none_on_call_failure():
    def boom(payload):
        raise RuntimeError("api down")

    rec = cv.verify_claims("<b>🌍 TOP STORIES</b>\n- x", "SOURCE: R", "2026-06-26", call=boom)
    assert rec is None


def test_run_verification_writes_record_when_evidence_present(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: R\n- Rates held", "2026-06-26")

    def fake_call(payload):
        return _tool_resp([{"claim": "Rates held", "verdict": "supported"}])

    cv.run_verification("<b>🌍 TOP STORIES</b>\n- Rates held.", "2026-06-26", call=fake_call)
    rec = json.loads((tmp_path / "verification-2026-06-26.json").read_text())
    assert rec["n_claims"] == 1


def test_run_verification_skips_when_no_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    # no evidence file written
    cv.run_verification("<b>🌍 TOP STORIES</b>\n- x", "2026-06-26",
                        call=lambda p: (_ for _ in ()).throw(AssertionError("should not call")))
    assert not (tmp_path / "verification-2026-06-26.json").exists()


def test_run_verification_never_raises_on_bad_call(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: R", "2026-06-26")
    # must not raise, and must not write a record
    cv.run_verification("<b>🌍 TOP STORIES</b>\n- x", "2026-06-26",
                        call=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not (tmp_path / "verification-2026-06-26.json").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_verify.py -k "verification_record or verify_claims or run_verification" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Implement caller, record, and orchestrators**

```python
# add to claim_verify.py (after parse_verify_response)
def _post_verify(payload: dict) -> dict:
    """Anthropic Messages call with one retry. Generous timeout: verification runs
    AFTER the brief is delivered, so a slow call never delays the reader (lesson
    e255436, where a 30s timeout wiped a Sonnet post-gen call)."""
    last_err = None
    for attempt in range(1, VERIFY_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=ANTHROPIC_HEADERS,
                json=payload,
                timeout=VERIFY_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_err = e
            log.warning(f"Claim verify API attempt {attempt} failed: {e}")
    raise last_err


def _verification_record(
    today: str, top_stories_present: bool, claims: list[dict]
) -> dict:
    counts = {v: 0 for v in _VALID_VERDICTS}
    for c in claims:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    return {
        "date": today,
        "model": VERIFY_MODEL,
        "top_stories_present": top_stories_present,
        "n_claims": len(claims),
        "counts_by_verdict": counts,
        "claims": claims,
    }


def _verification_path(day: str) -> Path:
    return DATA_DIR / f"verification-{day}.json"


def save_verification(record: dict, day: str) -> None:
    _write_json_atomic(_verification_path(day), record)


def verify_claims(
    brief_text: str, evidence: str, today: str, *, call=None
) -> dict | None:
    """Build the verification record for today, or None if the model call fails.
    An absent TOP STORIES section is recorded (top_stories_present=False), not failed."""
    caller = call or _post_verify
    top = extract_top_stories(brief_text)
    if not top.strip():
        return _verification_record(today, False, [])
    try:
        resp = caller(build_verify_request(top, evidence))
        claims = parse_verify_response(resp)
        return _verification_record(today, True, claims)
    except Exception as e:
        log.warning(f"Claim verification call failed; no record this run: {e}")
        return None


def run_verification(brief_text: str, today: str, *, call=None) -> None:
    """Fail-safe entry for mode_collect. Loads today's evidence, runs the check, and
    writes verification-{day}.json. Never raises; the brief is already delivered."""
    try:
        evidence = load_evidence(today)
        if not evidence.strip():
            log.info(f"Claim verify: no evidence for {today}; skipping")
            return
        record = verify_claims(brief_text, evidence, today, call=call)
        if record is not None:
            save_verification(record, today)
            log.info(
                f"Claim verify {today}: {record['n_claims']} claims, "
                f"{record['counts_by_verdict']}"
            )
    except Exception as e:
        log.warning(f"Claim verification skipped (brief unaffected): {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_verify.py -k "verification_record or verify_claims or run_verification" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add claim_verify.py tests/test_claim_verify.py
git commit -F - <<'EOF'
feat(verify): model caller, record assembly, fail-safe orchestrator

_post_verify (90s timeout + retry), _verification_record (counts by
verdict), verify_claims (absent TOP STORIES recorded, call failure ->
None), and run_verification (fail-safe entry: skips when no evidence,
never raises).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 5: Pilot-review aggregator

**Files:**
- Modify: `claim_verify.py`
- Test: `tests/test_claim_verify.py`

**Interfaces:**
- Produces: `summarize_verifications(data_dir: Path = DATA_DIR) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_claim_verify.py
def test_summarize_verifications_aggregates(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_verification(cv._verification_record("2026-06-25", True, [
        {"claim": "a", "verdict": "supported"},
        {"claim": "b", "verdict": "contradicted", "evidence": "- src", "reason": "opp"},
    ]), "2026-06-25")
    cv.save_verification(cv._verification_record("2026-06-26", True, [
        {"claim": "c", "verdict": "unsupported"},
        {"claim": "d", "verdict": "unverifiable"},
    ]), "2026-06-26")
    # a malformed file must be ignored, not crash
    (tmp_path / "verification-2026-06-27.json").write_text("{not json")

    rep = cv.summarize_verifications(tmp_path)
    assert rep["days"] == 2
    assert rep["n_claims"] == 4
    assert rep["totals_by_verdict"]["supported"] == 1
    assert rep["totals_by_verdict"]["contradicted"] == 1
    assert rep["totals_by_verdict"]["unsupported"] == 1
    # flagged = unsupported + contradicted + overstated (NOT unverifiable/supported)
    assert rep["flagged_total"] == 2
    flagged_claims = {f["claim"] for f in rep["flagged"]}
    assert flagged_claims == {"b", "c"}
    assert any(f["verdict"] == "contradicted" and f["date"] == "2026-06-25"
               for f in rep["flagged"])


def test_summarize_verifications_empty_dir(tmp_path):
    rep = cv.summarize_verifications(tmp_path)
    assert rep["days"] == 0
    assert rep["n_claims"] == 0
    assert rep["flag_rate"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_claim_verify.py -k summarize -v`
Expected: FAIL — `summarize_verifications` not defined.

- [ ] **Step 3: Implement the aggregator**

```python
# add to claim_verify.py (after run_verification)
def summarize_verifications(data_dir: Path = DATA_DIR) -> dict:
    """Aggregate all verification-*.json into a decision-ready report for the pilot
    gate. Headline metric is the flag breakdown (lead on `contradicted` — it is
    confound-free; raw `unsupported` is confounded by the brief's own web search)."""
    totals = {v: 0 for v in _VALID_VERDICTS}
    n_claims = 0
    days = 0
    flagged: list[dict] = []
    per_day: list[dict] = []
    for p in sorted(data_dir.glob("verification-*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        days += 1
        counts = rec.get("counts_by_verdict", {}) or {}
        for v in totals:
            totals[v] += int(counts.get(v, 0) or 0)
        n_claims += int(rec.get("n_claims", 0) or 0)
        per_day.append(
            {"date": rec.get("date"), "n_claims": rec.get("n_claims", 0), "counts": counts}
        )
        for c in rec.get("claims", []) or []:
            if isinstance(c, dict) and c.get("verdict") in _FLAG_VERDICTS:
                flagged.append(
                    {
                        "date": rec.get("date"),
                        "claim": c.get("claim"),
                        "verdict": c.get("verdict"),
                        "evidence": c.get("evidence", ""),
                        "reason": c.get("reason", ""),
                    }
                )
    flagged_total = sum(totals[v] for v in _FLAG_VERDICTS)
    return {
        "days": days,
        "n_claims": n_claims,
        "totals_by_verdict": totals,
        "flagged_total": flagged_total,
        "flag_rate": (flagged_total / n_claims) if n_claims else 0.0,
        "flagged": flagged,
        "per_day": per_day,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_claim_verify.py -k summarize -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add claim_verify.py tests/test_claim_verify.py
git commit -F - <<'EOF'
feat(verify): pilot-review aggregator over verification-*.json

summarize_verifications totals verdicts, computes the flag rate
(unsupported+contradicted+overstated), and lists flagged claims with
evidence; ignores malformed files.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 6: Wire into brief.py + Docker/CI allowlist

**Files:**
- Modify: `brief.py` (import block at `:93`; `mode_submit` near `:2467`; `mode_collect` near `:2572`)
- Modify: `Dockerfile:13`
- Modify: `.github/workflows/docker-publish.yml` (paths `:7-11`; ruff lists `:48-49`)

**Interfaces:**
- Consumes: `claim_verify.is_enabled`, `build_source_evidence`, `save_evidence`, `run_verification`.

- [ ] **Step 1: Add the import (after the `brief_memory` import block, `brief.py:93-99`)**

```python
from claim_verify import (
    build_source_evidence,
    is_enabled as claim_verify_enabled,
    run_verification,
    save_evidence as save_claim_evidence,
)
```

- [ ] **Step 2: Persist evidence in `mode_submit` (immediately AFTER the existing `save_source_index` block, `brief.py:2467-2471`)**

Existing block ends:
```python
    if brief_memory_enabled():
        try:
            save_source_index(build_source_index(feed_content, web_content), today)
        except Exception as e:
            log.warning(f"Source index persist skipped (brief unaffected): {e}")
```
Add directly below it:
```python
    if claim_verify_enabled():
        try:
            save_claim_evidence(build_source_evidence(feed_content, web_content), today)
        except Exception as e:
            log.warning(f"Claim evidence persist skipped (brief unaffected): {e}")
```

- [ ] **Step 3: Run verification in `mode_collect` (immediately AFTER the existing `brief_memory_enabled()` reconcile block, `brief.py:2572-2583`, before `clear_batch_state()`)**

Add directly below the reconcile block:
```python
        if claim_verify_enabled():
            try:
                run_verification(brief, today)
            except Exception as e:
                log.error(f"Claim verification skipped (brief unaffected): {e}")
```

- [ ] **Step 4: Update `Dockerfile:13` COPY allowlist**

From:
```dockerfile
COPY common.py trading.py validation.py brief.py brief_memory.py .
```
To:
```dockerfile
COPY common.py trading.py validation.py brief.py brief_memory.py claim_verify.py .
```

- [ ] **Step 5: Update `.github/workflows/docker-publish.yml`**

Add the path trigger (after the `brief_memory.py` line, `:8`):
```yaml
      - 'claim_verify.py'
```
Add to BOTH ruff lines (`:48-49`) — insert `claim_verify.py` after `brief_memory.py`:
```yaml
          ruff check brief.py brief_memory.py claim_verify.py common.py trading.py enrichment tests
          ruff format --check brief.py brief_memory.py claim_verify.py common.py trading.py enrichment tests
```

- [ ] **Step 6: Verify import + wiring smoke (no network)**

Run (PowerShell — Python on this host runs via PowerShell, per `python-via-powershell`):
```
python -c "import brief; print('claim_verify_enabled' in dir(brief)); print(brief.claim_verify_enabled())"
```
Expected: `True` then `False` (flag off by default). Import must not error.

- [ ] **Step 7: Full gate — ruff + format + the whole suite**

Run:
```
ruff check brief.py brief_memory.py claim_verify.py common.py trading.py enrichment tests
ruff format --check brief.py brief_memory.py claim_verify.py common.py trading.py enrichment tests
python -m pytest -q
```
Expected: ruff clean; all tests pass (existing suite + the new `tests/test_claim_verify.py`). If `ruff format` reports a file would be reformatted, run `ruff format <file>` and stage it.

- [ ] **Step 8: Commit**

```bash
git add brief.py Dockerfile .github/workflows/docker-publish.yml
git commit -F - <<'EOF'
feat(verify): wire claim-verification pilot into submit/collect (gated)

mode_submit persists claim_evidence-{day}.json; mode_collect runs the
shadow grounding check after deliver(), both gated by
CLAIM_VERIFY_ENABLED (default off). Add claim_verify.py to the Dockerfile
COPY allowlist and the CI path/ruff lists (dockerfile-copy-allowlist).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

---

## Pilot operation (post-merge, not a code task)

1. Push to `main` and deploy (Docker). Bundle with the held #4/#5a/#5b batch if still pending, per user.
2. Set `CLAIM_VERIFY_ENABLED=1` on the deploy host. Shadow logs begin on the next submit/collect cycle.
3. After ~14 days (≥10 briefs with data), on the host run:
   ```
   python -c "import json, claim_verify; print(json.dumps(claim_verify.summarize_verifications(), indent=2))"
   ```
4. Apply the pre-registered gate from the spec (Gate 0 instrument-validity first; then kill / keep / promote — leading on `contradicted`, treating raw `unsupported` as confounded by the brief's web search).

## Self-Review Notes

- **Spec coverage:** richer evidence (Task 1), TOP STORIES scope (Task 2), Sonnet forced-tool + verdict taxonomy incl. `unverifiable` (Task 3), record + fail-safe ladder + flag (Tasks 1/4/6), aggregator + flag semantics (Task 5), Docker/CI chore (Task 6), pilot operation + gate (operations section). The decision gate itself is human judgement at review time, not code — represented by `summarize_verifications` output + the operations section.
- **Confound:** encoded in `_FLAG_VERDICTS` (keeps `supported`/`unverifiable` out of the flag count) and the aggregator docstring; the contradicted-vs-unsupported weighting is applied by the human at the gate, by design.
- **Type consistency:** `claims` entries carry `claim` + `verdict` (+ optional `evidence`/`reason`) consistently from `parse_verify_response` → `_verification_record` → `summarize_verifications`. Tool name `emit_claim_checks` consistent in schema, request, and parser. Flag name `CLAIM_VERIFY_ENABLED` and model const `VERIFY_MODEL` consistent across module and wiring.

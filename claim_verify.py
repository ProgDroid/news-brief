"""Claim-verification grounding pilot: a flag-gated, log-only check that measures
how often the daily brief's TOP STORIES make claims unsupported by the source
material we fed the model. SHADOW only — never affects the delivered brief or
trading. Gated by CLAIM_VERIFY_ENABLED; fail-safe (any error leaves the brief
unaffected). Idea reimplemented from the Pharos verify pattern (AGPL); no code lifted."""

import json
import os
from pathlib import Path

import requests

from common import ANTHROPIC_HEADERS, DATA_DIR, _write_json_atomic, log

VERIFY_MODEL = "claude-sonnet-4-6"
VERIFY_TIMEOUT = (
    90  # generous: runs AFTER delivery, so latency is free (lesson e255436)
)
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
        if block.get("type") == "tool_use" and block.get("name") == "emit_claim_checks":
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
            {
                "date": rec.get("date"),
                "n_claims": rec.get("n_claims", 0),
                "counts": counts,
            }
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

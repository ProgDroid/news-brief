"""Claim-verification grounding pilot: a flag-gated, log-only check that measures
how often the daily brief's TOP STORIES make claims unsupported by the source
material we fed the model. SHADOW only — never affects the delivered brief or
trading. Gated by CLAIM_VERIFY_ENABLED; fail-safe (any error leaves the brief
unaffected). Idea reimplemented from the Pharos verify pattern (AGPL); no code lifted."""

import json
import os
from pathlib import Path

from common import DATA_DIR, _write_json_atomic, log

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

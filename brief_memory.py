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
HIGH_SEVERITY_BONUS_DAYS = 7  # extra retention days a "high" claim earns (=> 14d TTL)
_SEVERITY_RANK = {"low": 0, "normal": 1, "high": 2}  # cap-eviction ordering
_VALID_SEVERITY = frozenset(_SEVERITY_RANK)
_DEFAULT_SEVERITY = "normal"
RECONCILE_MODEL = "claude-haiku-4-5-20251001"
# A full MAX_CLAIMS ledger serialises to ~2400 output tokens, so the old 2048
# budget truncated the JSON array before its closing "]" — the parser then
# misreported the cut-off as "no JSON array". Give generous headroom and bound
# the model's output (see _RECONCILE_TEMPLATE) so it stays well inside this.
RECONCILE_MAX_TOKENS = 4096


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
            base["source_count"] = max(
                base.get("source_count", 0) or 0,
                mc.get("source_count", 0) or 0,
            )
            new_sev = _coerce_severity(mc.get("severity"))
            base["severity"] = new_sev or base.get("severity", _DEFAULT_SEVERITY)
            result.append(base)
            returned.add(cid)
        elif mc.get("claim"):
            result.append(
                {
                    "id": f"c-{next_num:04d}",
                    "claim": mc["claim"],
                    "topic": mc.get("topic", ""),
                    "first_seen": today,
                    "last_reaffirmed": today,
                    "restate_count": 1,
                    "source_count": mc.get("source_count", 0) or 0,
                    "severity": _coerce_severity(mc.get("severity"))
                    or _DEFAULT_SEVERITY,
                }
            )
            next_num += 1
    for c in prior.get("claims", []):
        if c.get("id") not in returned:
            result.append(dict(c))
    result = [
        c
        for c in result
        if _days_between(c["last_reaffirmed"], today) <= retire_after_days
    ]
    result.sort(key=lambda c: c["last_reaffirmed"], reverse=True)
    return {"version": 1, "claims": result[:cap]}


_RECONCILE_SYSTEM = (
    "You maintain a compact memory of durable facts a daily market brief has "
    "already told its reader, so tomorrow's brief stops re-explaining them."
)

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


def build_reconcile_prompt(
    ledger: dict, brief_text: str, source_index: str = ""
) -> str:
    current = json.dumps(ledger.get("claims", []), indent=2)
    return _RECONCILE_TEMPLATE.format(
        current=current,
        brief=brief_text,
        max_claims=MAX_CLAIMS,
        source_index=source_index.strip() or "(no source index available)",
    )


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
            entry = {k: item[k] for k in ("id", "claim", "topic") if k in item}
            sc = _coerce_source_count(item.get("source_count"))
            if sc is not None:
                entry["source_count"] = sc
            out.append(entry)
    return out


def _messages_call(system: str, user: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": RECONCILE_MODEL,
            "max_tokens": RECONCILE_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("stop_reason") == "max_tokens":
        # Don't return a half-written array for the parser to choke on — surface
        # the real cause so the fail-safe logs it as truncation, not a parse bug.
        raise ValueError(
            "reconcile response truncated (stop_reason=max_tokens); "
            "ledger/output exceeded the max_tokens budget"
        )
    blocks = body.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


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

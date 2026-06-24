"""Standing-claim ledger: gives the daily brief multi-day memory of facts it has
already established, so it stops re-explaining them. DESCRIPTIVE only — never
affects trading. Flag-gated by BRIEF_MEMORY_ENABLED; fail-safe (any error leaves
the brief unaffected and the prior ledger intact)."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests  # noqa: F401

from common import ANTHROPIC_HEADERS, DATA_DIR, _write_json_atomic, log  # noqa: F401

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
            result.append(
                {
                    "id": f"c-{next_num:04d}",
                    "claim": mc["claim"],
                    "topic": mc.get("topic", ""),
                    "first_seen": today,
                    "last_reaffirmed": today,
                    "restate_count": 1,
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

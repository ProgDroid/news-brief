"""Standing-claim ledger: gives the daily brief multi-day memory of facts it has
already established, so it stops re-explaining them. DESCRIPTIVE only — never
affects trading. Flag-gated by BRIEF_MEMORY_ENABLED; fail-safe (any error leaves
the brief unaffected and the prior ledger intact)."""

import json
import os
import re  # noqa: F401
from datetime import datetime  # noqa: F401
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

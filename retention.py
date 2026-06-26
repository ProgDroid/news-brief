"""Dated-artifact retention: deletes per-day artifact files older than the
retention window and trims the rolling signals log to the same window, so the
volume does not grow without bound. Hygiene only — fail-safe (any error leaves
files in place and never affects the brief, which is already delivered). Runs at
the tail of mode_collect. Window via NEWSBRIEF_RETENTION_DAYS (default 90);
days <= 0 disables. Targets ONLY date-bearing filenames, so bounded-state files
(book.json, brief_memory.json, feedback.json, ...) are structurally untouched."""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from common import DATA_DIR, log

DEFAULT_RETENTION_DAYS = 90
RETENTION_DAYS_ENV = "NEWSBRIEF_RETENTION_DAYS"

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _families() -> list[tuple[Path, str]]:
    """(directory, glob) specs for dated-file deletion. All derived from DATA_DIR
    so a single monkeypatch in tests relocates every family. weekly/ excluded by
    design; signals-log.jsonl is not matched by 'signals-*.json' (ends in .jsonl)."""
    return [
        (DATA_DIR / "briefs", "brief-*.md"),
        (DATA_DIR, "source_index-*.json"),
        (DATA_DIR, "claim_evidence-*.json"),
        (DATA_DIR, "verification-*.json"),
        (DATA_DIR / "enrichment", "enrichment-*.json"),
        (DATA_DIR / "signals", "signals-*.json"),
    ]


def _file_date(name: str):
    """The YYYY-MM-DD embedded in a filename as a date, or None if absent/invalid.
    None means 'undateable' -> never pruned."""
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve_days(days):
    if days is not None:
        return days
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning(
            f"Invalid {RETENTION_DAYS_ENV}={raw!r}; using {DEFAULT_RETENTION_DAYS}"
        )
        return DEFAULT_RETENTION_DAYS


def _cutoff(today: str, days: int):
    return datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=days)


def prune_dated_files(today: str, days: int) -> int:
    """Delete dated artifact files strictly older than (today - days). Files with
    no parseable date in the name are skipped. Returns the number deleted."""
    cutoff = _cutoff(today, days)
    deleted = 0
    for directory, pattern in _families():
        try:
            for path in directory.glob(pattern):
                d = _file_date(path.name)
                if d is not None and d < cutoff:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError as e:
                        log.warning(f"Retention: could not delete {path}: {e}")
        except Exception as e:
            log.warning(f"Retention: family {directory}/{pattern} skipped: {e}")
    return deleted


def trim_signals_log(today: str, days: int) -> int:
    """Trim signals-log.jsonl to lines whose 'date' is within the window. Lines
    with no parseable date are KEPT (keep-on-doubt). Atomic rewrite only when at
    least one line is dropped. Returns the number of lines dropped."""
    path = DATA_DIR / "signals" / "signals-log.jsonl"
    if not path.exists():
        return 0
    cutoff = _cutoff(today, days)
    kept: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        keep = True
        try:
            rec = json.loads(line)
            d = _file_date(str(rec.get("date", "")))
            if d is not None and d < cutoff:
                keep = False
        except Exception:
            keep = True  # unparseable line -> keep
        if keep:
            kept.append(line)
        else:
            dropped += 1
    if dropped:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, path)
    return dropped

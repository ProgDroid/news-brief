"""Preview the 7-day dated window against the real archived briefs.

Prints BRIEFS_DIR and the files found, which sections the parser recognised
in each, then the window block and its size. "No files" (a wrong volume or an
empty directory) and "files found, no sections recognised" read differently.

On the host:
    docker compose run --rm --entrypoint python newsbrief scripts/preview_dated_window.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import dated_memory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--today",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="submit day, YYYY-MM-DD (default: today UTC)",
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    briefs = brief.load_last_n_briefs(7)
    print(f"BRIEFS_DIR={brief.BRIEFS_DIR}")
    print(f"files={len(briefs)} dates={[d for d, _ in briefs]}")
    for day, text in briefs:
        sections = dated_memory.brief_sections(text)
        counts = {k: len(v) for k, v in sections.items()}
        print(f"  {day}: sections={counts or 'NONE RECOGNISED'}")

    w = dated_memory.build_window(briefs, args.today)
    print()
    print(w.block or "(empty window: no block would be added)")
    print()
    print(f"days={w.days} lines={w.lines} chars={w.chars}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

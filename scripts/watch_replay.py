"""Replay the watch judge for a past date: the news-brief-8lp validation gate.

Runs the PRODUCTION judge (watch.judge_watch, the same function mode_watch
calls) over the 48 h of captured items before --as-of, for the made-up rows in
--rows, and prints the verdicts. It never sends to Telegram and never writes
runtime_state, so it can be run any number of times.

On the host (the items live there):

    docker compose run --rm --entrypoint python newsbrief scripts/watch_replay.py \
        --as-of 2026-10-01T06:30:00Z --rows /app/logs/replay-rows.json

/app/logs is DATA_DIR inside the container, mounted from
${APPDATA_DIR}/news-brief on the host, so a rows file dropped there is visible.
--rows is a watch file in the operator's format (version 1). Use made-up rows
only, and keep that file out of this public repository.
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
import db  # noqa: E402
import watch  # noqa: E402


def _as_of(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--as-of", required=True, type=_as_of, help="ISO time, UTC")
    ap.add_argument("--rows", required=True, type=Path, help="watch-format file")
    args = ap.parse_args(argv)

    try:
        wf = watch.load_watch(args.rows)
    except watch.WatchFileError as e:
        print(f"rows file rejected: {e}")
        return 1
    for who, why in wf.skipped:
        print(f"skipped {who}: {why}")
    if not wf.events:
        print("no event rows to judge")
        return 0

    try:
        with db.connect() as conn:
            ji, verdicts = watch.judge_watch(
                conn,
                wf.events,
                args.as_of,
                post=brief._watch_post,
                model=brief._watch_model(),
            )
    except watch.WatchFileError as e:
        print(f"could not judge: {e}")
        return 1

    print(f"as_of={args.as_of.isoformat()} titles={ji.n_titles}")
    for v in verdicts:
        print(f"{v.row_id}  {v.verdict}  links={len(v.evidence)}  {v.reason}")
        for item in v.evidence:
            print(f"    {item.outlet}: {item.title}  {item.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Replay a saved brief request in real time, without delivering it.

news-brief-qap (storylines spec §6.1). Collect falls back to a real-time call
when the 04:30 batch is late; every test stubs that call, so this is the one
place the real-time path meets the real API before the fallback is trusted.
It prints the elapsed time that BRIEF_REALTIME_TIMEOUT should be set from.

Costs one full-price brief: about $0.89 measured on 2026-11-23 (377k input
tokens, 8 web searches; search results dominate the input). Sends nothing to
Telegram, writes nothing. On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/replay_brief_realtime.py YYYY-MM-DD

The date is a SUBMIT day with a brief_request-<date>.json on the volume.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import common  # noqa: E402


def main(argv: list[str] | None = None, *, call=None) -> int:
    """0 when a brief came back, 1 when none did, 2 when no request is saved."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: replay_brief_realtime.py YYYY-MM-DD")
        return 2
    day = args[0]
    params = brief.load_brief_request(day)
    if params is None:
        print(f"no saved request: {brief.DATA_DIR / f'brief_request-{day}.json'}")
        return 2
    started = time.time()
    text = brief.run_brief_realtime(
        params, timeout=common.BRIEF_REALTIME_TIMEOUT, call=call
    )
    elapsed = time.time() - started
    print(
        f"day={day} elapsed={elapsed:.0f}s ok={text is not None} chars={len(text or '')}"
    )
    return 0 if text else 1


if __name__ == "__main__":
    sys.exit(main())

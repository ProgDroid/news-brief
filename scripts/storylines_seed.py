"""Draft the initial storylines and send them as proposals (news-brief-al7.3).

Spec 10.5. Sonnet drafts about 20 storylines from the brief's topics, the
pinned subjects, the operator's theses and the watch triggers. They are stored
as `seed` proposals and sent to Telegram with Accept / Reject buttons. On the
host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/storylines_seed.py [--again]

Exit codes: 0 proposals sent, 1 nothing to propose (no headroom, or the model
gave nothing usable), 2 refused because seed proposals are still pending.
Never prints a storyline name or description.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import common  # noqa: E402
import db  # noqa: E402
import storyline_store  # noqa: E402
import storylines  # noqa: E402
import watch  # noqa: E402

THESIS_CHARS = 300


def _gather_theses() -> dict[str, str]:
    merged = {**brief.load_local_theses(), **brief.load_portfolio_theses()[0]}
    return {str(k): str(v)[:THESIS_CHARS] for k, v in merged.items()}


def _gather_watch_lines() -> list[str]:
    try:
        wf = watch.load_watch(brief.WATCH_FILE)
    except watch.WatchFileError:
        return []
    return [f"{e.name}: {e.trigger}" for e in wf.events]


def main(argv: list[str] | None = None, *, post=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--again", action="store_true", help="run although seed proposals are pending"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if post is None:

        def post(payload, **kw):
            return brief._post_messages(payload, **kw)

    with db.connect() as conn:
        pending = storyline_store.pending_proposals(conn, origin="seed")
        if pending and not args.again:
            print(
                f"seed: refused, {len(pending)} seed proposals are still pending "
                "(decide them, or pass --again)"
            )
            return 2
        max_count = min(
            storylines.SEED_MAX,
            common.STORYLINE_MAX_LIVE - storyline_store.count_live(conn),
        )
        if max_count <= 0:
            print("seed: no headroom under STORYLINE_MAX_LIVE")
            return 1
        live_names = [s.name for s in storyline_store.live_storylines(conn)]
        topics = [{"label": t["label"], "search": t["search"]} for t in brief.TOPICS]
        payload = storylines.build_seed_request(
            topics,
            brief.resolved_pins(brief.load_feedback()),
            _gather_theses(),
            _gather_watch_lines(),
            live_names,
            model=common.STORYLINE_UPDATE_MODEL,
            max_count=max_count,
        )
        resp = post(payload, timeout=180, max_attempts=1, label="Seed")
        proposed, problems = storylines.validate_seed(
            resp, max_count=max_count, live_names=live_names
        )
        rows = []
        for p in proposed:
            pid = storyline_store.add_proposal(
                conn, p.name, p.description, origin="seed", pinned=p.pinned
            )
            rows.append(
                {
                    "id": pid,
                    "name": p.name,
                    "description": p.description,
                    "pinned": p.pinned,
                }
            )
        # Commit BEFORE sending, so a tap on a button can find its row.
        conn.commit()
    for row in rows:
        brief.send_proposal(row)
    print(f"seed: proposed={len(rows)} dropped={len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())

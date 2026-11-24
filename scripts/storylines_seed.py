"""Draft the initial storylines and send them as proposals (news-brief-al7.3).

Spec 10.5. Sonnet drafts up to 40 storylines from the brief's topics, the
pinned subjects, the operator's theses and the watch triggers, every pin and
thesis first. They are stored as `seed` proposals and sent to Telegram with
Accept / Reject buttons, followed by one line naming any pin or thesis that no
proposal covers (al7.6). On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/storylines_seed.py [--again]

Exit codes: 0 proposals sent, 1 nothing to propose (no headroom, or the model
gave nothing usable), 2 refused because seed proposals are still pending.
Never prints a storyline name or description.
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import comprehend  # noqa: E402
import common  # noqa: E402
import db  # noqa: E402
import storyline_store  # noqa: E402
import storylines  # noqa: E402
import watch  # noqa: E402

THESIS_CHARS = 300


def _gather_theses() -> dict[str, str]:
    local = brief.load_local_theses()
    pushed, problem = brief.load_portfolio_theses()
    if problem:
        # a count only: never a key or thesis text
        print(
            f"seed: portfolio theses ignored ({problem}); "
            f"drafting from {len(local)} local theses"
        )
    merged = {**local, **pushed}
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

    model = common.STORYLINE_UPDATE_MODEL
    try:
        comprehend.price_of(model)
    except comprehend.UnpricedModel:
        print(f"seed: model {model} has no price: add it to PRICES_PER_MTOK first")
        return 2
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
        pins = brief.resolved_pins(brief.load_feedback())
        theses = _gather_theses()
        labels = storylines.coverage_labels(pins, theses)
        payload = storylines.build_seed_request(
            topics,
            pins,
            theses,
            _gather_watch_lines(),
            live_names,
            model=model,
            max_count=max_count,
        )
        # ~40 storylines of Sonnet output (al7.6); 180 s was sized for ~20.
        resp = post(payload, timeout=300, max_attempts=1, label="Seed")
        comprehend.record_spend(conn, "seed", model, resp.get("usage") or {})
        proposed, problems = storylines.validate_seed(
            resp, max_count=max_count, live_names=live_names, labels=labels
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
                    "covers": [labels[c] for c in p.covers],
                }
            )
        # Commit BEFORE sending, so a tap on a button can find its row.
        conn.commit()
    unsent = []
    for row in rows:
        try:
            ok = brief.send_proposal(row) is not None
        except Exception:
            ok = False
        if not ok:
            unsent.append(row["id"])
    sent = len(rows) - len(unsent)
    gaps = storylines.uncovered(proposed, labels)
    print(
        f"seed: proposed={len(rows)} sent={sent} dropped={len(problems)} "
        f"uncovered={len(gaps)}"
    )
    for problem in problems:
        print(f"  {problem}")
    coverage_sent = True
    if rows:
        coverage_sent = common.telegram_send_long(_coverage_text(gaps, labels))
        if not coverage_sent:
            print("  coverage line not sent")
    if unsent:
        print(f"  not sent (stored, still pending): ids {unsent}")
        return 1
    if not coverage_sent:
        return 1
    return 0 if rows else 1


def _coverage_text(gaps: list[str], labels: dict[str, str]) -> str:
    """The Telegram line that says which pins and theses no proposal covers, so
    the operator never has to check the proposals against them (al7.6). Names
    go to Telegram only, never to stdout."""
    if not gaps:
        return "🧵 Every thesis and pinned subject has a proposed storyline."
    lines = "\n".join(f"- {html.escape(labels[g])}" for g in gaps)
    return (
        f"🧵 No proposed storyline covers:\n{lines}\n"
        "Add one with /storyline add Name — description."
    )


if __name__ == "__main__":
    sys.exit(main())

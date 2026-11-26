"""Choose a zone reference: Opus at higher effort, scored on the operator's answers.

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-reference-selection.md
(news-brief-al7.7.2). Each candidate labels every zone pair of the audit week with
embed_calibrate's pair request, unchanged apart from the effort. It is scored only
on the pairs the operator answered same or different in grouping_label_audit.py.
The winner then labels the other dev week's zone too.

Answers are saved per candidate and week in the week's folder, so a rerun asks
nothing new. Prints numbers only, never a title or a text.

    docker compose run --rm --entrypoint python newsbrief \\
      scripts/grouping_reference_select.py

Exit codes: 0 done; 1 no database; 2 usage, missing files or answers, the spend
cap, or a judge configuration error; 3 answers still pending, rerun.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import db  # noqa: E402  (path shim above must run first)
import embed_calibrate as ec  # noqa: E402
import grouping_decider_dev as gdd  # noqa: E402
import grouping_label_audit as gla  # noqa: E402

PREREG = str(
    REPO_ROOT / "docs/superpowers/specs/2026-11-26-grouping-reference-selection.md"
)
MODEL = "claude-opus-5-5"
CANDIDATES = ((MODEL, "medium"), (MODEL, "high"))
EFFORTS = ("medium", "high")
AUDIT_WEEK, OTHER_WEEK = "2026-11-16", "2026-11-23"
PASS_SHARE = 0.85
SPEND_CAP = 15.0  # USD across every file this script writes


def reference_file(model: str, effort: str) -> str:
    return f"reference-{model}-{effort}.jsonl"


def gold(week: gdd.Week) -> tuple[dict[str, str], gla.AuditSet, dict[str, str]]:
    """(S, the audit set, the decider's labels). S: drawn pair -> the operator's
    same or different."""
    decider = gla.decider_labels(week)
    s = gla.draw(week, decider)
    answers = gla.load_answers(week.root / gla.ANSWERS_FILE)
    missing = sum(1 for r in s.order if r.key not in answers)
    if missing:
        raise gdd.Usage(
            f"{missing} of {len(s.order)} audit pairs not answered:"
            " finish grouping_label_audit.py --judge",
            ec.EXIT_USAGE,
        )
    g = {r.key: answers[r.key] for r in s.order if answers[r.key] != "unclear"}
    return g, s, decider


def agreement(labels: dict[str, str], g: dict[str, str]) -> int:
    """An unclear or a missing label is a disagreement."""
    return sum(1 for k, v in g.items() if labels.get(k) == v)


def select(scores: dict, n: int):
    """The pre-registered rule. scores: candidate -> agreements on S."""
    passing = [
        (-agree, EFFORTS.index(c[1]), c)
        for c, agree in scores.items()
        if n and agree >= PASS_SHARE * n - 1e-9
    ]
    return min(passing)[2] if passing else None


def _saved(week: gdd.Week, cand) -> tuple[dict[str, str], float, int]:
    return gdd.saved(week.root / reference_file(*cand))


def spent(weeks) -> float:
    return sum(_saved(w, c)[1] for w in weeks for c in CANDIDATES)


def ask(conn, post, cand, week: gdd.Week) -> int:
    """Asks the candidate about the zone pairs not saved yet; pending chunks."""
    model, effort = cand
    judge = ec.Judge(conn, post, model)
    with gdd._effort(effort):
        _, pending = ec._label_pairs(
            judge,
            week.root / reference_file(model, effort),
            [{"a": r.a, "b": r.b} for r in week.zone],
            week.texts,
        )
    return pending


def _opus_low(week: gdd.Week) -> dict[str, str]:
    return {r.key: "same" if r.same else "different" for r in week.rows}


def _zone_line(week: gdd.Week, labels: dict[str, str]) -> str:
    low = _opus_low(week)
    unclear = sum(1 for r in week.zone if labels.get(r.key) == "unclear")
    with_low = sum(1 for r in week.zone if labels.get(r.key) == low[r.key])
    return (
        f"zone={len(week.zone)} unclear={unclear}"
        f" agree_with_opus_low={with_low}/{len(week.zone)}"
    )


def _line(name, labels, g, s, decider) -> str:
    joins = [r for r in s.audit if decider[r.key] == "same" and r.key in g]
    rev = [r for r in s.audit if decider[r.key] != "same" and r.key in g]
    ctl = [r for r in s.controls if r.key in g]

    def ok(rows):
        return sum(1 for r in rows if labels.get(r.key) == g[r.key])

    return (
        f"{name}: agree={agreement(labels, g)}/{len(g)}"
        f" (audit: decider joins, Opus-low not {ok(joins)}/{len(joins)};"
        f" Opus-low same, decider not {ok(rev)}/{len(rev)}; controls {ok(ctl)}/{len(ctl)})"
    )


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="grouping_reference_select", description=__doc__.splitlines()[0]
    )
    return p.parse_args(argv)


def _cap_ok(weeks) -> bool:
    usd = spent(weeks)
    if usd >= SPEND_CAP:
        print(f"spend cap reached: usd={usd:.2f} of {SPEND_CAP:.2f}; stopped")
        return False
    return True


def main(argv=None, *, post=None) -> int:
    try:
        _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ec.EXIT_USAGE
    if not db.is_configured():
        print("no database is configured")
        return ec.EXIT_NONE
    post = post or ec._default_post
    print(f"pre-registered: docs/superpowers/specs/{Path(PREREG).name}")
    try:
        week = gdd.load_week(AUDIT_WEEK)
        other = gdd.load_week(OTHER_WEEK)
        g, s, decider = gold(week)
    except gdd.Usage as u:
        print(str(u))
        return u.code
    weeks = (week, other)
    print(f"week={week.name} S={len(g)} pass at {PASS_SHARE:.0%}")
    print(_line("opus-low (baseline)", _opus_low(week), g, s, decider))
    print(_line(f"{gdd._name(gla.DECIDER)} (baseline)", decider, g, s, decider))

    conn = db.connect()
    try:
        pending = 0
        for cand in CANDIDATES:
            if not _cap_ok(weeks):
                return ec.EXIT_USAGE
            pending += ask(conn, post, cand, week)
        if pending:
            print(f"INCOMPLETE: pending chunks={pending}; rerun to finish")
            return ec.EXIT_PENDING
        scores = {}
        for cand in CANDIDATES:
            labels, usd, _ = _saved(week, cand)
            scores[cand] = agreement(labels, g)
            print(
                f"{_line(gdd._name(cand), labels, g, s, decider)}"
                f" {_zone_line(week, labels)} usd={usd:.4f}"
            )
        chosen = select(scores, len(g))
        print(f"chosen: {gdd._name(chosen) if chosen else 'none'}")
        if chosen is None:
            print(
                "  note: no candidate reaches the bar: the fallback is the operator"
                " labelling a whole zone (its own pre-registration)"
            )
            return ec.EXIT_OK
        if not _cap_ok(weeks):
            return ec.EXIT_USAGE
        if ask(conn, post, chosen, other):
            print(f"INCOMPLETE: week {other.name} pending; rerun to finish")
            return ec.EXIT_PENDING
        labels, usd, _ = _saved(other, chosen)
        print(
            f"week={other.name} {gdd._name(chosen)}: {_zone_line(other, labels)}"
            f" usd={usd:.4f}"
        )
        print(f"total usd={spent(weeks):.4f} (cap {SPEND_CAP:.2f})")
        return ec.EXIT_OK
    except ec.ConfigStop as stop:
        print(f"judge configuration error, stopped: {stop}")
        return ec.EXIT_USAGE
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

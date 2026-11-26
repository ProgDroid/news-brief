"""The operator labels every zone pair of a week: the zone reference.

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-zone-labels.md
(news-brief-al7.7.3). Every zone pair is shown blind (the two texts only) and answered
one event, the same continuing story, different or unclear. Groups are events
(storylines spec 6.1), so only "one event" counts as the same for the reference.

Two stages. Stage 1 is the pairs the label audit drew, asked again in their own
shuffled order; its reading (the audit's eleven, the retest) is reported as soon as
it is done, before stage 2 starts. Stage 2 is the rest of the zone. A week without
audit answers has no stage 1.

    --judge   one pair at a time in this terminal; each answer is saved at once, so
              stopping and rerunning resumes. It stops at the end of stage 1. This
              is the only mode that shows text.
    --report  numbers only. Model agreement is shown on the dev weeks alone.

No API calls and no database.

    docker compose run --rm --entrypoint python newsbrief \\
      scripts/grouping_zone_labels.py --week-ending 2026-11-16 --judge

Exit codes: 0 done; 2 usage or missing files; 3 answers still missing (report).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import embed_calibrate as ec  # noqa: E402  (path shim above must run first)
import grouping_decider_dev as gdd  # noqa: E402
import grouping_label_audit as gla  # noqa: E402
import grouping_rules_check as grc  # noqa: E402

PREREG = str(REPO_ROOT / "docs/superpowers/specs/2026-11-26-grouping-zone-labels.md")
ANSWERS_FILE = "zone_labels.jsonl"
SEED = 20261127
KEYS = {"e": "event", "c": "story", "d": "different", "u": "unclear"}
AUDIT_WEEK = "2026-11-16"
RETEST_MIN = 0.80
OPUS = "claude-opus-5-5"
REFERENCE_EFFORTS = ("medium", "high")  # grouping_reference_select's saved answers


def _audit(week: gdd.Week):
    """The label audit's draw, or None for a week without audit answers."""
    if not (week.root / gla.ANSWERS_FILE).exists():
        return None
    return gla.draw(week, gla.decider_labels(week))


def _shuffled(rows) -> list:
    rows = sorted(rows, key=lambda r: r.key)
    random.Random(SEED).shuffle(rows)
    return rows


def stages(week: gdd.Week) -> tuple[list, list]:
    """(stage 1, stage 2): the audit's pairs, then the rest of the zone."""
    s = _audit(week)
    drawn = {r.key for r in s.order} if s else set()
    first = _shuffled(r for r in week.zone if r.key in drawn)
    return first, _shuffled(r for r in week.zone if r.key not in drawn)


def binary(label: str) -> str | None:
    """The reference: one event is the same; a story or different is not."""
    if label == "event":
        return "same"
    if label in ("story", "different"):
        return "different"
    return None


def reference(answers: dict[str, str]) -> dict[str, str]:
    """Pair key -> same or different; unclear answers are left out."""
    return {k: b for k, a in answers.items() if (b := binary(a)) is not None}


def judge(week: gdd.Week, *, input_fn=input) -> int:
    path = week.root / ANSWERS_FILE
    answers = gla.load_answers(path)
    first, rest = stages(week)
    rows = first + rest
    todo = [r for r in rows if r.key not in answers]
    open_1 = {r.key for r in first if r.key not in answers}
    print(f"{len(rows)} pairs, {len(rows) - len(todo)} already answered.")
    print(f"e  one event: {gla.DEFINITION}")
    print(
        "c  the same continuing story, not one event (a decision and the reactions"
        " to it, a later stage of the same story)"
    )
    print("d  different")
    print("u  unclear: only when the text cannot tell")
    print("Type e, c, d or u and Enter; q stops (answers so far are kept).")
    for i, r in enumerate(todo):
        print(f"\n[{len(rows) - len(todo) + i + 1}/{len(rows)}]")
        print(f"A: {week.texts[r.a]}")
        print(f"B: {week.texts[r.b]}")
        while True:
            try:
                got = input_fn("e / c / d / u / q: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                got = "q"
            if got == "q":
                print("Stopped; rerun to resume.")
                return ec.EXIT_OK
            if got in KEYS:
                break
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": r.key, "label": KEYS[got]}) + "\n")
        stopping = bool(open_1) and bool(rest)
        open_1.discard(r.key)
        if stopping and not open_1:
            print(
                f"\nStage 1 done ({len(first)} pairs). Run --report for its reading;"
                " rerun --judge to start stage 2."
            )
            return ec.EXIT_OK
    print("All pairs answered. Run --report.")
    return ec.EXIT_OK


def eleven_reading(event: int, other: int) -> str:
    """other: answered a continuing story or different."""
    if event + other == 0:
        return "no pair answered: no reading"
    if 2 * other > event + other:
        return "mostly not one event: under the definition, Opus was right on most"
    if 2 * event > event + other:
        return "mostly one event: the operator and Opus draw the event line differently"
    return "split"


def retest_reading(consistent: int, n: int) -> str:
    if n == 0:
        return "no pair to retest: no reading"
    if consistent >= RETEST_MIN * n - 1e-9:
        return "the operator is consistent"
    return "noisy: the next pre-registration must allow for it"


def _retest(week: gdd.Week, s, new: dict[str, str]) -> None:
    old = gla.load_answers(week.root / gla.ANSWERS_FILE)
    eleven = [r for r in s.audit if old.get(r.key) == "same" and not r.same]
    got = [new[r.key] for r in eleven]
    event = got.count("event")
    other = got.count("story") + got.count("different")
    print(
        f"the audit's same where Opus-low said different: n={len(eleven)}"
        f" now event={event} story={got.count('story')}"
        f" different={got.count('different')} unclear={got.count('unclear')}"
        f" -> {eleven_reading(event, other)}"
    )
    picked = {r.key for r in eleven}
    rest = [
        r
        for r in s.order
        if r.key not in picked and old.get(r.key) in ("same", "different")
    ]
    ok = sum(1 for r in rest if binary(new[r.key]) == old[r.key])
    print(
        f"retest on the other audit pairs: consistent={ok}/{len(rest)}"
        f" (needs {RETEST_MIN:.0%}) -> {retest_reading(ok, len(rest))}"
    )


def _models(week: gdd.Week) -> list[tuple[str, dict[str, str]]]:
    out = [("opus-low", {r.key: "same" if r.same else "different" for r in week.rows})]
    for effort in REFERENCE_EFFORTS:
        path = week.root / f"reference-{OPUS}-{effort}.jsonl"
        if path.exists():
            out.append((f"{OPUS}@{effort}", gdd.saved(path)[0]))
    path = week.root / gdd.labels_file(*gla.DECIDER)
    if path.exists():
        out.append((f"{gdd._name(gla.DECIDER)} (decider)", gdd.saved(path)[0]))
    return out


def _agreement(week: gdd.Week, g: dict[str, str]) -> None:
    for name, labels in _models(week):
        agree = sum(1 for k, v in g.items() if labels.get(k) == v)
        print(f"{name}: agree={agree}/{len(g)}")
    path = week.root / gdd.labels_file(*gla.DECIDER)
    if not path.exists():
        return
    decider = gdd.saved(path)[0]
    zone = {r.key for r in week.zone}
    rows = [
        dataclasses.replace(r, same=g[r.key] == "same") if r.key in g else r
        for r in week.rows
        if r.key not in zone or r.key in g
    ]
    sc = grc.score(rows, week.natural, [grc.option_1(r, decider) for r in rows])
    print(
        f"decider with the operator's zone labels: joins={sc.joins}"
        f" correct={sc.correct} wP={sc.precision:.3f} wR={sc.recall:.3f}"
        f" -> {'PASS' if ec.meets_bar(sc.precision, sc.recall) else 'FAIL'}"
        " (dev data: not evidence)"
    )


def report(week: gdd.Week) -> int:
    answers = gla.load_answers(week.root / ANSWERS_FILE)
    s = _audit(week)
    first, _ = stages(week)
    if first:
        open_1 = sum(1 for r in first if r.key not in answers)
        if open_1:
            print(
                f"{open_1} of {len(first)} stage 1 pairs not answered yet: run --judge"
            )
            return ec.EXIT_PENDING
        print(f"stage 1: {len(first)} pairs answered")
        _retest(week, s, answers)
    elif week.name == AUDIT_WEEK:
        print("retest: no audit answers for this week")
    missing = sum(1 for r in week.zone if r.key not in answers)
    if missing:
        print(f"{missing} of {len(week.zone)} zone pairs not answered yet: run --judge")
        return ec.EXIT_PENDING
    new = {r.key: answers[r.key] for r in week.zone}
    counts = {lab: list(new.values()).count(lab) for lab in KEYS.values()}
    print(
        f"week={week.name} zone={len(week.zone)} "
        + " ".join(f"{lab}={n}" for lab, n in counts.items())
    )
    if week.name not in gdd.DEV_WEEKS:
        print("not a dev week: model agreement is not shown (pre-registered)")
        return ec.EXIT_OK
    _agreement(week, reference(new))
    return ec.EXIT_OK


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="grouping_zone_labels", description=__doc__.splitlines()[0]
    )
    p.add_argument("--week-ending", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--judge", action="store_true")
    mode.add_argument("--report", action="store_true")
    return p.parse_args(argv)


def main(argv=None, *, input_fn=input) -> int:
    try:
        ns = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ec.EXIT_USAGE
    print(f"pre-registered: docs/superpowers/specs/{Path(PREREG).name}")
    try:
        week = gdd.load_week(ns.week_ending)
        if ns.judge:
            return judge(week, input_fn=input_fn)
        return report(week)
    except gdd.Usage as u:
        print(str(u))
        return u.code


if __name__ == "__main__":
    sys.exit(main())

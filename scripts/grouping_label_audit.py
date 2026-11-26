"""Ask the operator about the pairs where the decider and the Opus labels disagree.

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-label-audit.md
(news-brief-al7.7.1). The pairs are every zone pair of the week where the decider's join
differs from the Opus label, plus up to CONTROLS_PER_SIDE agreeing pairs on each side.
They are shown blind (the two texts only), in a fixed shuffled order.

    --judge   one pair at a time in this terminal; each answer is saved at once, so
              stopping and rerunning resumes. This is the only mode that shows text.
    --report  numbers only: the pre-registered reading and the rescored decider.

No API calls and no database.

    docker compose run --rm --entrypoint python newsbrief \\
      scripts/grouping_label_audit.py --week-ending 2026-11-16 --judge

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
import grouping_rules_check as grc  # noqa: E402

PREREG = str(REPO_ROOT / "docs/superpowers/specs/2026-11-26-grouping-label-audit.md")
DECIDER = ("claude-haiku-5-5", "high")
CONTROLS_PER_SIDE = 4
SEED = 20261126
H_NOT_USABLE, H_HOLDS = 0.50, 0.20
CONTROL_MIN_SHARE = 0.75
ANSWERS_FILE = "audit_labels.jsonl"
# The judge prompt's own definition (embed_calibrate._PAIR_SYSTEM); a test keeps
# the two in step.
DEFINITION = (
    "'same' when both report the same event: one happening (one decision, one "
    "attack, one release of figures), not merely the same topic, country or "
    "company"
)
KEYS = {"s": "same", "d": "different", "u": "unclear"}


@dataclasses.dataclass(frozen=True)
class AuditSet:
    audit: list  # Rows whose decider join differs from the Opus label
    controls: list  # Rows where they agree
    order: list  # every shown Row, shuffled


def decider_labels(week: gdd.Week) -> dict[str, str]:
    labels, _, _ = gdd.saved(week.root / gdd.labels_file(*DECIDER))
    missing = [r.key for r in week.zone if r.key not in labels]
    if missing:
        raise gdd.Usage(
            f"{gdd._name(DECIDER)} has {len(missing)} unanswered zone pairs:"
            " run grouping_decider_dev.py --dev first",
            ec.EXIT_USAGE,
        )
    return labels


def draw(week: gdd.Week, decider: dict[str, str]) -> AuditSet:
    joins = {r.key: decider[r.key] == "same" for r in week.zone}
    audit = [r for r in week.zone if joins[r.key] != r.same]
    rng = random.Random(SEED)
    controls = []
    for side in (True, False):
        pool = sorted(
            (r for r in week.zone if joins[r.key] == r.same and r.same == side),
            key=lambda r: r.key,
        )
        controls += rng.sample(pool, min(CONTROLS_PER_SIDE, len(pool)))
    order = sorted(audit + controls, key=lambda r: r.key)
    rng.shuffle(order)
    return AuditSet(audit=audit, controls=controls, order=order)


def load_answers(path: Path) -> dict[str, str]:
    out = {}
    for r in ec._read_jsonl(path):
        out[r["key"]] = r["label"]
    return out


def judge(week: gdd.Week, s: AuditSet, *, input_fn=input) -> int:
    path = week.root / ANSWERS_FILE
    answers = load_answers(path)
    todo = [r for r in s.order if r.key not in answers]
    print(f"{len(s.order)} pairs, {len(s.order) - len(todo)} already answered.")
    print(f"Label a pair {DEFINITION}. 'unclear' only when the text cannot tell.")
    print("Type s, d or u and Enter; q stops (answers so far are kept).")
    for r in todo:
        done = len(s.order) - len(todo) + todo.index(r) + 1
        print(f"\n[{done}/{len(s.order)}]")
        print(f"A: {week.texts[r.a]}")
        print(f"B: {week.texts[r.b]}")
        while True:
            try:
                got = input_fn("s / d / u / q: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                got = "q"
            if got == "q":
                print("Stopped; rerun to resume.")
                return ec.EXIT_OK
            if got in KEYS:
                break
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": r.key, "label": KEYS[got]}) + "\n")
    print("All pairs answered. Run --report.")
    return ec.EXIT_OK


def reading(h: int, d: int) -> str:
    if d == 0:
        return "no audit pair answered same or different: no reading"
    share = h / d
    if share >= H_NOT_USABLE:
        return "the Opus labels are not a usable reference in the zone"
    if share <= H_HOLDS:
        return "the Opus labels hold; decider tuning stops"
    return "mixed: the operator decides"


def report(week: gdd.Week, s: AuditSet, decider: dict[str, str]) -> int:
    answers = load_answers(week.root / ANSWERS_FILE)
    missing = sum(1 for r in s.order if r.key not in answers)
    if missing:
        print(f"{missing} of {len(s.order)} pairs not answered yet: run --judge")
        return ec.EXIT_PENDING
    h = o = unclear = 0
    for r in s.audit:
        a = answers[r.key]
        if a == "unclear":
            unclear += 1
            continue
        if (a == "same") == (decider[r.key] == "same"):
            h += 1
        else:
            o += 1
    d = h + o
    to_same = sum(1 for r in s.audit if decider[r.key] == "same")
    print(
        f"week={week.name} decider={gdd._name(DECIDER)} audit={len(s.audit)}"
        f" (decider joins, Opus not: {to_same}; Opus same, decider not:"
        f" {len(s.audit) - to_same})"
    )
    share = f"{h / d:.3f}" if d else "n/a"
    print(
        f"operator sides with the decider H={h}, with Opus O={o}, unclear={unclear};"
        f" H/D={share} -> {reading(h, d)}"
    )
    c_ok = c_n = 0
    for r in s.controls:
        a = answers[r.key]
        if a == "unclear":
            continue
        c_n += 1
        c_ok += (a == "same") == r.same
    c_share = c_ok / c_n if c_n else 0.0
    c_note = (
        "the operator's notion matches the prompt's"
        if c_n and c_share >= CONTROL_MIN_SHARE
        else "the operator's notion differs from the prompt's: the reading is about"
        " the definition"
    )
    print(f"controls: agree {c_ok}/{c_n} (needs {CONTROL_MIN_SHARE:.0%}) -> {c_note}")

    def scored(rows, tag):
        sc = grc.score(rows, week.natural, [grc.option_1(r, decider) for r in rows])
        print(
            f"{tag}: joins={sc.joins} correct={sc.correct} wP={sc.precision:.3f}"
            f" wR={sc.recall:.3f} -> {'PASS' if ec.meets_bar(sc.precision, sc.recall) else 'FAIL'}"
            " (dev data: not evidence)"
        )

    scored(week.rows, "with Opus labels")
    rows = []
    for r in week.rows:
        a = answers.get(r.key)
        if a is None:
            rows.append(r)
        elif a != "unclear":
            rows.append(dataclasses.replace(r, same=a == "same"))
    scored(rows, "with the operator's answers on the audited pairs")
    return ec.EXIT_OK


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="grouping_label_audit", description=__doc__.splitlines()[0]
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
        decider = decider_labels(week)
    except gdd.Usage as u:
        print(str(u))
        return u.code
    s = draw(week, decider)
    if ns.judge:
        return judge(week, s, input_fn=input_fn)
    return report(week, s, decider)


if __name__ == "__main__":
    sys.exit(main())

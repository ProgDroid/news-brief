"""Choose the grouping decider on the dev weeks, then score it on the test week.

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-decider-preregistration.md
(news-brief-k8s). Option 1 from grouping_rules_check stays as it is (rule A joins
without asking; the decider rules on every labelled pair from 0.65 up that rule A
does not join). Only the decider's model and effort vary, over CANDIDATES. The
decider gets embed_calibrate's pair request unchanged apart from those two.

    --dev   every candidate on both dev weeks (asking only what is not saved yet),
            then the pre-registered selection rule: prints the chosen candidate.
    --test  the same selection, worked out again from the saved dev answers (no
            calls), then that one candidate scored once on the test week.

Answers are saved per candidate and week in the week's folder, so a rerun asks
nothing new. Prints numbers only, never a title or a text.

    docker compose run --rm --entrypoint python newsbrief \\
      scripts/grouping_decider_dev.py --dev

Exit codes: 0 scored; 1 no database; 2 usage, missing files, dev answers missing
for --test, or a judge configuration error; 3 answers still pending, rerun.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import common  # noqa: E402  (path shim above must run first)
import db  # noqa: E402
import embed_calibrate as ec  # noqa: E402
import grouping_rules_check as grc  # noqa: E402

PREREG = str(
    REPO_ROOT / "docs/superpowers/specs/2026-11-26-grouping-decider-preregistration.md"
)
CANDIDATES = (
    ("claude-haiku-5-5", "low"),
    ("claude-haiku-5-5", "medium"),
    ("claude-haiku-5-5", "high"),
    ("claude-sonnet-5-5", "low"),
    ("claude-sonnet-5-5", "medium"),
)
EFFORTS = ("low", "medium", "high")
MONTHLY_CAP = 10.0  # estimated USD a month, an upper bound
DEV_WEEKS = ("2026-11-23", "2026-11-16")
TEST_WEEK = "2026-11-09"


class Usage(Exception):
    """A missing file or a missing answer: print it and exit 2 or 3."""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Week:
    name: str
    root: Path
    rows: list
    zone: list
    texts: dict
    natural: dict


@dataclass(frozen=True)
class WeekResult:
    precision: float
    recall: float
    est_month: float
    usd: float  # spend of the saved answers
    pairs: int  # pairs the saved answers labelled


def labels_file(model: str, effort: str) -> str:
    """The tested decider's saved labels are reused as its dev answers."""
    if (model, effort) == (grc.HAIKU_MODEL, "low"):
        return grc.HAIKU_FILE
    return f"decider-{model}-{effort}.jsonl"


def zone_pool(natural: dict[int, int]) -> int:
    """The week's neighbour pairs from ZONE_SIM up."""
    return sum(n for b, n in natural.items() if ec.GRID[b] >= grc.ZONE_SIM - 1e-9)


def est_monthly(cost_per_pair: float, pool: int) -> float:
    return cost_per_pair * pool * 52 / 12


def _name(c) -> str:
    return f"{c[0]}@{c[1]}"


def select(results: dict) -> tuple[tuple[str, str] | None, list[str]]:
    """The pre-registered rule. results: candidate -> [WeekResult per dev week]."""
    notes, passing = [], []
    for c, weeks in results.items():
        prec_ok = all(w.precision >= ec.BAR_PRECISION for w in weeks)
        bar_ok = prec_ok and all(w.recall >= ec.BAR_RECALL for w in weeks)
        cap_ok = all(w.est_month <= MONTHLY_CAP for w in weeks)
        if bar_ok and not cap_ok:
            notes.append(
                f"{_name(c)} passes the bar on both weeks but its estimate is over the"
                f" ${MONTHLY_CAP:.2f} cap: running it on the test week is the operator's call"
            )
        if bar_ok and cap_ok:
            usd = sum(w.usd for w in weeks)
            pairs = sum(w.pairs for w in weeks)
            passing.append((usd / pairs if pairs else 0.0, EFFORTS.index(c[1]), c))
    if not passing:
        notes.append(
            "no candidate passes the bar on both dev weeks within the cap: stop"
        )
        return None, notes
    return min(passing)[2], notes


def load_week(name: str) -> Week:
    root = common.DATA_DIR / "embed_calibration" / name
    missing = [
        n
        for n in ("snapshot.json", "pairs.json", "labels.jsonl")
        if not (root / n).exists()
    ]
    if missing:
        raise Usage(
            f"run embed_calibrate for week {name} first (missing: {', '.join(missing)})",
            ec.EXIT_USAGE,
        )
    data = json.loads((root / "pairs.json").read_text(encoding="utf-8"))
    if grc.SPACE not in data["natural"]:
        raise Usage(f"week {name} has no pool for {grc.SPACE}", ec.EXIT_USAGE)
    labels: dict[str, str] = {}
    for r in ec._read_jsonl(root / "labels.jsonl"):
        labels.update(r.get("labels") or {})
    if any(
        grc.SPACE in p["in_pool"] and f"{p['a']}-{p['b']}" not in labels
        for p in data["pairs"]
    ):
        raise Usage(
            f"week {name}: pairs not yet labelled, finish embed_calibrate",
            ec.EXIT_PENDING,
        )
    items = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))["items"]
    outlets = {it["id"]: it["outlet"] for it in items}
    rows = grc.labelled_rows(data["pairs"], labels, outlets, grc.SPACE)
    return Week(
        name=name,
        root=root,
        rows=rows,
        zone=[r for r in rows if grc.in_zone(r)],
        texts={it["id"]: it["text"] for it in items},
        natural={int(b): n for b, n in data["natural"][grc.SPACE].items()},
    )


def saved(path: Path) -> tuple[dict[str, str], float, int]:
    """(labels, spend, pairs labelled) from a candidate's saved answers."""
    labels, usd, pairs = {}, 0.0, 0
    for r in ec._read_jsonl(path):
        got = r.get("labels") or {}
        labels.update(got)
        usd += r.get("usd", 0.0)
        pairs += len(got)
    return labels, usd, pairs


@contextlib.contextmanager
def _effort(effort: str):
    """embed_calibrate's request builder reads JUDGE_EFFORT when it builds."""
    before = ec.JUDGE_EFFORT
    ec.JUDGE_EFFORT = effort
    try:
        yield
    finally:
        ec.JUDGE_EFFORT = before


def ask(conn, post, cand, week: Week) -> int:
    """Asks the candidate about the zone pairs not saved yet; pending chunks."""
    model, effort = cand
    judge = ec.Judge(conn, post, model)
    with _effort(effort):
        _, pending = ec._label_pairs(
            judge,
            week.root / labels_file(model, effort),
            [{"a": r.a, "b": r.b} for r in week.zone],
            week.texts,
        )
    return pending


def result(cand, week: Week) -> WeekResult | None:
    """Scored from the saved answers; None while a zone pair is unanswered."""
    labels, usd, pairs = saved(week.root / labels_file(*cand))
    if any(r.key not in labels for r in week.zone):
        return None
    s = grc.score(week.rows, week.natural, [grc.option_1(r, labels) for r in week.rows])
    per_pair = usd / pairs if pairs else 0.0
    agree = sum(
        1 for r in week.zone if labels[r.key] == ("same" if r.same else "different")
    )
    verdict = "PASS" if ec.meets_bar(s.precision, s.recall) else "FAIL"
    month = est_monthly(per_pair, zone_pool(week.natural))
    print(
        f"week={week.name} decider={_name(cand)} zone={len(week.zone)}"
        f" agree={agree}/{len(week.zone)} joins={s.joins} correct={s.correct}"
        f" wP={s.precision:.3f} wR={s.recall:.3f} -> {verdict}"
        f" usd_per_pair={per_pair:.7f} est_month_usd={month:.2f}"
        f" (upper bound; cap {MONTHLY_CAP:.2f})"
    )
    return WeekResult(s.precision, s.recall, month, usd, pairs)


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="grouping_decider_dev", description=__doc__.splitlines()[0]
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dev", action="store_true")
    mode.add_argument("--test", action="store_true")
    return p.parse_args(argv)


def main(argv=None, *, post=None) -> int:
    try:
        ns = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ec.EXIT_USAGE
    if not db.is_configured():
        print("no database is configured")
        return ec.EXIT_NONE
    post = post or ec._default_post
    print(f"pre-registered: docs/superpowers/specs/{Path(PREREG).name}")
    try:
        weeks = [load_week(n) for n in DEV_WEEKS]
        if ns.test:
            test_week = load_week(TEST_WEEK)
    except Usage as u:
        print(str(u))
        return u.code

    conn = db.connect()
    try:
        if ns.dev:
            pending = 0
            for cand in CANDIDATES:
                for w in weeks:
                    pending += ask(conn, post, cand, w)
            if pending:
                print(f"INCOMPLETE: pending chunks={pending}; rerun to finish")
                return ec.EXIT_PENDING
        results = {}
        for cand in CANDIDATES:
            per = [result(cand, w) for w in weeks]
            if any(x is None for x in per):
                print(f"{_name(cand)} has unanswered dev pairs: run --dev first")
                return ec.EXIT_USAGE
            results[cand] = per
        chosen, notes = select(results)
        print(f"chosen: {_name(chosen) if chosen else 'none'}")
        for n in notes:
            print(f"  note: {n}")
        if ns.dev or chosen is None:
            return ec.EXIT_OK
        if ask(conn, post, chosen, test_week):
            print("INCOMPLETE: test-week answers pending; rerun --test")
            return ec.EXIT_PENDING
        result(chosen, test_week)
        return ec.EXIT_OK
    except ec.ConfigStop as stop:
        print(f"judge configuration error, stopped: {stop}")
        return ec.EXIT_USAGE
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

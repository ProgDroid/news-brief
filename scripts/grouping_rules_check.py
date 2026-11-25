"""Score the pre-registered grouping rules on one calibration week.

The rules, thresholds and pass criteria are fixed in
docs/superpowers/specs/2026-11-25-grouping-rules-preregistration.md (news-brief-8om) and
are constants here. They are never changed after a week's result has been seen: a
change is a new pre-registration with a new week.

It reads what scripts/embed_calibrate.py wrote for the week (snapshot.json,
pairs.json, labels.jsonl), asks Haiku about the pairs in the decision zone with
embed_calibrate's own pair request, and caches those labels in haiku_labels.jsonl,
so a rerun asks nothing new. It prints numbers only, never a title or a text.

    docker compose run --rm --entrypoint python newsbrief \\
      scripts/grouping_rules_check.py --week-ending 2026-11-16

Exit codes: 0 scored; 1 no database; 2 usage, missing files, or a judge
configuration error; 3 labels or Haiku chunks still pending, rerun.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import common  # noqa: E402  (path shim above must run first)
import db  # noqa: E402
import embed_calibrate as ec  # noqa: E402

PREREG = str(
    REPO_ROOT / "docs/superpowers/specs/2026-11-25-grouping-rules-preregistration.md"
)
SPACE = "modal:Octen/Octen-Embedding-8B@1024"
RULE_A_SIM, RULE_B_SIM, ZONE_SIM = 0.80, 0.75, 0.65
A_PRECISION, B_PRECISION = 0.95, 0.90
HAIKU_MODEL = "claude-haiku-5-5"
HAIKU_FILE = "haiku_labels.jsonl"


@dataclass(frozen=True)
class Row:
    key: str  # "a-b", the labels' pair key
    a: int
    b: int
    sim: float
    same: bool  # the Opus label
    same_outlet: bool


@dataclass(frozen=True)
class Score:
    joins: int
    correct: int
    precision: float
    recall: float


def rule_a(r: Row) -> bool:
    return not r.same_outlet and r.sim >= RULE_A_SIM


def rule_b(r: Row) -> bool:
    return not r.same_outlet and r.sim >= RULE_B_SIM


def in_zone(r: Row) -> bool:
    """The pairs Haiku decides: from ZONE_SIM up, whatever rule A does not join."""
    return r.sim >= ZONE_SIM and not rule_a(r)


def option_1(r: Row, haiku: dict[str, str]) -> bool:
    return rule_a(r) or (in_zone(r) and haiku.get(r.key) == "same")


def labelled_rows(pairs, labels, outlets, space) -> list[Row]:
    """embed_calibrate.scoring_rows with the pair key and the outlet test kept:
    this space's own pool, `unclear` left out."""
    rows = []
    for p in pairs:
        if space not in p["in_pool"]:
            continue
        key = f"{p['a']}-{p['b']}"
        if labels[key] == "unclear" or p["sims"][space] is None:
            continue
        rows.append(
            Row(
                key=key,
                a=p["a"],
                b=p["b"],
                sim=p["sims"][space],
                same=labels[key] == "same",
                same_outlet=outlets[p["a"]] == outlets[p["b"]],
            )
        )
    return rows


def score(rows: list[Row], natural: dict[int, int], pred: list[bool]) -> Score:
    w, _ = ec.space_weights([r.sim for r in rows], natural)
    p, rec, _ = ec.weighted_prf(pred, [r.same for r in rows], w)
    return Score(
        joins=sum(pred),
        correct=sum(1 for x, r in zip(pred, rows) if x and r.same),
        precision=p,
        recall=rec,
    )


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """The Wilson score interval's lower bound for k successes in n."""
    if n == 0:
        return math.nan
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _parse(argv):
    p = argparse.ArgumentParser(
        prog="grouping_rules_check", description=__doc__.splitlines()[0]
    )
    p.add_argument(
        "--week-ending",
        required=True,
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc),
    )
    return p.parse_args(argv)


def main(argv=None, *, post=None) -> int:
    try:
        ns = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else ec.EXIT_USAGE
    if not db.is_configured():
        print("no database is configured")
        return ec.EXIT_NONE
    week = ns.week_ending.date().isoformat()
    root = common.DATA_DIR / "embed_calibration" / week
    missing = [
        n
        for n in ("snapshot.json", "pairs.json", "labels.jsonl")
        if not (root / n).exists()
    ]
    if missing:
        print(
            f"run embed_calibrate for week {week} first (missing: {', '.join(missing)})"
        )
        return ec.EXIT_USAGE
    data = json.loads((root / "pairs.json").read_text(encoding="utf-8"))
    if SPACE not in data["natural"]:
        print(
            f"pairs.json has no pool for {SPACE}: run embed_calibrate with that space"
        )
        return ec.EXIT_USAGE
    labels: dict[str, str] = {}
    for r in ec._read_jsonl(root / "labels.jsonl"):
        labels.update(r.get("labels") or {})
    unlabelled = sum(
        1
        for p in data["pairs"]
        if SPACE in p["in_pool"] and f"{p['a']}-{p['b']}" not in labels
    )
    if unlabelled:
        print(f"{unlabelled} pair(s) not yet labelled: finish embed_calibrate first")
        return ec.EXIT_PENDING
    items = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))["items"]
    outlets = {it["id"]: it["outlet"] for it in items}
    texts = {it["id"]: it["text"] for it in items}
    natural = {int(b): n for b, n in data["natural"][SPACE].items()}

    rows = labelled_rows(data["pairs"], labels, outlets, SPACE)
    # Self-check against the calibration's own row filter.
    calib_rows, unclear = ec.scoring_rows(data["pairs"], labels, SPACE)
    if len(calib_rows) != len(rows):
        print(f"row count {len(rows)} differs from embed_calibrate's {len(calib_rows)}")
        return ec.EXIT_USAGE
    print(
        f"week={week} space={SPACE} labelled={len(rows)} unclear={unclear}"
        f" (pre-registered: docs/superpowers/specs/{Path(PREREG).name})"
    )

    a = score(rows, natural, [rule_a(r) for r in rows])
    print(
        f"rule A (different outlets, sim>={RULE_A_SIM:.2f}): joins={a.joins}"
        f" correct={a.correct} wilson95_lo={wilson_lower(a.correct, a.joins):.3f}"
        f" wP={a.precision:.3f} wR={a.recall:.3f}"
        f" -> {_verdict(a.precision >= A_PRECISION)} (needs wP>={A_PRECISION:.2f})"
    )
    b = score(rows, natural, [rule_b(r) for r in rows])
    print(
        f"rule B (different outlets, sim>={RULE_B_SIM:.2f}): joins={b.joins}"
        f" correct={b.correct} wP={b.precision:.3f} wR={b.recall:.3f}"
        f" -> {_verdict(b.precision >= B_PRECISION)} (needs wP>={B_PRECISION:.2f})"
    )

    zone = [r for r in rows if in_zone(r)]
    conn = db.connect()
    try:
        judge = ec.Judge(conn, post or ec._default_post, HAIKU_MODEL)
        try:
            haiku, pending = ec._label_pairs(
                judge, root / HAIKU_FILE, [{"a": r.a, "b": r.b} for r in zone], texts
            )
        except ec.ConfigStop as stop:
            print(f"judge configuration error, stopped: {stop}")
            return ec.EXIT_USAGE
    finally:
        conn.close()
    print(
        f"haiku: model={HAIKU_MODEL} calls={judge.calls} usd={judge.usd:.4f} (this run)"
    )
    if pending:
        print(f"INCOMPLETE: pending haiku chunks={pending}; rerun to finish option 1")
        return ec.EXIT_PENDING

    agree = sum(
        1 for r in zone if haiku.get(r.key) == ("same" if r.same else "different")
    )
    o = score(rows, natural, [option_1(r, haiku) for r in rows])
    print(
        f"option 1 (rule A, or sim>={ZONE_SIM:.2f} and Haiku says same):"
        f" zone={len(zone)} haiku_agree={agree}/{len(zone)}"
        f" joins={o.joins} correct={o.correct} wP={o.precision:.3f} wR={o.recall:.3f}"
        f" -> {_verdict(ec.meets_bar(o.precision, o.recall))}"
        f" (needs wP>={ec.BAR_PRECISION:.2f} and wR>={ec.BAR_RECALL:.2f})"
    )
    return ec.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

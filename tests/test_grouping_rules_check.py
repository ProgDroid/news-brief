"""scripts/grouping_rules_check.py (news-brief-8om).

The rules are pre-registered in
docs/superpowers/specs/2026-11-25-grouping-rules-preregistration.md. These tests pin
the thresholds at their boundaries, check that the scorer uses embed_calibrate's own
weighting, and run the script end to end on invented files (Widget, Gizmo) with a
stub judge: nothing here reaches Anthropic. The end-to-end tests are DB-gated.
"""

import json
import math
import re
import sys
from pathlib import Path

import pytest

import common
import db

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import embed_calibrate as ec  # noqa: E402
import grouping_rules_check as grc  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (pgvector image).",
)

SPACE = "modal:Octen/Octen-Embedding-8B@1024"


def test_the_pre_registered_values_are_the_ones_in_force():
    """A change here is a new pre-registration with a new week, never an edit."""
    assert grc.SPACE == SPACE
    assert (grc.RULE_A_SIM, grc.RULE_B_SIM, grc.ZONE_SIM) == (0.80, 0.75, 0.65)
    assert (grc.A_PRECISION, grc.B_PRECISION) == (0.95, 0.90)
    assert grc.HAIKU_MODEL == "claude-haiku-5-5"
    assert (ec.BAR_PRECISION, ec.BAR_RECALL) == (0.90, 0.60)
    assert Path(grc.PREREG).exists()


def _row(sim, same=True, same_outlet=False, key="1-2"):
    a, b = (int(x) for x in key.split("-"))
    return grc.Row(key=key, a=a, b=b, sim=sim, same=same, same_outlet=same_outlet)


def test_rule_a_joins_cross_outlet_pairs_from_0_80():
    assert grc.rule_a(_row(0.80))
    assert not grc.rule_a(_row(0.7999))
    assert not grc.rule_a(_row(0.99, same_outlet=True))


def test_rule_b_joins_cross_outlet_pairs_from_0_75():
    assert grc.rule_b(_row(0.75))
    assert not grc.rule_b(_row(0.7499))
    assert not grc.rule_b(_row(0.99, same_outlet=True))


def test_haiku_decides_everything_from_0_65_that_rule_a_does_not_join():
    assert grc.in_zone(_row(0.65))
    assert grc.in_zone(_row(0.79))
    assert grc.in_zone(_row(0.97, same_outlet=True))
    assert not grc.in_zone(_row(0.6499))
    assert not grc.in_zone(_row(0.6499, same_outlet=True))
    assert not grc.in_zone(_row(0.80))  # rule A joins it without asking


def test_option_1_joins_on_rule_a_or_a_haiku_same_in_the_zone():
    h = {"1-2": "same", "3-4": "different", "5-6": "unclear"}
    assert grc.option_1(_row(0.85, key="7-8"), h)  # rule A, never asked
    assert grc.option_1(_row(0.70, key="1-2"), h)
    assert grc.option_1(_row(0.95, same_outlet=True, key="1-2"), h)
    assert not grc.option_1(_row(0.70, key="3-4"), h)
    assert not grc.option_1(_row(0.70, key="5-6"), h)  # unclear is no join
    assert not grc.option_1(_row(0.60, key="1-2"), h)  # below the zone


def test_scores_use_the_calibration_band_weights():
    """Weighted, not counted: 80% of the natural pairs sit in band 0.30, 10% in
    0.75 and 10% in 0.90. Rule B joins 5 correct pairs at 0.76 (weight 0.02
    each) and 10 pairs at 0.92, 5 of them correct (weight 0.01 each): wP =
    0.15 / 0.20 = 0.75, while the plain count is 10 of 15."""
    rows = (
        [_row(0.76, same=True, key=f"{i}-{i + 100}") for i in range(5)]
        + [_row(0.92, same=True, key=f"{i}-{i + 200}") for i in range(5)]
        + [_row(0.92, same=False, key=f"{i}-{i + 300}") for i in range(5)]
        + [_row(0.31, same=False, key=f"{i}-{i + 400}") for i in range(5)]
    )
    natural = {0: 80, 9: 10, 12: 10}
    s = grc.score(rows, natural, [grc.rule_b(r) for r in rows])
    assert s.joins == 15 and s.correct == 10
    assert s.precision == pytest.approx(0.75)
    assert s.recall == pytest.approx(1.0)


def test_wilson_lower_bound():
    assert grc.wilson_lower(50, 50) == pytest.approx(0.9287, abs=1e-4)
    assert grc.wilson_lower(0, 10) == pytest.approx(0.0)
    assert math.isnan(grc.wilson_lower(0, 0))


# ---- end to end ----

_ITEM = re.compile(r"Widget item (\d+) event (\d+)")


class StubHaiku:
    """Labels a pair same when both texts name the same event number."""

    def __init__(self, fail=False):
        self.fail, self.payloads = fail, []

    def __call__(self, payload):
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("boom")
        text = payload["messages"][0]["content"]
        answers = []
        for lab, a, b in re.findall(r"(P\d+)\nA: (.*)\nB: (.*)", text):
            ka, kb = _ITEM.search(a).group(2), _ITEM.search(b).group(2)
            answers.append({"pair": lab, "label": "same" if ka == kb else "different"})
        return {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "name": ec.PAIR_TOOL,
                    "input": {"answers": answers},
                }
            ],
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }


def _week(root: Path) -> None:
    """Twelve pairs in one space's pool. Items 1-24; event = pair number,
    except the four "different" pairs, whose B item names another event."""
    root.mkdir(parents=True)
    plan = [  # (sim, opus label, same outlet)
        (0.90, "same", False),
        (0.88, "same", False),
        (0.85, "same", False),
        (0.82, "same", False),
        (0.95, "different", True),  # a recurring series: zone, Haiku says different
        (0.93, "same", True),  # an outlet's own update: zone, Haiku says same
        (0.78, "same", False),
        (0.76, "same", False),
        (0.72, "different", False),
        (0.70, "same", False),
        (0.60, "same", False),  # below the zone: missed
        (0.55, "unclear", False),  # left out
    ]
    items, pairs, labels = [], [], {}
    for n, (sim, label, same_outlet) in enumerate(plan):
        a, b = 2 * n + 1, 2 * n + 2
        ev_b = n if label != "different" else 100 + n
        items += [
            {
                "id": a,
                "outlet": "Outlet One",
                "created_at": "2026-11-10T00:00:00+00:00",
                "title": f"Widget item {a}",
                "text": f"Widget item {a} event {n}",
            },
            {
                "id": b,
                "outlet": "Outlet One" if same_outlet else "Outlet Two",
                "created_at": "2026-11-10T01:00:00+00:00",
                "title": f"Widget item {b}",
                "text": f"Widget item {b} event {ev_b}",
            },
        ]
        pairs.append(
            {"a": a, "b": b, "sims": {SPACE: sim}, "title_sim": 0.1, "in_pool": [SPACE]}
        )
        labels[f"{a}-{b}"] = label
    natural = {}
    for sim, _, _ in plan:
        natural[str(ec.band_of(sim))] = natural.get(str(ec.band_of(sim)), 0) + 1
    (root / "snapshot.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    (root / "pairs.json").write_text(
        json.dumps({"meta": {}, "natural": {SPACE: natural}, "pairs": pairs}),
        encoding="utf-8",
    )
    (root / "labels.jsonl").write_text(
        json.dumps({"labels": labels}) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "_sleep", lambda s: None)
    # Twelve pairs over a dozen bands: at the real floor of 5 every band would
    # be thin and weigh 0. One pair a band keeps the weights live.
    monkeypatch.setattr(ec, "MIN_BAND_LABELLED", 1)
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


WEEK = ["--week-ending", "2026-11-16"]


@needs_db
def test_end_to_end_scores_each_rule_and_caches_haiku(conn, capsys):
    root = common.DATA_DIR / "embed_calibration" / "2026-11-16"
    _week(root)
    haiku = StubHaiku()
    assert grc.main(WEEK, post=haiku) == 0
    out = capsys.readouterr().out
    assert "labelled=11 unclear=1" in out
    # Rule A: the four cross-outlet pairs >= 0.80, all correct.
    assert re.search(r"rule A .*joins=4 correct=4 .*-> PASS", out)
    # Rule B adds 0.78 and 0.76: six joins, all correct.
    assert re.search(r"rule B .*joins=6 correct=6 .*-> PASS", out)
    # The zone is 0.95 and 0.93 (same outlet) and 0.78, 0.76, 0.72, 0.70.
    assert re.search(r"option 1 .*zone=6 haiku_agree=6/6", out)
    assert "Widget" not in out  # numbers only
    # Haiku got embed_calibrate's own pair request; only the model differs.
    assert {p["model"] for p in haiku.payloads} == {"claude-haiku-5-5"}
    first = haiku.payloads[0]
    texts = {
        it["id"]: it["text"]
        for it in json.loads((root / "snapshot.json").read_text(encoding="utf-8"))[
            "items"
        ]
    }
    sent = re.findall(
        r"P\d+\nA: Widget item (\d+) .*\nB: Widget item (\d+) ",
        first["messages"][0]["content"],
    )
    expected, _ = ec.pair_request(
        "claude-haiku-5-5", [(int(a), int(b)) for a, b in sent], texts
    )
    assert first == expected
    # A rerun reads Haiku's labels back and asks nothing.
    again = StubHaiku()
    assert grc.main(WEEK, post=again) == 0
    assert again.payloads == []


@needs_db
def test_option_1_fails_when_recall_is_short(conn, capsys):
    root = common.DATA_DIR / "embed_calibration" / "2026-11-16"
    _week(root)
    assert grc.main(WEEK, post=StubHaiku()) == 0
    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("option 1")
    )
    # Every same pair from 0.65 up is joined; only the 0.60 one is missed, so
    # recall sits below 1 and the verdict follows the spec's bar.
    m = re.search(r"wP=([\d.]+) wR=([\d.]+) -> (PASS|FAIL)", line)
    p, r, verdict = float(m.group(1)), float(m.group(2)), m.group(3)
    assert p == pytest.approx(1.0) and r < 1.0
    assert verdict == ("PASS" if ec.meets_bar(p, r) else "FAIL")


@needs_db
def test_a_failed_haiku_call_leaves_option_1_pending(conn, capsys):
    root = common.DATA_DIR / "embed_calibration" / "2026-11-16"
    _week(root)
    assert grc.main(WEEK, post=StubHaiku(fail=True)) == ec.EXIT_PENDING
    out = capsys.readouterr().out
    assert "rule A" in out and "INCOMPLETE" in out
    assert not any(ln.startswith("option 1") for ln in out.splitlines())


@needs_db
def test_missing_files_or_unfinished_labels_refuse(conn, capsys):
    assert grc.main(WEEK, post=StubHaiku()) == ec.EXIT_USAGE
    assert "run embed_calibrate" in capsys.readouterr().out
    root = common.DATA_DIR / "embed_calibration" / "2026-11-16"
    _week(root)
    (root / "labels.jsonl").write_text(
        json.dumps({"labels": {"1-2": "same"}}) + "\n", encoding="utf-8"
    )
    assert grc.main(WEEK, post=StubHaiku()) == ec.EXIT_PENDING
    assert "not yet labelled" in capsys.readouterr().out

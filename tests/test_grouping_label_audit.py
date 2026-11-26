"""scripts/grouping_label_audit.py (news-brief-al7.7.1).

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-label-audit.md. Runs on
invented files (Widget) from test_grouping_rules_check's twelve-pair week; no API,
no database.
"""

import json
import sys
from pathlib import Path

import pytest

import common

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed_calibrate as ec  # noqa: E402
import grouping_decider_dev as gdd  # noqa: E402
import grouping_label_audit as gla  # noqa: E402
import grouping_rules_check as grc  # noqa: E402
from test_grouping_rules_check import _week  # noqa: E402

WEEK = "2026-11-16"


def key(n: int) -> str:
    """The fixture's pair n is items 2n+1 and 2n+2."""
    return f"{2 * n + 1}-{2 * n + 2}"


# Zone pairs are 4-9. Opus: 4 different, 5-7 same, 8 different, 9 same.
DECIDER = {
    4: "same",
    5: "same",
    6: "different",
    7: "same",
    8: "different",
    9: "unclear",
}
AUDIT = {key(4), key(6), key(9)}  # decider join differs from Opus
CONTROLS = {key(5), key(7), key(8)}


@pytest.fixture()
def week(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "MIN_BAND_LABELLED", 1)
    root = tmp_path / "embed_calibration" / WEEK
    _week(root)
    labels = {key(n): lab for n, lab in DECIDER.items()}
    (root / gdd.labels_file(*gla.DECIDER)).write_text(
        json.dumps({"labels": labels, "usd": 0.0}) + "\n", encoding="utf-8"
    )
    return root


def _answer(root, answers):
    with (root / gla.ANSWERS_FILE).open("a", encoding="utf-8") as f:
        for k, lab in answers.items():
            f.write(json.dumps({"key": k, "label": lab}) + "\n")


class Inputs:
    def __init__(self, *got):
        self.got, self.calls = list(got), 0

    def __call__(self, prompt):
        self.calls += 1
        if not self.got:
            raise EOFError
        return self.got.pop(0)


def test_the_definition_shown_is_the_judge_prompts():
    assert gla.DEFINITION in ec._PAIR_SYSTEM


def test_the_pre_registered_values_are_the_ones_in_force():
    assert gla.DECIDER == ("claude-haiku-5-5", "high")
    assert gla.CONTROLS_PER_SIDE == 4
    assert (gla.H_NOT_USABLE, gla.H_HOLDS, gla.CONTROL_MIN_SHARE) == (0.5, 0.2, 0.75)
    assert Path(gla.PREREG).exists()


def test_every_disagreement_is_audited_and_the_order_is_fixed(week):
    w = gdd.load_week(WEEK)
    s = gla.draw(w, gla.decider_labels(w))
    assert {r.key for r in s.audit} == AUDIT
    assert {r.key for r in s.controls} == CONTROLS
    assert sorted(r.key for r in s.order) == sorted(AUDIT | CONTROLS)
    assert [r.key for r in gla.draw(w, gla.decider_labels(w)).order] == [
        r.key for r in s.order
    ]


def test_controls_are_capped_per_side():
    def row(n, same):
        return grc.Row(key=f"{n}-x", a=n, b=n, sim=0.7, same=same, same_outlet=False)

    zone = [row(n, True) for n in range(10)] + [row(n, False) for n in range(10, 20)]
    zone.append(row(99, False))
    decider = {r.key: "same" if r.same else "different" for r in zone}
    decider["99-x"] = "same"  # the one disagreement
    w = gdd.Week(name=WEEK, root=Path("."), rows=zone, zone=zone, texts={}, natural={})
    s = gla.draw(w, decider)
    assert [r.key for r in s.audit] == ["99-x"]
    assert sum(r.same for r in s.controls) == 4
    assert sum(not r.same for r in s.controls) == 4


def test_judge_shows_texts_blind_saves_each_answer_and_resumes(week, capsys):
    first = Inputs("s", "x", "d", "q")  # "x" is asked again
    assert gla.main(["--week-ending", WEEK, "--judge"], input_fn=first) == 0
    out = capsys.readouterr().out
    assert "Widget item" in out  # the judge's own texts
    assert "haiku" not in out.lower() and "opus" not in out.lower()
    assert len(gla.load_answers(week / gla.ANSWERS_FILE)) == 2
    rest = Inputs(*["s"] * 10)
    assert gla.main(["--week-ending", WEEK, "--judge"], input_fn=rest) == 0
    assert rest.calls == 4  # only the four left
    assert set(gla.load_answers(week / gla.ANSWERS_FILE)) == AUDIT | CONTROLS


def test_report_refuses_until_every_pair_is_answered(week, capsys):
    _answer(week, {key(4): "same"})
    assert gla.main(["--week-ending", WEEK, "--report"]) == ec.EXIT_PENDING
    assert "5 of 6 pairs not answered" in capsys.readouterr().out


def test_report_reads_the_answers_and_rescores_with_numbers_only(week, capsys):
    # 4: operator same = decider; 6: operator same = Opus; 9: operator different
    # = the decider's no-join. Controls all agree with the joint label.
    _answer(week, {key(4): "same", key(6): "same", key(9): "different"})
    _answer(week, {key(5): "same", key(7): "same", key(8): "different"})
    assert gla.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "Widget" not in out
    assert "H=2, with Opus O=1, unclear=0; H/D=0.667" in out
    assert "not a usable reference" in out
    assert "controls: agree 3/3" in out and "matches the prompt's" in out
    assert "with Opus labels: joins=7 correct=6" in out
    assert "on the audited pairs: joins=7 correct=7 wP=1.000" in out


def test_controls_that_disagree_flag_the_definition(week, capsys):
    _answer(week, {key(4): "different", key(6): "same", key(9): "same"})
    _answer(week, {key(5): "different", key(7): "different", key(8): "same"})
    assert gla.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "H=0, with Opus O=3" in out and "the Opus labels hold" in out
    assert "controls: agree 0/3" in out and "differs from the prompt's" in out


def test_an_operator_unclear_is_left_out_of_the_reading_and_the_rescore(week, capsys):
    _answer(week, {key(4): "unclear", key(6): "same", key(9): "same"})
    _answer(week, {key(5): "same", key(7): "same", key(8): "different"})
    assert gla.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "H=0, with Opus O=2, unclear=1" in out
    # pair 4 (a decider join Opus called different) is dropped, not counted
    assert "on the audited pairs: joins=6 correct=6" in out


@pytest.mark.parametrize(
    "h,d,expected",
    [
        (5, 10, "not a usable reference"),
        (2, 10, "the Opus labels hold"),
        (3, 10, "mixed"),
        (0, 0, "no reading"),
    ],
)
def test_the_reading_thresholds(h, d, expected):
    assert expected in gla.reading(h, d)


def test_a_decider_without_dev_answers_refuses(week, capsys):
    (week / gdd.labels_file(*gla.DECIDER)).unlink()
    assert gla.main(["--week-ending", WEEK, "--judge"], input_fn=Inputs()) == 2
    assert "--dev first" in capsys.readouterr().out

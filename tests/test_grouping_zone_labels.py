"""scripts/grouping_zone_labels.py (news-brief-al7.7.3).

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-zone-labels.md. Runs on
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
import grouping_zone_labels as gzl  # noqa: E402
from test_grouping_rules_check import _week  # noqa: E402

WEEK = "2026-11-16"


def key(n: int) -> str:
    """The fixture's pair n is items 2n+1 and 2n+2."""
    return f"{2 * n + 1}-{2 * n + 2}"


ZONE = {key(n) for n in range(4, 10)}
# Zone pairs 4-9. Opus-low: 4 different, 5-7 same, 8 different, 9 same.
DECIDER = {
    4: "same",
    5: "same",
    6: "different",
    7: "same",
    8: "different",
    9: "unclear",
}
# The audit drew 4, 6, 9 and controls 5, 7, 8. Pair 4 is the "eleven" here: the
# operator said same where Opus-low said different.
OLD = {4: "same", 5: "same", 6: "same", 7: "same", 8: "different", 9: "unclear"}
NEW = {4: "story", 5: "event", 6: "event", 7: "different", 8: "different", 9: "event"}


def _write(path, labels):
    with path.open("a", encoding="utf-8") as f:
        for n, lab in labels.items():
            f.write(json.dumps({"key": key(n), "label": lab}) + "\n")


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
    _write(root / gla.ANSWERS_FILE, OLD)
    return root


class Inputs:
    def __init__(self, *got):
        self.got, self.calls = list(got), 0

    def __call__(self, prompt):
        self.calls += 1
        if not self.got:
            raise EOFError
        return self.got.pop(0)


def test_the_pre_registered_values_are_the_ones_in_force():
    assert gzl.KEYS == {"e": "event", "c": "story", "d": "different", "u": "unclear"}
    assert gzl.SEED == 20261127
    assert gzl.ANSWERS_FILE == "zone_labels.jsonl" != gla.ANSWERS_FILE
    assert gzl.RETEST_MIN == 0.80
    assert gzl.AUDIT_WEEK == WEEK and WEEK in gdd.DEV_WEEKS
    assert gdd.TEST_WEEK not in gdd.DEV_WEEKS
    assert Path(gzl.PREREG).exists()


def test_only_one_event_counts_as_the_same():
    assert gzl.binary("event") == "same"
    assert gzl.binary("story") == gzl.binary("different") == "different"
    assert gzl.binary("unclear") is None
    got = gzl.reference({"a": "event", "b": "story", "c": "unclear", "d": "different"})
    assert got == {"a": "same", "b": "different", "d": "different"}


def _order(w):
    first, rest = gzl.stages(w)
    return [r.key for r in first + rest]


def test_every_zone_pair_is_shown_once_in_a_fixed_order(week):
    w = gdd.load_week(WEEK)
    first = _order(w)
    assert sorted(first) == sorted(ZONE)
    assert _order(w) == first
    # not the audit's order: the audited pairs cannot be picked out by position
    s = gla.draw(w, gla.decider_labels(w))
    assert first != [r.key for r in s.order]


@pytest.fixture()
def staged(week, monkeypatch):
    """One control a side: the audit draws 4, 6, 9, 8 and one of 5 and 7."""
    monkeypatch.setattr(gla, "CONTROLS_PER_SIDE", 1)
    w = gdd.load_week(WEEK)
    drawn = {r.key for r in gla.draw(w, gla.decider_labels(w)).order}
    assert len(drawn) == 5 and {key(4), key(6), key(8), key(9)} <= drawn
    return week, drawn


def test_stage_1_is_the_audit_draw_and_comes_first(staged):
    _, drawn = staged
    first, rest = gzl.stages(gdd.load_week(WEEK))
    assert {r.key for r in first} == drawn
    assert {r.key for r in rest} == ZONE - drawn


def test_judge_stops_after_stage_1_and_the_report_reads_it_early(staged, capsys):
    week, drawn = staged
    assert gzl.main(["--week-ending", WEEK, "--judge"], input_fn=Inputs("c", "q")) == 0
    capsys.readouterr()
    got = Inputs("e", "e", "e", "e", "e")
    assert gzl.main(["--week-ending", WEEK, "--judge"], input_fn=got) == 0
    out = capsys.readouterr().out
    assert "Stage 1 done (5 pairs)" in out and got.calls == 4  # stopped, one left
    assert set(gla.load_answers(week / gzl.ANSWERS_FILE)) == drawn
    assert gzl.main(["--week-ending", WEEK, "--report"]) == ec.EXIT_PENDING
    out = capsys.readouterr().out
    assert "stage 1: 5 pairs answered" in out and "the audit's same" in out
    assert "retest on the other audit pairs" in out
    assert "1 of 6 zone pairs not answered" in out
    assert "zone=" not in out and "agree=" not in out
    assert gzl.main(["--week-ending", WEEK, "--judge"], input_fn=Inputs("d")) == 0
    assert "All pairs answered" in capsys.readouterr().out
    assert gzl.main(["--week-ending", WEEK, "--report"]) == 0
    assert "zone=6" in capsys.readouterr().out


def test_judge_shows_texts_blind_saves_each_answer_and_resumes(week, capsys):
    got = Inputs("x", "e", "c", "q")
    assert gzl.main(["--week-ending", WEEK, "--judge"], input_fn=got) == 0
    out = capsys.readouterr().out
    assert gla.DEFINITION in out and "continuing story" in out
    assert "Widget item" in out  # the judge is the one mode that shows text
    saved = gla.load_answers(week / gzl.ANSWERS_FILE)
    assert sorted(saved.values()) == ["event", "story"]  # "x" was asked again
    assert got.calls == 4
    assert list(saved) == _order(gdd.load_week(WEEK))[:2]
    assert gla.load_answers(week / gla.ANSWERS_FILE) == {
        key(n): lab for n, lab in OLD.items()
    }  # the audit's answers are untouched
    again = Inputs("d", "d", "d", "u")
    assert gzl.main(["--week-ending", WEEK, "--judge"], input_fn=again) == 0
    out = capsys.readouterr().out
    assert "6 pairs, 2 already answered." in out and "All pairs answered" in out
    saved = gla.load_answers(week / gzl.ANSWERS_FILE)
    assert set(saved) == ZONE and list(saved.values()).count("unclear") == 1


def test_report_refuses_until_stage_1_is_answered(week, capsys):
    _write(week / gzl.ANSWERS_FILE, {4: "event"})
    assert gzl.main(["--week-ending", WEEK, "--report"]) == ec.EXIT_PENDING
    out = capsys.readouterr().out
    assert "5 of 6 stage 1 pairs not answered" in out and "retest" not in out


def test_report_on_the_audit_week_with_numbers_only(week, capsys):
    _write(week / gzl.ANSWERS_FILE, NEW)
    assert gzl.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "Widget" not in out
    assert "zone=6 event=3 story=1 different=2 unclear=0" in out
    # reference: 4 d, 5 s, 6 s, 7 d, 8 d, 9 s
    assert "opus-low: agree=5/6" in out
    assert "claude-haiku-5-5@high (decider): agree=2/6" in out
    assert "@medium" not in out  # no reference file in this week
    # rule A joins 0-3 (same); the decider joins 4, 5, 7, of which only 5 is same
    assert "zone labels: joins=7 correct=5" in out and "not evidence" in out
    assert "n=1 now event=0 story=1 different=0 unclear=0" in out
    assert "mostly not one event" in out
    # 5, 6, 8 agree with the audit; 7 went from same to different; 9 was unclear
    assert "consistent=3/4" in out and "noisy" in out


def test_saved_opus_reference_answers_are_scored_too(week, capsys):
    _write(week / gzl.ANSWERS_FILE, NEW)
    labels = {key(n): "same" for n in range(4, 10)}
    labels[key(8)] = "unclear"
    (week / f"reference-{gzl.OPUS}-medium.jsonl").write_text(
        json.dumps({"labels": labels, "usd": 0.1}) + "\n", encoding="utf-8"
    )
    assert gzl.main(["--week-ending", WEEK, "--report"]) == 0
    # same on 5, 6, 9 right; 4 and 7 wrong; 8 unclear is a miss
    assert "claude-opus-5-5@medium: agree=3/6" in capsys.readouterr().out


def test_an_unclear_answer_drops_out_of_the_reference_and_misses_the_retest(
    week, capsys
):
    _write(week / gzl.ANSWERS_FILE, {**NEW, 5: "unclear", 4: "unclear"})
    assert gzl.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "opus-low: agree=3/4" in out  # 4 and 5 left out of the reference
    assert "zone labels: joins=5 correct=4" in out  # 4 and 5 dropped
    assert "n=1 now event=0 story=0 different=0 unclear=1" in out
    assert "no pair answered: no reading" in out
    assert "consistent=2/4" in out  # 5 unclear and 7 flipped


def test_a_week_outside_the_dev_weeks_shows_no_model_numbers(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    _week(tmp_path / "embed_calibration" / gdd.TEST_WEEK)
    _write(tmp_path / "embed_calibration" / gdd.TEST_WEEK / gzl.ANSWERS_FILE, NEW)
    assert gzl.main(["--week-ending", gdd.TEST_WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "zone=6 event=3" in out and "not a dev week" in out
    assert "agree=" not in out and "joins=" not in out and "retest" not in out


def test_a_dev_week_without_audit_answers_skips_the_retest(week, capsys):
    (week / gla.ANSWERS_FILE).unlink()
    _write(week / gzl.ANSWERS_FILE, NEW)
    assert gzl.main(["--week-ending", WEEK, "--report"]) == 0
    out = capsys.readouterr().out
    assert "opus-low: agree=5/6" in out
    assert "retest: no audit answers" in out


@pytest.mark.parametrize(
    "event,other,expected",
    [
        (0, 0, "no reading"),
        (5, 6, "mostly not one event"),
        (6, 5, "mostly one event"),
        (5, 5, "split"),
    ],
)
def test_the_eleven_reading(event, other, expected):
    assert expected in gzl.eleven_reading(event, other)


@pytest.mark.parametrize(
    "k,n,expected",
    [
        (24, 30, "consistent"),
        (23, 30, "noisy"),
        (0, 0, "no reading"),
    ],
)
def test_the_retest_reading(k, n, expected):
    assert expected in gzl.retest_reading(k, n)


def test_a_missing_week_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    assert gzl.main(["--week-ending", WEEK, "--report"]) == ec.EXIT_USAGE
    assert "run embed_calibrate" in capsys.readouterr().out

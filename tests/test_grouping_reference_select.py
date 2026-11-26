"""scripts/grouping_reference_select.py (news-brief-al7.7.2).

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-reference-selection.md.
The rule is pure and tested case by case; the end-to-end tests run on invented files
(Widget) from test_grouping_rules_check's twelve-pair week with a stub judge, and
are DB-gated. Nothing here reaches Anthropic.
"""

import json
import sys
from pathlib import Path

import pytest

import common
import db

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed_calibrate as ec  # noqa: E402
import grouping_decider_dev as gdd  # noqa: E402
import grouping_label_audit as gla  # noqa: E402
import grouping_reference_select as grs  # noqa: E402
from test_grouping_rules_check import StubHaiku, _week  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (pgvector image).",
)

OPUS = "claude-opus-5-5"
MED, HIGH = (OPUS, "medium"), (OPUS, "high")


def key(n: int) -> str:
    return f"{2 * n + 1}-{2 * n + 2}"


# Zone pairs 4-9. Opus-low and the texts: 4 different, 5-7 same, 8 different, 9 same.
DECIDER = {
    4: "same",
    5: "same",
    6: "different",
    7: "same",
    8: "different",
    9: "unclear",
}
# The audit draws 4, 6, 9 and controls 5, 7, 8. The operator agrees with the texts
# and cannot tell on 9, so S is the other five.
OPERATOR = {
    4: "different",
    5: "same",
    6: "same",
    7: "same",
    8: "different",
    9: "unclear",
}


def test_the_pre_registered_values_are_the_ones_in_force():
    assert grs.CANDIDATES == (MED, HIGH)
    assert (grs.AUDIT_WEEK, grs.OTHER_WEEK) == ("2026-11-16", "2026-11-23")
    assert grs.PASS_SHARE == 0.85
    assert grs.SPEND_CAP == 15.0
    assert Path(grs.PREREG).exists()


def test_the_bar_is_34_of_40():
    assert grs.select({MED: 34}, 40) == MED
    assert grs.select({MED: 33}, 40) is None


def test_the_most_agreements_win_and_a_tie_goes_to_the_lower_effort():
    assert grs.select({MED: 35, HIGH: 37}, 40) == HIGH
    assert grs.select({HIGH: 36, MED: 36}, 40) == MED
    assert grs.select({MED: 0, HIGH: 0}, 0) is None


def test_an_unclear_or_missing_label_is_a_disagreement():
    g = {"a": "same", "b": "different", "c": "same"}
    assert grs.agreement({"a": "same", "b": "unclear"}, g) == 1


class StubEffort(StubHaiku):
    """Answers from the texts, except at the efforts in `blind`: all different."""

    def __init__(self, blind=()):
        super().__init__()
        self.blind = set(blind)

    def __call__(self, payload):
        resp = super().__call__(payload)
        if payload["output_config"]["effort"] in self.blind:
            for a in resp["content"][0]["input"]["answers"]:
                a["label"] = "different"
        return resp


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "_sleep", lambda s: None)
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    base = tmp_path / "embed_calibration"
    _week(base / grs.AUDIT_WEEK)
    _week(base / grs.OTHER_WEEK)
    audit = base / grs.AUDIT_WEEK
    labels = {key(n): lab for n, lab in DECIDER.items()}
    (audit / gdd.labels_file(*gla.DECIDER)).write_text(
        json.dumps({"labels": labels, "usd": 0.0}) + "\n", encoding="utf-8"
    )
    with (audit / gla.ANSWERS_FILE).open("w", encoding="utf-8") as f:
        for n, lab in OPERATOR.items():
            f.write(json.dumps({"key": key(n), "label": lab}) + "\n")
    yield c
    c.close()


def _root(week):
    return common.DATA_DIR / "embed_calibration" / week


@needs_db
def test_both_candidates_label_the_zone_and_the_tie_winner_labels_the_other_week(
    conn, capsys
):
    stub = StubEffort()
    assert grs.main([], post=stub) == 0
    out = capsys.readouterr().out
    assert "Widget" not in out
    assert "S=5" in out
    # The baselines, from saved labels: Opus-low matches the operator everywhere;
    # Haiku-high misses 4 (it joins) and 6 (it does not), and gets the controls.
    assert "opus-low (baseline): agree=5/5" in out
    assert (
        "claude-haiku-5-5@high (baseline): agree=3/5 (audit: decider joins,"
        " Opus-low not 0/1; Opus-low same, decider not 0/1; controls 3/3)"
    ) in out
    assert f"{OPUS}@medium: agree=5/5" in out and f"{OPUS}@high: agree=5/5" in out
    assert "zone=6 unclear=0 agree_with_opus_low=6/6" in out
    assert f"chosen: {OPUS}@medium" in out
    # One chunk per run: medium and high on 16 Nov, then medium alone on 23 Nov.
    assert [p["output_config"]["effort"] for p in stub.payloads] == [
        "medium",
        "high",
        "medium",
    ]
    assert {p["model"] for p in stub.payloads} == {OPUS}
    assert (_root(grs.OTHER_WEEK) / grs.reference_file(*MED)).exists()
    assert not (_root(grs.OTHER_WEEK) / grs.reference_file(*HIGH)).exists()
    assert ec.JUDGE_EFFORT == "low"  # restored for whoever runs next
    again = StubEffort()
    assert grs.main([], post=again) == 0
    assert again.payloads == []  # everything was saved


@needs_db
def test_a_candidate_below_the_bar_loses_to_one_above_it(conn, capsys):
    stub = StubEffort(blind={"medium"})
    assert grs.main([], post=stub) == 0
    out = capsys.readouterr().out
    # all different: right on 4 and 8 only
    assert f"{OPUS}@medium: agree=2/5" in out
    assert f"chosen: {OPUS}@high" in out
    assert [p["output_config"]["effort"] for p in stub.payloads][-1] == "high"
    assert (_root(grs.OTHER_WEEK) / grs.reference_file(*HIGH)).exists()


@needs_db
def test_no_candidate_at_the_bar_means_the_fallback_and_no_other_week(conn, capsys):
    stub = StubEffort(blind={"medium", "high"})
    assert grs.main([], post=stub) == 0
    out = capsys.readouterr().out
    assert "chosen: none" in out and "fallback" in out
    assert len(stub.payloads) == 2
    assert not list(_root(grs.OTHER_WEEK).glob("reference-*"))


@needs_db
def test_the_spend_cap_stops_before_a_call(conn, capsys, monkeypatch):
    monkeypatch.setattr(grs, "SPEND_CAP", 0.0)
    stub = StubEffort()
    assert grs.main([], post=stub) == ec.EXIT_USAGE
    assert "spend cap reached" in capsys.readouterr().out
    assert stub.payloads == []


@needs_db
def test_an_unanswered_audit_pair_refuses_before_a_call(conn, capsys):
    path = _root(grs.AUDIT_WEEK) / gla.ANSWERS_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    stub = StubEffort()
    assert grs.main([], post=stub) == ec.EXIT_USAGE
    assert "1 of 6 audit pairs not answered" in capsys.readouterr().out
    assert stub.payloads == []

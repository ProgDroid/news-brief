"""scripts/grouping_decider_dev.py (news-brief-k8s).

Pre-registered in docs/superpowers/specs/2026-11-26-grouping-decider-preregistration.md.
The selection rule is pure and tested case by case; the end-to-end tests run on
invented files (Widget, Gizmo) with a stub judge and are DB-gated. Nothing here
reaches Anthropic.
"""

import re
import sys
from pathlib import Path

import pytest

import common
import db

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed_calibrate as ec  # noqa: E402
import grouping_decider_dev as gdd  # noqa: E402
import grouping_rules_check as grc  # noqa: E402
from test_grouping_rules_check import StubHaiku, _week  # noqa: E402

needs_db = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: export DATABASE_URL (pgvector image).",
)

H, S = "claude-haiku-5-5", "claude-sonnet-5-5"


def test_the_pre_registered_values_are_the_ones_in_force():
    assert gdd.CANDIDATES == (
        (H, "low"),
        (H, "medium"),
        (H, "high"),
        (S, "low"),
        (S, "medium"),
    )
    assert gdd.MONTHLY_CAP == 10.0
    assert gdd.DEV_WEEKS == ("2026-11-23", "2026-11-16")
    assert gdd.TEST_WEEK == "2026-11-09"
    assert Path(gdd.PREREG).exists()


def test_the_tested_decider_reuses_its_saved_labels():
    assert gdd.labels_file(H, "low") == grc.HAIKU_FILE
    assert gdd.labels_file(S, "medium") == "decider-claude-sonnet-5-5-medium.jsonl"


def test_zone_pool_counts_the_neighbour_pairs_from_0_65():
    # band 6 is 0.60, band 7 is 0.65, band 13 is 0.95
    assert gdd.zone_pool({6: 5, 7: 3, 13: 2}) == 5


def test_monthly_estimate():
    assert gdd.est_monthly(0.0001, 10708) == pytest.approx(0.0001 * 10708 * 52 / 12)


def _wk(p=0.95, r=0.70, month=2.0, usd=0.01, pairs=200):
    return gdd.WeekResult(precision=p, recall=r, est_month=month, usd=usd, pairs=pairs)


def test_the_cheapest_candidate_that_passes_both_weeks_is_chosen():
    results = {
        (H, "medium"): [_wk(usd=0.02), _wk(usd=0.02)],
        (S, "low"): [_wk(usd=0.20), _wk(usd=0.20)],
    }
    chosen, _ = gdd.select(results)
    assert chosen == (H, "medium")


def test_a_cost_tie_goes_to_the_lower_effort():
    results = {
        (H, "high"): [_wk(usd=0.01), _wk(usd=0.01)],
        (H, "medium"): [_wk(usd=0.01), _wk(usd=0.01)],
    }
    assert gdd.select(results)[0] == (H, "medium")


def test_low_precision_or_recall_on_either_week_rules_a_candidate_out():
    results = {
        (H, "low"): [_wk(p=0.89), _wk()],  # precision short on week 1
        (H, "medium"): [_wk(), _wk(r=0.59)],  # recall short on week 2
    }
    chosen, notes = gdd.select(results)
    assert chosen is None
    assert any("no candidate" in n for n in notes)


def test_a_passing_candidate_over_the_cap_is_reported_not_chosen():
    results = {(S, "medium"): [_wk(month=36.0), _wk(month=34.0)]}
    chosen, notes = gdd.select(results)
    assert chosen is None
    assert any("claude-sonnet-5-5@medium" in n and "cap" in n for n in notes)


# ---- end to end ----


class StubDecider(StubHaiku):
    """Answers like StubHaiku (same event number = same), except that the models
    in `blind` call every pair different."""

    def __init__(self, blind=()):
        super().__init__()
        self.blind = set(blind)

    def __call__(self, payload):
        resp = super().__call__(payload)
        if payload["model"] in self.blind:
            for a in resp["content"][0]["input"]["answers"]:
                a["label"] = "different"
        return resp


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ec, "_sleep", lambda s: None)
    monkeypatch.setattr(ec, "MIN_BAND_LABELLED", 1)
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    db.run_migrations(c)
    c.commit()
    yield c
    c.close()


def _weeks(*names):
    for n in names:
        _week(common.DATA_DIR / "embed_calibration" / n)


@needs_db
def test_dev_runs_every_candidate_and_chooses_mechanically(conn, capsys):
    _weeks(*gdd.DEV_WEEKS)
    stub = StubDecider()
    assert gdd.main(["--dev"], post=stub) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("week=")]
    assert len(lines) == 10  # five candidates on two weeks
    assert "Widget" not in out
    # Each candidate's effort reached its requests, and only its model was used.
    seen = {(p["model"], p["output_config"]["effort"]) for p in stub.payloads}
    assert seen == set(gdd.CANDIDATES)
    # All pass; Haiku is cheaper per pair than Sonnet; the efforts tie on cost.
    assert f"chosen: {H}@low" in out
    assert ec.JUDGE_EFFORT == "low"  # restored for whoever runs next


@needs_db
def test_dev_skips_a_blind_model_and_the_cap_stops_an_expensive_one(
    conn, capsys, monkeypatch
):
    _weeks(*gdd.DEV_WEEKS)
    assert gdd.main(["--dev"], post=StubDecider(blind={H})) == 0
    assert f"chosen: {S}@low" in capsys.readouterr().out
    monkeypatch.setattr(gdd, "MONTHLY_CAP", 0.0)
    assert gdd.main(["--dev"], post=StubDecider(blind={H})) == 0
    out = capsys.readouterr().out
    assert "chosen: none" in out and "cap" in out


@needs_db
def test_test_week_scores_only_the_chosen_candidate(conn, capsys):
    _weeks(*gdd.DEV_WEEKS, gdd.TEST_WEEK)
    assert gdd.main(["--dev"], post=StubDecider(blind={H})) == 0
    capsys.readouterr()
    stub = StubDecider()
    assert gdd.main(["--test"], post=stub) == 0
    out = capsys.readouterr().out
    assert {p["model"] for p in stub.payloads} == {S}
    assert re.search(rf"week={gdd.TEST_WEEK} decider={S}@low .*-> PASS", out)


@needs_db
def test_test_week_refuses_before_the_dev_runs_are_complete(conn, capsys):
    _weeks(*gdd.DEV_WEEKS, gdd.TEST_WEEK)
    stub = StubDecider()
    assert gdd.main(["--test"], post=stub) == ec.EXIT_USAGE
    assert "run --dev" in capsys.readouterr().out
    assert stub.payloads == []

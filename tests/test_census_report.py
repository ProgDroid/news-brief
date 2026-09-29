"""`scripts/census_report.py`: the read-only readout (plan Task 9)."""

import psycopg
import pytest

import census_metrics

import census
import census_fixtures as cf
import db
from scripts import census_report

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)

NOW = cf.NOW


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        yield c


@pytest.fixture()
def ready(kb):
    cf.prepared(kb)
    return kb


def test_report_without_census_says_so(kb, capsys):
    assert census_report.main() == 2
    assert "not prepared" in capsys.readouterr().out


def test_report_shows_go_no_go_after_two_windows(ready):
    text = census_report.render(ready, NOW)
    assert "Go/no-go" not in text  # nothing done yet: no verdict to show

    cf.pass_gate(ready, NOW)
    text = census_report.render(ready, NOW)
    assert "Go/no-go" in text
    assert "[8, 8]" in text
    assert "mean 8.00" in text
    assert "verdict: GO" in text
    assert "override: none" in text


def test_report_prints_mde_at_current_k_before_the_end(ready):
    assert "current K" not in census_report.render(ready, NOW)
    cf.pass_gate(ready, NOW)
    text = census_report.render(ready, NOW)
    assert "current K = 2" in text
    assert "(final)" not in text


def test_report_final_mde_matches_metrics(ready):
    for order_no in range(1, 17):
        w = cf.window_id(ready, order_no)
        cf.label(ready, w, cf.cross_outlet_groups(ready, w, 8), NOW)
    text = census_report.render(ready, NOW)
    line = next(ln for ln in text.splitlines() if "current K = 16" in ln)
    assert "(final)" in line
    assert "m-bar 8.00, CV 0.00" in line
    assert "rho=0.10: 24.4 points" in line
    for rho in (0.05, 0.10, 0.30):  # spec 4.4
        value = census_metrics.mde([8] * 16, rho)
        assert f"rho={rho:.2f}: {value:.1f} points" in line
    assert "rho=0.20" not in line


def test_report_scores_window_two_pass_one(ready):
    cf.pass_gate(ready, NOW)
    repeat = cf.window_id(ready, 2, pass_=2)
    cf.label(ready, repeat, [], NOW)  # the repeat groups nothing at all
    text = census_report.render(ready, NOW)
    assert "[8, 8]" in text  # the gate still reads window 2's pass 1
    assert "current K = 2" in text  # the repeat is not a third window
    assert "one window: thin" in text
    assert "pairwise F1" in text and "ARI" in text


def test_report_marks_an_abandoned_repeat_void(ready):
    cf.pass_gate(ready, NOW)
    census.abandon(ready, cf.window_id(ready, 2, pass_=2), "test", NOW)
    assert "consistency unavailable: repeat void" in census_report.render(ready, NOW)


def test_report_prints_gap_deciles_and_c439ade(ready):
    text = census_report.render(ready, NOW)
    assert "biased toward passing (spec 4.3)" in text
    assert "deciles of gap" in text
    assert "c439ade deployed 2026-09-25" in text
    assert "block straddles it:" in text
    assert "descriptive only" in text


def test_report_is_read_only(ready, monkeypatch):
    ready.execute("SET TRANSACTION READ ONLY")
    census_report.render(ready, NOW)  # does not raise
    # A committing reader mid-render would have ended the READ ONLY
    # transaction; the state at the END is what proves it survived.
    assert ready.execute("SHOW transaction_read_only").fetchone()[0] == "on"
    ready.rollback()


def test_a_write_after_the_readers_is_refused(ready, monkeypatch):
    real = census_report._half_lines  # the last section, after every reader

    def writes_late(*args):
        args[0].execute("UPDATE census_block SET go_override_reason = 'x'")
        return real(*args)

    monkeypatch.setattr(census_report, "_half_lines", writes_late)
    ready.execute("SET TRANSACTION READ ONLY")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        census_report.render(ready, NOW)
    ready.rollback()


def test_main_runs_render_in_a_read_only_transaction(ready, monkeypatch, capsys):
    seen = []
    real = census_report.render

    def spy(conn, now):
        text = real(conn, now)
        seen.append(conn.execute("SHOW transaction_read_only").fetchone()[0])
        return text

    monkeypatch.setattr(census_report, "render", spy)
    assert census_report.main() == 0
    assert seen == ["on"]  # deleting main's SET TRANSACTION READ ONLY gives "off"


def test_report_survives_a_state_with_no_multi_outlet_groups(ready):
    cf.finish_empty(ready, [1, 2], NOW)
    text = census_report.render(ready, NOW)
    assert "detectable difference: undefined (no multi-outlet groups)" in text
    assert "Go/no-go" in text and "verdict: NO-GO" in text


def test_report_blind_precision_counts_only_complete_pass_one_windows(ready):
    w1 = cf.window_id(ready, 1)
    cf.label(ready, w1, cf.cross_outlet_groups(ready, w1, 8), NOW)
    pairs = census.precision_pairs(ready, w1)
    assert len(pairs) == 8
    for n, (a, b) in enumerate(pairs):
        census.save_precision(ready, w1, a, b, "same" if n < 6 else "different", NOW)
    # window 2: blind_done with its sample held for the repeat
    w2 = cf.window_id(ready, 2)
    cf.label(ready, w2, cf.cross_outlet_groups(ready, w2, 8), NOW)
    # window 3: blind_done, only two of its pairs answered: not complete
    w3 = cf.window_id(ready, 3)
    cf.label(ready, w3, cf.cross_outlet_groups(ready, w3, 8), NOW)
    for a, b in census.precision_pairs(ready, w3)[:2]:
        census.save_precision(ready, w3, a, b, "different", NOW)

    text = census_report.render(ready, NOW)
    assert "0.75 = 6/8 pairs over 1 complete windows" in text
    assert "precision 6/8" in text  # the by-half table agrees
    assert "0/2" not in text

    # the repeat done releases window 2's sample; answering it completes it
    repeat = cf.window_id(ready, 2, pass_=2)
    cf.label(ready, repeat, cf.cross_outlet_groups(ready, repeat, 8), NOW)
    cf.answer_precision(ready, w2, NOW)
    text = census_report.render(ready, NOW)
    assert "= 14/16 pairs over 2 complete windows" in text


def test_report_consistency_point_values(ready):
    cf.pass_gate(ready, NOW)
    w2 = cf.window_id(ready, 2)
    repeat = cf.window_id(ready, 2, pass_=2)
    pass1 = cf.cross_outlet_groups(ready, w2, 8)
    in_groups = {i for g in pass1 for i in g}
    spare = [
        it["id"] for it in census.window_items(ready, w2) if it["id"] not in in_groups
    ]
    z, u = spare[0], spare[1]
    assert {it["id"] for it in census.window_items(ready, repeat)} == {
        it["id"] for it in census.window_items(ready, w2)
    }

    # pass 2: pairs 0-5 agree, pair 6 dropped, pair 7's second item replaced
    # by z; u is unsure and sits with pair 0 (removed, it must not count).
    groups2 = [[(pass1[i][0], False), (pass1[i][1], False)] for i in range(6)] + [
        [(pass1[7][0], False), (z, False)]
    ]
    groups2[0].append((u, True))
    for seq, members in enumerate(groups2, start=1):
        gid = census.create_group(ready, repeat, NOW)
        census.save_assignments(
            ready, repeat, "fixture", seq, [(i, gid, un) for i, un in members], NOW
        )
    census.finish_blind(ready, repeat, NOW)

    text = census_report.render(ready, NOW)
    # tp 6, fp 1, fn 2  ->  P 6/7, R 6/8, F1 0.8 (a swap of the passes gives
    # P 0.75 and R 0.86 instead)
    assert "pairwise precision 0.86 (" in text
    assert "pairwise recall 0.75 (" in text
    assert "pairwise F1 0.80 (" in text

    items = [it["id"] for it in census.window_items(ready, w2) if it["id"] != u]
    a = {i: None for i in items}
    for n, g in enumerate(pass1):
        for i in g:
            a[i] = n
    b = {i: None for i in items}
    for n, members in enumerate(groups2):
        for i, unsure in members:
            if not unsure:
                b[i] = 100 + n
    ari = census_metrics.adjusted_rand_index(a, b)
    assert f"ARI {ari:.2f} (" in text
    assert f"{len(items)} sure items in both passes" in text
    assert "resamples undefined)" in text

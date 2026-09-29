"""`scripts/census_report.py`: the read-only readout (plan Task 9)."""

import psycopg
import pytest

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
    assert "rho=0.10: 24.4 points" in line


def test_report_scores_window_two_pass_one(ready):
    cf.pass_gate(ready, NOW)
    repeat = cf.window_id(ready, 2, pass_=2)
    cf.label(ready, repeat, [], NOW)  # the repeat groups nothing at all
    text = census_report.render(ready, NOW)
    assert "[8, 8]" in text  # the gate still reads window 2's pass 1
    assert "current K = 2" in text  # the repeat is not a third window
    assert "one window: thin" in text
    assert "pairwise F1" in text and "ARI" in text
    assert "precision pending" not in text


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
    ready.rollback()

    real = census_report._read_block

    def writes_then_reads(conn):
        conn.execute("UPDATE census_block SET go_override_reason = 'x'")
        return real(conn)

    monkeypatch.setattr(census_report, "_read_block", writes_then_reads)
    ready.execute("SET TRANSACTION READ ONLY")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        census_report.render(ready, NOW)
    ready.rollback()

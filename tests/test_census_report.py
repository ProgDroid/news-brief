"""`scripts/census_report.py`: the read-only readout (plan Task 9)."""

from datetime import timedelta, timezone

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
    # In the served order (D1): advance_to answers each precision sample.
    for order_no in range(1, 17):
        cf.advance_to(ready, order_no, NOW)
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


def _seed_repeat_done(conn, at) -> int:
    """The repeat's blind pass saved with nothing grouped, seeded in SQL:
    the served order reaches it only after window 8 and seven days, which
    would change the K these tests pin (D1 refuses a write out of order)."""
    repeat = cf.window_id(conn, 2, pass_=2)
    conn.execute(
        "UPDATE census_windows SET status = 'blind_done', blind_done_at = %s "
        "WHERE id = %s",
        (at, repeat),
    )
    conn.execute(
        "INSERT INTO census_events (window_id, kind, at) VALUES (%s, 'finish', %s)",
        (repeat, at),
    )
    conn.commit()
    return repeat


def test_report_scores_window_two_pass_one(ready):
    cf.pass_gate(ready, NOW)
    _seed_repeat_done(ready, NOW)  # the repeat groups nothing at all
    text = census_report.render(ready, NOW)
    assert "[8, 8]" in text  # the gate still reads window 2's pass 1
    assert "current K = 2" in text  # the repeat is not a third window
    assert "one window: thin" in text
    assert "pairwise F1" in text and "ARI" in text


def test_report_marks_an_abandoned_repeat_void(ready):
    cf.pass_gate(ready, NOW)
    at = cf.advance_to(ready, 2, NOW, pass_=2)
    census.abandon(ready, cf.window_id(ready, 2, pass_=2), "test", at)
    assert "consistency unavailable: repeat void" in census_report.render(ready, at)


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
    # (window 2 is served before window 3's rest: it comes first in order)
    _seed_repeat_done(ready, NOW)
    assert census.current_task(ready, NOW).window_id == w2
    cf.answer_precision(ready, w2, NOW)
    text = census_report.render(ready, NOW)
    assert "= 14/16 pairs over 2 complete windows" in text


def test_report_consistency_point_values(ready):
    cf.pass_gate(ready, NOW)
    at = cf.advance_to(ready, 2, NOW, pass_=2)  # D1: write the served repeat
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
        gid = census.create_group(ready, repeat, at)
        census.save_assignments(
            ready, repeat, "fixture", seq, [(i, gid, un) for i, un in members], at
        )
    census.finish_blind(ready, repeat, at)

    text = census_report.render(ready, at)
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


# ── Wall-clock minutes (objection 3) ────────────────────────────────────────


def _window_line(text: str, order_no: int, pass_: int = 1) -> str:
    return next(
        ln for ln in text.splitlines() if ln.startswith(f"#{order_no} pass {pass_} ")
    )


def test_wall_minutes_start_at_the_last_open_before_the_first_action(ready):
    """The auto-reload after the previous window's completion stamps an open
    hours or days before the sitting: the FIRST open measures the gap
    between sittings. A reload mid-sitting stamps a later one: the LAST open
    overall would cut the sitting short."""
    w1 = cf.window_id(ready, 1)
    assert census.serve_page(ready, NOW - timedelta(days=1)).window_id == w1
    assert census.serve_page(ready, NOW).window_id == w1  # the sitting begins
    first = NOW + timedelta(minutes=1)
    [group] = cf.cross_outlet_groups(ready, w1, 1)
    gid = census.create_group(ready, w1, first)
    census.save_assignments(
        ready, w1, "tab", 1, [(i, gid, False) for i in group], first
    )
    census.record_event(ready, w1, "action", first)
    assert census.serve_page(ready, NOW + timedelta(minutes=5)).window_id == w1
    census.finish_blind(ready, w1, NOW + timedelta(minutes=10))

    text = census_report.render(ready, NOW + timedelta(minutes=11))
    # first open: 1450.0; opened_at (first assignment): 9.0; last open: 5.0
    assert "active/wall min 10.0/10.0" in _window_line(text, 1)
    assert (
        "wall min: from the last page open at or before the window's first "
        "action or assignment (its opened_at when there is none) to its "
        "blind_done_at" in text
    )


def test_wall_minutes_fall_back_to_opened_at_without_an_action_or_assignment(
    ready,
):
    w1 = cf.window_id(ready, 1)
    assert census.serve_page(ready, NOW - timedelta(days=1)).window_id == w1
    census.save_assignments(ready, w1, "tab", 1, [], NOW)  # sets opened_at only
    census.finish_blind(ready, w1, NOW + timedelta(minutes=5))

    text = census_report.render(ready, NOW + timedelta(minutes=6))
    # opened_at: 5.0; the (stale) open: 1445.0; no fallback: n/a
    assert "active/wall min 0.0/5.0" in _window_line(text, 1)


# ── The by-half table's split point (D3) ────────────────────────────────────


def _complete_two_windows_starting(conn, starts) -> None:
    """Windows 1 and 2 complete with nothing grouped, their window_start then
    moved in SQL: the readout classifies by window_start and reads nothing
    else of it, so only the split point decides which half each lands in."""
    cf.finish_empty(conn, [1, 2], NOW)
    for order_no, start in zip((1, 2), starts):
        conn.execute(
            "UPDATE census_windows SET window_start = %s "
            "WHERE order_no = %s AND pass = 1",
            (start, order_no),
        )
    conn.commit()


def test_by_half_splits_at_the_deploy_and_a_straddling_window_counts_before(ready):
    deployed = cf.DEPLOYED_AT  # inside the fixture's block
    _complete_two_windows_starting(
        ready, [deployed - timedelta(hours=3), deployed + timedelta(hours=1)]
    )
    text = census_report.render(ready, NOW)
    assert "split at the recorded c439ade deploy time, 2026-09-25 12:00 UTC" in text
    assert (
        "a window is classified by its window_start, so one whose "
        f"{census.WINDOW_HOURS} hours contain the split point counts as before"
    ) in text
    # The straddling window (its six hours contain the deploy) is "before".
    assert "before the split: 1 windows" in text
    assert "after the split: 1 windows" in text


def test_by_half_splits_at_the_block_midpoint_when_the_deploy_is_outside(ready):
    start, end = ready.execute(
        "SELECT block_start, block_end FROM census_block"
    ).fetchone()
    ready.execute(
        "UPDATE census_block SET c439ade_deployed_at = %s",
        (start - timedelta(days=1),),
    )
    ready.commit()
    mid = start + (end - start) / 2
    _complete_two_windows_starting(
        ready, [mid - timedelta(hours=3), mid + timedelta(hours=1)]
    )
    text = census_report.render(ready, NOW)
    assert (
        f"split at the block midpoint, {mid.astimezone(timezone.utc):%Y-%m-%d %H:%M}"
        " UTC (the c439ade deploy time falls outside the block)"
    ) in text
    assert "before the split: 1 windows" in text
    assert "after the split: 1 windows" in text

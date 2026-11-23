"""scripts/brief_cost.py: re-price the brief from the host log (al7.3)."""

import statistics

import pytest

from scripts import brief_cost
from scripts.brief_cost import CostLine

BATCH = (
    "2026-11-23 06:00:12,733 INFO [collect] 2026-11-23 06:00:12,733 WARNING "
    "Batch result: stop_reason=end_turn blocks={'text': 1} text_len=4059 "
    "usage(in=205311 out=9907 cache_read=0 searches=3) est_batch_cost=$0.4123 "
    "tail='...'"
)
REALTIME = (
    "2026-11-23 09:03:24,011 INFO Realtime brief: stop_reason=end_turn "
    "elapsed=87s usage(in=376777 out=6069 searches=8)"
)
MODEL = "claude-sonnet-5"


def _batch(day, mode="collect", in_=1_000_000, out=0, searches=0):
    return (
        f"{day} 06:00:12,733 INFO [{mode}] {day} 06:00:12,733 WARNING Batch result: "
        f"stop_reason=end_turn text_len=1 usage(in={in_} out={out} cache_read=0 "
        f"searches={searches}) est_batch_cost=$9.9999"
    )


def test_parse_batch_line():
    assert brief_cost.parse_cost_lines([BATCH]) == [
        CostLine("2026-11-23", "collect", "batch", 205311, 9907, 3)
    ]


def test_parse_realtime_line():
    assert brief_cost.parse_cost_lines([REALTIME]) == [
        CostLine("2026-11-23", "manual", "realtime", 376777, 6069, 8)
    ]


def test_parse_ignores_unrelated_and_unfinished_lines():
    lines = [
        "2026-11-23 06:00:00,000 INFO nothing to see",
        "2026-11-23 06:00:00,000 INFO Realtime brief attempt 1 got 529; retry in 5s",
        "2026-11-23 06:00:00,000 INFO Batch result: no usage here",
        "",
    ]
    assert brief_cost.parse_cost_lines(lines) == []


def test_price_uses_project_rates_not_est_batch_cost():
    batch = CostLine("2026-11-23", "collect", "batch", 1_000_000, 0, 0)
    realtime = CostLine("2026-11-23", "manual", "realtime", 1_000_000, 0, 0)
    assert brief_cost.price(batch, MODEL) == pytest.approx(1.00)
    assert brief_cost.price(realtime, MODEL) == pytest.approx(2.00)


def test_price_adds_web_searches():
    c = CostLine("2026-11-23", "collect", "batch", 0, 0, 5)
    assert brief_cost.price(c, MODEL) == pytest.approx(0.05)


def test_one_collect_day_logged_twice_counts_once():
    first = _batch("2026-11-20", in_=1_000_000)
    rerun = _batch("2026-11-20", in_=3_000_000)
    got = brief_cost.parse_cost_lines([first, rerun])
    assert got == [CostLine("2026-11-20", "collect", "batch", 3_000_000, 0, 0)]


def test_same_day_different_modes_both_count():
    got = brief_cost.parse_cost_lines(
        [_batch("2026-11-20", "collect"), _batch("2026-11-20", "weekly")]
    )
    assert {c.mode for c in got} == {"collect", "weekly"}


def test_realtime_runs_are_never_deduplicated():
    assert len(brief_cost.parse_cost_lines([REALTIME, REALTIME])) == 2


def _costs():
    # collect: $1, $2, $6 (batch half price on 1M/2M/6M in); weekly $1; one realtime $2
    lines = [
        _batch("2026-11-01", in_=1_000_000),
        _batch("2026-11-02", in_=2_000_000),
        _batch("2026-11-03", in_=6_000_000),
        _batch("2026-11-08", "weekly", in_=1_000_000),
        "2026-11-09 09:00:00,000 INFO Realtime brief: stop_reason=end_turn "
        "elapsed=1s usage(in=1000000 out=0 searches=0)",
    ]
    return brief_cost.parse_cost_lines(lines)


def test_summarise_reports_span_per_mode_and_path():
    out = brief_cost.summarise(_costs(), MODEL)
    assert "collect: days=3" in out
    assert f"mean=${statistics.mean([1, 2, 6]):.2f}" in out
    assert "median=$2.00" in out
    assert "min=$1.00" in out
    assert "max=$6.00" in out
    assert "projected_30d=$90.00" in out
    assert "weekly: days=1" in out
    assert "realtime_runs=1" in out
    assert "span=2026-11-01..2026-11-09" in out
    assert "web_search=$0.01/req (unverified)" in out


def test_summarise_names_a_short_span():
    out = brief_cost.summarise(_costs(), MODEL)
    assert "SHORT: only 9 days in the log" in out


def test_summarise_does_not_flag_a_full_span():
    costs = [
        CostLine("2026-10-01", "collect", "batch", 1000, 0, 0),
        CostLine("2026-10-31", "collect", "batch", 1000, 0, 0),
    ]
    assert "SHORT" not in brief_cost.summarise(costs, MODEL)


def test_summarise_empty_log_says_so():
    assert "no cost lines" in brief_cost.summarise([], MODEL)


def test_main_reads_rotations_oldest_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brief_cost.common, "DATA_DIR", tmp_path)
    (tmp_path / "newsbrief.log.2").write_text(
        _batch("2026-11-20", in_=1_000_000) + "\n", encoding="utf-8"
    )
    # the live file holds the rerun; it is newer, so it must win
    (tmp_path / "newsbrief.log").write_text(
        _batch("2026-11-20", in_=2_000_000) + "\nbad \xff bytes\n", encoding="utf-8"
    )
    assert brief_cost.main(["--model", MODEL]) == 0
    out = capsys.readouterr().out
    assert "collect: days=1" in out
    assert "mean=$2.00" in out


def test_main_without_a_log_returns_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brief_cost.common, "DATA_DIR", tmp_path)
    assert brief_cost.main([]) == 1

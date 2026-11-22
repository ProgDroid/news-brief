"""Task 5: thesis evidence extraction from POSITION SIGNALS at collect."""

import logging

import pytest

import brief
import common
import db
import dated_memory
from dated_memory import EvidenceResult, EvidenceRow

SUBMIT_DAY = "2000-01-01"
LABELS = {"T1": "TESTCO", "T2": "cluster:widgets"}
SECTION_BRIEF = "🌍 TOP STORIES\n• x\n\n📈 POSITION SIGNALS\n• TESTCO up on orders\n"


def _resp(rows, stop="tool_use"):
    return {
        "stop_reason": stop,
        "content": [
            {"type": "tool_use", "name": "emit_evidence", "input": {"rows": rows}}
        ],
    }


def test_request_disables_thinking_and_forces_the_tool():
    req = dated_memory.build_evidence_request(
        "• TESTCO up on orders", {"T1": "TESTCO"}, model="m"
    )
    assert req["thinking"] == {"type": "disabled"}
    assert req["max_tokens"] == 2048
    assert req["model"] == "m"
    assert req["tool_choice"] == {"type": "tool", "name": "emit_evidence"}
    content = req["messages"][0]["content"]
    assert "T1 = TESTCO" in content
    assert "• TESTCO up on orders" in content
    assert content.rstrip().endswith("Call emit_evidence once.")
    row = req["tools"][0]["input_schema"]["properties"]["rows"]["items"]["properties"]
    assert req["tools"][0]["name"] == "emit_evidence"
    assert row["thesis"]["enum"] == ["T1"]
    assert row["direction"]["enum"] == ["supports", "challenges"]
    assert "POSITION SIGNALS" in req["system"]


def test_valid_rows_map_labels_to_keys():
    rows, problems = dated_memory.validate_evidence(
        _resp(
            [
                {"thesis": "T1", "direction": "supports", "line": "Orders rose."},
                {"thesis": "T2", "direction": "challenges", "line": "Price fell."},
            ]
        ),
        LABELS,
    )
    assert problems == []
    assert rows == [
        EvidenceRow("TESTCO", "supports", "Orders rose."),
        EvidenceRow("cluster:widgets", "challenges", "Price fell."),
    ]


def test_unknown_label_is_a_problem_not_a_row():
    rows, problems = dated_memory.validate_evidence(
        _resp([{"thesis": "T9", "direction": "supports", "line": "x"}]), LABELS
    )
    assert rows == []
    assert len(problems) == 1 and "T9" in problems[0]


def test_a_key_instead_of_a_label_is_a_problem():
    rows, problems = dated_memory.validate_evidence(
        _resp([{"thesis": "TESTCO", "direction": "supports", "line": "x"}]), LABELS
    )
    assert rows == [] and len(problems) == 1


def test_bad_direction_and_empty_line_are_problems():
    rows, problems = dated_memory.validate_evidence(
        _resp(
            [
                {"thesis": "T1", "direction": "neutral", "line": "x"},
                {"thesis": "T1", "direction": "supports", "line": "   "},
                {"thesis": "T1", "direction": "supports"},
                "not a dict",
                {"thesis": ["T1"], "direction": "supports", "line": "x"},
            ]
        ),
        LABELS,
    )
    assert rows == []
    assert len(problems) == 5


def test_long_line_is_cut_to_300():
    rows, problems = dated_memory.validate_evidence(
        _resp([{"thesis": "T1", "direction": "supports", "line": "a" * 500}]), LABELS
    )
    assert problems == []
    assert len(rows[0].line) == 300 and rows[0].line.endswith("…")
    exact = "b" * 300
    rows, _ = dated_memory.validate_evidence(
        _resp([{"thesis": "T1", "direction": "supports", "line": exact}]), LABELS
    )
    assert rows[0].line == exact


def test_duplicate_rows_collapse():
    one = {"thesis": "T1", "direction": "supports", "line": "Orders rose."}
    rows, problems = dated_memory.validate_evidence(_resp([one, dict(one)]), LABELS)
    assert len(rows) == 1 and problems == []


def test_no_tool_block_reports_stop_reason():
    rows, problems = dated_memory.validate_evidence(
        {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "hi"}]},
        LABELS,
    )
    assert rows == [] and len(problems) == 1 and "max_tokens" in problems[0]


def test_tool_block_without_a_rows_list_is_no_answer():
    resp = {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "emit_evidence", "input": {}}],
    }
    rows, problems = dated_memory.validate_evidence(resp, LABELS)
    assert rows == [] and "no emit_evidence answer" in problems[0]


def test_no_positions_section_makes_no_call():
    called = []
    r = dated_memory.extract_thesis_evidence(
        "🌍 TOP STORIES\n• x", ["TESTCO"], post=called.append, model="m"
    )
    assert r.status == "no_section" and called == [] and r.rows == []


def test_no_keys_makes_no_call():
    called = []
    r = dated_memory.extract_thesis_evidence(
        SECTION_BRIEF, [], post=called.append, model="m"
    )
    assert r.status == "no_theses" and called == []


def test_extract_ok_labels_keys_and_logs_stop_reason(caplog):
    sent = []

    def post(payload):
        sent.append(payload)
        resp = _resp([{"thesis": "T2", "direction": "supports", "line": "Up."}])
        resp["usage"] = {"input_tokens": 11, "output_tokens": 7}
        return resp

    with caplog.at_level(logging.INFO, logger="newsbrief"):
        r = dated_memory.extract_thesis_evidence(
            SECTION_BRIEF, ["TESTCO", "cluster:widgets"], post=post, model="m"
        )
    assert r.status == "ok"
    assert r.rows == [EvidenceRow("cluster:widgets", "supports", "Up.")]
    content = sent[0]["messages"][0]["content"]
    assert "T1 = TESTCO" in content and "T2 = cluster:widgets" in content
    assert "TESTCO up on orders" in content
    text = caplog.text
    assert "stop_reason=tool_use" in text and "in=11" in text and "out=7" in text
    assert "Up." not in text  # evidence lines are never logged


def test_extract_no_answer_status():
    r = dated_memory.extract_thesis_evidence(
        SECTION_BRIEF,
        ["TESTCO"],
        post=lambda p: {"stop_reason": "max_tokens", "content": []},
        model="m",
    )
    assert r.status == "no_answer" and r.rows == [] and "max_tokens" in r.problems[0]


def test_extract_ok_with_zero_rows_is_ok():
    r = dated_memory.extract_thesis_evidence(
        SECTION_BRIEF, ["TESTCO"], post=lambda p: _resp([]), model="m"
    )
    assert r == EvidenceResult("ok", [], [])


# ── brief.py side ───────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.log.append("commit")


def _rig(monkeypatch, tmp_path, *, has=False, rows=None):
    """collect rig (copy of tests/test_collect_day_key.py's shape, trimmed)."""
    order = []
    state = {"batch_id": "b1", "date": SUBMIT_DAY}
    rec = {"saved": [], "extract": [], "order": order}
    monkeypatch.setattr(brief, "load_state", lambda: state)
    monkeypatch.setattr(brief, "obtain_brief", lambda *a, **k: (SECTION_BRIEF, "batch"))
    monkeypatch.setattr(brief, "deliver", lambda *a, **k: None)
    monkeypatch.setattr(brief, "extract_signals", lambda raw, **kw: ([], "ok"))
    monkeypatch.setattr(brief, "normalize_signals", lambda raw: (raw, []))
    monkeypatch.setattr(brief, "annotate_signal_sources", lambda s: s)
    monkeypatch.setattr(brief, "save_signals", lambda *a, **k: None)
    monkeypatch.setattr(
        brief, "clear_batch_state", lambda: order.append("clear_batch_state")
    )
    monkeypatch.setattr(brief, "telegram_alert", lambda *a, **k: None)
    monkeypatch.setattr(brief, "mode_paper", lambda: {})
    monkeypatch.setattr(brief, "load_book", lambda: {})
    monkeypatch.setattr(brief, "daily_trade_message", lambda *a, **k: None)
    monkeypatch.setattr(
        brief,
        "run_retention",
        lambda today: order.append("retention") or {"deleted": 0, "trimmed_lines": 0},
    )
    monkeypatch.setattr(brief, "_census_nudge", lambda: None)
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    monkeypatch.setattr(brief, "brief_memory_enabled", lambda: False)
    monkeypatch.setattr(brief, "claim_verify_enabled", lambda: False)
    monkeypatch.setattr(common, "BRIEF_DATED_MEMORY_ENABLED", True)
    monkeypatch.setattr(db, "is_configured", lambda: True)
    monkeypatch.setattr(db, "connect", lambda **kw: _Conn(order))
    monkeypatch.setattr(brief, "_thesis_keys_quiet", lambda: (["TESTCO"], None))
    monkeypatch.setattr(dated_memory, "has_brief_evidence", lambda conn, day: has)

    def _extract(text, keys, *, post, model):
        order.append("extract")
        rec["extract"].append((text, keys, model))
        return EvidenceResult("ok", rows or [], [])

    def _save(conn, on_date, rows_, *, model):
        order.append("save")
        rec["saved"].append((on_date, list(rows_), model))
        return len(rows_)

    monkeypatch.setattr(dated_memory, "extract_thesis_evidence", _extract)
    monkeypatch.setattr(dated_memory, "save_brief_evidence", _save)
    return rec


ROW = EvidenceRow("TESTCO", "supports", "Orders rose.")


def test_collect_records_evidence_by_the_submit_day(monkeypatch, tmp_path):
    rec = _rig(monkeypatch, tmp_path, rows=[ROW])
    brief.mode_collect()
    assert rec["saved"] == [(SUBMIT_DAY, [ROW], common.EVIDENCE_MODEL)]
    assert rec["extract"][0][1] == ["TESTCO"]
    assert rec["order"].index("save") < rec["order"].index("commit")


def test_evidence_runs_after_clear_batch_state(monkeypatch, tmp_path):
    rec = _rig(monkeypatch, tmp_path, rows=[ROW])
    brief.mode_collect()
    o = rec["order"]
    assert o.index("clear_batch_state") < o.index("extract") < o.index("retention")


def test_a_day_with_rows_is_never_extracted_twice(monkeypatch, tmp_path, caplog):
    rec = _rig(monkeypatch, tmp_path, has=True, rows=[ROW])
    with caplog.at_level(logging.INFO, logger="newsbrief"):
        brief.mode_collect()
    assert rec["extract"] == [] and rec["saved"] == []
    assert f"Evidence: already recorded for {SUBMIT_DAY}" in caplog.text


def test_a_day_with_no_rows_may_be_extracted_again(monkeypatch, tmp_path):
    rec = _rig(monkeypatch, tmp_path, has=False, rows=[ROW])
    brief.mode_collect()
    assert len(rec["extract"]) == 1


def test_collect_with_knob_off_never_extracts(monkeypatch, tmp_path):
    rec = _rig(monkeypatch, tmp_path, rows=[ROW])
    monkeypatch.setattr(common, "BRIEF_DATED_MEMORY_ENABLED", False)
    brief.mode_collect()
    assert rec["extract"] == [] and rec["saved"] == []


def test_no_database_skips_without_a_call(monkeypatch, tmp_path, caplog):
    rec = _rig(monkeypatch, tmp_path, rows=[ROW])
    monkeypatch.setattr(db, "is_configured", lambda: False)
    with caplog.at_level(logging.INFO, logger="newsbrief"):
        brief.mode_collect()
    assert rec["extract"] == []
    assert "Evidence: skipped, no database" in caplog.text


def test_an_extraction_crash_does_not_stop_collect(monkeypatch, tmp_path, caplog):
    rec = _rig(monkeypatch, tmp_path, rows=[ROW])

    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(dated_memory, "extract_thesis_evidence", boom)
    with caplog.at_level(logging.INFO, logger="newsbrief"):
        brief.mode_collect()
    assert "retention" in rec["order"]
    assert "Evidence: crashed (brief unaffected): api down" in caplog.text


def test_evidence_log_line_counts_and_problems(monkeypatch, tmp_path, caplog):
    _rig(monkeypatch, tmp_path, rows=[ROW])
    monkeypatch.setattr(
        dated_memory,
        "extract_thesis_evidence",
        lambda *a, **k: EvidenceResult("ok", [ROW], ["row 2: unknown label 'T9'"]),
    )
    with caplog.at_level(logging.INFO, logger="newsbrief"):
        brief.mode_collect()
    assert "Evidence: status=ok written=1 problems=1 push=ok" in caplog.text
    assert any(
        r.levelno == logging.WARNING and "unknown label 'T9'" in r.getMessage()
        for r in caplog.records
    )
    assert "Orders rose." not in caplog.text


def test_evidence_log_line_names_a_missing_push(monkeypatch, tmp_path, caplog):
    _rig(monkeypatch, tmp_path, rows=[ROW])
    monkeypatch.setattr(
        brief, "_thesis_keys_quiet", lambda: (["TESTCO"], "missing — not landed")
    )
    with caplog.at_level(logging.INFO, logger="newsbrief"):
        brief.mode_collect()
    line = next(m for m in caplog.messages if m.startswith("Evidence: status="))
    assert "push=missing" in line


def test_thesis_keys_quiet_never_sends_telegram_and_reports_the_push(
    monkeypatch, tmp_path
):
    sent = []
    monkeypatch.setattr(brief, "THESIS_FILE", tmp_path / "theses.json")
    monkeypatch.setattr(brief, "PORTFOLIO_THESIS_FILE", tmp_path / "absent.json")
    (tmp_path / "theses.json").write_text('{"TESTCO": "a", "cluster:widgets": "b"}')
    for target, name in (
        (brief, "telegram_send"),
        (common, "telegram_send"),
        (brief, "telegram_alert"),
    ):
        monkeypatch.setattr(target, name, lambda *a, **k: sent.append(a))
    keys, problem = brief._thesis_keys_quiet()
    assert sent == []
    assert keys == ["TESTCO", "cluster:widgets"]
    assert problem is not None and problem.startswith("missing")


def test_thesis_keys_quiet_merges_pushed_keys_sorted(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "THESIS_FILE", tmp_path / "theses.json")
    monkeypatch.setattr(brief, "PORTFOLIO_THESIS_FILE", tmp_path / "p.json")
    (tmp_path / "theses.json").write_text('{"ZED": "a"}')
    (tmp_path / "p.json").write_text('{"version": 1, "theses": {"ALPHA": "b"}}')
    assert brief._thesis_keys_quiet() == (["ALPHA", "ZED"], None)


def test_evidence_timeout_constant():
    assert brief.EVIDENCE_TIMEOUT == 90


@pytest.mark.parametrize("has", [True, False])
def test_record_is_callable_directly(monkeypatch, tmp_path, has):
    rec = _rig(monkeypatch, tmp_path, has=has, rows=[ROW])
    brief._record_thesis_evidence(SECTION_BRIEF, SUBMIT_DAY)
    assert bool(rec["extract"]) is (not has)

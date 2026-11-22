"""news-brief-hw8: collect must load submit-time artifacts by the SUBMIT day.

Since the 2026-09-01 scheduler cutover, submit runs at 20:00 and collect at 06:00
the next UTC day. Submit writes enrichment-, source_index- and claim_evidence-
files keyed by its own date; collect used to load them by `now()`, a different
date, so all three silently fell back to their no-data paths. The submit day is
already in runtime state (`state["date"]`, written by mode_submit).
"""

import json

import brief
import claim_store
import db

SUBMIT_DAY = "2000-01-01"  # can never be the real collect day


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_collect(monkeypatch, tmp_path, state):
    seen = {}
    monkeypatch.setattr(brief, "load_state", lambda: state)
    monkeypatch.setattr(brief, "poll_batch", lambda bid, **kw: "RAW")
    monkeypatch.setattr(brief, "deliver", lambda *a, **k: None)
    monkeypatch.setattr(brief, "extract_signals", lambda raw, **kw: ([{"t": 1}], "ok"))
    monkeypatch.setattr(brief, "normalize_signals", lambda raw: (raw, []))
    monkeypatch.setattr(brief, "annotate_signal_sources", lambda s: s)
    monkeypatch.setattr(brief, "save_signals", lambda *a, **k: None)
    monkeypatch.setattr(brief, "clear_batch_state", lambda: None)
    monkeypatch.setattr(brief, "telegram_alert", lambda *a, **k: None)
    monkeypatch.setattr(brief, "mode_paper", lambda: {})
    monkeypatch.setattr(brief, "load_book", lambda: {})
    monkeypatch.setattr(brief, "daily_trade_message", lambda *a, **k: None)
    monkeypatch.setattr(
        brief, "run_retention", lambda today: {"deleted": 0, "trimmed_lines": 0}
    )
    monkeypatch.setattr(brief, "_census_nudge", lambda: None)
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)

    # Enrichment: present ONLY under the submit day.
    enr_dir = tmp_path / "enrichment"
    enr_dir.mkdir()
    (enr_dir / f"enrichment-{SUBMIT_DAY}.json").write_text(json.dumps({}))
    monkeypatch.setattr(brief, "bundles_from_dict", lambda d: "BUNDLES")

    def _annotate(signals, bundles):
        seen["annotated_with"] = bundles
        return signals

    monkeypatch.setattr(brief, "annotate_signals", _annotate)

    # Ledger reconcile: record which day's source index it asked for.
    monkeypatch.setattr(brief, "brief_memory_enabled", lambda: True)
    monkeypatch.setattr(db, "connect", lambda **kw: _Conn())
    monkeypatch.setattr(claim_store, "load_ledger", lambda conn: {})
    monkeypatch.setattr(claim_store, "next_ledger_num", lambda conn: 1)
    monkeypatch.setattr(claim_store, "save_ledger", lambda conn, b, a, t: (0, 0, 0))

    def _load_source_index(day):
        seen["source_index_day"] = day
        return ""

    monkeypatch.setattr(brief, "load_source_index", _load_source_index)
    monkeypatch.setattr(brief, "reconcile_ledger", lambda *a, **k: {})

    # Claim verification: record which day's evidence it was pointed at.
    monkeypatch.setattr(brief, "claim_verify_enabled", lambda: True)

    def _verify(brief_text, day, **kw):
        seen["verify_day"] = day

    monkeypatch.setattr(brief, "run_verification", _verify)
    return seen


def test_collect_loads_submit_artifacts_by_the_submit_day(monkeypatch, tmp_path):
    seen = _stub_collect(monkeypatch, tmp_path, {"batch_id": "b1", "date": SUBMIT_DAY})

    brief.mode_collect()

    assert seen.get("source_index_day") == SUBMIT_DAY
    assert seen.get("verify_day") == SUBMIT_DAY
    assert seen.get("annotated_with") == "BUNDLES", (
        "enrichment saved under the submit day was not found at collect"
    )


def test_collect_without_a_submit_day_falls_back_to_today(monkeypatch, tmp_path):
    # A state file written before submit stamped "date" must not crash collect.
    seen = _stub_collect(monkeypatch, tmp_path, {"batch_id": "b1"})

    brief.mode_collect()

    today = brief.datetime.now(brief.timezone.utc).strftime("%Y-%m-%d")
    assert seen.get("source_index_day") == today
    assert seen.get("verify_day") == today

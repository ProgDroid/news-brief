import json

import brief_memory as bm


def test_is_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("BRIEF_MEMORY_ENABLED", raising=False)
    assert bm.is_enabled() is False
    monkeypatch.setenv("BRIEF_MEMORY_ENABLED", "1")
    assert bm.is_enabled() is True


def test_empty_ledger_shape():
    assert bm.empty_ledger() == {"version": 1, "claims": []}


def test_load_missing_returns_empty(tmp_path):
    assert bm.load_ledger(tmp_path / "nope.json") == {"version": 1, "claims": []}


def test_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert bm.load_ledger(p) == {"version": 1, "claims": []}


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "ledger.json"
    ledger = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "BOJ at 1.0% since 2026-06-16",
                "topic": "japan",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 7,
            }
        ],
    }
    bm.save_ledger(ledger, p)
    assert bm.load_ledger(p) == ledger
    assert json.loads(p.read_text(encoding="utf-8")) == ledger

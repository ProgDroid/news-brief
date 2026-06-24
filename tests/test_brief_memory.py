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


def test_merge_reaffirm_carries_first_seen_and_increments():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "BOJ at 1.0%",
                "topic": "japan",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 5,
            }
        ],
    }
    out = bm.merge_ledger(
        prior,
        [
            {
                "id": "c-0001",
                "claim": "BOJ still at 1.0% (Himino: more to come)",
                "topic": "japan",
            }
        ],
        "2026-06-24",
    )
    c = out["claims"][0]
    assert c["id"] == "c-0001"
    assert c["first_seen"] == "2026-06-18"
    assert c["last_reaffirmed"] == "2026-06-24"
    assert c["restate_count"] == 6
    assert c["claim"] == "BOJ still at 1.0% (Himino: more to come)"


def test_merge_new_claim_gets_next_id_and_today():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0003",
                "claim": "x",
                "topic": "a",
                "first_seen": "2026-06-24",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
            }
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"claim": "China may miss 4.5-5% growth", "topic": "china"}],
        "2026-06-24",
    )
    new = [c for c in out["claims"] if c["claim"].startswith("China")][0]
    assert new["id"] == "c-0004"
    assert new["first_seen"] == new["last_reaffirmed"] == "2026-06-24"
    assert new["restate_count"] == 1


def test_merge_unreturned_prior_claim_is_kept():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "kept",
                "topic": "a",
                "first_seen": "2026-06-22",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 2,
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert [c["id"] for c in out["claims"]] == ["c-0001"]
    assert out["claims"][0]["last_reaffirmed"] == "2026-06-23"  # untouched


def test_merge_retires_stale_claims():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "old",
                "topic": "a",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-10",
                "restate_count": 1,
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")  # 14 days > 7
    assert out["claims"] == []


def test_merge_caps_to_most_recent():
    claims = [
        {
            "id": f"c-{i:04d}",
            "claim": str(i),
            "topic": "a",
            "first_seen": "2026-06-24",
            "last_reaffirmed": f"2026-06-{10 + i:02d}",
            "restate_count": 1,
        }
        for i in range(1, 6)
    ]
    prior = {"version": 1, "claims": claims}
    out = bm.merge_ledger(prior, [], "2026-06-24", cap=2, retire_after_days=999)
    kept = [c["last_reaffirmed"] for c in out["claims"]]
    assert kept == ["2026-06-15", "2026-06-14"]

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


def test_render_empty_ledger_is_blank():
    assert bm.render_established_block({"version": 1, "claims": []}) == ""


def test_render_lists_claims_with_instruction():
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
            },
        ],
    }
    block = bm.render_established_block(ledger)
    assert "ESTABLISHED" in block
    assert "BOJ at 1.0% since 2026-06-16" in block
    assert "japan" in block
    assert "one clause" in block.lower()


def test_build_reconcile_prompt_contains_ledger_and_brief():
    ledger = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "BOJ at 1.0%",
                "topic": "japan",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 7,
            }
        ],
    }
    p = bm.build_reconcile_prompt(ledger, "Today the BOJ left rates unchanged.")
    assert "c-0001" in p
    assert "Today the BOJ left rates unchanged." in p


def test_parse_extracts_array_and_filters():
    text = 'Here you go:\n[{"id":"c-0001","claim":"x","topic":"a"},{"claim":"y"},{"topic":"no-claim"}]'
    out = bm.parse_reconcile_response(text)
    assert out == [{"id": "c-0001", "claim": "x", "topic": "a"}, {"claim": "y"}]


def test_parse_raises_without_array():
    import pytest

    with pytest.raises(ValueError):
        bm.parse_reconcile_response("no array here")


def test_reconcile_success_merges(monkeypatch):
    prior = {"version": 1, "claims": []}

    def fake_call(system, user):
        return '[{"claim":"BOJ at 1.0% since 2026-06-16","topic":"japan"}]'

    out = bm.reconcile_ledger(prior, "BOJ held rates.", "2026-06-24", call=fake_call)
    assert out["claims"][0]["claim"] == "BOJ at 1.0% since 2026-06-16"
    assert out["claims"][0]["id"] == "c-0001"


def test_reconcile_failure_returns_prior_unchanged():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "x",
                "topic": "a",
                "first_seen": "2026-06-24",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
            }
        ],
    }

    def boom(system, user):
        raise RuntimeError("network down")

    out = bm.reconcile_ledger(prior, "brief", "2026-06-25", call=boom)
    assert out is prior  # untouched, memory never lost


def test_reconcile_bad_json_returns_prior():
    prior = {"version": 1, "claims": []}
    out = bm.reconcile_ledger(prior, "brief", "2026-06-24", call=lambda s, u: "garbage")
    assert out == prior


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_messages_call_raises_on_truncated_stop_reason(monkeypatch):
    # A max_tokens cutoff must surface as a clear truncation error, not be
    # returned as text that the parser later misreports as "no JSON array".
    body = {
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": '[{"id":"c-0001","claim":"trunc'}],
    }
    monkeypatch.setattr(bm.requests, "post", lambda *a, **k: _FakeResp(body))
    import pytest

    with pytest.raises(ValueError, match="(?i)truncat|max_tokens"):
        bm._messages_call("sys", "user")


def test_messages_call_budget_fits_full_ledger(monkeypatch):
    # 2048 was too small for a full 25-claim ledger (~2400 tokens) and caused
    # the truncation. The budget must comfortably exceed that.
    captured = {}

    def fake_post(*a, **k):
        captured["json"] = k["json"]
        return _FakeResp(
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "[]"}]}
        )

    monkeypatch.setattr(bm.requests, "post", fake_post)
    out = bm._messages_call("sys", "user")
    assert out == "[]"
    assert captured["json"]["max_tokens"] >= 4096


def test_reconcile_prompt_bounds_output_size():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief")
    assert f"at most {bm.MAX_CLAIMS}" in p


def test_part_a_feeds_back_beyond_2000_chars():
    import brief

    fb = {}
    yesterday = "HEAD " + ("x" * 2800) + " MARKER-3000 " + ("y" * 200)
    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", yesterday, "", fb, "", "", "", ""
    )
    assert "MARKER-3000" in prompt  # was truncated away at [:2000]


def test_part_a_instruction_mentions_standing_frames():
    import brief

    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", "yesterday text", "", {}, "", "", "", ""
    )
    assert "standing analytical frames" in prompt


def test_established_block_injected_into_prompt():
    import brief

    prompt = brief.build_daily_prompt(
        "feeds",
        "web",
        "chroma",
        "y",
        "",
        {},
        "",
        "",
        "",
        "",
        established_block="## ESTABLISHED — THE READER ALREADY KNOWS THESE\n  • [japan] BOJ at 1.0%",
    )
    assert "BOJ at 1.0%" in prompt
    assert "ESTABLISHED" in prompt


def test_no_established_block_when_empty():
    import brief

    prompt = brief.build_daily_prompt(
        "feeds", "web", "chroma", "y", "", {}, "", "", "", "", established_block=""
    )
    assert "ESTABLISHED — THE READER ALREADY KNOWS" not in prompt


def test_merge_keeps_peak_source_count_on_reaffirm():
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
                "source_count": 6,
            }
        ],
    }
    # observed today is LOWER (story aged out) -> peak must hold at 6
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "BOJ at 1.0%", "topic": "japan", "source_count": 2}],
        "2026-06-24",
    )
    assert out["claims"][0]["source_count"] == 6
    # observed today is HIGHER -> peak rises
    out2 = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "BOJ at 1.0%", "topic": "japan", "source_count": 9}],
        "2026-06-24",
    )
    assert out2["claims"][0]["source_count"] == 9


def test_merge_new_claim_takes_observed_source_count():
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(
        prior, [{"claim": "new fact", "topic": "x", "source_count": 4}], "2026-06-24"
    )
    assert out["claims"][0]["source_count"] == 4


def test_merge_missing_source_count_defaults_zero():
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(prior, [{"claim": "no count", "topic": "x"}], "2026-06-24")
    assert out["claims"][0]["source_count"] == 0


def test_merge_unreturned_claim_preserves_source_count():
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
                "source_count": 5,
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"][0]["source_count"] == 5


def test_parse_extracts_source_count():
    text = '[{"claim":"x","topic":"a","source_count":3}]'
    assert bm.parse_reconcile_response(text) == [
        {"claim": "x", "topic": "a", "source_count": 3}
    ]


def test_parse_tolerates_bad_source_count():
    # missing, non-numeric string, bool, and negative are all dropped/clamped
    text = (
        '[{"claim":"a"},'
        '{"claim":"b","source_count":"two"},'
        '{"claim":"c","source_count":true},'
        '{"claim":"d","source_count":"5"},'
        '{"claim":"e","source_count":-2}]'
    )
    out = bm.parse_reconcile_response(text)
    assert out[0] == {"claim": "a"}  # absent -> omitted
    assert out[1] == {"claim": "b"}  # "two" -> omitted
    assert out[2] == {"claim": "c"}  # bool -> omitted
    assert out[3] == {"claim": "d", "source_count": 5}  # numeric string -> int
    assert out[4] == {"claim": "e", "source_count": 0}  # negative -> clamped

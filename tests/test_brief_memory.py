import json

import pytest

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


def test_reconcile_prompt_teaches_severity():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief")
    assert "severity" in p
    assert '"high"' in p
    assert "when unsure" in p.lower()


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


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, ""),
        (1, "single-source"),
        (2, "corroborated"),
        (3, "corroborated"),
        (4, "widely corroborated"),
        (12, "widely corroborated"),
        (None, ""),
        ("bad", ""),
    ],
)
def test_corroboration_cue_buckets(n, expected):
    assert bm._corroboration_cue(n) == expected


def test_render_includes_corroboration_cue():
    ledger = {
        "version": 1,
        "claims": [
            {
                "id": "c-1",
                "claim": "Widely known fact",
                "topic": "macro",
                "first_seen": "2026-06-20",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 3,
                "source_count": 7,
            },
            {
                "id": "c-2",
                "claim": "Thin rumor",
                "topic": "tech",
                "first_seen": "2026-06-24",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 1,
                "source_count": 1,
            },
        ],
    }
    block = bm.render_established_block(ledger)
    assert "(widely corroborated) Widely known fact" in block
    assert "(single-source) Thin rumor" in block
    # instruction teaches the model how to use the cue
    assert "single-source" in block.lower()


def test_render_omits_cue_when_no_source_count():
    ledger = {
        "version": 1,
        "claims": [
            {
                "id": "c-1",
                "claim": "Legacy claim",
                "topic": "macro",
                "first_seen": "2026-06-20",
                "last_reaffirmed": "2026-06-24",
                "restate_count": 3,
            },  # no source_count
        ],
    }
    block = bm.render_established_block(ledger)
    assert "Legacy claim" in block
    assert "corroborated" not in block.split("Legacy claim")[0].split("\n")[-1]


def test_build_reconcile_prompt_includes_source_index():
    p = bm.build_reconcile_prompt(
        {"version": 1, "claims": []},
        "brief text",
        "SOURCE: Reuters\n- OPEC extends cut\nSOURCE: AP\n- OPEC extends cut",
    )
    assert "SOURCE: Reuters" in p
    assert "source_count" in p


def test_build_reconcile_prompt_placeholder_when_no_index():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief text")
    assert "no source index" in p.lower()


def test_reconcile_passes_index_and_records_count(monkeypatch):
    captured = {}

    def fake_call(system, user):
        captured["user"] = user
        return '[{"claim":"OPEC extends cut","topic":"oil","source_count":2}]'

    out = bm.reconcile_ledger(
        {"version": 1, "claims": []},
        "OPEC extended cuts.",
        "2026-06-24",
        call=fake_call,
        source_index="SOURCE: Reuters\n- OPEC extends cut",
    )
    assert "SOURCE: Reuters" in captured["user"]
    assert out["claims"][0]["source_count"] == 2


def test_build_source_index_extracts_sources_and_titles():
    import brief

    feed = (
        "\n### Reuters [WIRE] (WORLD)\n"
        "- OPEC+ extends production cut (Mon, 24 Jun)\n"
        "  long summary body that must be dropped\n"
        "- Fed holds rates (Mon)\n"
        "\n### Al Jazeera [REGIONAL · ARAB · STATE-FUNDED] (MIDEAST)\n"
        "- OPEC+ extends production cut (Tue)\n"
    )
    idx = brief.build_source_index(feed, "")
    assert "SOURCE: Reuters" in idx
    assert "SOURCE: Al Jazeera" in idx
    assert "- OPEC+ extends production cut" in idx
    assert "- Fed holds rates" in idx
    # summaries and pub dates are stripped
    assert "long summary body" not in idx
    assert "(Mon, 24 Jun)" not in idx


def test_build_source_index_handles_empty():
    import brief

    assert brief.build_source_index("(no RSS content)", "(no web content)") == ""


def test_parse_extracts_severity():
    text = '[{"claim":"x","topic":"a","severity":"high"}]'
    assert bm.parse_reconcile_response(text) == [
        {"claim": "x", "topic": "a", "severity": "high"}
    ]


def test_parse_tolerates_bad_severity():
    text = (
        '[{"claim":"a"},'
        '{"claim":"b","severity":"medium"},'
        '{"claim":"c","severity":5},'
        '{"claim":"d","severity":"HIGH"}]'
    )
    out = bm.parse_reconcile_response(text)
    assert out[0] == {"claim": "a"}  # absent -> omitted
    assert out[1] == {"claim": "b"}  # invalid enum -> omitted
    assert out[2] == {"claim": "c"}  # non-string -> omitted
    assert out[3] == {"claim": "d", "severity": "high"}  # case-normalized


def test_source_index_save_load_roundtrip(tmp_path, monkeypatch):
    import brief

    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    brief.save_source_index("SOURCE: Reuters\n- Big news", "2026-06-25")
    assert "SOURCE: Reuters" in brief.load_source_index("2026-06-25")


def test_load_source_index_missing_returns_empty(tmp_path, monkeypatch):
    import brief

    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    assert brief.load_source_index("2099-01-01") == ""


# ---------------------------------------------------------------------------
# Task 1: severity constants and helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("low", "low"),
        ("normal", "normal"),
        ("high", "high"),
        ("HIGH", "high"),
        ("  High ", "high"),
        ("medium", None),
        ("", None),
        (None, None),
        (2, None),
        (True, None),
    ],
)
def test_coerce_severity(raw, expected):
    assert bm._coerce_severity(raw) == expected


@pytest.mark.parametrize(
    "sev,expected",
    [("high", 7), ("normal", 0), ("low", 0), (None, 0), ("bogus", 0)],
)
def test_ttl_bonus(sev, expected):
    assert bm._ttl_bonus(sev) == expected


@pytest.mark.parametrize(
    "sev,expected",
    [("high", 2), ("normal", 1), ("low", 0), (None, 1), ("bogus", 1)],
)
def test_severity_rank(sev, expected):
    assert bm._severity_rank(sev) == expected


# ---------------------------------------------------------------------------
# Task 2: severity stored on new and reaffirmed claims in merge_ledger
# ---------------------------------------------------------------------------


def test_merge_new_claim_takes_observed_severity():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "war broke out", "topic": "geo", "severity": "high"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"


def test_merge_new_claim_defaults_severity_normal():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "minor fact", "topic": "x"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"


def test_merge_new_claim_invalid_severity_defaults_normal():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "fact", "topic": "x", "severity": "spicy"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"


def _high_prior():
    return {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "fact",
                "topic": "x",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-23",
                "restate_count": 5,
                "severity": "high",
            }
        ],
    }


def test_merge_reaffirm_updates_severity_downward():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x", "severity": "normal"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "normal"  # importance can fade


def test_merge_reaffirm_keeps_severity_when_omitted():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x"}],  # no severity
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"  # omission must not demote


def test_merge_reaffirm_keeps_severity_when_invalid():
    out = bm.merge_ledger(
        _high_prior(),
        [{"id": "c-0001", "claim": "fact", "topic": "x", "severity": "???"}],
        "2026-06-24",
    )
    assert out["claims"][0]["severity"] == "high"  # garbage must not demote


# ---------------------------------------------------------------------------
# Task 3: severity-aware retention (effective-age TTL filter + cap sort)
# ---------------------------------------------------------------------------


def test_high_severity_survives_to_14_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "war",
                "topic": "geo",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-10",  # exactly 14 days before 06-24
                "restate_count": 1,
                "severity": "high",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_high_severity_retires_after_14_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "war",
                "topic": "geo",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-09",  # 15 days before 06-24
                "restate_count": 1,
                "severity": "high",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_normal_severity_still_retires_at_7_days():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "minor",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-16",  # 8 days before 06-24
                "restate_count": 1,
                "severity": "normal",
            }
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_missing_severity_treated_as_normal_for_retention():
    # legacy claim with no severity field retires at the normal 7-day TTL
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "legacy",
                "topic": "x",
                "first_seen": "2026-06-01",
                "last_reaffirmed": "2026-06-16",  # 8 days -> past normal TTL
                "restate_count": 1,
            }  # no severity field
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert out["claims"] == []


def test_cap_keeps_high_severity_over_fresher_normal():
    prior = {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": "old major war",
                "topic": "geo",
                "first_seen": "2026-06-18",
                "last_reaffirmed": "2026-06-20",  # older
                "restate_count": 1,
                "severity": "high",
            },
            {
                "id": "c-0002",
                "claim": "fresh trivia",
                "topic": "x",
                "first_seen": "2026-06-24",
                "last_reaffirmed": "2026-06-24",  # fresher
                "restate_count": 1,
                "severity": "normal",
            },
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24", cap=1)
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_cap_orders_by_severity_then_recency():
    def mk(cid, day, sev):
        return {
            "id": cid,
            "claim": cid,
            "topic": "x",
            "first_seen": day,
            "last_reaffirmed": day,
            "restate_count": 1,
            "severity": sev,
        }

    prior = {
        "version": 1,
        "claims": [
            mk("c-low", "2026-06-24", "low"),  # freshest but lowest rank
            mk("c-normA", "2026-06-22", "normal"),
            mk("c-normB", "2026-06-23", "normal"),
            mk("c-high", "2026-06-20", "high"),  # oldest but highest rank
        ],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24", retire_after_days=999)
    assert [c["id"] for c in out["claims"]] == [
        "c-high",
        "c-normB",
        "c-normA",
        "c-low",
    ]

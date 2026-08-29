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


def test_working_set_takes_most_recent():
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
    out = bm.merge_ledger(prior, [], "2026-06-24", retire_after_days=999)
    kept = [c["last_reaffirmed"] for c in bm.select_working_set(out, limit=2)]
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
    assert f"at most {bm.WORKING_SET_SIZE}" in p


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


def test_working_set_keeps_high_severity_over_fresher_normal():
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
    out = bm.merge_ledger(prior, [], "2026-06-24")
    assert [c["id"] for c in bm.select_working_set(out, limit=1)] == ["c-0001"]


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


# ── Fix #1: storage / working-set split (news-brief-j07) ──────────────────────
def _mk_claim(cid, day="2026-06-24", sev="normal", claim=None):
    return {
        "id": cid,
        "claim": claim if claim is not None else cid,
        "topic": "x",
        "first_seen": day,
        "last_reaffirmed": day,
        "restate_count": 1,
        "severity": sev,
    }


def _ledger_of(n, **kw):
    return {
        "version": 1,
        "claims": [_mk_claim(f"c-{i:04d}", **kw) for i in range(1, n + 1)],
    }


def test_merge_stores_more_than_the_working_set_size():
    out = bm.merge_ledger(_ledger_of(30), [], "2026-06-24")
    assert len(out["claims"]) == 30


def test_select_working_set_limits_to_size():
    assert len(bm.select_working_set(_ledger_of(30))) == bm.WORKING_SET_SIZE


def test_select_working_set_takes_most_recent_first():
    ledger = {
        "version": 1,
        "claims": [
            _mk_claim("c-0001", "2026-06-10"),
            _mk_claim("c-0002", "2026-06-14"),
            _mk_claim("c-0003", "2026-06-12"),
        ],
    }
    got = [c["id"] for c in bm.select_working_set(ledger, limit=2)]
    assert got == ["c-0002", "c-0003"]


def test_select_working_set_prefers_high_severity_over_fresher_normal():
    ledger = {
        "version": 1,
        "claims": [
            _mk_claim("c-high", "2026-06-20", "high"),
            _mk_claim("c-norm", "2026-06-24", "normal"),
        ],
    }
    assert [c["id"] for c in bm.select_working_set(ledger, limit=1)] == ["c-high"]


def test_select_working_set_on_empty_ledger_is_empty():
    assert bm.select_working_set({"version": 1, "claims": []}) == []


def test_reconcile_prompt_sends_only_the_working_set():
    ledger = _ledger_of(30)
    p = bm.build_reconcile_prompt(ledger, "brief")
    sent = [c["id"] for c in ledger["claims"] if c["id"] in p]
    assert len(sent) == bm.WORKING_SET_SIZE


def test_render_established_block_shows_only_the_working_set():
    ledger = {
        "version": 1,
        "claims": [
            _mk_claim(f"c-{i:04d}", claim=f"fact number {i}") for i in range(1, 31)
        ],
    }
    assert (
        bm.render_established_block(ledger).count("fact number") == bm.WORKING_SET_SIZE
    )


def test_claim_outside_the_working_set_survives_reconcile():
    """The regression the split exists for: a claim the model never saw, because
    it fell outside the prompt window, must not be dropped from storage."""
    out = bm.merge_ledger(
        _ledger_of(30), [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24"
    )
    assert len(out["claims"]) == 30
    assert "c-0030" in {c["id"] for c in out["claims"]}


# ── Fix #3: claim status lifecycle, QUARANTINED (news-brief-jx9.8) ────────────
# The field is written and never read: rendering, TTL and working-set ordering
# all ignore it until the restatement guard (news-brief-93u) lifts the detector
# above its measured ~61% precision. The quarantine tests below pin that.
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("standing", "standing"),
        ("BROKEN", "broken"),
        ("  challenged  ", "challenged"),
        ("resolved", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_coerce_status(raw, expected):
    assert bm._coerce_status(raw) == expected


def test_merge_new_claim_defaults_to_standing():
    out = bm.merge_ledger({"version": 1, "claims": []}, [{"claim": "x"}], "2026-06-24")
    assert out["claims"][0]["status"] == "standing"


def test_merge_new_claim_takes_explicit_status():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x", "status": "challenged"}],
        "2026-06-24",
    )
    assert out["claims"][0]["status"] == "challenged"


def test_merge_invalid_status_defaults_to_standing():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x", "status": "wobbly"}],
        "2026-06-24",
    )
    assert out["claims"][0]["status"] == "standing"


def test_merge_marks_a_reaffirmed_claim_broken_and_dates_it():
    prior = {"version": 1, "claims": [_mk_claim("c-0001", "2026-06-20")]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "broken"}],
        "2026-06-24",
    )
    got = out["claims"][0]
    assert got["status"] == "broken"
    assert got["broke_on"] == "2026-06-24"


def test_merge_records_broken_by():
    prior = {"version": 1, "claims": [_mk_claim("c-0001", "2026-06-20")]}
    out = bm.merge_ledger(
        prior,
        [
            {
                "id": "c-0001",
                "claim": "c-0001",
                "status": "broken",
                "broken_by": "Trump reversed the licence",
            }
        ],
        "2026-06-24",
    )
    assert out["claims"][0]["broken_by"] == "Trump reversed the licence"


def test_merge_marks_a_reaffirmed_claim_challenged():
    prior = {"version": 1, "claims": [_mk_claim("c-0001", "2026-06-20")]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "challenged"}],
        "2026-06-24",
    )
    assert out["claims"][0]["status"] == "challenged"


def test_merge_keeps_prior_status_when_model_omits_it():
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), status="broken")]}
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    assert out["claims"][0]["status"] == "broken"


def test_broke_on_is_not_overwritten_by_a_later_merge():
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001"), status="broken", broke_on="2026-06-21")],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "broken"}],
        "2026-06-24",
    )
    assert out["claims"][0]["broke_on"] == "2026-06-21"


def test_prior_claim_without_status_reads_as_standing():
    prior = {"version": 1, "claims": [_mk_claim("c-0001")]}
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    assert out["claims"][0]["status"] == "standing"


def test_parse_extracts_status_and_broken_by():
    got = bm.parse_reconcile_response(
        '[{"claim": "a", "status": "broken", "broken_by": "the reversal"}]'
    )
    assert got[0]["status"] == "broken"
    assert got[0]["broken_by"] == "the reversal"


def test_parse_tolerates_bad_status():
    got = bm.parse_reconcile_response('[{"claim": "a", "status": 7}]')
    assert "status" not in got[0]


def test_reconcile_prompt_teaches_status_values_and_default():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert '"standing"' in p
    assert '"challenged"' in p
    assert '"broken"' in p
    assert "in doubt" in p


def test_reconcile_prompt_says_a_restatement_is_not_a_break():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "restatement" in p
    assert "not a break" in p


def test_reconcile_prompt_says_absence_is_not_contradiction():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "not contradiction" in p


# — Quarantine: nothing reads status yet —
def test_render_output_is_unaffected_by_status():
    standing = {"version": 1, "claims": [_mk_claim("c-0001", claim="a durable fact")]}
    broken = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", claim="a durable fact"),
                status="broken",
                broke_on="2026-06-24",
                broken_by="the reversal",
            )
        ],
    }
    assert bm.render_established_block(broken) == bm.render_established_block(standing)


def test_broken_claim_still_retires_on_ttl():
    """Quarantine boundary: news-brief-jx9.6 will exempt non-standing claims from
    the TTL. Until it lands, status must not change retention."""
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001", "2026-06-10"), status="broken")],
    }
    assert bm.merge_ledger(prior, [], "2026-06-24")["claims"] == []


# ── Fix #2: claim dedup / id reuse (news-brief-pon) ───────────────────────────
# Conservative by construction: two claims whose numeric tokens differ are never
# merged, whatever their wording overlap. Duplicates only waste working-set slots
# (storage is unbounded since news-brief-j07), but a false merge destroys an
# assertion, so the asymmetry is priced in.
@pytest.mark.parametrize(
    "a,b,expected",
    [
        # identical
        (
            "Ukraine was granted a Patriot production licence",
            "Ukraine was granted a Patriot production licence",
            True,
        ),
        # differs only in function words
        (
            "Ukraine has been granted a Patriot production licence",
            "Ukraine was granted the Patriot production licence",
            True,
        ),
        # same words, different number -> never merged
        (
            "The BOJ raised the policy rate to 1.0%",
            "The BOJ raised the policy rate to 1.5%",
            False,
        ),
        (
            "Japan's 10-year JGB held around 2.88%",
            "Japan's 10-year JGB held around 2.70%",
            False,
        ),
        # genuinely different assertions about the same subject
        (
            "Iran and Oman are negotiating a shipping framework",
            "Iran and Oman signed a shipping framework",
            False,
        ),
        # unrelated
        (
            "Ukraine was granted a Patriot production licence",
            "The BOJ held rates steady",
            False,
        ),
        # empty / degenerate never merges
        ("", "", False),
        ("the and of", "the and of", False),
    ],
)
def test_is_duplicate_claim(a, b, expected):
    assert bm._is_duplicate_claim(a, b) is expected


def test_merge_reuses_the_id_of_a_near_identical_new_claim():
    prior = {
        "version": 1,
        "claims": [
            _mk_claim(
                "c-0001", claim="Ukraine has been granted a Patriot production licence"
            )
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"claim": "Ukraine was granted the Patriot production licence"}],
        "2026-06-24",
    )
    assert len(out["claims"]) == 1
    assert out["claims"][0]["id"] == "c-0001"


def test_merge_reusing_an_id_counts_as_a_reaffirmation():
    prior = {
        "version": 1,
        "claims": [
            _mk_claim(
                "c-0001",
                "2026-06-20",
                claim="Ukraine has been granted a Patriot production licence",
            )
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"claim": "Ukraine was granted the Patriot production licence"}],
        "2026-06-24",
    )
    assert out["claims"][0]["restate_count"] == 2
    assert out["claims"][0]["last_reaffirmed"] == "2026-06-24"


def test_merge_does_not_merge_claims_differing_in_a_number():
    prior = {
        "version": 1,
        "claims": [_mk_claim("c-0001", claim="The BOJ raised the policy rate to 1.0%")],
    }
    out = bm.merge_ledger(
        prior,
        [{"claim": "The BOJ raised the policy rate to 1.5%"}],
        "2026-06-24",
    )
    assert len(out["claims"]) == 2


def test_merge_dedups_near_identical_claims_within_one_response():
    """The measured Patriot case: three near-identical claims in a single reply."""
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [
            {"claim": "Ukraine was granted a Patriot production licence"},
            {"claim": "Ukraine has been granted the Patriot production licence"},
            {"claim": "Ukraine was granted a Patriot production licence"},
        ],
        "2026-06-24",
    )
    assert len(out["claims"]) == 1


def test_merge_keeps_genuinely_distinct_claims():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [
            {"claim": "Ukraine was granted a Patriot production licence"},
            {"claim": "The BOJ held rates steady"},
        ],
        "2026-06-24",
    )
    assert len(out["claims"]) == 2


def test_merge_does_not_dedup_a_claim_the_model_gave_an_explicit_id():
    """An echoed id is authoritative — never second-guess it with similarity."""
    prior = {
        "version": 1,
        "claims": [
            _mk_claim(
                "c-0001", claim="Ukraine was granted a Patriot production licence"
            ),
            _mk_claim("c-0002", claim="The BOJ held rates steady"),
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0002", "claim": "Ukraine was granted a Patriot production licence"}],
        "2026-06-24",
    )
    assert {c["id"] for c in out["claims"]} == {"c-0001", "c-0002"}


def test_reconcile_prompt_asks_to_reuse_existing_ids():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "different words" in p
    assert "twice" in p


# ── Fix #6: claim text immutable once status != standing (news-brief-jx9.5) ───
def test_claim_text_is_frozen_when_the_same_reply_marks_it_broken():
    """The measured Patriot case: the 2026-08-29 replay marked the claim broken
    AND rewrote its text into a description of its own reversal, so the ledger
    read back as though the reversal had itself been reversed."""
    prior = {
        "version": 1,
        "claims": [
            _mk_claim("c-0001", claim="Ukraine holds a Patriot production licence")
        ],
    }
    out = bm.merge_ledger(
        prior,
        [
            {
                "id": "c-0001",
                "claim": "Trump reversed course on Ukraine's Patriot-production licence",
                "status": "broken",
                "broken_by": "Trump reversed the licence",
            }
        ],
        "2026-06-24",
    )
    got = out["claims"][0]
    assert got["claim"] == "Ukraine holds a Patriot production licence"
    assert got["status"] == "broken"
    assert got["broken_by"] == "Trump reversed the licence"


def test_claim_text_is_frozen_on_an_already_broken_claim():
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", claim="the original assertion"),
                status="broken",
                broke_on="2026-06-21",
            )
        ],
    }
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "a later rewrite"}], "2026-06-24"
    )
    assert out["claims"][0]["claim"] == "the original assertion"


def test_claim_text_is_frozen_when_marked_challenged():
    prior = {
        "version": 1,
        "claims": [_mk_claim("c-0001", claim="the original assertion")],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "a rewrite", "status": "challenged"}],
        "2026-06-24",
    )
    assert out["claims"][0]["claim"] == "the original assertion"


def test_a_standing_claim_can_still_be_refined():
    """Rewording is correct for a refinement and wrong only for a break."""
    prior = {
        "version": 1,
        "claims": [_mk_claim("c-0001", claim="the original assertion")],
    }
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "the refined assertion"}], "2026-06-24"
    )
    assert out["claims"][0]["claim"] == "the refined assertion"


def test_a_reworded_duplicate_cannot_rewrite_a_broken_claim():
    """The dedup path (news-brief-pon) folds through the same reaffirm, so it
    must not become a back door around immutability."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim(
                    "c-0001", claim="Ukraine was granted a Patriot production licence"
                ),
                status="broken",
                broke_on="2026-06-21",
            )
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"claim": "Ukraine has been granted the Patriot production licence"}],
        "2026-06-24",
    )
    assert len(out["claims"]) == 1
    assert (
        out["claims"][0]["claim"] == "Ukraine was granted a Patriot production licence"
    )


# ── Fix #14: extractor provenance on every row (news-brief-jx9.4) ─────────────
# Spec 12.2: the model is a configuration value and integration errors are
# permanent, so a row that cannot name its extractor cannot be re-audited when
# the extractor changes. Precedent: the Sonnet 4.6 -> 5 swap silently changed
# thinking behaviour and inflated tokens ~30%, truncating signals with no error.
def test_new_claim_records_extractor_model_and_prompt_version():
    out = bm.merge_ledger({"version": 1, "claims": []}, [{"claim": "x"}], "2026-06-24")
    got = out["claims"][0]
    assert got["extractor_model"] == bm.RECONCILE_MODEL
    assert got["prompt_version"] == bm.PROMPT_VERSION


def test_reaffirmed_claim_updates_provenance_to_the_current_extractor():
    """A reaffirmed row's content is only ever as recent as the extractor that
    last touched it, so provenance tracks the latest write, not the first."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001"),
                extractor_model="claude-sonnet-4-6",
                prompt_version=0,
            )
        ],
    }
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    got = out["claims"][0]
    assert got["extractor_model"] == bm.RECONCILE_MODEL
    assert got["prompt_version"] == bm.PROMPT_VERSION


def test_provenance_is_injectable_so_it_records_what_actually_ran():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x"}],
        "2026-06-24",
        extractor_model="some-other-model",
        prompt_version=99,
    )
    got = out["claims"][0]
    assert got["extractor_model"] == "some-other-model"
    assert got["prompt_version"] == 99


def test_a_broken_claim_updates_provenance_even_though_its_text_is_frozen():
    """The text freeze from news-brief-jx9.5 must not freeze provenance too: the
    status verdict on this row came from the current extractor."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", claim="the original assertion"),
                extractor_model="claude-sonnet-4-6",
                prompt_version=0,
            )
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "a rewrite", "status": "broken"}],
        "2026-06-24",
    )
    got = out["claims"][0]
    assert got["claim"] == "the original assertion"
    assert got["extractor_model"] == bm.RECONCILE_MODEL
    assert got["prompt_version"] == bm.PROMPT_VERSION


def test_an_untouched_prior_claim_keeps_its_own_provenance():
    """A claim the model never returned was not re-extracted, so nothing about
    its provenance changed - including a pre-versioning row that has none."""
    prior = {"version": 1, "claims": [_mk_claim("c-0001"), _mk_claim("c-0002")]}
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    untouched = next(c for c in out["claims"] if c["id"] == "c-0002")
    assert "extractor_model" not in untouched
    assert "prompt_version" not in untouched


# ── Fix #13: origin extracted vs authored (news-brief-jx9.3) ──────────────────
# Spec 4.1. QUARANTINED on the read side for the same reason as status: the rule
# says an authored row may never render back as established fact, but this is an
# unmeasured classifier in the very prompt where news-brief-47q proves the
# failure mode already happened (severity came back 'high' 25/25). A uniformly
# 'authored' result would silently empty the ESTABLISHED block and kill the
# anti-repetition feature. Enforce the filter once the gold set shows variance.
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("extracted", "extracted"),
        ("AUTHORED", "authored"),
        ("  authored  ", "authored"),
        ("inferred", None),
        ("", None),
        (None, None),
        (2, None),
    ],
)
def test_coerce_origin(raw, expected):
    assert bm._coerce_origin(raw) == expected


def test_new_claim_defaults_to_extracted():
    out = bm.merge_ledger({"version": 1, "claims": []}, [{"claim": "x"}], "2026-06-24")
    assert out["claims"][0]["origin"] == "extracted"


def test_new_claim_can_be_marked_authored():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [
            {
                "claim": "this represents a genuine escalation ladder",
                "origin": "authored",
            }
        ],
        "2026-06-24",
    )
    assert out["claims"][0]["origin"] == "authored"


def test_invalid_origin_defaults_to_extracted():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x", "origin": "vibes"}],
        "2026-06-24",
    )
    assert out["claims"][0]["origin"] == "extracted"


def test_reaffirm_keeps_prior_origin_when_the_model_omits_it():
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), origin="authored")]}
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    assert out["claims"][0]["origin"] == "authored"


def test_reaffirm_can_promote_an_authored_claim_to_extracted():
    """Sourcing can arrive later, and a claim that becomes source-grounded should
    stop being quarantined interpretation."""
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), origin="authored")]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "origin": "extracted"}],
        "2026-06-24",
    )
    assert out["claims"][0]["origin"] == "extracted"


def test_parse_extracts_origin():
    got = bm.parse_reconcile_response('[{"claim": "a", "origin": "authored"}]')
    assert got[0]["origin"] == "authored"


def test_parse_tolerates_bad_origin():
    assert (
        "origin" not in bm.parse_reconcile_response('[{"claim": "a", "origin": 2}]')[0]
    )


def test_reconcile_prompt_teaches_origin_with_a_default_and_a_negative_case():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert '"extracted"' in p
    assert '"authored"' in p
    assert "interpretation" in p


def test_render_output_is_unaffected_by_origin():
    """Quarantine: the 4.1 filter is not wired until the field shows variance."""
    extracted = {"version": 1, "claims": [_mk_claim("c-0001", claim="a fact")]}
    authored = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001", claim="a fact"), origin="authored")],
    }
    assert bm.render_established_block(authored) == bm.render_established_block(
        extracted
    )

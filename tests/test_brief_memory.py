import json
import logging

import pytest

import brief_memory as bm
import common


def test_is_enabled_reads_the_knob(monkeypatch):
    assert bm.is_enabled() is False
    monkeypatch.setattr(common, "BRIEF_MEMORY_ENABLED", True)
    assert bm.is_enabled() is True


def test_the_environment_no_longer_enables_the_ledger(monkeypatch):
    """The flag is a `settings` row now. A host that still exports the variable
    must not get a second, competing switch — that split is the bug class
    phase 2 exists to retire."""
    monkeypatch.setenv("BRIEF_MEMORY_ENABLED", "1")
    assert bm.is_enabled() is False


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
    assert "BACKGROUND ALREADY REPORTED" in block  # header rewritten by jx9.2
    assert "BOJ at 1.0% since 2026-06-16" in block
    assert "japan" in block
    assert "do not re-report" in block.lower()


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
    assert "use it by default" in p.lower()  # rubric recalibrated by 47q


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
def test_a_broken_claim_is_not_rendered_as_background():
    """INVERTED by news-brief-jx9.6, deliberately, not deleted: this asserted the
    render was unaffected by status while the field was quarantined. Presenting a
    broken claim under 'Previous briefs already reported these' states a fact the
    ledger knows to be false — the exact failure the epic exists to remove."""
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
    assert bm.render_established_block(broken) == ""


def test_a_challenged_claim_is_rendered_with_a_doubt_cue():
    """It is still live and still worth not re-explaining, but the model must not
    lean on it. Same parenthetical shape as the corroboration cue already there."""
    challenged = {
        "version": 1,
        "claims": [
            dict(_mk_claim("c-0001", claim="a durable fact"), status="challenged")
        ],
    }
    block = bm.render_established_block(challenged)
    assert "a durable fact" in block
    assert "in doubt" in block


def test_a_broken_claim_is_exempt_from_the_ttl():
    """INVERTED by news-brief-jx9.6, deliberately, not deleted: this test used to
    assert the opposite as the write-then-quarantine boundary. The quarantine
    lifted once five gold-set runs measured `broken` at ~100% precision. A broken
    claim is the accountability record — spec 3.3 says the original claim is what
    accountability is measured against — so it must outlive the TTL."""
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001", "2026-06-10"), status="broken")],
    }
    assert len(bm.merge_ledger(prior, [], "2026-06-24")["claims"]) == 1


def test_a_challenged_claim_is_exempt_from_the_ttl():
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001", "2026-06-10"), status="challenged")],
    }
    assert len(bm.merge_ledger(prior, [], "2026-06-24")["claims"]) == 1


def test_a_standing_claim_still_retires_on_ttl():
    """The exemption is for resolved and disputed rows only. Ordinary silence
    still ages a claim out, which is what keeps the ledger from growing without
    bound."""
    prior = {"version": 1, "claims": [_mk_claim("c-0001", "2026-06-10")]}
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


# ── Fix #9/#10: ESTABLISHED block rewrite + driver (news-brief-jx9.2) ─────────
def test_established_block_drops_the_heading_the_model_echoed():
    """The model adopted the old heading as vocabulary and shipped literal
    '(established)' tags meaning 'I know this and am not telling you'. Success
    criterion #4 is zero withheld-explanation markers."""
    ledger = {"version": 1, "claims": [_mk_claim("c-0001", claim="a fact")]}
    block = bm.render_established_block(ledger)
    assert "THE READER ALREADY KNOWS THESE" not in block
    assert "in place of an explanation" in block.lower()


def test_established_block_permits_restating_a_driver():
    """Section 1.2: all three memory channels were suppressive, while MARKET
    PULSE asks the model to explain moves. Yesterday's driver is by construction
    not today's news, so the block has to grant permission explicitly."""
    ledger = {"version": 1, "claims": [_mk_claim("c-0001", claim="a fact")]}
    block = bm.render_established_block(ledger).lower()
    assert "driver" in block
    assert "still operating" in block


def test_render_includes_the_driver_when_present():
    ledger = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", claim="Brent holds a risk premium"),
                driver="Hormuz transit risk",
            )
        ],
    }
    assert "Hormuz transit risk" in bm.render_established_block(ledger)


def test_render_omits_the_driver_marker_when_absent():
    ledger = {"version": 1, "claims": [_mk_claim("c-0001", claim="a plain fact")]}
    rows = [
        ln
        for ln in bm.render_established_block(ledger).splitlines()
        if ln.strip().startswith("•")
    ]
    assert len(rows) == 1
    assert "driver:" not in rows[0]


def test_merge_records_driver_on_a_new_claim():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "Brent holds a risk premium", "driver": "Hormuz transit risk"}],
        "2026-06-24",
    )
    assert out["claims"][0]["driver"] == "Hormuz transit risk"


def test_merge_keeps_a_prior_driver_when_the_model_omits_it():
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001"), driver="Hormuz transit risk")],
    }
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    assert out["claims"][0]["driver"] == "Hormuz transit risk"


def test_parse_extracts_driver():
    got = bm.parse_reconcile_response('[{"claim": "a", "driver": "the mechanism"}]')
    assert got[0]["driver"] == "the mechanism"


def test_reconcile_prompt_asks_for_a_driver():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "driver" in p


# ── news-brief-47q: severity rubric is degenerate (high on 25/25) ─────────────
def test_reconcile_prompt_gives_worked_examples_for_every_severity_level():
    """The old rubric said 'use normal by default' but gave examples only for
    high, so everything read as high. Section 12.2: a field needs per-value
    rules, a stated default, worked examples spanning its range, and an explicit
    negative case, or it comes back uniform - which is worse than missing."""
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    # The old rubric already had per-value rules; what it lacked was concrete
    # examples below the "high" tier, which is why everything read as high.
    assert "scheduled rate decision" in p  # a worked "normal" example
    assert "procedural step" in p  # a worked "low" example


def test_reconcile_prompt_warns_against_over_marking_high():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "miscalibrated" in p
    assert "continuation" in p


def test_reconcile_budget_keeps_headroom_for_a_full_working_set():
    """Every field added to the reply schema eats output budget. This repo has
    hit max_tokens truncation four times; a truncated reply fails safe (the prior
    ledger is kept) but silently loses a day of memory, so keep real headroom
    rather than a thin margin. Adding another field should trip this test."""
    item = {
        "id": "c-0001",
        "claim": "x" * 200,
        "topic": "geopolitics",
        "source_count": 4,
        "severity": "high",
        "status": "broken",
        "broken_by": "y" * 60,
        "origin": "extracted",
        "driver": "z" * 60,
        "kind": "claim",
        "horizon_days": 180,
    }
    worst = json.dumps([item] * bm.WORKING_SET_SIZE, indent=2)
    approx_tokens = len(worst) / 3.5  # conservative chars/token for JSON
    assert bm.RECONCILE_MAX_TOKENS >= approx_tokens * 1.5


# ── Fix #5: claim-admission guard (news-brief-93u) ────────────────────────────
# Spec 3.3: "Market levels are Observation rows; a claim may cite them but must
# not be one." The prompt has forbidden ephemeral price levels since the feature
# shipped and they were admitted anyway, so the rule needs enforcement at merge
# time. The boundary is not lexical — gs-14 ("Brent surged 1.2% on Sunday, first
# real repricing") and gs-16 ("Brent muted at -0.2%; market pricing contained
# war, repricing trigger remains an Iranian tanker hit") open identically and
# only one is admissible — so the model labels and merge_ledger enforces.
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claim", "claim"),
        ("observation", "observation"),
        ("OBSERVATION", "observation"),
        ("  claim  ", "claim"),
        ("measurement", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_coerce_kind(raw, expected):
    assert bm._coerce_kind(raw) == expected


def test_an_observation_is_not_admitted_to_the_ledger():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [
            {
                "claim": "Japan's 10-year yield held around 2.88% on Thursday",
                "kind": "observation",
            }
        ],
        "2026-06-24",
    )
    assert out["claims"] == []


def test_a_claim_is_admitted():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "BOJ raised its policy rate to 1.0%", "kind": "claim"}],
        "2026-06-24",
    )
    assert [c["claim"] for c in out["claims"]] == ["BOJ raised its policy rate to 1.0%"]


def test_a_missing_kind_is_admitted():
    """Fails OPEN, deliberately. Defaulting a missing field to rejection means a
    model that quietly stops emitting `kind` silently empties the ledger with no
    error raised — the exact silent-shape-change this repo has been bitten by.
    Absence is made loud instead: `kind` is a variance field in the gold scorer,
    so a field coming back absent shows up in the report the run it happens."""
    out = bm.merge_ledger({"version": 1, "claims": []}, [{"claim": "x"}], "2026-06-24")
    assert len(out["claims"]) == 1


def test_an_unrecognised_kind_is_admitted():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x", "kind": "vibes"}],
        "2026-06-24",
    )
    assert len(out["claims"]) == 1


def test_an_admitted_row_does_not_store_kind():
    """Admission is a decision made about a row, not a property of it. Every row
    that survives the guard is a claim by construction, so storing the field
    would add a column that is uniform on every read — which 12.2 rates worse
    than a missing one, because it looks populated. Variance is read off the
    model's replies in the scorer, where it carries information."""
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "x", "kind": "claim"}],
        "2026-06-24",
    )
    assert "kind" not in out["claims"][0]


def test_a_rejected_observation_does_not_consume_an_id():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [
            {"claim": "Brent -0.6% on the day", "kind": "observation"},
            {"claim": "the MOU was signed on June 14", "kind": "claim"},
        ],
        "2026-06-24",
    )
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_an_echoed_id_is_exempt_from_the_admission_guard():
    """The guard runs at ADMISSION. An echoed id is reaffirmation of a row that
    is already in the ledger, and jx9.5 makes stored claims immutable, so a
    late 'observation' label must not retro-evict an existing claim."""
    prior = {"version": 1, "claims": [_mk_claim("c-0001", claim="a standing fact")]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "a standing fact", "kind": "observation"}],
        "2026-06-24",
    )
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_a_reworded_duplicate_is_exempt_from_the_admission_guard():
    """The dedup path (news-brief-pon) resolves to an existing row, so it is
    reaffirmation too — the same back door immutability had to close."""
    prior = {"version": 1, "claims": [_mk_claim("c-0001", claim="a standing fact")]}
    out = bm.merge_ledger(
        prior,
        [{"claim": "a standing fact", "kind": "observation"}],
        "2026-06-24",
    )
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


def test_a_rejected_observation_is_logged_with_its_claim_text(caplog):
    """A silent drop is unattributable. Log the gate that fired and the text it
    fired on, so the operator can tell a working guard from an over-firing one."""
    with caplog.at_level(logging.INFO):
        bm.merge_ledger(
            {"version": 1, "claims": []},
            [{"claim": "RGLD -0.6% while bullion was flat", "kind": "observation"}],
            "2026-06-24",
        )
    assert "observation" in caplog.text
    assert "RGLD -0.6% while bullion was flat" in caplog.text


def test_parse_extracts_kind():
    got = bm.parse_reconcile_response('[{"claim": "a", "kind": "observation"}]')
    assert got[0]["kind"] == "observation"


def test_parse_tolerates_bad_kind():
    assert "kind" not in bm.parse_reconcile_response('[{"claim": "a", "kind": 7}]')[0]


def test_reconcile_prompt_teaches_kind_with_a_default_and_a_negative_case():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert '"observation"' in p
    assert 'use "claim"' in p  # the stated default, wrapped across lines
    assert "2.88%" in p  # the spec's named admission failure, as a worked case


def test_reconcile_prompt_shows_both_sides_of_the_price_boundary():
    """One worked example teaches the easy half. The measured failure is the
    boundary itself, so the rubric has to carry a price-anchored row that IS
    admissible next to one that is not."""
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "surged 1.2%" in p  # gs-14: a day's move with a label on it
    assert "contained war" in p  # gs-16: a thesis that names its own falsifier


# ── jx9.9: an unmarked rewrite is a contradiction, not a refinement ───────────
# jx9.5 froze claim text once status != standing, but the freeze is conditioned
# on a field the MODEL controls. Three v4 gold-set runs showed it keeping
# status=standing and editing the claim to match the new facts instead: every
# true break scored "standing" had been rewritten (6/6 run A, 3/3 runs B and C).
# The ledger then self-corrects with no trace it was ever wrong, which destroys
# the accountability record Epic 1 exists to produce (spec 3.3).
@pytest.mark.parametrize(
    "stored,rewritten,expected",
    [
        # gs-09: the measured case — an asserted number disappears.
        ("Colombia holds 6 pts, Portugal 4 pts", "both teams hold 4 pts", {"6"}),
        # Refinement ADDS detail; nothing asserted is withdrawn.
        (
            "BOJ raised the rate to 1.0%",
            "BOJ raised the rate to 1.0% on June 16",
            set(),
        ),
        # Pure rewording carries no numeric assertion either way.
        ("Iran suspended the talks", "Tehran has suspended negotiations", set()),
        # Identical text is not a rewrite at all.
        ("Colombia holds 6 pts", "Colombia holds 6 pts", set()),
    ],
)
def test_dropped_numbers(stored, rewritten, expected):
    assert set(bm._dropped_numbers(stored, rewritten)) == expected


def _standing(cid="c-0001", claim="Colombia holds 6 pts, Portugal 4 pts"):
    return {"version": 1, "claims": [_mk_claim(cid, claim=claim)]}


def test_a_rewrite_that_drops_an_asserted_number_does_not_replace_the_claim():
    out = bm.merge_ledger(
        _standing(), [{"id": "c-0001", "claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    assert out["claims"][0]["claim"] == "Colombia holds 6 pts, Portugal 4 pts"


def test_a_rewrite_that_drops_an_asserted_number_marks_the_claim_challenged():
    """challenged, not broken: a dropped number can also be innocent
    compression, and challenged is still read by nothing, so a false fire costs
    nothing today. That is what makes this the cheap moment to enforce it."""
    out = bm.merge_ledger(
        _standing(), [{"id": "c-0001", "claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    assert out["claims"][0]["status"] == "challenged"


def test_the_attempted_rewrite_is_recorded_as_the_evidence():
    """The contradiction has to leave a record, or the guard just suppresses an
    edit and the ledger still cannot say what it got wrong."""
    out = bm.merge_ledger(
        _standing(), [{"id": "c-0001", "claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    assert "both teams hold 4 pts" in out["claims"][0]["broken_by"]


def test_the_guard_stamps_broke_on():
    out = bm.merge_ledger(
        _standing(), [{"id": "c-0001", "claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    assert out["claims"][0]["broke_on"] == "2026-06-27"


def test_a_refinement_that_only_adds_a_number_still_rewords():
    """The whole point of the 'dropped, not changed' rule: adding a date or a
    detail is exactly the refinement the prompt is right to permit."""
    out = bm.merge_ledger(
        {
            "version": 1,
            "claims": [_mk_claim("c-0001", claim="BOJ raised the rate to 1.0%")],
        },
        [{"id": "c-0001", "claim": "BOJ raised the rate to 1.0% on June 16"}],
        "2026-06-27",
    )
    got = out["claims"][0]
    assert got["claim"] == "BOJ raised the rate to 1.0% on June 16"
    assert got["status"] == "standing"


def test_a_reword_carrying_no_numbers_still_rewords():
    out = bm.merge_ledger(
        {
            "version": 1,
            "claims": [_mk_claim("c-0001", claim="Iran suspended the talks")],
        },
        [{"id": "c-0001", "claim": "Tehran has suspended negotiations"}],
        "2026-06-27",
    )
    assert out["claims"][0]["claim"] == "Tehran has suspended negotiations"


def test_an_explicitly_marked_break_is_still_broken_not_downgraded():
    """The guard must not soften a verdict the model was willing to state."""
    out = bm.merge_ledger(
        _standing(),
        [
            {
                "id": "c-0001",
                "claim": "both teams hold 4 pts",
                "status": "broken",
                "broken_by": "both on 4 pts",
            }
        ],
        "2026-06-27",
    )
    got = out["claims"][0]
    assert got["status"] == "broken"
    assert got["claim"] == "Colombia holds 6 pts, Portugal 4 pts"


def test_the_guard_does_not_reopen_an_already_broken_row():
    """A non-standing row is frozen by jx9.5 before this guard is reached."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", claim="Colombia holds 6 pts"),
                status="broken",
                broke_on="2026-06-25",
            )
        ],
    }
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    got = out["claims"][0]
    assert got["status"] == "broken"
    assert got["broke_on"] == "2026-06-25"


def test_the_guard_logs_the_rewrite_it_refused(caplog):
    with caplog.at_level(logging.INFO):
        bm.merge_ledger(
            _standing(),
            [{"id": "c-0001", "claim": "both teams hold 4 pts"}],
            "2026-06-27",
        )
    assert "c-0001" in caplog.text
    assert "6" in caplog.text


def test_a_number_dropping_rewrite_with_no_id_becomes_a_new_row():
    """_is_duplicate_claim already blocks a merge on any numeric difference, so
    this case cannot reach the guard — and must not, because minting a separate
    row leaves the original assertion intact, which is the outcome that matters."""
    out = bm.merge_ledger(
        _standing(), [{"claim": "both teams hold 4 pts"}], "2026-06-27"
    )
    claims = {c["claim"] for c in out["claims"]}
    assert "Colombia holds 6 pts, Portugal 4 pts" in claims
    assert "both teams hold 4 pts" in claims


# ── Fix #11/#12: retention exemption + horizon (news-brief-jx9.6) ─────────────
# The write-then-quarantine on `status` LIFTS here. Five gold-set runs measured
# `broken` at ~100% precision (the one false positive sat on an inadmissible row
# the 93u guard now rejects upstream), and jx9.9 closed the silent-absorption
# path, so a contradiction now leaves a record instead of vanishing. The two
# statuses get opposite treatment on purpose: `broken` is RESOLVED and belongs in
# storage for measurement, `challenged` is LIVE and has to stay in the model's
# view or it can never resolve — the crowding-out mechanism the replay showed
# losing 54 of 68 claims. `origin` stays quarantined: still unmeasured.
def test_a_broken_claim_leaves_the_working_set():
    ledger = {
        "version": 1,
        "claims": [
            dict(_mk_claim("c-0001"), status="broken"),
            _mk_claim("c-0002"),
        ],
    }
    assert [c["id"] for c in bm.select_working_set(ledger)] == ["c-0002"]


def test_a_challenged_claim_is_never_the_one_evicted():
    """Cap exemption, implemented as priority rather than as extra slots: a
    challenged row outranks standing rows so it is never what gets crowded out."""
    claims = [_mk_claim(f"c-{i:04d}", sev="high") for i in range(1, 31)]
    claims.append(dict(_mk_claim("c-0099", sev="low"), status="challenged"))
    got = bm.select_working_set({"version": 1, "claims": claims})
    assert "c-0099" in [c["id"] for c in got]


def test_the_working_set_still_respects_the_prompt_budget():
    """The cap is a PROMPT BUDGET, not a storage limit. Exempting challenged rows
    by growing the window would risk the truncation this repo has hit four times,
    so the exemption must never make the window bigger."""
    claims = [dict(_mk_claim(f"c-{i:04d}"), status="challenged") for i in range(1, 41)]
    assert len(bm.select_working_set({"version": 1, "claims": claims})) == (
        bm.WORKING_SET_SIZE
    )


# horizon_days / resolution_date ship WRITE-THEN-QUARANTINE, and that is the
# consistent rule this epic settled on: quarantine is the default for an
# UNMEASURED field, and measurement is what lifts it. `status` earned its lift
# today across five runs; these have earned nothing yet, and 12.2 predicts what
# happens to an unmeasured field — severity came back `high` 25/25.
@pytest.mark.parametrize(
    "raw,expected",
    [
        (30, 30),
        ("90", 90),
        (1, 1),
        (0, None),  # a zero-day horizon is not a horizon
        (-5, None),
        (99999, None),  # beyond any brief's reach; almost certainly a mis-parse
        ("soon", None),
        (None, None),
        (True, None),  # bool is an int subclass; reject it explicitly
    ],
)
def test_coerce_horizon_days(raw, expected):
    assert bm._coerce_horizon_days(raw) == expected


def test_resolution_date_is_first_seen_plus_the_horizon():
    out = bm.merge_ledger(
        {"version": 1, "claims": []},
        [{"claim": "a thesis", "horizon_days": 30}],
        "2026-06-01",
    )
    got = out["claims"][0]
    assert got["horizon_days"] == 30
    assert got["resolution_date"] == "2026-07-01"


def test_a_claim_without_a_horizon_carries_no_resolution_date():
    """No code-side default. A numeric default would make the field degenerate by
    construction, and absence is the honest reading — the same call made for
    prompt_version on pre-versioning rows."""
    out = bm.merge_ledger(
        {"version": 1, "claims": []}, [{"claim": "a fact"}], "2026-06-01"
    )
    assert "horizon_days" not in out["claims"][0]
    assert "resolution_date" not in out["claims"][0]


def test_horizon_elapsed_is_recorded_when_the_claim_resolves():
    """'Broken at 2 days against a 180-day horizon' is a calibration datapoint;
    'broken' alone is not (spec 3.3)."""
    prior = {
        "version": 1,
        "claims": [
            dict(_mk_claim("c-0001", "2026-06-01"), horizon_days=180),
        ],
    }
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "broken", "broken_by": "x"}],
        "2026-06-03",
    )
    assert out["claims"][0]["horizon_elapsed"] == 2


def test_horizon_elapsed_is_not_rewritten_by_a_later_reaffirmation():
    """It records when the ledger learned the claim failed, exactly as broke_on
    does, so a later touch must not move it."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", "2026-06-01"),
                status="broken",
                broke_on="2026-06-03",
                horizon_elapsed=2,
            )
        ],
    }
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-20")
    assert out["claims"][0]["horizon_elapsed"] == 2


def test_resolution_date_does_not_affect_retention():
    """The quarantine: resolution_date is written and read by NOTHING. A standing
    claim long past its horizon still lives or dies purely on the TTL."""
    prior = {
        "version": 1,
        "claims": [
            dict(
                _mk_claim("c-0001", "2026-06-23"),
                horizon_days=1,
                resolution_date="2026-06-24",
            )
        ],
    }
    assert len(bm.merge_ledger(prior, [], "2026-06-25")["claims"]) == 1


def test_parse_extracts_horizon_days():
    got = bm.parse_reconcile_response('[{"claim": "a", "horizon_days": 90}]')
    assert got[0]["horizon_days"] == 90


def test_parse_tolerates_a_bad_horizon():
    row = bm.parse_reconcile_response('[{"claim": "a", "horizon_days": "soon"}]')[0]
    assert "horizon_days" not in row


def test_reconcile_prompt_teaches_horizon_days_with_worked_examples():
    p = bm.build_reconcile_prompt({"version": 1, "claims": []}, "brief").lower()
    assert "horizon_days" in p
    assert "180" in p  # a worked long-horizon example


def test_a_reaffirmation_can_set_a_horizon_the_row_never_had():
    """Otherwise the 25 rows already in the live ledger could never acquire one."""
    prior = {"version": 1, "claims": [_mk_claim("c-0001")]}
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "c-0001", "horizon_days": 60}], "2026-06-24"
    )
    assert out["claims"][0]["horizon_days"] == 60


def test_a_reaffirmation_does_not_move_an_existing_horizon():
    """The horizon is a commitment made when the claim was made; letting it drift
    each day would make 'broken at 2 days against 180' unfalsifiable."""
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001"), horizon_days=180)],
    }
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "c-0001", "horizon_days": 7}], "2026-06-24"
    )
    assert out["claims"][0]["horizon_days"] == 180


# ── bqa.9 item 1: the six-value lifecycle (spec section 6, migration 0006) ────
# `claims.status` permits six values; _VALID_STATUS knew three. Everything
# outside the three coerced to None, every caller fell back to 'standing', and
# the two predicates written as `!= standing` then did the damage: the TTL
# filter deleted a confirmed/expired/withdrawn row after 7 days and
# select_working_set rendered it as live fact.
#
# The fix is two sets, not one, because TTL and render do NOT partition the same
# way. A challenged claim is LIVE — it must outlive the TTL (a challenge that
# ages out can never resolve, the jx9.6 failure) but it must stay in the render
# window. Collapsing both sites onto a single "terminal" set would regress it.

_SCHEMA_STATUSES = frozenset(
    {"standing", "challenged", "broken", "confirmed", "expired", "withdrawn"}
)


def test_valid_status_covers_every_state_the_kb_schema_permits():
    """The one hardcoded copy of the enum in this suite. tests/test_kb_schema.py
    proves this same set equals the live CHECK on claims.status, so the two
    layers cannot drift apart in silence — which two hardcoded lists would."""
    assert bm._VALID_STATUS == _SCHEMA_STATUSES


def test_the_status_sets_are_the_schemas_own_partitions():
    """_TERMINAL_STATUS is exactly the tuple in claims_freeze_claim_text(), and
    its complement is exactly the `WHERE status IN ('standing','challenged')`
    partial index. Neither set is invented here."""
    assert bm._TERMINAL_STATUS == {"broken", "confirmed", "expired", "withdrawn"}
    assert bm._VALID_STATUS - bm._TERMINAL_STATUS == {"standing", "challenged"}
    # TTL exemption is the wider set: every resolved row PLUS the live-but-
    # disputed one. Only ordinary silence on a standing claim ages it out.
    assert bm._TTL_EXEMPT_STATUS == bm._VALID_STATUS - {bm._DEFAULT_STATUS}


@pytest.mark.parametrize(
    "status", sorted({"broken", "confirmed", "expired", "withdrawn"})
)
def test_every_terminal_status_is_exempt_from_the_ttl(status):
    """Spec section 6 item 1, the deletion half. A resolved claim IS the
    accountability record this epic exists to build; ageing one out destroys the
    thing being measured."""
    prior = {
        "version": 1,
        "claims": [dict(_mk_claim("c-0001", "2026-06-10"), status=status)],
    }
    out = bm.merge_ledger(prior, [], "2026-06-24")  # 14 days > the 7-day TTL
    assert [c["id"] for c in out["claims"]] == ["c-0001"]


@pytest.mark.parametrize(
    "status", sorted({"broken", "confirmed", "expired", "withdrawn"})
)
def test_every_terminal_status_leaves_the_working_set(status):
    """Spec section 6 item 1, the render half. Rendering a settled claim under
    "previous briefs already reported these" states as live a fact the ledger
    knows to be resolved. Only `broken` was excluded before."""
    ledger = {
        "version": 1,
        "claims": [
            dict(_mk_claim("c-0001"), status=status),
            _mk_claim("c-0002"),
        ],
    }
    assert [c["id"] for c in bm.select_working_set(ledger)] == ["c-0002"]


def test_a_confirmed_claim_cannot_be_reworded():
    """The jx9.5 text freeze reads `was == now == standing`, so it was already
    written for six values — but a stored 'confirmed' coerced to None, fell back
    to 'standing', and walked straight through it. Widening the set closes a
    second Patriot-shaped hole that item 1 does not name."""
    prior = {
        "version": 1,
        "claims": [
            dict(_mk_claim("c-0001", claim="the original wording"), status="confirmed")
        ],
    }
    out = bm.merge_ledger(
        prior, [{"id": "c-0001", "claim": "a rewrite of its own outcome"}], "2026-06-24"
    )
    assert out["claims"][0]["claim"] == "the original wording"
    assert out["claims"][0]["status"] == "confirmed"


@pytest.mark.parametrize(
    "status", sorted({"broken", "confirmed", "expired", "withdrawn"})
)
def test_a_terminal_status_survives_a_reply_that_omits_status(status):
    """_apply_status computes `prior` by coercing the row's OWN stored status.
    With a three-value set that coercion returned None for three of the six and
    reset the row to 'standing' before the model's value was even considered —
    an interlock the reader can defeat by omission is not an interlock."""
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), status=status)]}
    out = bm.merge_ledger(prior, [{"id": "c-0001", "claim": "c-0001"}], "2026-06-24")
    assert out["claims"][0]["status"] == status


@pytest.mark.parametrize(
    "status", sorted({"broken", "confirmed", "expired", "withdrawn"})
)
def test_a_terminal_claim_refuses_a_proposed_transition(status):
    """Mirrors claims_freeze_claim_text()'s first RAISE in Python, where the
    migration says the PRIMARY enforcement belongs. Without this the write path
    hands Postgres a row it will reject, turning a logged refusal into a crash."""
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), status=status)]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "standing"}],
        "2026-06-24",
    )
    assert out["claims"][0]["status"] == status


def test_a_challenged_claim_can_still_return_to_standing():
    """Spec 2.3: a challenge can be answered. 'challenged' is deliberately
    excluded from the terminal set, so standing -> challenged -> standing must
    keep working — the refusal above must not swallow it."""
    prior = {"version": 1, "claims": [dict(_mk_claim("c-0001"), status="challenged")]}
    out = bm.merge_ledger(
        prior,
        [{"id": "c-0001", "claim": "c-0001", "status": "standing"}],
        "2026-06-24",
    )
    assert out["claims"][0]["status"] == "standing"


def test_merge_accepts_an_externally_allocated_next_num():
    """After the cutover, ids are allocated against the whole table including
    retired rows, so the caller supplies the number."""
    prior = {"version": 1, "claims": []}
    out = bm.merge_ledger(prior, [{"claim": "a new fact"}], "2026-06-24", next_num=51)
    assert out["claims"][0]["id"] == "c-0051"


def test_merge_still_allocates_from_prior_when_not_told():
    prior = {"version": 1, "claims": [_mk_claim("c-0003")]}
    out = bm.merge_ledger(prior, [{"claim": "a new fact"}], "2026-06-24")
    assert {c["id"] for c in out["claims"]} == {"c-0003", "c-0004"}


def test_reconcile_passes_next_num_through_to_merge():
    """Production does not call merge_ledger -- brief.py calls reconcile_ledger,
    which called merge_ledger with no kwargs. Adding the parameter to
    merge_ledger alone would leave it UNREACHABLE from the only call site that
    matters, and the collision would happen with every test passing."""
    out = bm.reconcile_ledger(
        {"version": 1, "claims": []},
        "a brief",
        "2026-06-24",
        call=lambda system, prompt: '[{"claim": "a new fact"}]',
        next_num=77,
    )
    assert out["claims"][0]["id"] == "c-0077"

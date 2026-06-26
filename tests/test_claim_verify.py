import claim_verify as cv


def test_build_source_evidence_keeps_summaries_and_labels():
    feed = (
        "### Reuters [wire] (WORLD)\n"
        "- Central bank holds rates (Mon)\n"
        "  The bank kept its policy rate unchanged at 4.5 percent.\n"
        "### AlJazeera [regional · arab · state-funded] (WORLD)\n"
        "- Talks resume in Geneva (Mon)\n"
        "  Negotiators returned to the table after a week-long pause.\n"
    )
    web = "### SomePage [regional] (ANALYSIS)\nA short meta description of the page.\n"
    out = cv.build_source_evidence(feed, web)
    assert "SOURCE: Reuters" in out
    assert "SOURCE: AlJazeera" in out
    assert "SOURCE: SomePage" in out
    # headline retained
    assert "- Central bank holds rates (Mon)" in out
    # summary retained (this is what build_source_index drops)
    assert "kept its policy rate unchanged at 4.5 percent" in out
    # web body retained
    assert "short meta description" in out


def test_build_source_evidence_caps_long_detail_lines():
    feed = "### X [wire] (WORLD)\n- A title (Mon)\n  " + ("z" * 900) + "\n"
    out = cv.build_source_evidence(feed, "")
    # each detail line capped at 400 chars
    detail = [ln for ln in out.splitlines() if ln.startswith("  ")][0]
    assert len(detail.strip()) == 400


def test_build_source_evidence_handles_empty_placeholders():
    out = cv.build_source_evidence("(no RSS content)", "(no web content)")
    assert "SOURCE:" not in out


def test_is_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("CLAIM_VERIFY_ENABLED", raising=False)
    assert cv.is_enabled() is False
    monkeypatch.setenv("CLAIM_VERIFY_ENABLED", "1")
    assert cv.is_enabled() is True


def test_save_and_load_evidence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: Reuters\n- A title (Mon)\n  detail", "2026-06-26")
    assert "SOURCE: Reuters" in cv.load_evidence("2026-06-26")


def test_load_evidence_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    assert cv.load_evidence("2026-06-26") == ""


BRIEF = (
    "<b>🌍 TOP STORIES</b>\n"
    "- Central bank held rates at 4.5%.\n"
    "- Geneva talks resumed after a pause.\n"
    "<b>📈 MARKET PULSE — WHAT MOVED</b>\n"
    "- Oil up 2% on the news.\n"
    "<b>👁 WATCH / FORWARD</b>\n"
    "- Fed minutes due Thursday.\n"
)


def test_extract_top_stories_returns_section_only():
    out = cv.extract_top_stories(BRIEF)
    assert "Central bank held rates" in out
    assert "Geneva talks resumed" in out
    # stops at the next heading
    assert "Oil up 2%" not in out
    assert "MARKET PULSE" not in out
    # keeps its own heading
    assert "TOP STORIES" in out


def test_extract_top_stories_absent_returns_empty():
    assert cv.extract_top_stories("<b>📈 MARKET PULSE</b>\n- x\n") == ""


def test_extract_top_stories_runs_to_end_when_last_section():
    brief = "<b>🌍 TOP STORIES</b>\n- Only section here.\n"
    assert "Only section here" in cv.extract_top_stories(brief)


def test_extract_top_stories_ignores_inline_bold_in_bullets():
    brief = (
        "<b>🌍 TOP STORIES</b>\n"
        "- A bullet with <b>inline</b> emphasis stays in.\n"
        "<b>📈 MARKET PULSE</b>\n- out\n"
    )
    out = cv.extract_top_stories(brief)
    assert "inline" in out
    assert "out" not in out


def _tool_resp(claims):
    return {
        "content": [
            {
                "type": "tool_use",
                "name": "emit_claim_checks",
                "input": {"claims": claims},
            }
        ]
    }


def test_build_verify_request_forces_the_tool():
    req = cv.build_verify_request("<b>🌍 TOP STORIES</b>\n- x", "SOURCE: R\n- x")
    assert req["model"] == cv.VERIFY_MODEL
    assert req["tool_choice"] == {"type": "tool", "name": "emit_claim_checks"}
    assert req["tools"][0]["name"] == "emit_claim_checks"
    body = req["messages"][0]["content"]
    assert "TOP STORIES" in body and "SOURCE: R" in body


def test_parse_verify_response_keeps_valid_claims():
    resp = _tool_resp(
        [
            {
                "claim": "Rates held at 4.5%",
                "verdict": "supported",
                "evidence": "- Central bank holds rates",
                "reason": "matches headline",
            },
            {
                "claim": "War declared",
                "verdict": "unsupported",
                "evidence": "",
                "reason": "no source",
            },
        ]
    )
    out = cv.parse_verify_response(resp)
    assert len(out) == 2
    assert out[0]["verdict"] == "supported"
    assert out[1]["verdict"] == "unsupported"
    assert out[0]["evidence"] == "- Central bank holds rates"


def test_parse_verify_response_drops_invalid_verdict_and_empty_claim():
    resp = _tool_resp(
        [
            {"claim": "ok claim", "verdict": "made-up"},  # bad verdict -> drop
            {"claim": "", "verdict": "supported"},  # empty claim -> drop
            {"verdict": "supported"},  # no claim -> drop
            {"claim": "good", "verdict": "CONTRADICTED"},  # case-insensitive -> keep
        ]
    )
    out = cv.parse_verify_response(resp)
    assert len(out) == 1
    assert out[0] == {"claim": "good", "verdict": "contradicted"}


def test_parse_verify_response_no_tool_block_raises():
    import pytest

    with pytest.raises(ValueError):
        cv.parse_verify_response({"content": [{"type": "text", "text": "hi"}]})


def test_verification_record_counts_verdicts():
    claims = [
        {"claim": "a", "verdict": "supported"},
        {"claim": "b", "verdict": "unsupported"},
        {"claim": "c", "verdict": "contradicted"},
    ]
    rec = cv._verification_record("2026-06-26", True, claims)
    assert rec["date"] == "2026-06-26"
    assert rec["model"] == cv.VERIFY_MODEL
    assert rec["top_stories_present"] is True
    assert rec["n_claims"] == 3
    assert rec["counts_by_verdict"]["supported"] == 1
    assert rec["counts_by_verdict"]["unsupported"] == 1
    assert rec["counts_by_verdict"]["contradicted"] == 1
    assert rec["counts_by_verdict"]["overstated"] == 0


def test_verify_claims_absent_top_stories_records_empty():
    rec = cv.verify_claims(
        "<b>📈 MARKET PULSE</b>\n- x",
        "SOURCE: R",
        "2026-06-26",
        call=lambda payload: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    assert rec["top_stories_present"] is False
    assert rec["n_claims"] == 0


def test_verify_claims_happy_path_uses_injected_call():
    def fake_call(payload):
        return _tool_resp([{"claim": "Rates held", "verdict": "supported"}])

    rec = cv.verify_claims(
        "<b>🌍 TOP STORIES</b>\n- Rates held.",
        "SOURCE: R\n- Rates held",
        "2026-06-26",
        call=fake_call,
    )
    assert rec["top_stories_present"] is True
    assert rec["n_claims"] == 1
    assert rec["claims"][0]["claim"] == "Rates held"


def test_verify_claims_returns_none_on_call_failure():
    def boom(payload):
        raise RuntimeError("api down")

    rec = cv.verify_claims(
        "<b>🌍 TOP STORIES</b>\n- x", "SOURCE: R", "2026-06-26", call=boom
    )
    assert rec is None


def test_run_verification_writes_record_when_evidence_present(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: R\n- Rates held", "2026-06-26")

    def fake_call(payload):
        return _tool_resp([{"claim": "Rates held", "verdict": "supported"}])

    cv.run_verification(
        "<b>🌍 TOP STORIES</b>\n- Rates held.", "2026-06-26", call=fake_call
    )
    rec = json.loads((tmp_path / "verification-2026-06-26.json").read_text())
    assert rec["n_claims"] == 1


def test_run_verification_skips_when_no_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    # no evidence file written
    cv.run_verification(
        "<b>🌍 TOP STORIES</b>\n- x",
        "2026-06-26",
        call=lambda p: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    assert not (tmp_path / "verification-2026-06-26.json").exists()


def test_run_verification_never_raises_on_bad_call(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "DATA_DIR", tmp_path)
    cv.save_evidence("SOURCE: R", "2026-06-26")
    # must not raise, and must not write a record
    cv.run_verification(
        "<b>🌍 TOP STORIES</b>\n- x",
        "2026-06-26",
        call=lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert not (tmp_path / "verification-2026-06-26.json").exists()

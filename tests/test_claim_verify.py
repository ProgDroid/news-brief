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

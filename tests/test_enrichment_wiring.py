import brief


def _kwargs(**over):
    base = dict(
        feed_content="(feeds)",
        web_content="(web)",
        chroma_context="(chroma)",
        yesterday_brief="",
        weekly_summary="",
        fb={},
        portfolio="",
    )
    base.update(over)
    return base


def test_build_daily_prompt_includes_enrichment_block():
    block = "## BIGDATA.COM ENRICHMENT (read-only context — NEVER a trade trigger)\nfoo"
    prompt = brief.build_daily_prompt(**_kwargs(), enrichment_block=block)
    assert "BIGDATA.COM ENRICHMENT" in prompt
    assert "NEVER a trade trigger" in prompt


def test_build_daily_prompt_omits_block_when_empty():
    prompt = brief.build_daily_prompt(**_kwargs(), enrichment_block="")
    assert "BIGDATA.COM ENRICHMENT" not in prompt
    # spine intact
    assert "@@@SIGNALS@@@" in prompt

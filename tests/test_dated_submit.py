"""The dated-memory knob (storylines phase 1, news-brief-al7.1, spec section 3).

BRIEF_DATED_MEMORY_ENABLED puts a 7-day window of dated headlines, and dated
claims, into the prompt. Off, the prompt is today's, byte for byte: proven here
against GOLDEN_PROMPT, text captured from the code at HEAD before the edit with
the clock frozen (never by comparing the edited function with itself).
"""

from datetime import datetime, timezone

import pytest

import brief
import common
import dated_memory

# Captured from build_daily_prompt at 5345027, before the edit, clock frozen at
# 2026-11-22 04:30 UTC. It includes the WEEKLY CONTEXT block.
GOLDEN_PROMPT = "".join(
    [
        "Today is Sunday, 22 November 2026 (UTC). Produce the morning brief.\n",
        "\n",
        "## READER OVERRIDES\n",
        "PINNED — always include at least a one-line pulse for, in this order: ukraine, iran, korea, japan, china\n",
        "\n",
        "## SOURCE MATERIAL\n",
        "Each source is tagged [WIRE|ANALYST|REGIONAL|PRIMARY]. Treat WIRE as the record of\n",
        "what happened (anchor facts here), but lead your interpretation with ANALYST /\n",
        "REGIONAL / PRIMARY material and the market action below. Compress recap; do not\n",
        "echo a headline the market has already priced.\n",
        "feeds\n",
        "web\n",
        "\n",
        "## PODCAST ANALYST CONTEXT\n",
        "Relevant excerpts from an indexed archive of geopolitical/macro podcast episodes.\n",
        "Use for forward-looking analytical framing. Cite the show name if drawing on a specific insight.\n",
        "chroma\n",
        "\n",
        "## WEB SEARCH TOPICS\n",
        "Search for current developments on each before writing. Anchor facts on Reuters; for\n",
        "local colour and forward framing, prefer the region-native sources above.\n",
        "- Ukraine war latest news\n",
        "- US Iran negotiations Hormuz\n",
        "- North Korea South Korea news\n",
        "- Japan geopolitics economy BOJ\n",
        "- China Taiwan economy trade\n",
        "\n",
        "## YESTERDAY'S BRIEF\n",
        'For any section where the situation is materially unchanged from yesterday, replace the paragraph with a single sentence: "No significant change — [one-line summary]." This applies to standing analytical frames too — named theses, recurring podcast framings, and one-time events already reported: state them in at most one clause and never re-explain them. Only write a full paragraph when something new or materially different has occurred.\n',
        "\n",
        "YESTERDAY\n",
        "\n",
        "## WEEKLY CONTEXT (for trend awareness)\n",
        "Use this to identify multi-day patterns, unresolved watch list items, and slow-moving trends. Do not repeat it — use it as background framing only.\n",
        "\n",
        "WEEKLY-SUMMARY\n",
        "\n",
        "\n",
        "\n",
        "## OUTPUT FORMAT\n",
        "\n",
        'Telegram HTML only. Allowed tags: <b>, <i>, <code>, <a href="...">\n',
        "Use <b> for section headings. Bullets with •. No markdown. No # headers. No asterisks.\n",
        "Output only the HTML — no preamble, no sign-off, no code fences.\n",
        "\n",
        "Structure — a fixed spine with a dynamic middle:\n",
        "\n",
        "<b>🌍 TOP STORIES</b>\n",
        '- [3–5 bullets, only genuinely significant developments. For each, answer the "so what"\n',
        "  in tight prose: the non-obvious context the reader lacks, where it is heading and on\n",
        "  what timeframe, and the market channel it moves through. Only where warranted — never\n",
        "  a checklist, never padding.]\n",
        "\n",
        "<b>📈 MARKET PULSE — WHAT MOVED</b>\n",
        "[2–4 bullets: the notable moves above and the likely why. Flag any move NOT explained\n",
        "by today's news as a potential early signal. Omit only if no market data was provided.]\n",
        "\n",
        "[DYNAMIC TOPIC SECTIONS — your discretion:\n",
        "• Render a section for EVERY pinned topic listed under READER OVERRIDES, even if quiet\n",
        '  (a quiet pin collapses to: "No significant change — one sentence"). Never drop a pin.\n',
        "• ALSO add a section for any UNPINNED topic that is materially significant today.\n",
        "• Order by significance. Use a <b>flag + NAME</b> heading per section.]\n",
        "\n",
        "<b>📊 MACRO SIGNAL</b>\n",
        "[paragraph if material; omit entirely if nothing significant]\n",
        "\n",
        "<b>📌 POSITION SIGNALS</b>\n",
        "- [news that confirms or challenges a held position or thesis — name the ticker/thesis and\n",
        "  the signal direction. Omit the section entirely if nothing is materially relevant.]\n",
        "\n",
        "<b>👁 WATCH / FORWARD</b>\n",
        "- [2–4 forward-looking things to monitor in the next 24–72h that could move markets.\n",
        "  Surface catalysts not already covered in-line above; do not repeat a forward-look you\n",
        "  have already stated in a TOP STORIES bullet.]\n",
        "\n",
        "Keep the entire brief under 600 words.",
    ]
)

ARGS = ("feeds", "web", "chroma", "YESTERDAY", "WEEKLY-SUMMARY", {}, "", "", "", "", "")


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 22, 4, 30, tzinfo=timezone.utc)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(brief, "datetime", _FrozenDT)


def test_prompt_with_no_window_matches_head(frozen_clock):
    assert brief.build_daily_prompt(*ARGS) == GOLDEN_PROMPT
    assert brief.build_daily_prompt(*ARGS, dated_window="") == GOLDEN_PROMPT


def test_golden_prompt_really_contains_the_weekly_block():
    assert "## WEEKLY CONTEXT (for trend awareness)" in GOLDEN_PROMPT
    assert "WEEKLY-SUMMARY" in GOLDEN_PROMPT


def test_window_replaces_the_weekly_slice_and_keeps_yesterday():
    p = brief.build_daily_prompt(
        "f",
        "w",
        "c",
        "YESTERDAY",
        "WEEKLY-SUMMARY",
        {},
        "",
        "",
        "",
        "",
        "",
        dated_window="## THE LAST 7 DAYS — ALREADY REPORTED\n11-20 [top] X",
    )
    assert "THE LAST 7 DAYS" in p
    assert "WEEKLY CONTEXT" not in p
    assert "WEEKLY-SUMMARY" not in p
    assert "YESTERDAY" in p


def test_window_sits_in_the_weekly_blocks_position(frozen_clock):
    p = brief.build_daily_prompt(*ARGS, dated_window="WINDOW-BLOCK")
    assert p.index("YESTERDAY'S BRIEF") < p.index("WINDOW-BLOCK")
    # Where the weekly block was: after yesterday, before the output format.
    assert p.index("WINDOW-BLOCK") < p.index("## OUTPUT FORMAT")
    golden_weekly_at = GOLDEN_PROMPT.index("## WEEKLY CONTEXT")
    assert p.index("WINDOW-BLOCK") - p.index("YESTERDAY'S BRIEF") <= (
        golden_weekly_at - GOLDEN_PROMPT.index("YESTERDAY'S BRIEF")
    )


def test_yesterday_is_still_sliced_to_6000_with_a_window():
    p = brief.build_daily_prompt(
        "f",
        "w",
        "c",
        "Y" * 7000,
        "",
        {},
        "",
        dated_window="WINDOW-BLOCK",
    )
    assert "Y" * 6000 in p and "Y" * 6001 not in p


# ── mode_submit ──────────────────────────────────────────────────────────────

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _Resp:
    status_code = 200
    text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return {"id": "batch_1"}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """mode_submit with every input stubbed. Returns what the stubs recorded."""
    rec = {
        "prompt_calls": [],
        "render_calls": [],
        "window_calls": [],
        "briefs_calls": [],
    }
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    monkeypatch.setattr(brief, "load_state", lambda: {})
    monkeypatch.setattr(brief, "save_state", lambda updates: None)
    monkeypatch.setattr(brief, "load_temp_sources", lambda: [])
    monkeypatch.setattr(brief, "fetch_feed_blocks", lambda feeds: ["FEED"])
    monkeypatch.setattr(brief, "fetch_mauldin_twie", lambda: "")
    monkeypatch.setattr(brief, "fetch_web_source", lambda s: "")
    monkeypatch.setattr(brief, "brief_memory_enabled", lambda: False)
    monkeypatch.setattr(brief, "claim_verify_enabled", lambda: False)
    monkeypatch.setattr(brief, "load_feedback", lambda: {})
    monkeypatch.setattr(brief, "build_chroma_context", lambda fb: "")
    monkeypatch.setattr(brief, "load_yesterday_brief", lambda: "")
    monkeypatch.setattr(brief, "load_last_weekly_summary", lambda: "WEEKLY-TEXT")
    monkeypatch.setattr(brief, "fetch_portfolio_weights", lambda: None)
    monkeypatch.setattr(brief, "load_book", lambda: {})
    monkeypatch.setattr(brief, "performance_prompt_block", lambda book: "")
    monkeypatch.setattr(brief, "build_market_pulse", lambda pins: "")
    monkeypatch.setattr(brief, "resolved_pins", lambda fb: [])

    def _no_enrichment(*a, **k):
        raise RuntimeError("enrichment stubbed off")

    monkeypatch.setattr(brief, "build_universe", _no_enrichment)

    def _prompt(*a, **k):
        rec["prompt_calls"].append((a, k))
        return "PROMPT"

    monkeypatch.setattr(brief, "build_daily_prompt", _prompt)
    monkeypatch.setattr(brief.requests, "post", lambda url, json=None, **kw: _Resp())

    def _briefs(n=7):
        rec["briefs_calls"].append(n)
        return [("2026-11-20", "TEXT")]

    monkeypatch.setattr(brief, "load_last_n_briefs", _briefs)

    def _window(briefs, today):
        rec["window_calls"].append((briefs, today))
        return dated_memory.WindowResult(
            block="WINDOW-BLOCK", days=2, lines=5, chars=99
        )

    monkeypatch.setattr(brief.dated_memory, "build_window", _window)
    return rec


def _dated(monkeypatch, on):
    monkeypatch.setattr(common, "BRIEF_DATED_MEMORY_ENABLED", on, raising=False)


def _memory_on(monkeypatch, rec, established="ESTABLISHED"):
    """brief-memory on with a stubbed ledger; records render_established_block."""
    monkeypatch.setattr(brief, "brief_memory_enabled", lambda: True)
    monkeypatch.setattr(brief, "save_source_index", lambda *a, **k: None)
    monkeypatch.setattr(brief, "build_source_index", lambda *a, **k: {})

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(brief.db, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(brief.claim_store, "load_ledger", lambda conn: {"LEDGER": 1})

    def _render(ledger, **kw):
        rec["render_calls"].append((ledger, kw))
        return established

    monkeypatch.setattr(brief, "render_established_block", _render)


def test_submit_with_knob_off_never_builds_the_window(monkeypatch, rig):
    _dated(monkeypatch, False)
    _memory_on(monkeypatch, rig)
    brief.mode_submit()
    assert rig["window_calls"] == []
    assert rig["briefs_calls"] == []
    ((args, kwargs),) = rig["prompt_calls"]
    assert kwargs.get("dated_window", "") == ""
    assert args[4] == "WEEKLY-TEXT"
    ((_, rkw),) = rig["render_calls"]
    assert not rkw.get("dated")


def test_submit_with_knob_on_passes_the_window_and_dated_claims(
    monkeypatch, rig, caplog
):
    _dated(monkeypatch, True)
    _memory_on(monkeypatch, rig)
    with caplog.at_level("INFO"):
        brief.mode_submit()
    ((args, kwargs),) = rig["prompt_calls"]
    assert kwargs["dated_window"] == "WINDOW-BLOCK"
    # The weekly slice is still passed positionally; the prompt decides.
    assert args[4] == "WEEKLY-TEXT"
    ((_, rkw),) = rig["render_calls"]
    assert rkw["dated"] is True
    assert rkw["today"] == TODAY
    assert rig["briefs_calls"] == [7]
    assert rig["window_calls"] == [([("2026-11-20", "TEXT")], TODAY)]
    assert "Dated window: 2 days, 5 lines, 99 chars" in caplog.text


def test_submit_knob_on_without_brief_memory_still_passes_the_window(monkeypatch, rig):
    _dated(monkeypatch, True)
    brief.mode_submit()
    ((_, kwargs),) = rig["prompt_calls"]
    assert kwargs["dated_window"] == "WINDOW-BLOCK"
    assert rig["render_calls"] == []


def test_submit_survives_a_window_failure(monkeypatch, rig, caplog):
    _dated(monkeypatch, True)

    def _boom(briefs, today):
        raise RuntimeError("window exploded")

    monkeypatch.setattr(brief.dated_memory, "build_window", _boom)
    with caplog.at_level("INFO"):
        brief.mode_submit()
    ((args, kwargs),) = rig["prompt_calls"]
    assert kwargs.get("dated_window", "") == ""
    assert args[4] == "WEEKLY-TEXT"
    assert "Dated window skipped (brief unaffected): window exploded" in caplog.text


def test_submit_survives_an_unreadable_archive(monkeypatch, rig, caplog):
    _dated(monkeypatch, True)

    def _boom(n=7):
        raise OSError("archive gone")

    monkeypatch.setattr(brief, "load_last_n_briefs", _boom)
    with caplog.at_level("INFO"):
        brief.mode_submit()
    ((_, kwargs),) = rig["prompt_calls"]
    assert kwargs.get("dated_window", "") == ""
    assert "Dated window skipped (brief unaffected): archive gone" in caplog.text


def test_submit_empty_window_falls_back_to_the_weekly_slice(monkeypatch, rig):
    _dated(monkeypatch, True)
    monkeypatch.setattr(
        brief.dated_memory,
        "build_window",
        lambda briefs, today: dated_memory.WindowResult(
            block="", days=0, lines=0, chars=0
        ),
    )
    brief.mode_submit()
    ((_, kwargs),) = rig["prompt_calls"]
    assert kwargs.get("dated_window", "") == ""


def test_dated_claims_failure_degrades_without_losing_the_window(monkeypatch, rig):
    _dated(monkeypatch, True)
    _memory_on(monkeypatch, rig)

    def _bad(ledger, **kw):
        raise ValueError("today unparseable")

    monkeypatch.setattr(brief, "render_established_block", _bad)
    brief.mode_submit()
    ((args, kwargs),) = rig["prompt_calls"]
    assert args[10] == brief.claim_store.degraded_block()
    assert kwargs["dated_window"] == "WINDOW-BLOCK"


# ── the knobs ────────────────────────────────────────────────────────────────


def test_knob_defaults():
    assert common.KNOBS["BRIEF_DATED_MEMORY_ENABLED"].default is False
    ev = common.KNOBS["EVIDENCE_MODEL"]
    assert ev.default == "claude-haiku-5-5"
    assert ev.env == "NEWSBRIEF_EVIDENCE_MODEL"


def _weekly_log_line(caplog):
    return next(m for m in caplog.messages if m.startswith("Chroma: "))


def test_submit_log_says_weekly_yes_when_the_dated_knob_is_off(
    monkeypatch, rig, caplog
):
    _dated(monkeypatch, False)
    with caplog.at_level("INFO"):
        brief.mode_submit()
    assert _weekly_log_line(caplog).endswith("| Weekly: yes")


def test_submit_log_says_the_window_replaced_the_weekly_slice(monkeypatch, rig, caplog):
    _dated(monkeypatch, True)
    with caplog.at_level("INFO"):
        brief.mode_submit()
    assert _weekly_log_line(caplog).endswith("| Weekly: replaced by dated window")


def test_submit_log_says_weekly_yes_when_the_window_is_empty(monkeypatch, rig, caplog):
    _dated(monkeypatch, True)
    monkeypatch.setattr(
        brief.dated_memory,
        "build_window",
        lambda briefs, today: dated_memory.WindowResult(
            block="", days=0, lines=0, chars=0
        ),
    )
    with caplog.at_level("INFO"):
        brief.mode_submit()
    assert _weekly_log_line(caplog).endswith("| Weekly: yes")

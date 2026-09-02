"""Phase 5 Telegram command handlers: /watch /unwatch /positions /performance."""

from datetime import datetime, timedelta, timezone

import pytest

import brief
import config
import common
import db
import scheduler
import trading


def _fb():
    return {"focus": [], "mute": [], "notes": []}


def _update(text):
    return {"message": {"text": text, "chat": {"id": config.chat_id()}}}


def _capture(monkeypatch):
    # Patch both namespaces: direct handler sends resolve to brief.telegram_send,
    # while telegram_send_long (used by /positions, /performance, /dig) calls the
    # common-namespace telegram_send.
    sent = []

    def _send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(brief, "telegram_send", _send)
    monkeypatch.setattr(common, "telegram_send", _send)
    return sent


def test_watch_adds_inferred_crypto(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    items = trading.load_watchlist()["items"]
    assert items == [
        {
            "raw": "BTC",
            "asset_class": "crypto",
            "instrument": "XBTUSD",
            "added": items[0]["added"],
        }
    ]
    assert "crypto" in sent[0] and "XBTUSD" in sent[0]


def test_watch_explicit_prediction(monkeypatch, tmp_path):
    _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    monkeypatch.setattr(trading, "polygram_market", lambda mid: {"question": "x"})
    brief._handle_telegram_update(_update("/watch prediction 0xabc"), _fb())
    items = trading.load_watchlist()["items"]
    assert items[0]["asset_class"] == "prediction" and items[0]["instrument"] == "0xabc"


def test_watch_unresolvable_reports_and_skips(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    monkeypatch.setattr(trading, "resolve_symbol", lambda t, c, o: None)
    brief._handle_telegram_update(_update("/watch NOTATHING"), _fb())
    assert trading.load_watchlist()["items"] == []
    assert "Couldn't resolve" in sent[0]


def test_watch_duplicate_is_noop(monkeypatch, tmp_path):
    _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    brief._handle_telegram_update(_update("/watch BTC"), _fb())
    assert len(trading.load_watchlist()["items"]) == 1


def test_unwatch_removes(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    trading.save_watchlist(
        {"items": [{"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"}]}
    )
    brief._handle_telegram_update(_update("/unwatch BTC"), _fb())
    assert trading.load_watchlist()["items"] == []
    assert "Unwatched" in sent[0]


def test_unwatch_missing_reports(monkeypatch, tmp_path):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/unwatch GHOST"), _fb())
    assert "not on the watchlist" in sent[0]


def test_positions_lists_open_with_marks(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        brief,
        "load_book",
        lambda: {
            "positions": [
                {
                    "status": "open",
                    "asset_class": "crypto",
                    "instrument": "XBTUSD",
                    "ticker": "BTC",
                    "direction": "bullish",
                    "entry_price": 100.0,
                },
                {
                    "status": "closed",
                    "asset_class": "equity",
                    "instrument": "bp.uk",
                    "ticker": "BP",
                    "direction": "bullish",
                    "entry_price": 5.0,
                },
            ]
        },
    )
    monkeypatch.setattr(brief, "price_position", lambda p: 150.0)
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "BTC" in sent[0] and "crypto" in sent[0]
    assert "+50.0%" in sent[0]
    assert "BP" not in sent[0]  # closed excluded


def test_positions_empty(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "No open positions" in sent[0]


def test_positions_unpriceable_shows_dash(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        brief,
        "load_book",
        lambda: {
            "positions": [
                {
                    "status": "open",
                    "asset_class": "equity",
                    "instrument": "x.us",
                    "ticker": "X",
                    "direction": "bullish",
                    "entry_price": 10.0,
                },
            ]
        },
    )
    monkeypatch.setattr(brief, "price_position", lambda p: None)
    brief._handle_telegram_update(_update("/positions"), _fb())
    assert "—" in sent[0]


def test_performance_wraps_report(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(
        brief, "performance_report", lambda book: "📊 PERFORMANCE REPORT\nstub"
    )
    brief._handle_telegram_update(_update("/performance"), _fb())
    assert "PERFORMANCE REPORT" in sent[0]


def test_resolved_pins_defaults_when_absent():
    # A feedback dict with no "pin" key resolves to the default five.
    assert brief.resolved_pins({"focus": [], "mute": [], "notes": []}) == [
        "ukraine",
        "iran",
        "korea",
        "japan",
        "china",
    ]


def test_resolved_pins_uses_explicit_list():
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "taiwan"]}
    assert brief.resolved_pins(fb) == ["china", "taiwan"]


def test_resolved_pins_empty_list_is_respected():
    # An explicit empty list means "no pins" — distinct from "key absent".
    fb = {"focus": [], "mute": [], "notes": [], "pin": []}
    assert brief.resolved_pins(fb) == []


def test_pin_seeds_defaults_then_adds(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda m: sent.append(m))
    fb = {"focus": [], "mute": [], "notes": []}
    fb = brief._handle_telegram_update(_update("/pin taiwan"), fb)
    # First pin materialises the defaults, then appends the new topic.
    assert fb["pin"] == ["ukraine", "iran", "korea", "japan", "china", "taiwan"]
    assert "taiwan" in sent[-1].lower()


def test_pin_lowercases_and_dedupes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china"]}
    fb = brief._handle_telegram_update(_update("/pin China"), fb)
    assert fb["pin"] == ["china"]  # case-folded, no duplicate


def test_unpin_removes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "japan"]}
    fb = brief._handle_telegram_update(_update("/unpin japan"), fb)
    assert fb["pin"] == ["china"]


def test_unpin_default_member_materialises_then_removes(monkeypatch):
    monkeypatch.setattr(brief, "telegram_send", lambda m: None)
    fb = {"focus": [], "mute": [], "notes": []}  # no pin key → defaults active
    fb = brief._handle_telegram_update(_update("/unpin korea"), fb)
    assert "korea" not in fb["pin"]
    assert fb["pin"] == ["ukraine", "iran", "japan", "china"]


def test_feedback_summary_lists_pins():
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "iran"]}
    out = brief.feedback_summary(fb)
    assert "Pinned:" in out
    assert "china" in out and "iran" in out


def test_feedback_summary_shows_default_pins_when_absent():
    out = brief.feedback_summary({"focus": [], "mute": [], "notes": []})
    assert "Pinned:" in out
    assert "ukraine" in out  # defaults surfaced, not hidden


# ── Temporary sources: store ──────────────────────────────────────────────────
def _isolate_sources(monkeypatch, tmp_path=None):
    """Swap the source store for an in-memory one, returning it.

    Sources are rows now. These tests are about brief's validation and the
    /addsource wizard, not about SQL, so they substitute at exactly the seam
    production uses. The SQL — the upsert's dedup, the atomic delete, the
    importer — is covered in test_config.py against a real database, where it
    can be tested honestly.

    The fake mirrors the unique index: adding a URL that is already present
    replaces it rather than appending a second row.
    """
    store: list[dict] = []

    def _add(entry):
        store[:] = [s for s in store if s.get("url") != entry["url"]]
        store.append(dict(entry))

    def _delete(url):
        for i, s in enumerate(store):
            if s.get("url") == url:
                return store.pop(i)
        return None

    # Non-dict entries pass through unchanged: the loader's job is to drop them,
    # so the fake must be able to hand it one.
    monkeypatch.setattr(
        config,
        "sources",
        lambda: [dict(s) if isinstance(s, dict) else s for s in store],
    )
    monkeypatch.setattr(config, "add_source", _add)
    monkeypatch.setattr(config, "delete_source", _delete)
    return store


def test_load_temp_sources_missing_file_is_empty(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    assert brief.load_temp_sources() == []


def test_load_temp_sources_non_list_ignored(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "sources", lambda: {"not": "a list"})
    assert brief.load_temp_sources() == []  # degrades, does not raise


def test_load_temp_sources_survives_an_unreadable_store(monkeypatch, tmp_path):
    """The file version of this contract was "one bad hand-edit must not take
    down the morning brief". Its database equivalent is a store that raises —
    Postgres unreachable, or the table not there yet. The brief still ships on
    the always-on RSS_FEEDS baseline; source configuration is never load-bearing
    for delivery."""
    _isolate_sources(monkeypatch, tmp_path)

    def _boom():
        raise RuntimeError("postgres is gone")

    monkeypatch.setattr(config, "sources", _boom)
    assert brief.load_temp_sources() == []
    assert brief.all_sources() == brief.RSS_FEEDS


def test_load_temp_sources_drops_invalid_entries(monkeypatch, tmp_path):
    store = _isolate_sources(monkeypatch, tmp_path)
    store.extend(
        [
            {"name": "Good", "url": "https://x/feed", "category": "Iran"},
            {"name": "NoUrl", "category": "iran"},  # missing url → dropped
            "garbage",  # not a dict → dropped
            {
                "name": "BadKind",
                "url": "https://y/feed",
                "category": "geo",
                "kind": "weird",
            },
        ]
    )
    out = brief.load_temp_sources()
    assert [s["name"] for s in out] == ["Good", "BadKind"]
    assert out[0]["category"] == "iran"  # lower-cased
    assert out[0]["kind"] == "regional"  # default kind
    assert out[1]["kind"] == "regional"  # invalid kind coerced to default


def test_load_temp_sources_defaults_source_type_to_feed(monkeypatch, tmp_path):
    store = _isolate_sources(monkeypatch, tmp_path)
    store.append({"name": "NoType", "url": "https://x/feed", "category": "iran"})
    out = brief.load_temp_sources()
    assert out[0]["source_type"] == "feed"  # absent → default


def test_load_temp_sources_accepts_and_normalizes_source_type(monkeypatch, tmp_path):
    store = _isolate_sources(monkeypatch, tmp_path)
    store.extend(
        [
            {
                "name": "Page",
                "url": "https://x/dash",
                "category": "us",
                "source_type": "page",
            },
            {
                "name": "Bad",
                "url": "https://y/dash",
                "category": "us",
                "source_type": "weird",
            },
        ]
    )
    out = brief.load_temp_sources()
    assert out[0]["source_type"] == "page"  # valid kept
    assert out[1]["source_type"] == "feed"  # invalid coerced to default


def test_load_temp_sources_state_funded_and_perspective(monkeypatch, tmp_path):
    store = _isolate_sources(monkeypatch, tmp_path)
    store.extend(
        [
            {
                "name": "Tagged",
                "url": "https://a/feed",
                "category": "geo",
                "state_funded": True,
                "perspective": "ARAB",
            },
            {
                "name": "BadPersp",
                "url": "https://b/feed",
                "category": "geo",
                "perspective": "MARTIAN",  # not in VALID_PERSPECTIVES → dropped
            },
            {"name": "Plain", "url": "https://c/feed", "category": "geo"},
        ]
    )
    out = brief.load_temp_sources()
    assert out[0]["state_funded"] is True and out[0]["perspective"] == "ARAB"
    assert out[1]["state_funded"] is False  # absent → default
    assert "perspective" not in out[1]  # invalid value dropped, not coerced
    assert out[2]["state_funded"] is False and "perspective" not in out[2]


def test_source_header_untagged_is_byte_identical():
    # Regression guard: untagged source must match the pre-feature format exactly.
    assert brief._source_header("Reuters World", "wire", "geo") == (
        "\n### Reuters World [WIRE] (GEO)"
    )


def test_source_header_perspective_only():
    assert brief._source_header("SCMP", "regional", "china", perspective="CHINESE") == (
        "\n### SCMP [REGIONAL · CHINESE] (CHINA)"
    )


def test_source_header_state_funded_only():
    assert brief._source_header("NHK", "regional", "japan", state_funded=True) == (
        "\n### NHK [REGIONAL · STATE-FUNDED] (JAPAN)"
    )


def test_source_header_both():
    assert brief._source_header(
        "Al Jazeera", "regional", "geo", perspective="ARAB", state_funded=True
    ) == ("\n### Al Jazeera [REGIONAL · ARAB · STATE-FUNDED] (GEO)")


def test_baked_in_feed_perspective_assignments():
    by_name = {f["name"]: f for f in brief.RSS_FEEDS}
    expected = {
        "Al Jazeera": ("ARAB", True),
        "NHK World": ("JAPANESE", True),
        "Yonhap (English)": ("KOREAN", True),
        "SCMP": ("CHINESE", False),
        "Kyiv Independent": ("UKRAINIAN", False),
    }
    for name, (persp, sf) in expected.items():
        assert by_name[name].get("perspective") == persp, name
        assert by_name[name].get("state_funded", False) is sf, name
        assert persp in brief.VALID_PERSPECTIVES
    # Wires/analysts/think-tanks stay untagged (sample check).
    for name in ("Reuters World", "ISW Daily Assessment", "38 North"):
        assert "perspective" not in by_name[name], name
        assert by_name[name].get("state_funded", False) is False, name


def test_system_prompt_teaches_perspective_tags():
    p = brief.SYSTEM_PROMPT
    assert "STATE-FUNDED" in p
    assert "perspective" in p.lower()
    assert "attribute" in p.lower()  # attribution instruction present
    assert "untagged" in p.lower()  # absent-tag semantics taught


def test_add_temp_source_dedupes_by_url(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    e1 = {"name": "A", "url": "https://x/feed", "category": "iran", "kind": "regional"}
    e2 = {"name": "A v2", "url": "https://x/feed", "category": "iran", "kind": "wire"}
    brief.add_temp_source(e1)
    brief.add_temp_source(e2)  # same URL → replaces, not duplicates
    srcs = brief.load_temp_sources()
    assert len(srcs) == 1 and srcs[0]["name"] == "A v2" and srcs[0]["kind"] == "wire"


def test_remove_temp_source_by_id(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    e = {"name": "A", "url": "https://x/feed", "category": "iran", "kind": "regional"}
    brief.add_temp_source(e)
    removed = brief.remove_temp_source(brief._source_id("https://x/feed"))
    assert removed and removed["name"] == "A"
    assert brief.load_temp_sources() == []
    assert brief.remove_temp_source("nope") is None


def test_build_google_news_url_uses_site_recipe():
    url = brief.build_google_news_url("timesofisrael.com")
    assert "site%3Atimesofisrael.com" in url
    assert "news.google.com/rss/search" in url


# ── /addsource wizard + /sources + callbacks ──────────────────────────────────
def _cb(data, msg_id=10):
    return {
        "id": "cbid",
        "data": data,
        "message": {"message_id": msg_id, "chat": {"id": config.chat_id()}},
    }


def _wire_telegram(monkeypatch):
    """Capture the interactive Telegram surface; send_buttons returns a msg_id."""
    cap = {"buttons": [], "edits": [], "acks": [], "sends": []}
    monkeypatch.setattr(
        brief,
        "telegram_send_buttons",
        lambda t, kb: cap["buttons"].append((t, kb)) or 10,
    )
    monkeypatch.setattr(
        brief,
        "telegram_edit_text",
        lambda mid, t, kb=None: cap["edits"].append((mid, t, kb)),
    )
    monkeypatch.setattr(
        brief,
        "telegram_answer_callback",
        lambda cid, text=None: cap["acks"].append(cid),
    )
    monkeypatch.setattr(brief, "telegram_send", lambda t: cap["sends"].append(t))
    return cap


def test_addsource_wizard_quick_domain(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    cap = _wire_telegram(monkeypatch)
    chat = str(config.chat_id())

    brief._handle_telegram_update(_update("/addsource"), _fb())
    assert brief._WIZARD[chat]["step"] == "category"

    brief._handle_callback_query(_cb("as:cat:iran"))
    assert brief._WIZARD[chat]["category"] == "iran"
    assert brief._WIZARD[chat]["step"] == "kind"

    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    assert brief._WIZARD[chat]["step"] == "url"

    brief._handle_telegram_update(_update("timesofisrael.com"), _fb())
    w = brief._WIZARD[chat]
    assert w["step"] == "confirm" and w["name"] == "timesofisrael.com"
    assert "site%3Atimesofisrael.com" in w["url"]

    brief._handle_callback_query(_cb("as:confirm"))
    assert chat not in brief._WIZARD  # wizard cleared on confirm
    srcs = brief.load_temp_sources()
    assert len(srcs) == 1
    assert srcs[0]["category"] == "iran" and srcs[0]["kind"] == "regional"
    assert cap["acks"]  # every callback acknowledged
    assert srcs[0]["source_type"] == "feed"  # bare domain → feed, no extra step


def test_addsource_wizard_full_url_asks_feed_or_page(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(config.chat_id())
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:us"))
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    brief._handle_telegram_update(
        _update("https://www.bcaresearch.com/dashboard/x"), _fb()
    )
    w = brief._WIZARD[chat]
    assert w["url"] == "https://www.bcaresearch.com/dashboard/x"
    assert w["name"] == "www.bcaresearch.com"
    assert w["step"] == "source_type"  # full URL → asks feed-or-page

    brief._handle_callback_query(_cb("as:stype:page"))
    assert brief._WIZARD[chat]["source_type"] == "page"
    assert brief._WIZARD[chat]["step"] == "confirm"

    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert len(srcs) == 1 and srcs[0]["source_type"] == "page"


def test_addsource_wizard_full_url_feed_choice(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:wire"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    brief._handle_telegram_update(_update("https://site.com/feed.xml"), _fb())
    brief._handle_callback_query(_cb("as:stype:feed"))
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert (
        srcs[0]["source_type"] == "feed"
        and srcs[0]["url"] == "https://site.com/feed.xml"
    )


def test_addsource_wizard_rejects_garbage_url(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    cap = _wire_telegram(monkeypatch)
    chat = str(config.chat_id())
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:iran"))
    brief._handle_callback_query(_cb("as:kind:regional"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    brief._handle_telegram_update(_update("not a url at all"), _fb())
    assert brief._WIZARD[chat]["step"] == "url"  # stays put, re-prompts
    assert any("neither a domain nor a URL" in s for s in cap["sends"])


def test_addsource_wizard_captures_state_funded_and_perspective(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(config.chat_id())
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:regional"))
    assert brief._WIZARD[chat]["step"] == "state_funded"

    brief._handle_callback_query(_cb("as:sf:1"))
    assert brief._WIZARD[chat]["state_funded"] is True
    assert brief._WIZARD[chat]["step"] == "perspective"

    brief._handle_callback_query(_cb("as:persp:ARAB"))
    assert brief._WIZARD[chat]["perspective"] == "ARAB"
    assert brief._WIZARD[chat]["step"] == "url"

    brief._handle_telegram_update(_update("aljazeera.com"), _fb())
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert srcs[0]["state_funded"] is True and srcs[0]["perspective"] == "ARAB"


def test_addsource_wizard_perspective_skip(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(config.chat_id())
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cat:geo"))
    brief._handle_callback_query(_cb("as:kind:wire"))
    brief._handle_callback_query(_cb("as:sf:0"))
    brief._handle_callback_query(_cb("as:persp:_skip"))
    assert brief._WIZARD[chat]["step"] == "url"
    assert "perspective" not in brief._WIZARD[chat]

    brief._handle_telegram_update(_update("example.com"), _fb())
    brief._handle_callback_query(_cb("as:confirm"))
    srcs = brief.load_temp_sources()
    assert srcs[0]["state_funded"] is False and "perspective" not in srcs[0]


def test_addsource_wizard_cancel_clears(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    chat = str(config.chat_id())
    brief._handle_telegram_update(_update("/addsource"), _fb())
    brief._handle_callback_query(_cb("as:cancel"))
    assert chat not in brief._WIZARD


def test_sources_remove_via_button(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    _wire_telegram(monkeypatch)
    brief.add_temp_source(
        {"name": "X", "url": "https://x/feed", "category": "iran", "kind": "regional"}
    )
    brief._handle_callback_query(_cb(f"rmsrc:{brief._source_id('https://x/feed')}"))
    assert brief.load_temp_sources() == []


def test_removesource_by_name(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    sent = _capture(monkeypatch)
    brief.add_temp_source(
        {
            "name": "Times",
            "url": "https://x/feed",
            "category": "iran",
            "kind": "regional",
        }
    )
    brief._handle_telegram_update(
        _update("/removesource times"), _fb()
    )  # case-insensitive
    assert brief.load_temp_sources() == []
    assert "Removed" in sent[0]


def test_removesource_unknown_reports(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    sent = _capture(monkeypatch)
    brief._handle_telegram_update(_update("/removesource ghost"), _fb())
    assert "No temp source" in sent[0]


def test_foreign_chat_callback_is_ignored(monkeypatch, tmp_path):
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    cap = _wire_telegram(monkeypatch)
    brief.add_temp_source(
        {"name": "X", "url": "https://x/feed", "category": "iran", "kind": "regional"}
    )
    foreign = {
        "id": "z",
        "data": f"rmsrc:{brief._source_id('https://x/feed')}",
        "message": {"message_id": 1, "chat": {"id": "999999"}},
    }
    # via _handle_update: the gate lives in the router, not the handler
    brief._handle_update({"update_id": 1, "callback_query": foreign}, _fb())
    assert len(brief.load_temp_sources()) == 1  # not removed
    assert cap["acks"] == []  # and not acked: no API call for a foreign tap


# ── /close /unwatch /unpin button pickers + /reset confirm ────────────────────
def test_close_picker_lists_distinct_open_tickers(monkeypatch):
    cap = _wire_telegram(monkeypatch)
    monkeypatch.setattr(
        brief,
        "load_book",
        lambda: {
            "positions": [
                {"status": "open", "ticker": "BTC", "instrument": "XBTUSD"},
                {"status": "open", "ticker": "BTC", "instrument": "XBTUSD"},  # dup
                {"status": "closed", "ticker": "BP", "instrument": "bp.uk"},
            ]
        },
    )
    brief._handle_telegram_update(_update("/close"), _fb())
    _text, kb = cap["buttons"][0]
    assert len(kb) == 1  # one button, deduped, closed excluded
    assert kb[0][0]["callback_data"] == f"close:{brief._short_id('BTC')}"


def test_close_picker_empty(monkeypatch):
    cap = _wire_telegram(monkeypatch)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    brief._handle_telegram_update(_update("/close"), _fb())
    assert any("No open positions" in s for s in cap["sends"])


def test_close_button_routes_to_close_ticker(monkeypatch):
    _wire_telegram(monkeypatch)
    monkeypatch.setattr(
        brief,
        "load_book",
        lambda: {"positions": [{"status": "open", "ticker": "BTC", "instrument": "x"}]},
    )
    called = []
    monkeypatch.setattr(brief, "_close_ticker", lambda t: called.append(t))
    brief._handle_callback_query(_cb(f"close:{brief._short_id('BTC')}"), _fb())
    assert called == ["BTC"]


def test_close_text_form_still_works(monkeypatch):
    _wire_telegram(monkeypatch)
    called = []
    monkeypatch.setattr(brief, "_close_ticker", lambda t: called.append(t))
    brief._handle_telegram_update(_update("/close AAPL_US_EQ"), _fb())
    assert called == ["AAPL_US_EQ"]


def test_unwatch_button_removes_right_item(monkeypatch, tmp_path):
    _wire_telegram(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    trading.save_watchlist(
        {
            "items": [
                {"raw": "BTC", "asset_class": "crypto", "instrument": "XBTUSD"},
                {"raw": "ETH", "asset_class": "crypto", "instrument": "ETHUSD"},
            ]
        }
    )
    brief._handle_callback_query(
        _cb(f"unwatch:{brief._short_id('crypto|XBTUSD')}"), _fb()
    )
    assert [i["raw"] for i in trading.load_watchlist()["items"]] == ["ETH"]


def test_unwatch_picker_empty(monkeypatch, tmp_path):
    cap = _wire_telegram(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")
    brief._handle_telegram_update(_update("/unwatch"), _fb())
    assert any("Watchlist is empty" in s for s in cap["sends"])


def test_unpin_button_removes_pin_and_threads_fb(monkeypatch):
    _wire_telegram(monkeypatch)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "japan"]}
    out = brief._handle_callback_query(_cb(f"unpin:{brief._short_id('japan')}"), fb)
    assert out["pin"] == ["china"]


def test_unpin_picker_defaults_when_no_explicit_pins(monkeypatch):
    cap = _wire_telegram(monkeypatch)
    brief._handle_telegram_update(_update("/unpin"), _fb())  # no pin key → defaults
    _text, kb = cap["buttons"][0]
    datas = [b["callback_data"] for row in kb for b in row]
    assert f"unpin:{brief._short_id('ukraine')}" in datas  # default pin offered


def test_reset_asks_confirmation_not_immediate(monkeypatch):
    cap = _wire_telegram(monkeypatch)
    fb = {"focus": ["x"], "mute": [], "notes": []}
    out = brief._handle_telegram_update(_update("/reset"), fb)
    assert out["focus"] == ["x"]  # NOT cleared yet — just prompted
    kb = cap["buttons"][0][1]
    datas = [b["callback_data"] for row in kb for b in row]
    assert "reset:yes" in datas and "reset:no" in datas


def test_reset_yes_clears(monkeypatch):
    _wire_telegram(monkeypatch)
    out = brief._handle_callback_query(
        _cb("reset:yes"),
        {"focus": ["x"], "mute": ["y"], "notes": ["z"], "pin": ["china"]},
    )
    assert out == {"focus": [], "mute": [], "notes": []}


def test_reset_no_keeps_overrides(monkeypatch):
    _wire_telegram(monkeypatch)
    fb = {"focus": ["x"], "mute": [], "notes": []}
    out = brief._handle_callback_query(_cb("reset:no"), fb)
    assert out == fb


def test_handle_update_threads_fb_through_callback(monkeypatch):
    _wire_telegram(monkeypatch)
    fb = {"focus": [], "mute": [], "notes": [], "pin": ["china", "japan"]}
    update = {"callback_query": _cb(f"unpin:{brief._short_id('china')}")}
    out = brief._handle_update(update, fb)
    assert out["pin"] == ["japan"]


# ── setMyCommands self-registration ───────────────────────────────────────────
def test_register_bot_commands_hash_gated(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "STATE_FILE", tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(
        brief, "telegram_set_my_commands", lambda cmds: calls.append(cmds) or True
    )
    brief.register_bot_commands_if_changed()
    brief.register_bot_commands_if_changed()  # unchanged → no second API call
    assert len(calls) == 1
    assert any(c["command"] == "addsource" for c in calls[0])


# ── Temp sources partitioning ────────────────────────────────────────────────────
def test_split_temp_sources_partitions_by_type():
    feed_default = {"name": "F", "url": "u1", "category": "us", "kind": "wire"}
    feed_explicit = {
        "name": "F2",
        "url": "u2",
        "category": "us",
        "kind": "wire",
        "source_type": "feed",
    }
    page = {
        "name": "P",
        "url": "u3",
        "category": "us",
        "kind": "regional",
        "source_type": "page",
    }
    feeds, pages = brief._split_temp_sources([feed_default, page, feed_explicit])
    assert [s["name"] for s in feeds] == ["F", "F2"]
    assert [s["name"] for s in pages] == ["P"]


# ── Single-user chat gate ────────────────────────────────────────────────────────
# This bot is single-user: every update must come from TELEGRAM_CHAT_ID. The gate
# lives in _handle_update (the sole router), NOT in the individual handlers, so
# these tests go through _handle_update — calling a handler directly bypasses the
# gate by design and proves nothing. The other ~80 tests in this file do call the
# handlers directly; that is fine, they exercise command logic, not authorization.
FOREIGN_CHAT = 999_999_999


def _msg_update(text, chat_id):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat_id}}}


def test_message_from_foreign_chat_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "chat_id", lambda: "42")
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")

    fb = _fb()
    out = brief._handle_update(_msg_update("/watch BTC", FOREIGN_CHAT), fb)

    assert out == fb  # feedback untouched
    assert trading.load_watchlist()["items"] == []  # command never ran
    assert sent == []  # and nothing was said back


def test_message_from_configured_chat_is_handled(monkeypatch, tmp_path):
    """Guards against the gate rejecting everything (a passing foreign-chat test
    on its own would still pass if the router dropped every update)."""
    monkeypatch.setattr(config, "chat_id", lambda: "42")
    _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")

    brief._handle_update(_msg_update("/watch BTC", 42), _fb())
    assert trading.load_watchlist()["items"] != []


def test_message_with_no_chat_is_ignored(monkeypatch, tmp_path):
    """Fail closed: a malformed update yields chat id "", which must not match."""
    monkeypatch.setattr(config, "chat_id", lambda: "42")
    sent = _capture(monkeypatch)
    monkeypatch.setattr(trading, "WATCHLIST_FILE", tmp_path / "wl.json")

    brief._handle_update({"update_id": 1, "message": {"text": "/watch BTC"}}, _fb())
    assert trading.load_watchlist()["items"] == []
    assert sent == []


def test_callback_from_configured_chat_is_acked(monkeypatch, tmp_path):
    """Positive counterpart to test_foreign_chat_callback_is_ignored: the gate must
    not stop legitimate taps reaching the handler and being answered."""
    monkeypatch.setattr(config, "chat_id", lambda: "42")
    _isolate_sources(monkeypatch, tmp_path)
    brief._WIZARD.clear()
    cap = _wire_telegram(monkeypatch)

    brief._handle_update(_msg_update("/addsource", 42), _fb())
    brief._handle_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cbid",
                "data": "as:cat:iran",
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        },
        _fb(),
    )
    assert cap["acks"] == ["cbid"]
    assert brief._WIZARD["42"]["category"] == "iran"


@pytest.mark.parametrize(
    "update",
    [
        {},  # no message, no callback_query
        {"message": None},
        {"message": {"text": "/watch BTC", "chat": None}},
        {"message": {"text": "/watch BTC", "chat": {}}},  # chat with no id
        {"callback_query": None},
        {"callback_query": {"id": "x", "data": "rmsrc:y"}},  # inline-mode: no message
        {"callback_query": {"id": "x", "data": "rmsrc:y", "message": {}}},
    ],
    ids=[
        "empty",
        "null-message",
        "null-chat",
        "chat-without-id",
        "null-callback",
        "callback-without-message",
        "callback-with-empty-message",
    ],
)
def test_malformed_updates_are_unauthorized(monkeypatch, update):
    """Every shape that lacks a resolvable chat must fail closed, not raise.

    _drain_update_batch catches exceptions per-update, so a raise here would be
    survivable but would fire a "Command failed" alert on every junk update."""
    monkeypatch.setattr(config, "chat_id", lambda: "42")
    assert brief._update_chat_id(update) == ""
    assert brief._is_authorized(update) is False


def test_close_ticker_routes_live_to_venue_sell(monkeypatch, tmp_path):
    import polygram_live

    live = {
        "id": "L",
        "status": "open",
        "execution": "live",
        "sleeve": "B",
        "asset_class": "prediction",
        "instrument": "m",
        "ticker": "m",
        "outcome": "No",
    }
    book = {"positions": [live]}
    monkeypatch.setattr(brief, "load_book", lambda: book)
    monkeypatch.setattr(brief, "save_book", lambda b: None)
    monkeypatch.setattr(brief.trading, "BOOK_FILE", tmp_path / "book.json")
    monkeypatch.setattr(brief, "_pos_ticker", lambda p: "m")
    paper_called = []
    monkeypatch.setattr(
        brief,
        "_close_position_at_market",
        lambda p, day, r: paper_called.append(p["id"]),
    )
    live_called = []

    def fake_live_close(p, reason):
        live_called.append((p["id"], reason))
        p["status"] = "closed"
        return True

    monkeypatch.setattr(polygram_live, "close_live_position", fake_live_close)
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t))
    brief._close_ticker("m")
    assert live_called == [("L", "manual")]  # live routed to venue sell
    assert paper_called == []  # paper path NOT used for the live row
    assert live["status"] == "closed"


def test_predict_wizard_thesis_to_market(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    monkeypatch.setattr(brief, "telegram_send_buttons", lambda text, kb: 111)
    edits = []
    monkeypatch.setattr(
        brief, "telegram_edit_text", lambda mid, text, kb: edits.append((text, kb))
    )
    monkeypatch.setattr(
        trading,
        "_gather_pg_candidates",
        lambda s: [
            {
                "market_id": "2774056",
                "question": "Hormuz normal by Aug 31?",
                "yes_price": 0.13,
                "end_date": "2026-08-31",
                "event_id": "evt_h",
            }
        ],
    )
    brief._WIZARD.clear()
    brief._predict_start("42")
    assert (
        brief._WIZARD["42"]["step"] == "pr_thesis"
        and brief._WIZARD["42"]["msg_id"] == 111
    )
    # user replies with the thesis free-text -> search + show market buttons
    brief._wizard_handle_text("42", "hormuz shipping stays disrupted")
    w = brief._WIZARD["42"]
    assert w["step"] == "pr_market" and w["thesis"] == "hormuz shipping stays disrupted"
    assert w["candidates"][0]["event_id"] == "evt_h"
    assert edits  # market buttons were rendered


def test_predict_disabled_says_so(monkeypatch):
    monkeypatch.setattr(common, "PG_LIVE_ENABLED", False)
    monkeypatch.setattr(common, "PG_B_ENABLED", False)
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t))
    brief._WIZARD.clear()
    brief._predict_start("42")
    assert "42" not in brief._WIZARD and sent and "disabled" in sent[0].lower()


def test_predict_commit_opens_and_logs(monkeypatch):
    import polygram_live

    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    book = {"positions": []}
    monkeypatch.setattr(brief, "load_book", lambda: book)
    monkeypatch.setattr(brief, "save_book", lambda b: None)
    monkeypatch.setattr(brief.trading, "BOOK_FILE", "x")
    monkeypatch.setattr(
        brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext()
    )
    monkeypatch.setattr(trading, "_sleeve_b_open_ok", lambda b, m, o, a: (True, ""))
    opened = {}

    def fake_open(book, **kw):
        opened.update(kw)
        row = {
            "id": "2026-09-01:prediction:m:YES:live",
            "execution": "live",
            "sleeve": "B",
            "instrument": kw["market_id"],
            "outcome": kw["outcome"],
            "entry_price": 0.62,
            "cost_basis": kw["amount"],
            "status": "open",
        }
        book["positions"].append(row)
        return row

    monkeypatch.setattr(polygram_live, "open_live_position", fake_open)
    logged = []
    monkeypatch.setattr(common, "append_thesis", lambda r: logged.append(r))
    monkeypatch.setattr(brief, "telegram_edit_text", lambda *a: None)
    brief._WIZARD["42"] = {
        "step": "pr_confirm",
        "msg_id": 1,
        "thesis": "oil stays bid",
        "market_id": "m",
        "event_id": "evt",
        "question": "Q?",
        "prices": [0.62, 0.38],
        "token_ids": ["tYes", "tNo"],
        "outcome": "Yes",
        "side_index": 0,
        "stake": 5.0,
        "hold_mode": "settle",
        "p_hat": 0.75,
        "end_date": "2026-09-01",
    }
    brief._predict_commit("42")
    assert opened["sleeve"] == "B" and opened["event_id"] == "evt"
    assert (
        opened["token_id"] == "tYes"
        and opened["outcome"] == "Yes"
        and opened["amount"] == 5.0
    )
    assert logged and logged[0]["p_hat"] == 0.75 and logged[0]["entry_price"] == 0.62
    assert logged[0]["resolve_by"] == "2026-09-01" and logged[0]["traded"] is True
    assert "42" not in brief._WIZARD


def test_predict_commit_blocked_by_cap(monkeypatch):
    import polygram_live

    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    monkeypatch.setattr(brief, "load_book", lambda: {"positions": []})
    monkeypatch.setattr(
        brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext()
    )
    monkeypatch.setattr(brief.trading, "BOOK_FILE", "x")
    monkeypatch.setattr(
        trading,
        "_sleeve_b_open_ok",
        lambda b, m, o, a: (False, "over total Sleeve-B cap"),
    )
    calls = []
    monkeypatch.setattr(
        polygram_live, "open_live_position", lambda book, **k: calls.append(k)
    )
    logged = []
    monkeypatch.setattr(common, "append_thesis", lambda r: logged.append(r))
    edits = []
    monkeypatch.setattr(
        brief, "telegram_edit_text", lambda mid, text, kb: edits.append(text)
    )
    brief._WIZARD["42"] = {
        "step": "pr_confirm",
        "msg_id": 1,
        "thesis": "t",
        "market_id": "m",
        "event_id": "e",
        "question": "Q",
        "prices": [0.6, 0.4],
        "token_ids": ["a", "b"],
        "outcome": "Yes",
        "side_index": 0,
        "stake": 99.0,
        "hold_mode": "settle",
        "p_hat": None,
        "end_date": "2026-09-01",
    }
    brief._predict_commit("42")
    assert calls == [] and logged == []  # blocked before any order/log
    assert edits and "cap" in edits[-1].lower()
    assert "42" not in brief._WIZARD


def test_predict_commit_passes_real_live_exposure(monkeypatch):
    """Sleeve-B opens must feed the true cross-sleeve live exposure into the global
    cap check (PG_LIVE_TOTAL_CAP), not 0.0 — otherwise Sleeve B escapes the ceiling."""
    import polygram_live

    monkeypatch.setattr(common, "PG_LIVE_ENABLED", True)
    monkeypatch.setattr(common, "PG_B_ENABLED", True)
    # A pre-existing open live row (Sleeve A) already consuming $12 of the global cap.
    book = {
        "positions": [
            {
                "execution": "live",
                "sleeve": "A",
                "status": "open",
                "cost_basis": 12.0,
            }
        ]
    }
    monkeypatch.setattr(brief, "load_book", lambda: book)
    monkeypatch.setattr(brief, "save_book", lambda b: None)
    monkeypatch.setattr(brief.trading, "BOOK_FILE", "x")
    monkeypatch.setattr(
        brief, "file_lock", lambda *a, **k: __import__("contextlib").nullcontext()
    )
    monkeypatch.setattr(trading, "_sleeve_b_open_ok", lambda b, m, o, a: (True, ""))
    opened = {}

    def fake_open(book, **kw):
        opened.update(kw)
        row = {"id": "L", "status": "open", **kw}
        book["positions"].append(row)
        return row

    monkeypatch.setattr(polygram_live, "open_live_position", fake_open)
    monkeypatch.setattr(common, "append_thesis", lambda r: None)
    monkeypatch.setattr(brief, "telegram_edit_text", lambda *a: None)
    brief._WIZARD["42"] = {
        "step": "pr_confirm",
        "msg_id": 1,
        "thesis": "t",
        "market_id": "m",
        "event_id": "e",
        "question": "Q",
        "prices": [0.6, 0.4],
        "token_ids": ["a", "b"],
        "outcome": "Yes",
        "side_index": 0,
        "stake": 3.0,
        "hold_mode": "settle",
        "p_hat": None,
        "end_date": "2026-09-01",
    }
    brief._predict_commit("42")
    assert opened["live_exposure"] == 12.0  # real cross-sleeve exposure, not 0.0


def test_predict_stake_respects_effective_cap(monkeypatch):
    """Stake presets + free-text are bounded by min(PG_B_POS_CAP, PG_LIVE_PER_TRADE_CAP)
    so the wizard never offers/accepts a stake the foundation per-trade cap will reject."""
    monkeypatch.setattr(common, "PG_B_POS_CAP", 10.0)
    monkeypatch.setattr(
        common, "PG_LIVE_PER_TRADE_CAP", 5.0
    )  # tighter -> effective cap 5
    edits = []
    monkeypatch.setattr(
        brief, "telegram_edit_text", lambda mid, text, kb: edits.append((text, kb))
    )
    brief._WIZARD["42"] = {"step": "pr_side", "msg_id": 1}
    brief._predict_show_stake("42", "YES")
    text, kb = edits[-1]
    labels = [b["text"] for r in kb for b in r]
    assert "$5" in labels and "$10" not in labels  # $10 preset dropped (> cap)
    assert "max $5" in text.lower()
    # free-text over the cap is rejected with a clear message; wizard does NOT advance
    edits.clear()
    brief._WIZARD["42"]["step"] = "pr_stake_text"
    brief._wizard_handle_text("42", "8")
    assert (
        brief._WIZARD["42"]["step"] == "pr_stake_text"
    )  # still awaiting a valid stake
    assert "stake" not in brief._WIZARD["42"]
    assert edits and "5" in edits[-1][0]


# ── Pickers label predictions by question, not market id ──────────────────────


def test_close_picker_labels_prediction_by_question(monkeypatch):
    book = {
        "positions": [
            {
                "status": "open",
                "asset_class": "prediction",
                "execution": "live",
                "ticker": "2774056",
                "instrument": "2774056",
                "topic": "Will Iran & Israel agree a ceasefire before October 2026?",
            },
            {
                "status": "open",
                "asset_class": "equity",
                "ticker": "SHEL_US_EQ",
                "instrument": "SHEL_US_EQ",
            },
        ]
    }
    monkeypatch.setattr(brief, "load_book", lambda: book)
    captured = {}
    monkeypatch.setattr(
        brief,
        "_picker_send",
        lambda text, rows, mid: captured.update(rows=rows),
    )
    brief._close_picker_render()
    pred, equity = captured["rows"][0][0], captured["rows"][1][0]
    assert "Will Iran & Israel" in pred["text"]  # button labels are NOT HTML-parsed
    assert "2774056" not in pred["text"]
    assert "💵" in pred["text"]  # real money is unmistakable before you tap
    assert len(pred["text"]) <= brief._BUTTON_NAME_CAP + 4  # ❌ + 💵 + ellipsis
    # The tap still resolves by the hashed TICKER, so relabelling changed nothing.
    assert pred["callback_data"] == f"close:{brief._short_id('2774056')}"
    assert equity["text"] == "❌ SHEL_US_EQ"  # non-predictions unchanged


# ── /jobs and /run: the scheduler's operator surface ─────────────────────────

JOBS_NOW = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)


def _job_row(**kw):
    row = {
        "id": 1,
        "job_name": "collect",
        "scheduled_for": None,
        "trigger": "scheduled",
        "status": "finished",
        "started_at": JOBS_NOW - timedelta(hours=8),
        "finished_at": JOBS_NOW - timedelta(hours=7),
        "exit_code": 0,
        "created_at": JOBS_NOW - timedelta(hours=8),
    }
    row.update(kw)
    return row


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_jobs_view_reports_a_clean_run_as_a_success():
    out = brief._format_jobs({"collect": _job_row()}, JOBS_NOW)
    assert "✅ <b>collect</b>" in out
    assert "exit 0" in out and "7h ago" in out


def test_jobs_view_distinguishes_a_failing_exit_code_from_a_clean_one():
    out = brief._format_jobs({"collect": _job_row(exit_code=2)}, JOBS_NOW)
    assert "❌ <b>collect</b>" in out and "exit 2" in out


def test_jobs_view_shows_a_missed_run_as_missed_not_as_a_success():
    """The row a started_at ordering would have hidden. It has to read as a
    problem, not as an absence."""
    out = brief._format_jobs(
        {
            "weekly": _job_row(
                job_name="weekly",
                status="missed",
                started_at=None,
                finished_at=None,
                exit_code=None,
            )
        },
        JOBS_NOW,
    )
    assert "⚠️ <b>weekly</b>" in out and "missed" in out


def test_jobs_view_shows_a_running_job_with_how_long_it_has_been_going():
    out = brief._format_jobs(
        {
            "monitor": _job_row(
                job_name="monitor",
                status="running",
                started_at=JOBS_NOW - timedelta(minutes=2),
                finished_at=None,
                exit_code=None,
            )
        },
        JOBS_NOW,
    )
    assert "🔄 <b>monitor</b>" in out and "2m" in out


def test_jobs_view_lists_every_scheduled_job_even_with_an_empty_ledger():
    """Answering only for jobs that have run would make a job that has NEVER
    run invisible — the failure most worth seeing on the morning after a
    cutover."""
    out = brief._format_jobs({}, JOBS_NOW)

    for spec in scheduler.SCHEDULES:
        assert spec.job in out
    assert out.count("never run") == len(scheduler.SCHEDULES)


def test_jobs_view_gives_every_job_a_next_due_time():
    out = brief._format_jobs({}, JOBS_NOW)
    assert out.count("next:") == len(scheduler.SCHEDULES)


def test_jobs_says_why_it_cannot_answer_rather_than_showing_an_empty_list(monkeypatch):
    """A database that is down must not render as four jobs that never ran —
    that is a confident wrong answer, and the operator would act on it."""
    sent = _capture(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "connect", boom)

    brief._handle_telegram_update(_update("/jobs"), _fb())

    assert len(sent) == 1
    assert "connection refused" in sent[0] and "RuntimeError" in sent[0]


def test_run_queues_the_job_and_confirms(monkeypatch):
    sent = _capture(monkeypatch)
    queued = []
    monkeypatch.setattr(db, "connect", lambda **kw: _FakeConn())
    monkeypatch.setattr(
        db, "enqueue_manual", lambda conn, job: (queued.append(job), 9)[1]
    )

    brief._handle_telegram_update(_update("/run collect"), _fb())

    assert queued == ["collect"]
    assert "collect" in sent[0]


def test_run_refuses_an_unknown_job_before_it_touches_the_database(monkeypatch):
    """Validation first. A typo must cost a reply, not a connection and a row
    the supervisor then has to discard with an alert."""
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        db, "connect", lambda **kw: pytest.fail("validated after connecting")
    )

    brief._handle_telegram_update(_update("/run bakfill"), _fb())

    assert "bakfill" in sent[0]
    assert "collect" in sent[0], "a refusal should name the jobs that do exist"


def test_run_with_no_argument_lists_what_can_be_run(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(
        db, "connect", lambda **kw: pytest.fail("no job was named; nothing to queue")
    )

    brief._handle_telegram_update(_update("/run"), _fb())

    assert all(spec.job in sent[0] for spec in scheduler.SCHEDULES)


def test_run_says_why_it_cannot_queue_rather_than_claiming_it_did(monkeypatch):
    sent = _capture(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "connect", boom)

    brief._handle_telegram_update(_update("/run collect"), _fb())

    assert "connection refused" in sent[0]
    assert "queued" not in sent[0].lower()


def test_the_new_commands_reach_telegram_autocomplete():
    """setMyCommands syncs off BOT_COMMANDS, so a handler missing from that list
    works only for someone who already knows it exists."""
    names = [name for name, _ in brief.BOT_COMMANDS]
    assert "jobs" in names and "run" in names

"""Phase 5 Telegram command handlers: /watch /unwatch /positions /performance."""

import brief
import trading


def _fb():
    return {"focus": [], "mute": [], "notes": []}


def _update(text):
    return {"message": {"text": text, "chat": {"id": brief.TELEGRAM_CHAT_ID}}}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda text: sent.append(text) or True)
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
    monkeypatch.setattr(trading, "resolve_stooq_symbol", lambda t, c, o: None)
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

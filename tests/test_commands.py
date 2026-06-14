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

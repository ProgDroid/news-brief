"""/evidence: picker, per-key listing, since: form and the ev: callback.

No database: db.connect and the store are stubbed. Keys and lines are invented.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import brief
import common
import config
import dated_memory
import db


def _fb():
    return {"focus": [], "mute": [], "notes": []}


def _update(text):
    return {"message": {"text": text, "chat": {"id": config.chat_id()}}}


def _cb(data, msg_id=10):
    return {
        "id": "cbid",
        "data": data,
        "message": {"message_id": msg_id, "chat": {"id": config.chat_id()}},
    }


def _capture(monkeypatch):
    # Both namespaces: direct sends resolve to brief.telegram_send while
    # telegram_send_long goes through common.telegram_send.
    sent = []

    def _send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(brief, "telegram_send", _send)
    monkeypatch.setattr(common, "telegram_send", _send)
    return sent


class _Conn:
    pass


def _stub_db(monkeypatch, *, quiet=("TESTCO",), logged=(), rows=None, calls=None):
    @contextmanager
    def _connect(**kw):
        yield _Conn()

    monkeypatch.setattr(db, "connect", _connect)
    monkeypatch.setattr(brief, "_thesis_keys_quiet", lambda: (list(quiet), None))

    def _keys(conn, *, since):
        if calls is not None:
            calls.append(("keys", since))
        return list(logged)

    def _recent(conn, key, *, since):
        if calls is not None:
            calls.append(("recent", key, since))
        return list(rows or [])

    monkeypatch.setattr(dated_memory, "keys_with_evidence", _keys)
    monkeypatch.setattr(dated_memory, "recent_evidence", _recent)


def _wire(monkeypatch):
    cap = {"buttons": [], "edits": []}

    def _buttons(text, rows):
        cap["buttons"].append((text, rows))
        return 10

    monkeypatch.setattr(brief, "telegram_send_buttons", _buttons)
    monkeypatch.setattr(
        brief,
        "telegram_edit_text",
        lambda mid, text, rows=None: cap["edits"].append((mid, text, rows)),
    )
    monkeypatch.setattr(brief, "telegram_answer_callback", lambda *a, **k: None)
    return cap


def _all_buttons(cap):
    return [b for _t, rows in cap["buttons"] for r in rows for b in r]


def test_evidence_alone_sends_a_picker_of_theses_and_logged_keys(monkeypatch):
    sent = _capture(monkeypatch)
    cap = _wire(monkeypatch)
    _stub_db(monkeypatch, quiet=["TESTCO"], logged=["OLDCO"])
    brief._handle_telegram_update(_update("/evidence"), _fb())
    buttons = _all_buttons(cap)
    assert [b["callback_data"] for b in buttons] == [
        f"ev:{brief._short_id('OLDCO')}",
        f"ev:{brief._short_id('TESTCO')}",
    ]
    assert [b["text"] for b in buttons] == ["📒 OLDCO", "📒 TESTCO"]
    assert sent == []


def test_picker_label_is_cut_to_40_characters(monkeypatch):
    _capture(monkeypatch)
    cap = _wire(monkeypatch)
    key = "cluster:" + "x" * 60
    _stub_db(monkeypatch, quiet=[key])
    brief._evidence_picker_render()
    (b,) = _all_buttons(cap)
    assert len(b["text"]) == 40
    assert b["text"].startswith("📒 cluster:")


def test_picker_with_no_keys_says_so(monkeypatch):
    sent = _capture(monkeypatch)
    cap = _wire(monkeypatch)
    _stub_db(monkeypatch, quiet=[], logged=[])
    brief._evidence_picker_render()
    assert sent == ["📒 No theses yet — set one with /thesis."]
    assert cap["buttons"] == []


def test_picker_with_message_id_edits_in_place(monkeypatch):
    _capture(monkeypatch)
    cap = _wire(monkeypatch)
    _stub_db(monkeypatch, quiet=["TESTCO"])
    brief._evidence_picker_render(42)
    assert cap["buttons"] == []
    assert cap["edits"][0][0] == 42


def test_evidence_keys_is_a_sorted_deduplicated_union(monkeypatch):
    _stub_db(monkeypatch, quiet=["B", "A"], logged=["C", "A"])
    assert brief._evidence_keys(_Conn()) == ["A", "B", "C"]


def test_evidence_keys_asks_the_store_for_the_default_window(monkeypatch):
    calls = []
    _stub_db(monkeypatch, calls=calls)
    brief._evidence_keys(_Conn())
    want = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    assert calls == [("keys", want)]


def test_evidence_with_a_key_lists_rows_newest_first_and_escaped(monkeypatch):
    sent = _capture(monkeypatch)
    year = datetime.now(timezone.utc).year
    rows = [
        {
            "on_date": f"{year}-11-21",
            "direction": "challenges",
            "line": "A <b> B",
            "source": "brief",
        },
        {
            "on_date": f"{year}-11-20",
            "direction": "supports",
            "line": "C",
            "source": "brief",
        },
    ]
    _stub_db(monkeypatch, rows=rows)
    brief._handle_telegram_update(_update("/evidence TESTCO"), _fb())
    text = "\n".join(sent)
    assert "<b>📒 Evidence — TESTCO</b> (last 30 days)" in text
    a = text.index("11-21 ⬇ challenges — A &lt;b&gt; B")
    b = text.index("11-20 ⬆ supports — C")
    assert a < b
    assert f"{year}-11-21" not in text


def test_key_is_escaped_in_header(monkeypatch):
    sent = _capture(monkeypatch)
    _stub_db(monkeypatch, rows=[])
    brief._evidence_render("A<b>")
    assert "A&lt;b&gt;" in "\n".join(sent)
    assert "A<b>" not in "\n".join(sent)


def test_earlier_year_rows_show_the_full_date(monkeypatch):
    sent = _capture(monkeypatch)
    year = datetime.now(timezone.utc).year
    rows = [
        {
            "on_date": f"{year - 1}-12-30",
            "direction": "supports",
            "line": "old",
            "source": "brief",
        }
    ]
    _stub_db(monkeypatch, rows=rows)
    brief._evidence_render("TESTCO", f"{year - 1}-12-01")
    assert f"{year - 1}-12-30 ⬆ supports — old" in "\n".join(sent)


def test_default_evidence_call_uses_the_30_day_window(monkeypatch):
    _capture(monkeypatch)
    calls = []
    _stub_db(monkeypatch, calls=calls)
    brief._handle_telegram_update(_update("/evidence TESTCO"), _fb())
    want = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    assert ("recent", "TESTCO", want) in calls


def test_evidence_since_passes_the_date_to_the_store(monkeypatch):
    sent = _capture(monkeypatch)
    calls = []
    row = {"on_date": "2026-01-05", "direction": "supports", "line": "x", "source": "b"}
    _stub_db(monkeypatch, calls=calls, rows=[row])
    brief._handle_telegram_update(_update("/evidence TESTCO since:2026-01-01"), _fb())
    assert ("recent", "TESTCO", "2026-01-01") in calls
    assert "(since 2026-01-01)" in "\n".join(sent)


@pytest.mark.parametrize(
    "text",
    [
        "/evidence TESTCO since:2026-13-45",
        "/evidence TESTCO since:2026-02-30",
        "/evidence TESTCO since:yesterday",
        "/evidence TESTCO since:",
    ],
)
def test_evidence_since_with_a_bad_date_shows_the_format(monkeypatch, text):
    sent = _capture(monkeypatch)
    calls = []
    _stub_db(monkeypatch, calls=calls)
    brief._handle_telegram_update(_update(text), _fb())
    assert sent == ["Format: <code>/evidence KEY since:YYYY-MM-DD</code>"]
    assert calls == []


def test_evidence_with_no_rows_says_so(monkeypatch):
    sent = _capture(monkeypatch)
    _stub_db(monkeypatch, rows=[])
    brief._handle_telegram_update(_update("/evidence TESTCO"), _fb())
    assert sent == ["No evidence logged for <b>TESTCO</b> in that period."]


def test_unreadable_db_reports_unknown_not_empty(monkeypatch):
    sent = _capture(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "connect", _boom)
    brief._handle_telegram_update(_update("/evidence TESTCO"), _fb())
    text = "\n".join(sent)
    assert "UNKNOWN" in text
    assert "connection refused" in text
    assert "No evidence logged" not in text


def test_unreadable_db_on_the_picker_reports_unknown(monkeypatch):
    sent = _capture(monkeypatch)
    _wire(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("down")

    monkeypatch.setattr(db, "connect", _boom)
    monkeypatch.setattr(brief, "_thesis_keys_quiet", lambda: (["TESTCO"], None))
    brief._handle_telegram_update(_update("/evidence"), _fb())
    assert "UNKNOWN" in "\n".join(sent)


def test_callback_resolves_the_short_id_over_the_live_list(monkeypatch):
    sent = _capture(monkeypatch)
    _wire(monkeypatch)
    calls = []
    _stub_db(monkeypatch, quiet=["AAA", "TESTCO", "ZZZ"], calls=calls)
    brief._handle_callback_query(_cb(f"ev:{brief._short_id('TESTCO')}"), _fb())
    assert [c[1] for c in calls if c[0] == "recent"] == ["TESTCO"]
    assert "No evidence logged for <b>TESTCO</b>" in "\n".join(sent)


def test_stale_callback_says_no_longer_listed(monkeypatch):
    sent = _capture(monkeypatch)
    _wire(monkeypatch)
    calls = []
    _stub_db(monkeypatch, quiet=["TESTCO"], calls=calls)
    brief._handle_callback_query(_cb(f"ev:{brief._short_id('GONECO')}"), _fb())
    assert "That thesis is no longer listed." in "\n".join(sent)
    assert not [c for c in calls if c[0] == "recent"]


def test_evidence_is_in_the_command_menu():
    assert ("evidence", "Thesis evidence log, last 30 days") in brief.BOT_COMMANDS
    assert "/evidence" in brief.HELP_TEXT
    assert "since:" in brief.HELP_TEXT


def test_evidence_days_constant():
    assert brief.EVIDENCE_DAYS == 30

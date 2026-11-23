"""/storyline list|add|retire, the proposal buttons and send_proposal.

No database: db.connect and the store are stubbed. Names are invented.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import brief
import common
import config
import db
import storyline_store as ss

S = ss.Storyline


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


class _Conn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _wire(monkeypatch):
    """Capture every send channel, so 'nothing sent' cannot pass vacuously."""
    cap = {"sent": [], "buttons": [], "edits": [], "conn": _Conn()}

    def _send(text, *a, **k):
        cap["sent"].append(text)
        return True

    def _buttons(text, rows):
        cap["buttons"].append((text, rows))
        return 77

    monkeypatch.setattr(brief, "telegram_send", _send)
    monkeypatch.setattr(common, "telegram_send", _send)
    monkeypatch.setattr(brief, "telegram_send_buttons", _buttons)
    monkeypatch.setattr(common, "telegram_send_buttons", _buttons)
    monkeypatch.setattr(
        brief,
        "telegram_edit_text",
        lambda mid, text, rows=None: cap["edits"].append((mid, text, rows)),
    )
    monkeypatch.setattr(common, "telegram_edit_text", brief.telegram_edit_text)
    monkeypatch.setattr(brief, "telegram_answer_callback", lambda *a, **k: None)

    @contextmanager
    def _connect(**kw):
        yield cap["conn"]

    monkeypatch.setattr(db, "connect", _connect)
    return cap


def _nothing_sent(cap):
    return not (cap["sent"] or cap["buttons"] or cap["edits"])


def test_list_shows_names_status_and_counts(monkeypatch):
    cap = _wire(monkeypatch)
    live = [
        S(1, "Widget tariffs", "d", "active", True, None),
        S(2, "Gizmo strike", "d", "dormant", False, None),
    ]
    monkeypatch.setattr(ss, "live_storylines", lambda conn: live)
    seen = {}

    def _attached(conn, *, since):
        seen["since"] = since
        return {1: 4}

    monkeypatch.setattr(ss, "attached_since", _attached)
    brief._handle_telegram_update(_update("/storyline"), _fb())
    text = "\n".join(cap["sent"])
    line1 = next(ln for ln in text.splitlines() if "Widget tariffs" in ln)
    line2 = next(ln for ln in text.splitlines() if "Gizmo strike" in ln)
    assert "active" in line1 and "📌" in line1 and "4" in line1
    assert "dormant" in line2 and "📌" not in line2 and "0" in line2
    age = datetime.now(timezone.utc) - seen["since"]
    assert 6.99 < age.total_seconds() / 86400 < 7.01


def test_list_word_form_is_the_same_as_bare(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(
        ss,
        "live_storylines",
        lambda conn: [S(1, "Widget tariffs", "", "active", False, None)],
    )
    monkeypatch.setattr(ss, "attached_since", lambda conn, *, since: {})
    brief._handle_telegram_update(_update("/storyline list"), _fb())
    assert "Widget tariffs" in "\n".join(cap["sent"])


def test_list_escapes_html(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(
        ss, "live_storylines", lambda conn: [S(1, "A <b> B", "", "active", False, None)]
    )
    monkeypatch.setattr(ss, "attached_since", lambda conn, *, since: {})
    brief._storyline_list_render()
    assert "A &lt;b&gt; B" in "\n".join(cap["sent"])


def test_list_empty(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(ss, "live_storylines", lambda conn: [])
    monkeypatch.setattr(ss, "attached_since", lambda conn, *, since: {})
    brief._handle_telegram_update(_update("/storyline"), _fb())
    assert cap["sent"] == ["No storylines yet."]


def test_db_error_replies_unavailable(monkeypatch):
    cap = _wire(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "connect", _boom)
    brief._handle_telegram_update(_update("/storyline"), _fb())
    text = "\n".join(cap["sent"])
    assert "Storylines unavailable" in text and "connection refused" in text
    assert "No storylines yet" not in text


def test_add_parses_em_dash_and_hyphen(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_MAX_LIVE", 7)
    calls = []

    def _add(conn, name, description, *, origin, pinned=False, max_live):
        calls.append((name, description, origin, pinned, max_live))
        return ss.Outcome(5, None)

    monkeypatch.setattr(ss, "add_storyline", _add)
    brief._handle_telegram_update(
        _update("/storyline add Widget tariffs — Import duties on widgets"), _fb()
    )
    brief._handle_telegram_update(
        _update("/storyline add Gizmo-strike - Dockworkers walk out"), _fb()
    )
    assert calls == [
        ("Widget tariffs", "Import duties on widgets", "operator", False, 7),
        ("Gizmo-strike", "Dockworkers walk out", "operator", False, 7),
    ]
    assert cap["conn"].commits == 2
    assert any("Widget tariffs" in s for s in cap["sent"])


def test_add_without_separator_replies_usage(monkeypatch):
    cap = _wire(monkeypatch)
    called = []
    monkeypatch.setattr(ss, "add_storyline", lambda *a, **k: called.append(1))
    for text in (
        "/storyline add Widget tariffs",
        "/storyline add Widget tariffs — ",
        "/storyline add — only a description",
        "/storyline add",
    ):
        cap["sent"].clear()
        brief._handle_telegram_update(_update(text), _fb())
        assert len(cap["sent"]) == 1 and "/storyline add" in cap["sent"][0], text
    assert called == []
    assert cap["conn"].commits == 0


def test_add_at_bound_says_retire_first(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_MAX_LIVE", 2)
    monkeypatch.setattr(ss, "add_storyline", lambda *a, **k: ss.Outcome(None, "bound"))
    brief._handle_telegram_update(_update("/storyline add A — b"), _fb())
    assert cap["sent"] == [
        "At the limit of 2 live storylines — retire one first (/storyline retire)."
    ]
    assert cap["conn"].commits == 0


def test_add_duplicate_says_so(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(
        ss, "add_storyline", lambda *a, **k: ss.Outcome(None, "duplicate")
    )
    brief._handle_telegram_update(_update("/storyline add Widget tariffs — b"), _fb())
    (msg,) = cap["sent"]
    assert "already" in msg and "Widget tariffs" in msg


def test_retire_picker_then_tap_retires(monkeypatch):
    cap = _wire(monkeypatch)
    live = [S(3, "Widget tariffs", "", "active", False, None)]
    monkeypatch.setattr(ss, "live_storylines", lambda conn: live)
    brief._handle_telegram_update(_update("/storyline retire"), _fb())
    (_t, rows) = cap["buttons"][0]
    (b,) = [b for r in rows for b in r]
    assert b["callback_data"] == "slr:3" and "Widget tariffs" in b["text"]
    calls = []

    def _retire(conn, sid, *, on_date):
        calls.append((sid, on_date))
        return True

    monkeypatch.setattr(ss, "retire_storyline", _retire)
    brief._handle_callback_query(_cb("slr:3", msg_id=55), _fb())
    today = datetime.now(timezone.utc).date().isoformat()
    assert calls == [(3, today)]
    assert cap["conn"].commits == 1
    assert cap["edits"][-1][:2] == (55, "Retired: Widget tariffs")


def test_retire_tap_on_a_gone_storyline_says_so(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(ss, "live_storylines", lambda conn: [])
    monkeypatch.setattr(ss, "retire_storyline", lambda *a, **k: False)
    brief._handle_callback_query(_cb("slr:3"), _fb())
    assert "Retired:" not in cap["edits"][-1][1]


def test_retire_picker_empty(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(ss, "live_storylines", lambda conn: [])
    brief._storyline_retire_picker()
    assert cap["sent"] == ["No storylines yet."]
    assert cap["buttons"] == []


def _proposal(monkeypatch, name="Gizmo strike"):
    monkeypatch.setattr(ss, "get_proposal", lambda conn, pid: {"id": pid, "name": name})


def test_accept_tap_edits_message_to_accepted(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_MAX_LIVE", 9)
    _proposal(monkeypatch)
    calls = []

    def _decide(conn, pid, accept, *, max_live):
        calls.append((pid, accept, max_live))
        return ss.Outcome(4, None)

    monkeypatch.setattr(ss, "decide_proposal", _decide)
    brief._handle_callback_query(_cb("slp:a:8", msg_id=31), _fb())
    assert calls == [(8, True, 9)]
    assert cap["conn"].commits == 1
    assert cap["edits"] == [(31, "✅ Accepted: Gizmo strike", [])]


def test_second_accept_tap_says_already(monkeypatch):
    cap = _wire(monkeypatch)
    _proposal(monkeypatch)
    monkeypatch.setattr(
        ss, "decide_proposal", lambda *a, **k: ss.Outcome(None, "already accepted")
    )
    brief._handle_callback_query(_cb("slp:a:8", msg_id=31), _fb())
    (mid, text, rows) = cap["edits"][-1]
    assert mid == 31 and text == "Already accepted."


def test_accept_at_bound_keeps_buttons(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(common, "STORYLINE_MAX_LIVE", 2)
    _proposal(monkeypatch)
    monkeypatch.setattr(
        ss, "decide_proposal", lambda *a, **k: ss.Outcome(None, "bound")
    )
    brief._handle_callback_query(_cb("slp:a:8", msg_id=31), _fb())
    (mid, text, rows) = cap["edits"][-1]
    assert "At the limit of 2 live storylines" in text
    assert rows is None  # None leaves the keyboard in place
    assert cap["conn"].commits == 0


def test_accept_duplicate_keeps_buttons(monkeypatch):
    cap = _wire(monkeypatch)
    _proposal(monkeypatch)
    monkeypatch.setattr(
        ss, "decide_proposal", lambda *a, **k: ss.Outcome(None, "duplicate")
    )
    brief._handle_callback_query(_cb("slp:a:8", msg_id=31), _fb())
    (mid, text, rows) = cap["edits"][-1]
    assert "Gizmo strike" in text and "already" in text and rows is None


def test_reject_tap(monkeypatch):
    cap = _wire(monkeypatch)
    _proposal(monkeypatch)
    calls = []

    def _decide(conn, pid, accept, *, max_live):
        calls.append((pid, accept))
        return ss.Outcome(None, None)

    monkeypatch.setattr(ss, "decide_proposal", _decide)
    brief._handle_callback_query(_cb("slp:r:8", msg_id=31), _fb())
    assert calls == [(8, False)]
    assert cap["conn"].commits == 1
    assert cap["edits"] == [(31, "❌ Rejected: Gizmo strike", [])]


def test_unknown_proposal_tap(monkeypatch):
    cap = _wire(monkeypatch)
    monkeypatch.setattr(ss, "get_proposal", lambda conn, pid: None)
    called = []
    monkeypatch.setattr(ss, "decide_proposal", lambda *a, **k: called.append(1))
    brief._handle_callback_query(_cb("slp:a:8", msg_id=31), _fb())
    assert called == []
    assert cap["edits"][-1][1] == "That proposal no longer exists."


def test_malformed_callback_data_changes_nothing(monkeypatch):
    cap = _wire(monkeypatch)
    for data in ("slp:x:8", "slp:a:zz", "slp:a", "slr:", "slr:abc"):
        brief._handle_callback_query(_cb(data), _fb())
    assert _nothing_sent(cap)
    assert cap["conn"].commits == 0


def test_send_proposal_escapes_html_and_carries_both_buttons(monkeypatch):
    cap = _wire(monkeypatch)
    mid = brief.send_proposal(
        {"id": 12, "name": "A <b> B", "description": "x & y", "pinned": False}
    )
    assert mid == 77
    ((text, rows),) = cap["buttons"]
    assert text == "🧵 Proposed storyline: <b>A &lt;b&gt; B</b>\nx &amp; y"
    buttons = [b for r in rows for b in r]
    assert [(b["text"], b["callback_data"]) for b in buttons] == [
        ("Accept", "slp:a:12"),
        ("Reject", "slp:r:12"),
    ]


def test_send_proposal_marks_pinned(monkeypatch):
    cap = _wire(monkeypatch)
    brief.send_proposal({"id": 1, "name": "N", "description": "d", "pinned": True})
    assert cap["buttons"][0][0].endswith("d · 📌 pinned")


def test_storyline_is_in_bot_commands_and_help():
    assert ("storyline", "Storylines: list, add, retire") in brief.BOT_COMMANDS
    assert "/storyline" in brief.HELP_TEXT


def test_knob_default_and_compose_anchor():
    assert common.KNOBS["STORYLINE_MAX_LIVE"].default == 60
    with open("docker-compose.yml", encoding="utf-8") as f:
        assert "STORYLINE_MAX_LIVE=${STORYLINE_MAX_LIVE:-}" in f.read()

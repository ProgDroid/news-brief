"""Telegram delivery formatting, atomic persistence, and command isolation."""

import json

import brief
import config


# ── sanitise_html ─────────────────────────────────────────────────────────────
def test_sanitise_strips_fences_and_disallowed_tags():
    raw = "```html\n<b>x</b><div>y</div><script>z</script>\n```"
    assert brief.sanitise_html(raw) == "<b>x</b>yz"


def test_sanitise_keeps_allowed_tags():
    raw = '<b>bold</b> <i>it</i> <a href="https://x.test">link</a> <code>c</code>'
    assert brief.sanitise_html(raw) == raw


def test_sanitise_script_not_confused_with_s_tag():
    assert brief.sanitise_html("<s>ok</s><script>bad</script>") == "<s>ok</s>bad"


def test_sanitise_escapes_bare_lt_in_prose():
    # "oil <$60" is the canonical 400-the-chunk case: a bare < is not tag-shaped,
    # so the disallowed-tag strip leaves it, and Telegram's HTML parser rejects it.
    assert brief.sanitise_html("oil <$60 soon") == "oil &lt;$60 soon"


def test_sanitise_escapes_bare_ampersand_and_gt():
    assert brief.sanitise_html("AT&T yields > 5%") == "AT&amp;T yields &gt; 5%"


def test_sanitise_preserves_existing_entities():
    # An already-escaped entity must not be double-escaped.
    assert brief.sanitise_html("5 &lt; 10 &amp; rising") == "5 &lt; 10 &amp; rising"


def test_sanitise_escapes_bare_chars_around_stripped_tag():
    assert brief.sanitise_html("<div>oil <$60</div>") == "oil &lt;$60"


def test_sanitise_keeps_allowed_tag_with_ampersand_url():
    # & inside a surviving allowed tag's attributes is left intact (not escaped).
    raw = '<a href="https://x.test?a=1&b=2">link</a>'
    assert brief.sanitise_html(raw) == raw


# ── split_html_message ────────────────────────────────────────────────────────
def test_split_short_text_passthrough():
    assert brief.split_html_message("hello", max_len=40) == ["hello"]


def test_split_on_paragraphs():
    text = ("A" * 30) + "\n\n" + ("B" * 30)
    chunks = brief.split_html_message(text, max_len=40)
    assert chunks == ["A" * 30, "B" * 30]


def test_split_hard_wraps_oversized_paragraph():
    text = "C" * 100
    chunks = brief.split_html_message(text, max_len=40)
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks) == text


def test_split_hard_wrap_preserves_whitespace_at_the_slice_boundary():
    # max_len=36 puts the slice boundary exactly on the space: the first slice
    # is "A"*35 + " ", which .strip() would silently eat, merging the words.
    text = "A" * 35 + " " + "B" * 30
    chunks = brief.split_html_message(text, max_len=36)
    assert "".join(chunks) == text
    assert all(len(c) <= 36 for c in chunks)


# ── feedback_summary escaping ─────────────────────────────────────────────────
def test_feedback_summary_escapes_stored_user_text():
    fb = {"focus": [], "mute": [], "notes": ["watch JPY <155 & oil"]}
    out = brief.feedback_summary(fb)
    assert "&lt;155" in out
    assert "&amp;" in out
    assert "<155" not in out


# ── atomic JSON io ────────────────────────────────────────────────────────────
def test_write_and_load_roundtrip(tmp_path):
    path = tmp_path / "nested" / "data.json"
    brief._write_json_atomic(path, {"a": 1})
    assert brief._load_json_or(path, None) == {"a": 1}
    assert not path.with_suffix(".json.tmp").exists()


def test_load_missing_returns_default(tmp_path):
    assert brief._load_json_or(tmp_path / "absent.json", {"d": True}) == {"d": True}


def test_load_corrupt_quarantines_and_returns_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"truncated": ')  # simulates a crash mid-write
    assert brief._load_json_or(path, {}) == {}
    assert not path.exists()
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == '{"truncated": '


# ── Telegram command handling ─────────────────────────────────────────────────
def _capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "telegram_send", lambda t: sent.append(t) or True)
    return sent


def test_handle_update_escapes_user_echo(monkeypatch):
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(config, "chat_id", lambda: "123")
    fb = {"focus": [], "mute": [], "notes": []}
    update = {"message": {"text": "/note watch JPY <155", "chat": {"id": 123}}}
    fb = brief._handle_telegram_update(update, fb)
    assert fb["notes"] == ["watch JPY <155"]  # stored raw
    assert "&lt;155" in sent[0]  # echoed escaped
    assert "<155" not in sent[0]


def test_handle_update_ignores_foreign_chat(monkeypatch):
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(config, "chat_id", lambda: "123")
    fb = {"focus": [], "mute": [], "notes": []}
    update = {"message": {"text": "/reset", "chat": {"id": 999}}}
    assert brief._handle_telegram_update(update, fb) == fb
    assert sent == []


def test_poison_message_does_not_jam_offset(monkeypatch, tmp_path):
    """One crashing command must not block later ones nor the offset save."""
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(config, "chat_id", lambda: "123")
    monkeypatch.setattr(brief, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(brief, "FEEDBACK_FILE", tmp_path / "feedback.json")
    # fb without a "focus" key makes /focus raise KeyError — the poison
    updates = [
        {"update_id": 7, "message": {"text": "/focus boom", "chat": {"id": 123}}},
        {"update_id": 8, "message": {"text": "/help", "chat": {"id": 123}}},
    ]

    brief._drain_update_batch(updates, {}, 0)

    assert any("Command failed" in m for m in sent)  # poison reported
    assert brief.HELP_TEXT in sent  # later update still handled
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["tg_offset"] == 9  # offset advanced past the poison


def test_offset_saved_without_clobbering_other_state(monkeypatch, tmp_path):
    """The offset save must merge into existing state, not overwrite it."""
    _capture_sends(monkeypatch)
    monkeypatch.setattr(config, "chat_id", lambda: "123")
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(brief, "STATE_FILE", state_file)
    monkeypatch.setattr(brief, "FEEDBACK_FILE", tmp_path / "feedback.json")
    state_file.write_text(json.dumps({"batch_id": "batch_abc", "tg_offset": 5}))
    updates = [{"update_id": 5, "message": {"text": "/help", "chat": {"id": 123}}}]
    offset = (brief.load_state() or {}).get("tg_offset", 0)

    brief._drain_update_batch(updates, {"focus": [], "mute": [], "notes": []}, offset)

    state = json.loads(state_file.read_text())
    assert state["tg_offset"] == 6
    assert state["batch_id"] == "batch_abc"  # survived — the A2 regression test


# ── t212 auth ─────────────────────────────────────────────────────────────────
def test_t212_auth_header_is_basic_base64(monkeypatch):
    import common

    monkeypatch.setattr(common, "T212_API_KEY_ID", "kid")
    monkeypatch.setattr(common, "T212_API_KEY", "secret")
    assert brief.t212_auth_header() == "Basic a2lkOnNlY3JldA=="

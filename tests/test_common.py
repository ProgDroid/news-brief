"""common.py: shared infra smoke + relocated behaviour."""

import importlib

import common


def test_common_imports_and_exposes_infra():
    assert callable(common.telegram_send)
    assert callable(common._write_json_atomic)
    assert callable(common._load_json_or)
    assert callable(common.sanitise_html)
    assert callable(common.split_html_message)
    assert callable(common.t212_auth_header)
    assert common.MODEL  # non-empty model id
    assert str(common.SIGNALS_DIR).endswith("signals")


def test_model_defaults_to_sonnet_5():
    assert common.MODEL == "claude-sonnet-5"


def test_model_honours_newsbrief_model_override(monkeypatch):
    # Constants are read at import time, so overriding requires a reload. Restore
    # the default afterwards so a mutated global doesn't leak into other tests.
    monkeypatch.setenv("NEWSBRIEF_MODEL", "claude-opus-4-8")
    try:
        importlib.reload(common)
        assert common.MODEL == "claude-opus-4-8"
    finally:
        monkeypatch.delenv("NEWSBRIEF_MODEL", raising=False)
        importlib.reload(common)
    assert common.MODEL == "claude-sonnet-5"


def test_load_json_or_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    assert common._load_json_or(p, {"d": 1}) == {"d": 1}  # missing -> default
    common._write_json_atomic(p, {"a": 2})
    assert common._load_json_or(p, None) == {"a": 2}


def test_sanitise_html_strips_disallowed_tags():
    out = common.sanitise_html("<b>ok</b><script>bad()</script>")
    assert "<b>ok</b>" in out
    assert "script" not in out


def test_sanitise_html_keeps_http_and_https_links():
    out = common.sanitise_html('<a href="https://example.com">ok</a>')
    assert 'href="https://example.com"' in out
    out2 = common.sanitise_html('<a href="http://example.com">ok</a>')
    assert 'href="http://example.com"' in out2


def test_sanitise_html_defangs_javascript_scheme():
    out = common.sanitise_html('<a href="javascript:alert(1)">click</a>')
    # the <a>...</a> pair survives (no orphaned </a>) but the scheme is defanged
    assert "javascript:" not in out
    assert 'href="#"' in out
    assert "<a " in out and "</a>" in out
    assert ">click</a>" in out


def test_sanitise_html_defangs_data_scheme():
    out = common.sanitise_html("<a href='data:text/html;base64,PHNjcmlwdD4='>x</a>")
    assert "data:" not in out
    assert "href='#'" in out


def test_telegram_send_long_splits_oversized_message(monkeypatch):
    # A message longer than the Telegram cap must be split into several sends,
    # not passed through as one 400-triggering payload.
    sent = []
    monkeypatch.setattr(
        common, "telegram_send", lambda chunk: sent.append(chunk) or True
    )
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)

    para = "x" * 3000
    text = f"{para}\n\n{para}\n\n{para}"  # ~9000 chars, no single para over the cap
    assert common.telegram_send_long(text) is True
    assert len(sent) >= 2
    assert all(len(c) <= common.TELEGRAM_MAX_LEN for c in sent)


def test_telegram_send_long_short_message_sends_once(monkeypatch):
    sent = []
    monkeypatch.setattr(
        common, "telegram_send", lambda chunk: sent.append(chunk) or True
    )
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)

    assert common.telegram_send_long("hello") is True
    assert sent == ["hello"]


def test_telegram_send_long_reports_failure(monkeypatch):
    # If any chunk fails to send, the overall result is False so callers can alert.
    monkeypatch.setattr(common, "telegram_send", lambda chunk: False)
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)
    assert common.telegram_send_long("hello") is False


def test_log_handlers_include_rotating_file_handler():
    # newsbrief.log must rotate, not grow unbounded for the life of the container.
    from logging.handlers import RotatingFileHandler

    handlers = common._log_handlers()
    try:
        rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        assert rotating, "expected a RotatingFileHandler for bounded log growth"
        assert rotating[0].maxBytes > 0
        assert rotating[0].backupCount > 0
    finally:
        for h in handlers:
            h.close()

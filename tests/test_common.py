"""common.py: shared infra smoke + relocated behaviour."""

import importlib

import pytest

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


def test_the_environment_no_longer_overrides_a_knob_at_import(monkeypatch):
    """NEWSBRIEF_MODEL used to be read into a constant at import, so overriding
    it needed a reload. It is bootstrap input for the settings importer now and
    nothing else, so a reload with the variable set must NOT change the value —
    otherwise the environment would still be a second, competing source of truth
    and the bug class this phase retires would only be half gone.

    The row-backed override is covered in test_config.py, against a real
    database, which is the only place it can be tested honestly.
    """
    monkeypatch.setenv("NEWSBRIEF_MODEL", "claude-opus-4-8")
    try:
        importlib.reload(common)
        assert common.MODEL == "claude-sonnet-5"
    finally:
        monkeypatch.delenv("NEWSBRIEF_MODEL", raising=False)
        importlib.reload(common)


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


def test_append_thesis_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "THESIS_LOG_FILE", tmp_path / "thesis_log.json")
    common.append_thesis({"id": "t1", "market_id": "m", "p_hat": 0.8})
    common.append_thesis({"id": "t2", "market_id": "n", "p_hat": None})
    log = common.load_thesis_log()
    assert [r["id"] for r in log] == ["t1", "t2"]


def test_load_thesis_log_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "THESIS_LOG_FILE", tmp_path / "nope.json")
    assert common.load_thesis_log() == []


# ── Knobs: coercion and the __getattr__ seam ─────────────────────────────────
# These need no database. `conftest` pins the settings map to empty, so every
# knob resolves through the real coercion path to its declared default — which
# is exactly what a first boot with no rows does.


def test_every_knob_resolves_to_its_declared_default():
    for name, spec in common.KNOBS.items():
        assert getattr(common, name) == spec.default, name


def test_an_unknown_knob_raises_rather_than_resolving():
    """The registry is what makes a typo loud. Without it `common.PG_A_ENABLD`
    would be a lookup that misses and returns a default, and the sleeve would
    read as disabled for a reason nobody could see."""
    with pytest.raises(AttributeError, match="PG_A_ENABLD"):
        common.PG_A_ENABLD


@pytest.mark.parametrize(
    "kind, raw, expected",
    [
        (bool, "1", True),
        (bool, "true", True),
        (bool, " ON ", True),
        (bool, "0", False),
        (bool, "", False),
        (bool, "no", False),
        (int, "21", 21),
        (int, "21.0", 21),  # a float-looking edit of an int knob still reads
        (float, "0.75", 0.75),
        (str, " claude-opus-5 ", "claude-opus-5"),
    ],
)
def test_coercion(kind, raw, expected):
    assert common.coerce_knob(common.Knob(kind, "SENTINEL"), raw) == expected


@pytest.mark.parametrize("kind, raw", [(int, "abc"), (float, "1,5"), (int, "")])
def test_a_malformed_value_falls_back_to_the_default(kind, raw):
    """A fat-fingered row must not be able to take down a job: every knob has a
    safe default by construction, so the default is the honest answer."""
    assert common.coerce_knob(common.Knob(kind, 7), raw) == 7


def test_a_bool_knob_never_falls_back():
    """Anything unrecognised is False, not the default. A live-money flag whose
    stored value is gibberish must read OFF, and `PG_LIVE_ENABLED` defaulting to
    False is not something to rely on if the default ever changes."""
    assert common.coerce_knob(common.Knob(bool, True), "banana") is False

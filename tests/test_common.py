"""common.py: shared infra smoke + relocated behaviour."""

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


def test_load_json_or_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    assert common._load_json_or(p, {"d": 1}) == {"d": 1}  # missing -> default
    common._write_json_atomic(p, {"a": 2})
    assert common._load_json_or(p, None) == {"a": 2}


def test_sanitise_html_strips_disallowed_tags():
    out = common.sanitise_html("<b>ok</b><script>bad()</script>")
    assert "<b>ok</b>" in out
    assert "script" not in out


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

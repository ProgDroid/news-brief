"""Theses: the /thesis-written theses.json merged with the operator-pushed
theses_portfolio.json (news-brief-ajf)."""

import json

import pytest

import brief
import common
import config


@pytest.fixture
def files(monkeypatch, tmp_path):
    local = tmp_path / "theses.json"
    pushed = tmp_path / "theses_portfolio.json"
    monkeypatch.setattr(brief, "THESIS_FILE", local)
    monkeypatch.setattr(brief, "PORTFOLIO_THESIS_FILE", pushed)
    return local, pushed


@pytest.fixture
def sent(monkeypatch):
    out = []

    def _send(text):
        out.append(text)
        return True

    monkeypatch.setattr(brief, "telegram_send", _send)
    monkeypatch.setattr(common, "telegram_send", _send)
    return out


def _push(path, theses, version=1):
    path.write_text(
        json.dumps(
            {"version": version, "generated": "2026-11-20T21:00:00Z", "theses": theses}
        ),
        encoding="utf-8",
    )


def test_pushed_line_wins_a_key_clash_and_both_sides_survive(files, sent):
    local, pushed = files
    local.write_text(json.dumps({"SHEL": "local shel", "BP": "local bp"}))
    _push(pushed, {"SHEL": "pushed shel", "cluster:energy": "pushed cluster"})

    assert brief.load_theses() == {
        "SHEL": "pushed shel",
        "BP": "local bp",
        "cluster:energy": "pushed cluster",
    }
    assert sent == []


def test_missing_pushed_file_is_reported_and_returns_local(files, sent):
    """A push that never lands must not drop the pushed theses silently
    (user decision 2026-11-18)."""
    local, _ = files
    local.write_text(json.dumps({"BP": "local bp"}))

    assert brief.load_theses() == {"BP": "local bp"}
    assert len(sent) == 1
    assert "theses_portfolio.json" in sent[0] and "missing" in sent[0]


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        json.dumps({"version": 2, "theses": {"SHEL": "x"}}),
        json.dumps({"version": 1, "theses": ["SHEL", "x"]}),
        json.dumps({"version": 1, "theses": {"SHEL": 3}}),
        json.dumps({"version": 1}),
        json.dumps(["not", "an", "object"]),
    ],
    ids=["bad-json", "unknown-version", "list", "non-str-value", "no-theses", "array"],
)
def test_malformed_pushed_file_is_reported_not_fatal(files, sent, body):
    local, pushed = files
    local.write_text(json.dumps({"BP": "local bp"}))
    pushed.write_text(body, encoding="utf-8")

    assert brief.load_theses() == {"BP": "local bp"}
    assert len(sent) == 1
    assert "theses_portfolio.json" in sent[0]
    # Left in place, so the next push simply replaces it.
    assert pushed.read_text(encoding="utf-8") == body


def test_thesis_command_never_writes_pushed_lines_into_theses_json(files, sent):
    local, pushed = files
    local.write_text(json.dumps({"BP": "local bp"}))
    _push(pushed, {"SHEL": "pushed shel"})

    update = {
        "message": {"text": "/thesis XOM = long", "chat": {"id": config.chat_id()}}
    }
    brief._handle_telegram_update(update, {"focus": [], "mute": [], "notes": []})

    assert json.loads(local.read_text()) == {"BP": "local bp", "XOM": "long"}
    assert json.loads(pushed.read_text())["theses"] == {"SHEL": "pushed shel"}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _position(ticker, value):
    return {
        "instrument": {"ticker": ticker, "name": ticker.lower()},
        "walletImpact": {"currentValue": value, "unrealizedProfitLoss": 0},
    }


def test_small_line_with_a_thesis_reaches_the_prompt(monkeypatch, files, sent):
    _, pushed = files
    _push(pushed, {"TINY_US_EQ": "tiny thesis"})
    monkeypatch.setattr(brief, "T212_API_KEY", "k")
    monkeypatch.setattr(brief, "t212_auth_header", lambda: "Basic x")
    positions = [
        _position("BIG_US_EQ", 990.0),
        _position("TINY_US_EQ", 4.0),  # 0.4%: under the dust cut, has a thesis
        _position("DUST_US_EQ", 3.0),  # 0.3%: under the dust cut, no thesis
    ]
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(positions))

    out = brief.fetch_portfolio_weights()

    assert "BIG_US_EQ" in out
    assert "TINY_US_EQ (tiny_us_eq): 0.4% — tiny thesis" in out
    assert "DUST_US_EQ" not in out


def test_thesis_ack_says_when_the_pushed_file_overrides_it(files, sent):
    """r1w: /thesis on a key the pushed file also holds replied 'set' though
    the pushed line wins in the brief."""
    local, pushed = files
    _push(pushed, {"SHEL": "pushed shel"})
    update = {
        "message": {"text": "/thesis SHEL = mine", "chat": {"id": config.chat_id()}}
    }
    brief._handle_telegram_update(update, {"focus": [], "mute": [], "notes": []})
    assert any("overridden by theses_portfolio.json" in m for m in sent)
    sent.clear()
    update["message"]["text"] = "/thesis BP = mine"
    brief._handle_telegram_update(update, {"focus": [], "mute": [], "notes": []})
    assert not any("overridden" in m for m in sent)

"""news-brief-qap: the host check for the real-time brief path.

Every other test stubs the HTTP call; this script is what puts the real-time
path in front of the real API before the bead closes (red-team objection 3).
"""

import brief
from scripts import replay_brief_realtime as replay


def _resp(stop_reason, text):
    return {
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _answer(resp):
    return lambda payload, **kw: resp


def _never_send(*a, **k):
    raise AssertionError("the replay must not send anything to Telegram")


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "DATA_DIR", tmp_path)
    for name in ("telegram_send", "telegram_send_long", "telegram_alert"):
        monkeypatch.setattr(brief, name, _never_send)


def test_replay_prints_a_summary_and_delivers_nothing(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    brief.save_brief_request({"messages": []}, "2000-01-01")
    before = sorted(p.name for p in tmp_path.iterdir())

    code = replay.main(["2000-01-01"], call=_answer(_resp("end_turn", "Brief.")))

    assert code == 0
    out = capsys.readouterr().out
    assert "day=2000-01-01" in out and "ok=True" in out and "chars=6" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # wrote nothing


def test_replay_reports_a_failed_call(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    brief.save_brief_request({"messages": []}, "2000-01-01")

    code = replay.main(["2000-01-01"], call=_answer(_resp("max_tokens", "cut")))

    assert code == 1
    assert "ok=False" in capsys.readouterr().out


def test_replay_without_a_saved_request_exits_2(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)

    assert replay.main(["2000-01-01"]) == 2
    assert "brief_request-2000-01-01.json" in capsys.readouterr().out

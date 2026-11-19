"""Once-per-row alert planning for watch event rows (news-brief-8lp)."""

from watch import (
    STATE_PREFIX,
    EventRow,
    Item,
    Verdict,
    format_alert,
    format_problems,
    plan_alerts,
    strength,
    trigger_hash,
)

E1 = EventRow("e1", "Example ceasefire signed", "A ceasefire is formally signed.")
LINK = Item("Truce agreed", "https://example.com/1", "Wire")


def _v(verdict, evidence=(LINK,), row_id="e1"):
    return Verdict(row_id, verdict, list(evidence), "reason")


def _state(strength_, row=E1):
    return {STATE_PREFIX + row.id: {"hash": trigger_hash(row), "strength": strength_}}


def test_strength_levels():
    assert strength(_v("triggered")) == 2
    assert strength(_v("unclear")) == 1
    assert strength(_v("unclear", evidence=())) == 0
    assert strength(_v("not", evidence=())) == 0
    assert strength(_v("invalid", evidence=())) == 0


def test_first_trigger_alerts():
    alerts, stale = plan_alerts([E1], [_v("triggered")], {})
    assert [(a.row.id, a.key, a.value) for a in alerts] == [
        ("e1", "watch.alerted.e1", {"hash": trigger_hash(E1), "strength": 2})
    ]
    assert stale == []


def test_repeat_at_same_strength_is_silent():
    alerts, _ = plan_alerts([E1], [_v("triggered")], _state(2))
    assert alerts == []


def test_edited_trigger_alerts_again():
    edited = EventRow("e1", E1.name, "A ceasefire is signed AND ratified.")
    alerts, _ = plan_alerts([edited], [_v("triggered")], _state(2))
    assert len(alerts) == 1


def test_unclear_then_triggered_alerts_again():
    alerts, _ = plan_alerts([E1], [_v("triggered")], _state(1))
    assert len(alerts) == 1 and alerts[0].value["strength"] == 2


def test_triggered_then_unclear_or_not_is_silent():
    assert plan_alerts([E1], [_v("unclear")], _state(2))[0] == []
    assert plan_alerts([E1], [_v("not", evidence=())], _state(2))[0] == []


def test_not_and_unclear_without_evidence_never_alert():
    assert plan_alerts([E1], [_v("not", evidence=())], {})[0] == []
    assert plan_alerts([E1], [_v("unclear", evidence=())], {})[0] == []


def test_removed_row_key_is_returned_for_clearing():
    state = {**_state(2), "watch.alerted.gone": {"hash": "x", "strength": 2}}
    state["unrelated.key"] = 1
    _, stale = plan_alerts([E1], [_v("not", evidence=())], state)
    assert stale == ["watch.alerted.gone"]


def test_format_alert_escapes_html():
    row = EventRow("e<1>", "A<b>&", "t")
    item = Item("T<i>", "https://example.com/?a=1&b=2", "W&W")
    (alert,), _ = plan_alerts([row], [Verdict("e<1>", "triggered", [item], "x<y")], {})
    msg = format_alert(alert)
    assert "A&lt;b&gt;&amp;" in msg and "e&lt;1&gt;" in msg and "x&lt;y" in msg
    assert "<b>&" not in msg and "T&lt;i&gt;" in msg
    assert 'href="https://example.com/?a=1&amp;b=2"' in msg
    assert "TRIGGERED" in msg


def test_format_alert_marks_unclear():
    (alert,), _ = plan_alerts([E1], [_v("unclear")], {})
    assert "UNCLEAR" in format_alert(alert)


def test_format_problems_none_when_nothing_to_report():
    assert format_problems([], []) is None


def test_format_problems_lists_skipped_rows_and_invalid_verdicts():
    msg = format_problems(
        [("p9", "unknown kind 'x'")],
        [Verdict("e2", "invalid", [], "cited unknown labels ['I9']")],
    )
    assert "p9" in msg and "unknown kind" in msg
    assert "e2" in msg and "I9" in msg


def test_skipped_event_row_keeps_its_key():
    """A row the loader skipped as malformed is still in the file: fixing the
    typo must not re-ring an old trigger (red-team, 2026-11-19)."""
    state = {**_state(2), "watch.alerted.typo": {"hash": "x", "strength": 2}}
    _, stale = plan_alerts(
        [E1], [_v("not", evidence=())], state, keep_ids={"e1", "typo"}
    )
    assert stale == []


def test_format_alert_escapes_the_outlet():
    item = Item("t", "https://example.com/", "W&W")
    (alert,), _ = plan_alerts([E1], [Verdict("e1", "triggered", [item], "r")], {})
    assert "W&amp;W" in format_alert(alert)


def test_format_problems_escapes_every_field():
    msg = format_problems(
        [("<id>", "bad & worse")], [Verdict("e<2>", "invalid", [], "x<y")]
    )
    assert "&lt;id&gt;" in msg and "bad &amp; worse" in msg
    assert "e&lt;2&gt;" in msg and "x&lt;y" in msg

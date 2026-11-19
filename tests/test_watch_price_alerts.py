"""Price alert planning (news-brief-vap): alert on crossing, re-arm when the row
stops firing (user, 2026-11-19). Rows are made up."""

from watch import (
    PRICE_STATE_PREFIX,
    PriceRow,
    PriceVerdict,
    WatchFile,
    format_price_alert,
    format_problems,
    keep_ids,
    plan_price_alerts,
    price_hash,
)

P1 = PriceRow("p1", "Example dip", "add", "QQQ", "below", 400.0)
KEY = PRICE_STATE_PREFIX + "p1"
KEEP = {"p1"}


def _v(state, row_id="p1", detail="QQQ 390.00 vs below 400 (-2.5%) @ 2026-11-18"):
    return PriceVerdict(row_id, state, detail)


def _stored(fired, row=P1):
    return {PRICE_STATE_PREFIX + row.id: {"hash": price_hash(row), "fired": fired}}


def test_crossing_fires_once_then_silent_while_fired():
    alerts, updates, stale = plan_price_alerts([P1], [_v("fired")], {}, KEEP)
    assert [(a.key, a.value) for a in alerts] == [
        (KEY, {"hash": price_hash(P1), "fired": True})
    ]
    assert updates == {} and stale == []
    alerts, updates, _ = plan_price_alerts([P1], [_v("fired")], _stored(True), KEEP)
    assert alerts == [] and updates == {}


def test_not_rearms_and_a_recross_fires_again():
    alerts, updates, _ = plan_price_alerts([P1], [_v("not")], _stored(True), KEEP)
    assert alerts == []
    assert updates == {KEY: {"hash": price_hash(P1), "fired": False}}
    alerts, _, _ = plan_price_alerts([P1], [_v("fired")], _stored(False), KEEP)
    assert len(alerts) == 1


def test_not_when_already_armed_writes_nothing():
    _, updates, _ = plan_price_alerts([P1], [_v("not")], _stored(False), KEEP)
    assert updates == {}


def test_edited_level_rearms():
    edited = PriceRow("p1", P1.name, P1.action, P1.signal, P1.condition, 380.0)
    alerts, _, _ = plan_price_alerts([edited], [_v("fired")], _stored(True), KEEP)
    assert len(alerts) == 1


def test_unknown_changes_nothing():
    state = _stored(True)
    alerts, updates, stale = plan_price_alerts([P1], [_v("unknown")], state, KEEP)
    assert alerts == [] and updates == {} and stale == []


def test_removed_row_key_cleared_but_skipped_row_key_kept():
    state = {
        **_stored(True),
        PRICE_STATE_PREFIX + "gone": {"hash": "x", "fired": True},
        PRICE_STATE_PREFIX + "typo": {"hash": "x", "fired": True},
        "watch.alerted.e1": {"hash": "x", "strength": 2},  # another stage's key
    }
    _, _, stale = plan_price_alerts([P1], [_v("not")], state, {"p1", "typo"})
    assert stale == [PRICE_STATE_PREFIX + "gone"]


def test_keep_ids_excludes_positional_skips():
    wf = WatchFile(
        events=[], prices=[P1], skipped=[("typo", "unknown kind"), ("#3", "no id")]
    )
    assert keep_ids(wf) == {"p1", "typo"}


def test_format_price_alert_escapes_and_says_fired():
    row = PriceRow("p<1>", "A&B", "add", "QQQ", "below", 400.0)
    (alert,), _, _ = plan_price_alerts(
        [row], [_v("fired", row_id="p<1>")], {}, {"p<1>"}
    )
    msg = format_price_alert(alert)
    assert "FIRED" in msg and "A&amp;B" in msg and "p&lt;1&gt;" in msg
    assert "QQQ 390.00 vs below 400" in msg


def test_format_problems_lists_unknown_prices():
    msg = format_problems([], [], [_v("unknown", detail="QQQ: fetch failed")])
    assert "p1" in msg and "QQQ: fetch failed" in msg
    assert format_problems([], [], []) is None

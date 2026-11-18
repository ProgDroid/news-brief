"""The operator-pushed watch file loader (news-brief-8lp).

Rows are made up, like the bead's example. Never put a real watch row here:
this repository is public.
"""

import json

import pytest

from watch import EventRow, PriceRow, WatchFile, WatchFileError, load_watch

EV_E1 = {
    "id": "e1",
    "kind": "event",
    "name": "Example ceasefire signed",
    "trigger": "A ceasefire or framework agreement is formally signed by both parties.",
}
PRICE_P1 = {
    "id": "p1",
    "kind": "price",
    "action": "rebuy",
    "name": "Example trend turned",
    "signal": "SPY",
    "condition": "above_ma200",
    "level": None,
}


def _write(tmp_path, rows=None, *, raw=None):
    path = tmp_path / "watch.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(
                {"version": 1, "generated": "2026-11-20T21:00:00Z", "rows": rows}
            ),
            encoding="utf-8",
        )
    return path


@pytest.mark.parametrize(
    "raw,word",
    [
        (None, "missing"),
        ("{not json", "unreadable"),
        ("[]", "object"),
        ('{"version": 2, "rows": []}', "version"),
        ('{"version": 1, "rows": {}}', "rows"),
        (json.dumps({"version": 1, "rows": [EV_E1, EV_E1]}), "duplicate"),
    ],
    ids=["missing", "bad-json", "array", "version", "rows-not-list", "duplicate-id"],
)
def test_file_level_problems_reject_the_whole_file(tmp_path, raw, word):
    path = tmp_path / "watch.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    with pytest.raises(WatchFileError) as exc:
        load_watch(path)
    assert word in str(exc.value)


def test_file_is_left_byte_identical_after_a_rejection(tmp_path):
    path = _write(tmp_path, raw='{"version": 9, "rows": []}')
    before = path.read_bytes()
    with pytest.raises(WatchFileError):
        load_watch(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["watch.json"]


@pytest.mark.parametrize(
    "bad_row,reason_word",
    [
        ({"id": "x", "kind": "event", "name": "n"}, "trigger"),
        ({"id": "x", "kind": "event", "name": "n", "trigger": "  "}, "trigger"),
        ({"kind": "event", "name": "n", "trigger": "t"}, "id"),
        ({"id": "x", "kind": "telepathy", "name": "n"}, "kind"),
        (
            {
                "id": "x",
                "kind": "price",
                "name": "n",
                "action": "add",
                "signal": "QQQ",
                "condition": "sideways",
                "level": 1.0,
            },
            "condition",
        ),
        (
            {
                "id": "x",
                "kind": "price",
                "name": "n",
                "action": "add",
                "signal": "QQQ",
                "condition": "below",
                "level": None,
            },
            "level",
        ),
        ("not an object", "object"),
    ],
    ids=[
        "event-no-trigger",
        "event-blank-trigger",
        "no-id",
        "unknown-kind",
        "price-bad-condition",
        "price-no-level",
        "row-not-object",
    ],
)
def test_bad_row_is_skipped_and_named_while_good_rows_survive(
    tmp_path, bad_row, reason_word
):
    wf = load_watch(_write(tmp_path, rows=[EV_E1, bad_row, PRICE_P1]))
    assert [r.id for r in wf.events] == ["e1"]
    assert [r.id for r in wf.prices] == ["p1"]
    assert len(wf.skipped) == 1
    assert reason_word in wf.skipped[0][1]


def test_skipped_row_is_named_by_id_or_position(tmp_path):
    wf = load_watch(
        _write(
            tmp_path,
            rows=[
                {"id": "x", "kind": "telepathy", "name": "n"},
                {"kind": "event", "name": "n", "trigger": "t"},
            ],
        )
    )
    assert [who for who, _ in wf.skipped] == ["x", "#1"]


def test_rows_parse_into_typed_rows(tmp_path):
    p2 = {
        "id": "p2",
        "kind": "price",
        "action": "add",
        "name": "Example dip",
        "signal": "QQQ",
        "condition": "below",
        "level": 400,
    }
    wf = load_watch(_write(tmp_path, rows=[EV_E1, PRICE_P1, p2]))
    assert wf == WatchFile(
        events=[EventRow("e1", EV_E1["name"], EV_E1["trigger"])],
        prices=[
            PriceRow("p1", "Example trend turned", "rebuy", "SPY", "above_ma200", None),
            PriceRow("p2", "Example dip", "add", "QQQ", "below", 400.0),
        ],
        skipped=[],
    )


def test_zero_event_rows_is_valid(tmp_path):
    assert load_watch(_write(tmp_path, rows=[])) == WatchFile([], [], [])

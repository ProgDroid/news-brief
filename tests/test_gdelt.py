from backtest.gdelt.aggregate import (
    MIDEAST_FIPS,
    GdeltDaily,
    aggregate_events,
    fold_daily,
    is_conflict,
    merge_daily,
)
from backtest.gdelt.events import GdeltEvent, parse_row
from backtest.gdelt.snap import snap_forward, to_sentiment_series


def _row(**over) -> list[str]:
    """A well-formed 58-column GDELT 1.0 Event row (all '0'/'' except set fields)."""
    f = ["0"] * 58
    f[1] = "20260115"  # SQLDATE
    f[28] = "19"  # EventRootCode (fight)
    f[29] = "4"  # QuadClass (material conflict)
    f[30] = "-8.5"  # GoldsteinScale
    f[31] = "12"  # NumMentions
    f[34] = "-6.25"  # AvgTone
    f[51] = "IR"  # ActionGeo_CountryCode (Iran, FIPS)
    for k, v in over.items():
        f[int(k[1:])] = v  # key 'c51' -> index 51
    return f


def test_parse_row_well_formed():
    ev = parse_row(_row())
    assert ev == GdeltEvent(
        date="2026-01-15",
        event_root_code="19",
        quad_class=4,
        goldstein=-8.5,
        num_mentions=12.0,
        avg_tone=-6.25,
        geo_country="IR",
    )


def test_parse_row_too_short_returns_none():
    assert parse_row(["0"] * 40) is None


def test_parse_row_bad_numeric_returns_none():
    assert parse_row(_row(c30="not-a-number")) is None


def test_parse_row_bad_date_returns_none():
    assert parse_row(_row(c1="2026")) is None


def test_parse_row_uppercases_and_strips_geo():
    assert parse_row(_row(c51=" iz ")).geo_country == "IZ"


def _ev(geo="IR", root="19", quad=4, gold=-8.0, men=10.0, tone=-5.0, d="2026-01-15"):
    return GdeltEvent(d, root, quad, gold, men, tone, geo)


def test_is_conflict_by_quadclass_or_rootcode():
    assert is_conflict(_ev(quad=3, root="04"))  # verbal conflict
    assert is_conflict(_ev(quad=1, root="19"))  # cooperative quad but fight root
    assert not is_conflict(_ev(quad=2, root="04"))  # material coop, benign root


def test_aggregate_events_region_and_conflict_sums():
    evs = [
        _ev(geo="IR", men=10.0, tone=-4.0, quad=4, root="19"),  # region + conflict
        _ev(geo="IZ", men=5.0, tone=2.0, quad=2, root="04"),  # region, not conflict
        _ev(
            geo="US", men=99.0, tone=-9.0, quad=4, root="20"
        ),  # out of region -> ignored
    ]
    day = aggregate_events("2026-01-15", evs, MIDEAST_FIPS)
    assert day.date == "2026-01-15"
    assert day.n_region_events == 2
    assert day.n_conflict_events == 1
    assert day.conflict_mentions == 10.0
    assert day.mention_weight == 15.0
    assert day.tone_weighted_sum == 10.0 * -4.0 + 5.0 * 2.0  # -30.0
    assert day.signal("conflict_mentions") == 10.0
    assert day.signal("mean_tone") == -30.0 / 15.0


def test_signal_unknown_field_raises():
    day = aggregate_events("2026-01-15", [_ev()], MIDEAST_FIPS)
    try:
        day.signal("nope")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_fold_daily_groups_by_date():
    evs = [_ev(d="2026-01-15"), _ev(d="2026-01-16")]
    out = fold_daily(evs, MIDEAST_FIPS)
    assert set(out) == {"2026-01-15", "2026-01-16"}


def test_merge_daily_field_wise_sum_and_redate():
    a = GdeltDaily("2026-01-17", 10.0, 1, 2, -30.0, -80.0, 15.0)
    b = GdeltDaily("2026-01-18", 4.0, 1, 1, 8.0, -2.0, 4.0)
    m = merge_daily(a, b, date_iso="2026-01-19")
    assert m.date == "2026-01-19"
    assert m.conflict_mentions == 14.0
    assert m.n_region_events == 3
    assert m.mention_weight == 19.0
    assert m.goldstein_weighted_sum == -82.0


def _daily(d, cm=1.0, mw=2.0, tws=-4.0):
    return GdeltDaily(d, cm, 1, 1, tws, -3.0, mw)


def test_snap_forward_weekend_folds_into_monday():
    trading = {"2026-01-16", "2026-01-20"}  # Fri, then Tue (Mon 19th holiday)
    dailies = {
        "2026-01-17": _daily("2026-01-17", cm=3.0, mw=3.0),  # Sat
        "2026-01-18": _daily("2026-01-18", cm=5.0, mw=5.0),  # Sun
        "2026-01-20": _daily("2026-01-20", cm=2.0, mw=2.0),  # Tue (trading)
    }
    out = snap_forward(dailies, trading)
    assert set(out) == {"2026-01-20"}  # all three land on Tue
    assert out["2026-01-20"].conflict_mentions == 10.0
    assert out["2026-01-20"].date == "2026-01-20"


def test_snap_forward_trading_day_maps_to_itself():
    out = snap_forward({"2026-01-16": _daily("2026-01-16", cm=7.0)}, {"2026-01-16"})
    assert out["2026-01-16"].conflict_mentions == 7.0


def test_snap_forward_drops_when_no_session_within_gap():
    # next session is 10 days away -> dropped (max_gap default 4)
    out = snap_forward({"2026-01-01": _daily("2026-01-01")}, {"2026-01-15"})
    assert out == {}


def test_to_sentiment_series_sorted_and_field_applied():
    snapped = {
        "2026-01-20": _daily("2026-01-20", cm=2.0),
        "2026-01-16": _daily("2026-01-16", cm=9.0),
    }
    s = to_sentiment_series(snapped, "conflict_mentions", label="USO:cm")
    assert s.ticker == "USO:cm"
    assert [p.date for p in s.points] == ["2026-01-16", "2026-01-20"]
    assert s.points[0].value == 9.0

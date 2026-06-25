from backtest.gdelt.events import GdeltEvent, parse_row


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

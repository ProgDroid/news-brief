# backtest/gdelt/events.py
"""Parse GDELT 1.0 daily Event CSV rows into typed events. Pure stdlib.

GDELT 1.0 Event Table: tab-delimited, header-less, 58 columns (post-2013-04,
SOURCEURL present). Indices below are the GDELT 1.0 codebook order, verified
against a live file in the spike runner's operator step. Country codes are
FIPS 10-4 (NOT ISO): Iraq=IZ, Yemen=YM, Iran=IR, Israel=IS.
"""

from dataclasses import dataclass

COL_SQLDATE = 1  # YYYYMMDD
COL_EVENT_ROOT_CODE = 28  # CAMEO root, "01".."20"
COL_QUAD_CLASS = (
    29  # 1 verbal-coop 2 material-coop 3 verbal-conflict 4 material-conflict
)
COL_GOLDSTEIN = 30  # -10..+10 (conflict negative)
COL_NUM_MENTIONS = 31
COL_AVG_TONE = 34  # document tone (usually -10..+10)
COL_ACTION_GEO_COUNTRY = 51  # FIPS 10-4 country code of the event location
_MIN_COLS = 52  # must reach through ActionGeo country


@dataclass(frozen=True)
class GdeltEvent:
    date: str  # ISO YYYY-MM-DD
    event_root_code: str
    quad_class: int
    goldstein: float
    num_mentions: float
    avg_tone: float
    geo_country: str  # FIPS, "" if absent


def _iso_from_sqldate(s: str) -> str | None:
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_row(fields: list[str]) -> GdeltEvent | None:
    """Typed event from a split GDELT row, or None if malformed. Tolerant: a
    short row or any unparseable numeric yields None so one bad line never kills
    a day's parse."""
    if len(fields) < _MIN_COLS:
        return None
    iso = _iso_from_sqldate(fields[COL_SQLDATE])
    if iso is None:
        return None
    try:
        quad = int(fields[COL_QUAD_CLASS])
        gold = float(fields[COL_GOLDSTEIN])
        mentions = float(fields[COL_NUM_MENTIONS])
        tone = float(fields[COL_AVG_TONE])
    except ValueError:
        return None
    return GdeltEvent(
        date=iso,
        event_root_code=fields[COL_EVENT_ROOT_CODE].strip(),
        quad_class=quad,
        goldstein=gold,
        num_mentions=mentions,
        avg_tone=tone,
        geo_country=fields[COL_ACTION_GEO_COUNTRY].strip().upper(),
    )

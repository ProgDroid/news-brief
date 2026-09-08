"""Per-feed roll-off arithmetic, pinned on synthetic windows (news-brief-b42.2).

No database and no network: every test builds a feed whose geometry is known by
construction, so a wrong reconstruction is a failing assertion rather than a
plausible number nobody can check. The production data these run against is four
days of one host's polling, and there is no second source to check it against --
which is the argument for pinning the arithmetic here first.

The censoring is the thing to keep in view. `feed_sightings` records an item that
was SEEN; an item published and gone between two polls leaves no row at all. So
every loss figure the script derives is a floor, and the tests that matter most
are the ones asserting UNKNOWN where a zero would read as a measurement.
"""

import statistics
from datetime import datetime, timedelta, timezone

import pytest

from scripts.measure_roll_off import (
    Poll,
    Sighting,
    departures,
    feed_kinds,
    feed_stats,
    flicker_ratio,
    fit_budget,
    full_turnover_minutes,
    is_proxy,
    overlap,
    overlap_floor,
    poll_pairs,
    proposed_interval_minutes,
    single_sighting_fraction,
    turnover_from_rate,
    window_at,
)

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _seen(name: str, first: int, last: int) -> Sighting:
    return Sighting(name, _at(first), _at(last))


def _ok(minutes: int) -> Poll:
    return Poll(_at(minutes), None, 10)


def _failed(minutes: int, kind: str = "http_403") -> Poll:
    return Poll(_at(minutes), kind, 0)


# ── Window reconstruction ────────────────────────────────────────────────────


def test_an_item_is_in_the_window_between_its_first_and_last_sighting():
    sightings = [_seen("a", 0, 60)]
    assert window_at(sightings, _at(30)) == {"a"}


def test_the_window_excludes_an_item_that_has_not_appeared_or_has_gone():
    """The presence sibling is in the same call on purpose: a window that always
    came back empty would satisfy either exclusion on its own."""
    sightings = [_seen("early", 0, 30), _seen("live", 0, 120), _seen("late", 90, 120)]

    assert window_at(sightings, _at(60)) == {"live"}


# ── Overlap between consecutive polls ────────────────────────────────────────


def test_overlap_is_the_share_of_a_polls_items_still_present_at_the_next():
    sightings = [
        _seen("stays", 0, 60),
        _seen("also_stays", 0, 60),
        _seen("rolls_off", 0, 30),
        _seen("rolls_off_too", 0, 30),
    ]
    assert overlap(sightings, _at(30), _at(60)) == 0.5


def test_overlap_is_unknown_rather_than_zero_when_the_first_window_was_empty():
    """Dividing by an empty window yields 0.0, which reads as 'everything rolled
    off' -- the strongest possible claim, from no evidence at all."""
    assert overlap([_seen("later", 90, 120)], _at(30), _at(60)) is None


def test_only_successful_polls_are_paired():
    """A failed poll never updated last_seen_at, so treating it as an
    observation reports the feed's whole window as having turned over -- the
    large, clean, fictitious signal capture.rolled_off's docstring warns about."""
    polls = [_ok(0), _failed(15), _ok(30)]

    pairs = poll_pairs(polls, nominal_minutes=30)

    # Pairing failures too would give two pairs, (0,15) and (15,30), each
    # reporting a window that no poll actually observed.
    assert [(a.polled_at, b.polled_at) for a, b in pairs] == [(_at(0), _at(30))]


def test_a_pair_spanning_a_longer_gap_than_the_schedule_is_excluded():
    """Two successful polls either side of an outage measure a two-hour interval,
    not a thirty-minute one. Counting that as turnover at the nominal cadence
    would justify a shorter interval using evidence from a longer one."""
    polls = [_ok(0), _ok(30), _ok(180)]

    pairs = poll_pairs(polls, nominal_minutes=30)

    assert [(a.polled_at, b.polled_at) for a, b in pairs] == [(_at(0), _at(30))]


# ── Single sightings ─────────────────────────────────────────────────────────


def test_an_item_seen_once_and_gone_counts_as_a_single_sighting():
    sightings = [_seen("once", 30, 30), _seen("twice", 0, 30)]

    assert single_sighting_fraction(sightings, newest_ok=_at(60)) == 0.5


def test_an_item_last_seen_at_the_newest_poll_is_not_counted_yet():
    """It is right-censored: still in the window, and its residence is unfinished.
    Counting it as a single sighting inflates the fraction with items whose
    lifetime is simply not over, and does so worst on the busiest feeds."""
    sightings = [_seen("still_here", 60, 60), _seen("twice", 0, 30)]

    assert single_sighting_fraction(sightings, newest_ok=_at(60)) == 0.0


# ── Turnover and the interval it implies ─────────────────────────────────────


def test_turnover_scales_with_the_gap_the_overlap_was_measured_over():
    """Half the window replaced in 30 minutes means a full window in 60."""
    assert full_turnover_minutes(0.5, gap_minutes=30) == 60.0


def test_turnover_is_unknown_when_nothing_rolled_off():
    """An unchanged window sets no upper bound on the interval. Reporting a
    number here -- any number -- would be inventing the one fact the measurement
    exists to supply."""
    assert full_turnover_minutes(1.0, gap_minutes=30) is None


def test_the_proposed_interval_is_a_fraction_of_turnover_and_clamped():
    assert proposed_interval_minutes(240.0, safety=4, lo=10, hi=240) == 60
    assert proposed_interval_minutes(8.0, safety=4, lo=10, hi=240) == 10
    assert proposed_interval_minutes(4800.0, safety=4, lo=10, hi=240) == 240


def test_the_budget_slows_every_feed_rather_than_dropping_any():
    """Cadence is not free -- 26 feeds at five minutes is ~7500 requests a day on
    one UA, and Nitter already 429s adjacent feeds. Over budget, every feed slows
    proportionally: dropping a feed silently is the failure mode capture exists
    to make impossible."""
    intervals = {"fast": 10, "slow": 60}  # 144 + 24 = 168 requests/day

    fitted = fit_budget(intervals, budget_per_day=84)

    assert set(fitted) == {"fast", "slow"}
    assert fitted["fast"] == 20
    assert fitted["slow"] == 120


# ── The per-feed summary ─────────────────────────────────────────────────────


def test_a_feed_with_no_usable_pair_reports_unknown_not_zero():
    """The distinction this repo keeps losing: ABSENT and UNKNOWN are different
    facts, and a feed whose polls all failed has told us nothing about its
    window. A 0.0 here would be read as 'turns over completely', which is the
    opposite of what the data says, and would earn it the shortest interval."""
    stats = feed_stats([_failed(0), _failed(30)], [], nominal_minutes=30)

    assert stats["pairs"] == 0
    assert stats["median_overlap"] is None
    assert stats["proposed_minutes"] is None


def test_the_summary_reports_the_median_overlap_over_every_usable_pair():
    polls = [_ok(0), _ok(30), _ok(60)]
    # Each pair keeps "stays" and loses the other member: 1 of 2, twice over.
    sightings = [
        _seen("stays", 0, 60),
        _seen("gone_by_30", 0, 0),
        _seen("brief", 30, 30),
    ]

    stats = feed_stats(polls, sightings, nominal_minutes=30)

    assert stats["pairs"] == 2
    assert stats["median_overlap"] == pytest.approx(0.5)


# ── Rate-based turnover: use the whole span, not a pair ──────────────────────
#
# Why the second pass exists. Median pairwise overlap answers "what does a
# typical 30 minutes look like", and for a feed that loses an item every few
# hours the answer is "nothing happened" -- so the median pins at 1.00 and the
# turnover comes back UNKNOWN for exactly the feeds where slowing is safest.
# 17 of 26 feeds landed there on the first host run. Counting departures over
# the whole 4.2 days uses every observation instead of summarising each pair.


def test_departures_counts_only_items_whose_residence_has_ended():
    """Same censoring as the singles fraction: an item last seen at the newest
    successful poll is still in the window and has not departed."""
    sightings = [_seen("gone", 0, 30), _seen("also_gone", 0, 30), _seen("here", 0, 60)]

    assert departures(sightings, newest_ok=_at(60)) == 2


def test_turnover_from_rate_spreads_departures_over_the_whole_span():
    """10 items, 5 of them replaced across 100 minutes, so the window turns over
    in 200. No pair is consulted -- that is the point."""
    assert turnover_from_rate(10, departed=5, span_minutes=100) == 200.0


def test_turnover_from_rate_is_unknown_when_nothing_departed():
    """Dividing by zero departures is an infinite turnover, which would render
    as the safest feed on the board rather than the least measured one."""
    assert turnover_from_rate(10, departed=0, span_minutes=100) is None


def test_the_worst_pair_is_reported_not_only_the_typical_one():
    """Loss happens in the tail. A burst that empties a window shows up in one
    pair out of two hundred and is erased by a median."""
    assert overlap_floor([0.5, 0.9, 1.0], pct=0) == 0.5


def test_a_low_percentile_survives_a_feed_that_is_usually_quiet():
    """The median and the floor disagree here BY CONSTRUCTION, which is the
    whole reason for reporting both: nineteen calm pairs and one collapse."""
    overlaps = [1.0] * 19 + [0.2]

    assert statistics.median(overlaps) == 1.0
    assert overlap_floor(overlaps, pct=5) == 0.2


# ── Flicker: the reconstruction interpolates, entries_seen does not ──────────


def test_flicker_shows_as_a_window_wider_than_the_feed_ever_returned():
    """An item seen at poll 1 and poll 50 is counted present throughout, so
    "seen, gone, came back" is indistinguishable from "continuously present".
    feed_polls.entries_seen is what the poll ACTUALLY returned, so the ratio
    between them measures the interpolation directly. Reuters Markets
    reconstructs to 150 against a Google News cap of 100."""
    assert flicker_ratio(150, entries_seen=100) == 1.5


def test_no_flicker_when_the_window_matches_what_the_poll_returned():
    assert flicker_ratio(100, entries_seen=100) == 1.0


def test_flicker_is_unknown_when_no_poll_reported_its_size():
    """Zero entries_seen is a feed that never returned anything, not a feed
    whose window is perfectly faithful."""
    assert flicker_ratio(100, entries_seen=0) is None


# ── Proxy or native: the question spec section 3 actually asks ───────────────


def test_a_google_news_feed_is_a_proxy_and_a_direct_one_is_not():
    """Section 3 makes this the comparison b42.2 owes: for the 8 capped proxies
    the loss is truncation INSIDE one poll at the 100-entry cap, not roll-off
    between polls -- a different mode with a different remedy, since no interval
    can fix it. Both halves asserted together: a classifier returning False for
    everything satisfies the second on its own."""
    proxy = "https://news.google.com/rss/search?q=when:2d+site%3Areuters.com%2Fmarkets"

    assert is_proxy(proxy) is True
    assert is_proxy("https://www.aljazeera.com/xml/rss/all.xml") is False


def test_the_summary_prefers_the_rate_over_the_pair_and_keeps_the_worst_one():
    """The integration the second pass exists for: a feed whose pairs are mostly
    quiet still gets a turnover, because departures are counted over the span."""
    polls = [_ok(30 * i) for i in range(5)]  # 0..120, four pairs
    sightings = [_seen("a", 0, 30), _seen("b", 0, 60), _seen("c", 0, 120)]

    stats = feed_stats(polls, sightings, nominal_minutes=30)

    # "a" and "b" ended before the newest poll; "c" is still open.
    assert stats["departed"] == 2
    assert stats["turnover_rate_minutes"] is not None
    assert stats["proposed_minutes"] is not None
    assert stats["worst_overlap"] <= stats["median_overlap"]


def test_a_feed_the_list_does_not_carry_is_absent_rather_than_native():
    """'?' and 'native' are different facts. Defaulting an unrecognised name to
    native would answer section 3's proxy-versus-native comparison with a guess,
    silently, on whichever side happened to be the default."""
    sources = [
        {"name": "Reuters Markets", "url": "https://news.google.com/rss/search?q=x"},
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    ]

    kinds = feed_kinds(sources)

    assert kinds == {"Reuters Markets": "gnews", "Al Jazeera": "native"}
    assert "Meduza" not in kinds

"""Unit tests for continuous capture (news-brief-b42.1). No network, no DB."""

import re
from datetime import datetime, timezone

import brief
import capture
import common
import scheduler


FEED = {
    "name": "Test Wire",
    "url": "https://example.com/feed",
    "category": "macro",
    "kind": "wire",
}

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item>
  <title>First headline</title>
  <link>https://example.com/a?utm_source=rss#frag</link>
  <guid>guid-a</guid>
  <description>&lt;p&gt;Body of &lt;b&gt;a&lt;/b&gt;.&lt;/p&gt;</description>
  <pubDate>Tue, 02 Sep 2026 10:00:00 GMT</pubDate>
</item>
<item>
  <title>Second headline</title>
  <link>https://example.com/b</link>
  <description>Body of b.</description>
  <pubDate>Tue, 02 Sep 2026 11:00:00 GMT</pubDate>
</item>
</channel></rss>"""


class _Resp:
    status_code = 200
    headers: dict = {}

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


# Captured from brief.py as of commit b8149bd (the parent of the fetch_rss
# split), by running the UNMODIFIED fetch_rss against RSS above with the same
# monkeypatch this test uses, then taking repr() of its return value. This is
# the equality guard for success criterion 4 -- "the brief's output is
# byte-identical across the fetch_rss split" -- so it must never be
# regenerated from the post-split code; that would pin whatever the split
# produces instead of proving it matches what came before.
EXPECTED_RENDER = (
    "\n### Test Wire [WIRE] (MACRO)\n"
    "- First headline (Tue, 02 Sep 2026 10:00:00 GMT)\n"
    "  Body of a.\n"
    "- Second headline (Tue, 02 Sep 2026 11:00:00 GMT)\n"
    "  Body of b."
)


def test_fetch_rss_output_is_unchanged_by_the_split(monkeypatch):
    """Characterization: pins the rendered string byte-for-byte.

    Written before the refactor and never edited to match new output. If this
    test needs changing, the brief's prompt changed, which this issue forbids.
    """
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    out = brief.fetch_rss(FEED)
    assert out == EXPECTED_RENDER
    assert "First headline" in out
    assert "Second headline" in out
    assert "Body of a." in out
    assert out.startswith(
        brief._source_header("Test Wire", "wire", "macro", None, False)
    )
    assert "<b>" not in out, "HTML must still be stripped"


def test_an_empty_feed_still_returns_empty_string_and_logs(monkeypatch, caplog):
    """brief.py:1877-1880 returns "" AND logs on one path. A test pinning only
    the return value passes even if the warning disappears -- and that warning is
    the only signal distinguishing a malformed feed from a quiet one."""
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(empty))
    with caplog.at_level("WARNING"):
        assert brief.fetch_rss(FEED) == ""
    assert "No entries: Test Wire" in caplog.text


def test_fetch_feed_entries_returns_structured_entries(monkeypatch):
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    got = brief.fetch_feed_entries(FEED)
    assert got.failure is None
    assert [e["title"] for e in got.entries] == ["First headline", "Second headline"]
    assert got.entries[0]["guid"] == "guid-a"
    assert got.entries[0]["summary"] == "Body of a."
    assert got.entries[0]["published_at"] is not None


def test_a_missing_guid_is_none_not_absent(monkeypatch):
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries[1]["guid"] is None


def test_an_unparseable_date_becomes_none_and_keeps_the_entry(monkeypatch):
    bad = RSS.replace(b"Tue, 02 Sep 2026 10:00:00 GMT", b"not a date")
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(bad))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries[0]["published_at"] is None
    assert got.entries[0]["title"] == "First headline"


def test_a_403_is_reported_as_a_kind_not_as_emptiness(monkeypatch):
    """An empty list is ambiguous across 403 / timeout / malformed / quiet, and
    the tally promises to tell them apart."""

    class Forbidden(_Resp):
        status_code = 403

        def raise_for_status(self):
            # Real requests.Response.raise_for_status() attaches
            # response=self -- match that here so the double doesn't mislead
            # a later reader into thinking the exception arrives bare.
            raise brief.requests.HTTPError("403", response=self)

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: Forbidden(b""))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries == []
    assert got.failure == "http_403"


def test_an_empty_feed_is_reported_as_empty_not_malformed(monkeypatch):
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(empty))
    assert brief.fetch_feed_entries(FEED).failure == "empty"


def test_outlet_defaults_to_the_feed_name():
    assert brief.outlet_for({"name": "TASS", "url": "u", "category": "geo"}) == "TASS"


def test_an_explicit_outlet_key_wins():
    feed = {
        "name": "Reuters Markets",
        "url": "u",
        "category": "macro",
        "outlet": "Reuters",
    }
    assert brief.outlet_for(feed) == "Reuters"


def test_both_reuters_feeds_resolve_to_one_outlet():
    named = {f["name"]: f for f in brief.RSS_FEEDS}
    assert brief.outlet_for(named["Reuters Markets"]) == "Reuters"
    assert brief.outlet_for(named["Reuters World"]) == "Reuters"


def test_jacob_shapiro_publishes_under_one_outlet_across_two_media():
    """jashap.substack.com and the @jacobshap Nitter feed are the same author.
    Left unmapped they become two outlets, and one take reaching both reads as
    two independent sources corroborating each other."""
    named = {f["name"]: f for f in brief.RSS_FEEDS}
    assert brief.outlet_for(named["Intersubjectively Transmissible"]) == "Jacob Shapiro"
    assert brief.outlet_for(named["Jacob Shapiro (@jacobshap)"]) == "Jacob Shapiro"


def test_no_feed_ships_a_product_name_as_an_outlet():
    """outlets.name is UNIQUE(lower(name)) and is the corroboration dimension,
    so a feed-product name in it invents a publisher that does not exist."""
    product_names = {
        "ISW Daily Assessment",
        "BOJ Statements",
        "EIA Today in Energy",
        "Reuters Markets",
        "Reuters World",
        "Marko Papic (@geo_papic)",
        "Jacob Shapiro (@jacobshap)",
        "Intersubjectively Transmissible",
    }
    for feed in brief.RSS_FEEDS:
        if feed["name"] in product_names:
            assert brief.outlet_for(feed) != feed["name"], (
                f"{feed['name']} is a product name and needs an explicit outlet"
            )


def test_feeds_sharing_an_outlet_agree_on_its_metadata():
    """A developer error caught here rather than at runtime: outlets carries
    kind/perspective/state_funded, and two feeds mapping to one outlet cannot
    disagree about them. `category` is deliberately excluded — it is a property
    of the reader's slicing, not of the publisher, and outlets has no such
    column."""
    by_outlet: dict[str, list[dict]] = {}
    for feed in brief.RSS_FEEDS:
        by_outlet.setdefault(brief.outlet_for(feed), []).append(feed)
    for outlet, feeds in by_outlet.items():
        shapes = {
            (
                f.get("kind", "regional"),
                f.get("perspective"),
                bool(f.get("state_funded", False)),
            )
            for f in feeds
        }
        assert len(shapes) == 1, f"{outlet} has feeds disagreeing on metadata: {shapes}"


def test_load_temp_sources_carries_the_outlet_key(monkeypatch):
    """load_temp_sources rebuilds each entry from a fixed field list, so a key it
    does not name is silently dropped — and the mapping would then work for
    baked-in feeds and fail invisibly for user sources."""
    monkeypatch.setattr(
        brief.config,
        "sources",
        lambda: [
            {
                "name": "Reuters Tech",
                "url": "https://x/y",
                "category": "macro",
                "outlet": "Reuters",
            }
        ],
    )
    loaded = brief.load_temp_sources()
    assert loaded[0]["outlet"] == "Reuters"


def test_capture_polls_feeds_and_never_page_sources(monkeypatch):
    """all_sources() is the wrong entry point: it includes source_type='page'
    entries, which are scraped pages with no entry list. RSS_FEEDS carries no
    source_type key at all, so its absence must mean "feed"."""
    monkeypatch.setattr(
        brief,
        "RSS_FEEDS",
        [{"name": "Baked", "url": "https://a/f", "category": "macro"}],
    )
    monkeypatch.setattr(
        brief,
        "load_temp_sources",
        lambda: [
            {
                "name": "UserFeed",
                "url": "https://b/f",
                "category": "geo",
                "source_type": "feed",
            },
            {
                "name": "UserPage",
                "url": "https://c/p",
                "category": "geo",
                "source_type": "page",
            },
        ],
    )
    names = [f["name"] for f in capture.capture_sources()]
    assert names == ["Baked", "UserFeed"]


def test_the_real_feed_list_never_polls_one_host_back_to_back():
    """The regression test for a production failure: the documented Nitter 429
    is an ADJACENCY bug, not a volume one — the X feeds sit next to each other in
    RSS_FEEDS, so one 429'd on most runs, 48 chances a day.

    Kept here against the REAL list, which is what capture orders. The synthetic
    version of this moved to tests/test_common.py with the function itself
    (news-brief-bzo); this one stays because it is the only test that fails when
    a newly added feed makes the real list unspreadable."""
    ordered = common.order_by_host(list(brief.RSS_FEEDS))
    hosts = [common.feed_host(f) for f in ordered]
    assert len(ordered) == len(brief.RSS_FEEDS)
    assert all(a != b for a, b in zip(hosts, hosts[1:])), hosts


def test_a_pass_stops_at_its_deadline_and_records_what_it_skipped(monkeypatch):
    """26 feeds x 3 attempts x 20s is ~26 minutes, which outlives the 30-minute
    interval -- and the supervisor telegram_alerts a job still running at its
    next fire time, 48 chances a day."""
    feeds = [
        {"name": f"F{i}", "url": f"https://h{i}.example/f", "category": "geo"}
        for i in range(5)
    ]
    monkeypatch.setattr(capture, "capture_sources", lambda: feeds)
    monkeypatch.setattr(capture, "DEADLINE_SECONDS", 0)
    recorded = []
    monkeypatch.setattr(
        capture,
        "record_poll",
        lambda conn, run, name, failure, seen: recorded.append((name, failure)),
    )
    monkeypatch.setattr(capture, "start_run", lambda conn, enabled: 1)
    monkeypatch.setattr(capture, "finish_run", lambda conn, run, tally: None)
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)

    class _FakeConn:
        """`run` commits per feed, so a bare object() raises AttributeError
        before the deadline logic is ever reached."""

        def commit(self):
            return None

    tally = capture.run(conn=_FakeConn())
    assert len(recorded) == 5, "every feed gets a poll row, reached or not"
    assert all(failure == "deadline" for _, failure in recorded)
    assert tally.feeds_failed == 5


def test_capture_is_a_job_mode():
    """A JOB_MODES entry is what gives capture the advisory lock and its
    job_runs row through the mode dispatch — the rule that every entry path to a
    job, including `docker compose run`, records itself."""
    assert "capture" in brief.JOB_MODES


def test_every_job_mode_has_a_dispatch_entry():
    """JOB_MODES membership alone is not enough: the dispatch lookup runs FIRST,
    so a job mode with no MODES entry prints usage and exits 1 — and the
    supervisor turns that into a Telegram alert on every fire time, 48 a day for
    capture. This assertion is the reason MODES is module-level."""
    assert brief.JOB_MODES <= set(brief.MODES)


def test_capture_has_a_schedule_whose_grace_clears_one_tick():
    spec = next(s for s in scheduler.SCHEDULES if s.job == "capture")
    assert spec.kind == "interval"
    assert spec.every_minutes == 30
    assert spec.grace_minutes * 60 > scheduler.TICK_SECONDS


def test_the_pass_deadline_is_shorter_than_the_interval():
    """A pass that outlives its fire time trips supervisor.py's overlap
    telegram_alert, 48 chances a day."""
    spec = next(s for s in scheduler.SCHEDULES if s.job == "capture")
    assert capture.DEADLINE_SECONDS < spec.every_minutes * 60


def test_the_capture_knob_defaults_off():
    assert common.KNOBS["CAPTURE_ENABLED"].default is False


# ── The capture window is narrower than the brief's ──────────────────────────
#
# news-brief-b42.4. Four Google News proxies return exactly 100 entries per
# poll -- the cap -- because `when:2d` offers Google ~370 candidates for 100
# relevance-ranked slots. Ranking is not chronological, so items move in and out
# of view (measured flicker 1.51 on Reuters Markets), and an item that is never
# in the top 100 at any poll instant is lost unobservably.
#
# Narrowing the window removes the MECHANISM rather than fixing a measured loss:
# at when:6h there are ~20 candidates for 100 slots and nothing can be ranked
# out. But the brief fetches these feeds at brief time and takes the newest 25,
# so a 6h window at 06:00 would show it only the overnight hours. One URL cannot
# serve both readers, which is what `capture_url` is for.


def test_capture_prefers_the_capture_url_where_a_feed_carries_one(monkeypatch):
    """Presence and absence in one call: a substitution that returned the
    override for everything would pass the first assertion alone."""
    monkeypatch.setattr(
        brief,
        "RSS_FEEDS",
        [
            {
                "name": "Capped",
                "url": "https://news.google.com/rss/search?q=when:2d+site%3Aa.com",
                "capture_url": "https://news.google.com/rss/search?q=when:6h+site%3Aa.com",
            },
            {"name": "Plain", "url": "https://b.example/feed"},
        ],
    )
    monkeypatch.setattr(brief, "load_temp_sources", lambda: [])

    urls = {f["name"]: f["url"] for f in capture.capture_sources()}

    assert urls["Capped"] == "https://news.google.com/rss/search?q=when:6h+site%3Aa.com"
    assert urls["Plain"] == "https://b.example/feed"


def test_substituting_the_capture_url_does_not_mutate_the_brief_s_feed(monkeypatch):
    """The brief reads RSS_FEEDS directly and needs the WIDE window: at 06:00 a
    6h window would hand it the overnight hours and nothing else. A substitution
    that edited the dict in place would silently narrow the brief too."""
    feed = {
        "name": "Capped",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aa.com",
        "capture_url": "https://news.google.com/rss/search?q=when:6h+site%3Aa.com",
    }
    monkeypatch.setattr(brief, "RSS_FEEDS", [feed])
    monkeypatch.setattr(brief, "load_temp_sources", lambda: [])

    capture.capture_sources()

    assert feed["url"] == "https://news.google.com/rss/search?q=when:2d+site%3Aa.com"


def test_a_capture_url_points_at_the_same_source_through_a_shorter_window():
    """The pair must not drift. Two hand-maintained URLs invite an edit to one
    and not the other, and the failure would be silent: capture would quietly be
    reading a different source from the brief, with both feeds still working."""
    # The four measured as capping on 2026-09-08 (100 entries every poll, at
    # when:1d and wider). Pinned rather than counted, so dropping an override
    # fails here instead of silently reinstating the truncation; changing this
    # set is a claim about the feeds and wants a fresh measurement behind it.
    assert {f["name"] for f in brief.RSS_FEEDS if f.get("capture_url")} == {
        "Reuters Markets",
        "Reuters World",
        "Kyiv Independent",
        "Yonhap (English)",
    }

    for feed in brief.RSS_FEEDS:
        override = feed.get("capture_url")
        if not override:
            continue
        site = _site_term(feed["url"])
        assert site and _site_term(override) == site, feed["name"]
        assert _when_hours(override) < _when_hours(feed["url"]), feed["name"]


def test_every_google_news_feed_keeps_a_freshness_window():
    """Dropping `when:` is the documented way to chase volume and get staleness
    instead: a deep section path can hold ~100 OLD items and almost nothing
    recent, so a no-window query returns a full feed of stale headlines."""
    for feed in brief.RSS_FEEDS:
        for url in (feed["url"], feed.get("capture_url")):
            if url and "news.google.com" in url:
                assert _when_hours(url) is not None, feed["name"]


def _when_hours(url: str):
    """The `when:` window in hours, or None if the URL carries no window."""
    found = re.search(r"when:(\d+)([hd])", url)
    if not found:
        return None
    return int(found.group(1)) * (24 if found.group(2) == "d" else 1)


def _site_term(url: str):
    """The `site:` term, URL-encoded as it appears in the query."""
    found = re.search(r"site%3A([^&+]+)", url)
    return found.group(1) if found else None


class _CommitOnlyConn:
    """`run` commits per feed, so a bare object() raises AttributeError before
    the logic under test is reached."""

    def commit(self):
        return None


def test_a_pass_past_its_deadline_never_spaces_the_feeds_it_skips(monkeypatch):
    """Why `HostSpacer` is an object with an explicit `wait` and not a generator
    that sleeps as it yields (news-brief-bzo).

    `run` checks its deadline BEFORE fetching. A generator would sleep on the way
    to handing over each feed, so a pass already out of time would burn the host
    gap on every feed it was about to record as `deadline`.

    All five feeds share ONE host on purpose: with distinct hosts the spacer
    would never sleep anyway and this test would pass against the bug.
    """
    feeds = [
        {"name": f"F{i}", "url": "https://one.example/f", "category": "geo"}
        for i in range(5)
    ]
    monkeypatch.setattr(capture, "capture_sources", lambda: feeds)
    monkeypatch.setattr(capture, "DEADLINE_SECONDS", 0)
    recorded = []
    monkeypatch.setattr(
        capture,
        "record_poll",
        lambda conn, run, name, failure, seen: recorded.append((name, failure)),
    )
    monkeypatch.setattr(capture, "start_run", lambda conn, enabled: 1)
    monkeypatch.setattr(capture, "finish_run", lambda conn, run, tally: None)
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)

    waited = []
    spacer = common.HostSpacer(5, clock=lambda: 0.0, sleeper=waited.append)

    capture.run(conn=_CommitOnlyConn(), spacer=spacer)

    assert len(recorded) == 5, "every feed still gets a poll row"
    assert waited == [], "a pass past its deadline slept for feeds it never fetched"


# --- Per-feed poll intervals (news-brief-b42.5 Phase 1).


def _native(**extra):
    return {"name": "N", "url": "https://example.com/rss", **extra}


def _google_news(**extra):
    return {
        "name": "GN",
        "url": "https://news.google.com/rss/search?q=when:2d+site%3Aexample.com",
        **extra,
    }


def test_a_feed_with_no_declared_interval_polls_at_nominal():
    assert capture.poll_interval_minutes(_native()) == capture._interval_minutes()


def test_a_declared_interval_is_honoured():
    assert capture.poll_interval_minutes(_native(poll_every_minutes=90)) == 90


def test_a_declared_interval_above_the_ceiling_is_clamped():
    assert (
        capture.poll_interval_minutes(_native(poll_every_minutes=9999))
        == capture.max_poll_interval_minutes()
    )


def test_the_ceiling_is_derived_from_nominal_rather_than_hardcoded():
    """Spec section 3.2. The evidence bound is nominal x MAX_GAP_FACTOR = 45
    minutes, so what the measurement fixes is the EXTRAPOLATION FACTOR, not an
    absolute number. A hardcoded 120 silently collapses every declared interval
    if capture is ever retimed to 60, and makes the valid range empty at 120."""
    assert capture.max_poll_interval_minutes() == capture._interval_minutes() * 4
    assert capture.max_poll_interval_minutes() > capture._interval_minutes()


def test_a_declared_interval_below_nominal_falls_back_to_nominal():
    """The scheduler tick is the floor: a smaller number is a promise the
    schedule cannot keep."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes=5)) == (
        capture._interval_minutes()
    )


def test_a_non_integer_declared_interval_falls_back_to_nominal():
    """Fail open: a bad value means today's behaviour, never a crash in the poll
    loop. `True` is included because bool is an int subclass and would otherwise
    be read as a one-minute interval."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes="fast")) == (
        capture._interval_minutes()
    )
    assert capture.poll_interval_minutes(_native(poll_every_minutes=True)) == (
        capture._interval_minutes()
    )


def test_a_rank_ordered_feed_is_never_slowed_even_when_it_declares_an_interval():
    """Google News returns RELEVANCE-ranked results under a 100-entry cap, so an
    item missing from a poll may never have departed: turnover is unmeasurable
    in principle, and such a feed stays at nominal whatever it declares."""
    assert capture.poll_interval_minutes(_google_news(poll_every_minutes=120)) == (
        capture._interval_minutes()
    )


def test_a_native_feed_declaring_THE_SAME_interval_is_slowed():
    """The control for the test above. Without it, an implementation that never
    slows anything passes the pinned-feed assertion."""
    assert capture.poll_interval_minutes(_native(poll_every_minutes=120)) == 120


def test_no_rank_ordered_feed_in_the_real_list_declares_an_interval():
    """Self-maintaining: a ninth Google News proxy tests itself. The two control
    assertions keep it honest -- without them it passes on a predicate that is
    constantly False."""
    assert any(capture.rank_ordered(f) for f in brief.RSS_FEEDS), "control failed"
    assert any(not capture.rank_ordered(f) for f in brief.RSS_FEEDS), "control failed"
    for f in brief.RSS_FEEDS:
        if capture.rank_ordered(f):
            assert capture.POLL_INTERVAL_KEY not in f, (
                f"{f['name']} is relevance-ranked and cannot carry an interval"
            )


def test_every_declared_interval_in_the_real_list_is_within_range():
    """VACUOUS TODAY, and kept knowingly: no feed declares an interval until
    rollout. It is the assertion that fires when someone pastes a 240 back in."""
    for f in brief.RSS_FEEDS:
        declared = f.get(capture.POLL_INTERVAL_KEY)
        if declared is not None:
            assert isinstance(declared, int) and not isinstance(declared, bool), f
            assert (
                capture._interval_minutes()
                < declared
                <= capture.max_poll_interval_minutes()
            ), f["name"]


def test_the_due_check_fails_open_when_the_table_cannot_be_read():
    """Every failure in this subsystem degrades to today's behaviour. The worst
    case spends requests, the only resource it was ever optimising.

    Lives here rather than in test_capture_store.py: it needs no database, and
    that module's skipmark would hide it from every local pytest run."""

    class _BrokenConn:
        def execute(self, *a, **kw):
            raise RuntimeError("relation feed_polls does not exist")

    feeds = [
        {"name": "A", "url": "https://a.example/rss", "poll_every_minutes": 120},
        {"name": "B", "url": "https://b.example/rss"},
    ]
    assert capture.due_feeds(_BrokenConn(), feeds, datetime.now(timezone.utc)) == feeds


def test_feeds_total_still_counts_every_source_not_just_the_due_ones(monkeypatch):
    """feeds_total is persisted to capture_runs and read by the health surface.
    Redefining it as "the due ones" would silently change what every historical
    row means."""
    feeds = [
        {"name": f"F{i}", "url": f"https://h{i}.example/f", "category": "geo"}
        for i in range(5)
    ]
    monkeypatch.setattr(capture, "capture_sources", lambda: feeds)
    monkeypatch.setattr(capture, "due_feeds", lambda conn, fs, now: fs[:2])
    monkeypatch.setattr(common, "CAPTURE_ENABLED", True)
    monkeypatch.setattr(capture, "start_run", lambda conn, enabled: 1)
    monkeypatch.setattr(capture, "finish_run", lambda conn, run, tally: None)
    monkeypatch.setattr(
        capture, "record_poll", lambda conn, run, name, failure, seen: None
    )
    monkeypatch.setattr(
        brief,
        "fetch_feed_entries",
        lambda f: brief.FeedFetch(entries=[], failure="http_403"),
    )

    tally = capture.run(conn=_CommitOnlyConn(), spacer=common.HostSpacer(0))

    assert tally.feeds_total == 5, "every source is still counted"
    assert tally.feeds_not_due == 3


# ── bare quote-tweets (news-brief-grc) ────────────────────────────────────────

# MEASURED from the live Nitter at aegyptvault.local:5678 on 2026-09-10, item
# guid 2098067254663581699. Nitter puts the author's OWN words in <title> and
# the quoted tweet in <description>, so a quote-tweet with no added comment has
# an empty title by construction.
QT_DESCRIPTION = (
    "<hr/>\n<blockquote>\n<b>Clash Report (@clashreport)</b>\n<p>\n"
    "<p>Yemen\u2019s Houthis seized Zuqar Island in the southern Red Sea after "
    "rocket attacks and a boat-borne ground assault, according to a Yemeni "
    "military source. <br>\n<br>\nSource: AFP</p>\n"
    '<img src="http://x/pic/media%2FHR3B2bhaIAAWyCA.jpg" />\n</p>\n'
    '<footer>\n\u2014 <cite><a href="http://x/clashreport/status/1">q</a></cite>\n'
    "</footer>\n</blockquote>"
)


def test_entry_from_derives_an_attributed_title_for_a_bare_quote_tweet():
    e = brief._entry_from(
        {"title": "", "link": "http://x/a/status/1#m", "summary": QT_DESCRIPTION}
    )
    assert e["title"], "a quote-tweet carries readable text and must not be titleless"
    # Provenance: the words are Clash Report's, not the amplifying account's.
    assert e["title"].startswith("QT Clash Report (@clashreport): ")
    assert "Zuqar Island" in e["title"]
    # The author must appear once, not twice: it is also the first line of the
    # tag-stripped body, which is where the excerpt is taken from.
    assert e["title"].count("Clash Report") == 1
    # The body is untouched: production logs it with its leading newlines
    # intact, and only the TITLE is derived.
    assert e["summary"].lstrip().startswith("Clash Report (@clashreport)")
    assert "Zuqar Island" in e["summary"]


def test_entry_from_leaves_a_real_title_alone():
    e = brief._entry_from(
        {"title": "  Simpler times  ", "link": "http://x/1", "summary": "<p>body</p>"}
    )
    assert e["title"] == "Simpler times"


def test_entry_from_has_no_title_when_there_is_nothing_to_read():
    """A media-only post strips to nothing. It stays titleless so store_items
    still skips it -- deriving a title from an empty body would manufacture a
    row with no content in it."""
    e = brief._entry_from(
        {"title": "", "link": "http://x/1", "summary": '<img src="http://x/p.jpg"/>'}
    )
    assert e["title"] == ""


def test_derived_title_drops_nitters_footer_permalink():
    """A short quote used to run the excerpt into the <footer><cite> link, so
    the headline ended in a raw status URL. The body keeps it; the title does
    not. MEASURED case: guid 2098060529344622911, 2026-09-10."""
    short_qt = (
        "<hr/>\n<blockquote>\n<b>Rick Palacios Jr. (@RickPalaciosJr)</b>\n"
        "<p><p>7% mortgage rates officially back today.</p></p>\n<footer>\n"
        '\u2014 <cite><a href="http://x/RickPalaciosJr/status/2">http://x/'
        "RickPalaciosJr/status/2</a></cite>\n</footer>\n</blockquote>"
    )
    e = brief._entry_from({"title": "", "link": "http://x/1", "summary": short_qt})
    assert e["title"] == (
        "QT Rick Palacios Jr. (@RickPalaciosJr): 7% mortgage rates officially "
        "back today."
    )
    assert "http" not in e["title"]
    assert "status/2" in e["summary"], "the body still carries the permalink"

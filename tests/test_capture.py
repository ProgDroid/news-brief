"""Unit tests for continuous capture (news-brief-b42.1). No network, no DB."""

import brief
import capture
import common


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


def test_no_two_consecutive_fetches_share_a_host():
    """The documented Nitter 429 is an ADJACENCY bug, not a volume one: the two
    X feeds sit next to each other in RSS_FEEDS, so one 429'd on most runs. At
    48 passes a day that collision would recur 48 times a day."""
    feeds = [
        {"name": "A", "url": "https://nitter.example/a/rss", "category": "geo"},
        {"name": "B", "url": "https://nitter.example/b/rss", "category": "geo"},
        {"name": "C", "url": "https://other.example/c", "category": "geo"},
    ]
    ordered = capture.order_by_host(feeds)
    hosts = [f["url"].split("/")[2] for f in ordered]
    assert len(ordered) == 3
    assert all(a != b for a, b in zip(hosts, hosts[1:])), hosts


def test_the_real_feed_list_never_polls_one_host_back_to_back():
    """Written against the real RSS_FEEDS as well as a synthetic list, because
    this is the regression test for a production failure."""
    ordered = capture.order_by_host(list(brief.RSS_FEEDS))
    hosts = [f["url"].split("/")[2] for f in ordered]
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

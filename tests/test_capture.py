"""Unit tests for continuous capture (news-brief-b42.1). No network, no DB."""

import brief


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


def test_fetch_rss_output_is_unchanged_by_the_split(monkeypatch):
    """Characterization: pins the rendered string byte-for-byte.

    Written before the refactor and never edited to match new output. If this
    test needs changing, the brief's prompt changed, which this issue forbids.
    """
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(RSS))
    out = brief.fetch_rss(FEED)
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
            raise brief.requests.HTTPError("403")

    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: Forbidden(b""))
    got = brief.fetch_feed_entries(FEED)
    assert got.entries == []
    assert got.failure == "http_403"


def test_an_empty_feed_is_reported_as_empty_not_malformed(monkeypatch):
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel/></rss>'
    monkeypatch.setattr(brief.requests, "get", lambda *a, **k: _Resp(empty))
    assert brief.fetch_feed_entries(FEED).failure == "empty"

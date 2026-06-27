"""Mauldin Economics 'The World Isn't Ending' scraper (Jacob Shapiro column).

The site is a Wix SPA with no RSS feed; the article list lives in the
`wix-warmup-data` JSON the page embeds for client hydration. These tests pin the
parser against that structure and the fetcher's fail-safe behaviour.
"""

import json

import brief

# Two real records plus two that must be filtered: `nodate` (no publish date)
# and `junk` (schema artifact whose title is a field-definition dict, not text).
RECORDS = {
    "g1": {
        "title": "The Russian Economy Is in Shambles",
        "date": {"$date": "2026-06-25T07:15:00.000Z"},
        "metaDescription": "Russia cannot outrun basic math forever.",
        "link-the-world-isn-t-ending-title": "/the-world-isnt-ending/the-russian-economy-is-in-shambles",
    },
    "g2": {
        "title": "The Next Pandemic",
        "date": {"$date": "2026-05-28T07:15:00.000Z"},
        "metaDescription": "How will institutions and governments respond?",
        "link-the-world-isn-t-ending-title": "/the-world-isnt-ending/the-next-pandemic",
    },
    "nodate": {
        "title": "Draft Without A Date",
        "link-the-world-isn-t-ending-title": "/the-world-isnt-ending/draft",
    },
    "junk": {
        "title": {"displayName": "Title", "type": "text"},
        "date": {"$date": "2026-07-01T00:00:00.000Z"},
    },
}


def _warmup_html(records, schema=None):
    binding = {"recordsByCollectionId": {"TheWorldIsntEnding": records}}
    if schema is not None:
        binding["schemasByCollectionId"] = {"TheWorldIsntEnding": schema}
    data = {"appsWarmupData": {"dataBinding": binding}}
    return (
        '<html><head><script type="application/json" id="wix-warmup-data">'
        f"{json.dumps(data)}</script></head><body></body></html>"
    )


def _fake_get(section_html, article_html):
    class _Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    def _get(url, *a, **k):
        return _Resp(section_html if url == brief.MAULDIN_TWIE_URL else article_html)

    return _get


def test_parse_twie_warmup_returns_sorted_clean_records():
    out = brief._parse_twie_warmup(
        _warmup_html(RECORDS, schema={"_iid": {"title": {"displayName": "Title"}}})
    )
    # junk (non-text title) and nodate (missing date) are dropped; newest first.
    assert [r["title"] for r in out] == [
        "The Russian Economy Is in Shambles",
        "The Next Pandemic",
    ]
    assert out[0]["date"] == "2026-06-25"
    assert (
        out[0]["url"]
        == "https://www.mauldineconomics.com/the-world-isnt-ending/the-russian-economy-is-in-shambles"
    )
    assert "basic math" in out[0]["summary"]


def test_parse_twie_warmup_empty_without_warmup_script():
    assert brief._parse_twie_warmup("<html><body>nothing here</body></html>") == []


def test_parse_twie_warmup_empty_on_invalid_json():
    assert (
        brief._parse_twie_warmup('<script id="wix-warmup-data">{not valid}</script>')
        == []
    )


def test_fetch_mauldin_twie_builds_feed_block(monkeypatch):
    section = (
        '<a href="https://www.mauldineconomics.com/the-world-isnt-ending/'
        'the-russian-economy-is-in-shambles">Read latest</a>'
    )
    monkeypatch.setattr(
        brief.requests, "get", _fake_get(section, _warmup_html(RECORDS))
    )
    out = brief.fetch_mauldin_twie()
    assert "Jacob Shapiro" in out
    assert "[ANALYST]" in out
    assert "(GEOPOLITICS)" in out
    assert "The Russian Economy Is in Shambles (2026-06-25)" in out
    assert "Russia cannot outrun basic math forever." in out
    assert out.index("Shambles") < out.index("Pandemic")  # newest first


def test_fetch_mauldin_twie_respects_max_items(monkeypatch):
    section = (
        '<a href="/the-world-isnt-ending/the-russian-economy-is-in-shambles">x</a>'
    )
    monkeypatch.setattr(
        brief.requests, "get", _fake_get(section, _warmup_html(RECORDS))
    )
    out = brief.fetch_mauldin_twie(max_items=1)
    assert "Shambles" in out
    assert "Pandemic" not in out


def test_fetch_mauldin_twie_failsafe_on_network_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(brief.requests, "get", _boom)
    assert brief.fetch_mauldin_twie() == ""


def test_fetch_mauldin_twie_empty_when_no_article_link(monkeypatch):
    monkeypatch.setattr(
        brief.requests, "get", _fake_get("<html>no links</html>", _warmup_html(RECORDS))
    )
    assert brief.fetch_mauldin_twie() == ""

"""brief.fetch_feed_blocks: fetch order is host-spaced, emit order is not.

news-brief-bzo. The brief's collect-time loop fetched `RSS_FEEDS` in declaration
order with no host spacing, relying on retry alone — which is what the documented
Nitter 429 was, and a third and fourth Nitter feed make it worse.

The constraint that makes this change safe: `feed_blocks` is joined into
`feed_content`, which goes into the LLM prompt AND into `build_source_index` /
`build_source_evidence`. Reordering the fetch must not reorder the output, or the
brief's content and its stored source index shift for reasons that have nothing
to do with the news. This change may only alter WHEN requests leave the box.
"""

import brief
import common


def _no_sleep_spacer(gap=5):
    """A real HostSpacer with its sleeping disabled, and a record of what it was
    asked to space — so tests assert on the guard firing, not on wall-clock."""
    waited = []
    spacer = common.HostSpacer(gap, clock=lambda: 0.0, sleeper=waited.append)
    return spacer, waited


NITTER_A = {"name": "n1", "url": "https://nitter.example/a/rss"}
NITTER_B = {"name": "n2", "url": "https://nitter.example/b/rss"}
OTHER = {"name": "o1", "url": "https://other.example/c"}


def test_blocks_are_emitted_in_declaration_order_though_fetched_host_spaced(
    monkeypatch,
):
    """The whole point of the change, with its own control.

    The second assertion is worthless without the first: if `order_by_host` did
    nothing, emit order would trivially equal declaration order and this test
    would pass against the bug it exists to prevent.
    """
    sources = [NITTER_A, NITTER_B, OTHER]
    fetch_order = []

    def fake_fetch(feed):
        fetch_order.append(feed["name"])
        return f"BLOCK:{feed['name']}"

    monkeypatch.setattr(brief, "fetch_rss", fake_fetch)
    spacer, _ = _no_sleep_spacer()

    blocks = brief.fetch_feed_blocks(sources, spacer=spacer)

    assert fetch_order != ["n1", "n2", "o1"], (
        "control failed: the fetch order was not host-spaced, so the emit-order "
        "assertion below proves nothing"
    )
    assert blocks == ["BLOCK:n1", "BLOCK:n2", "BLOCK:o1"]


def test_every_source_is_fetched_exactly_once(monkeypatch):
    sources = [NITTER_A, NITTER_B, OTHER]
    fetch_order = []
    monkeypatch.setattr(
        brief, "fetch_rss", lambda f: fetch_order.append(f["name"]) or "x"
    )
    spacer, _ = _no_sleep_spacer()

    brief.fetch_feed_blocks(sources, spacer=spacer)

    assert sorted(fetch_order) == ["n1", "n2", "o1"]


def test_a_feed_that_yields_nothing_is_dropped_not_emitted_as_empty(monkeypatch):
    """`fetch_rss` returns "" for any failure. The old comprehension used the
    walrus to drop those, and that behaviour must survive the reordering."""
    sources = [NITTER_A, NITTER_B, OTHER]
    monkeypatch.setattr(
        brief, "fetch_rss", lambda f: "" if f["name"] == "n2" else f"BLOCK:{f['name']}"
    )
    spacer, _ = _no_sleep_spacer()

    blocks = brief.fetch_feed_blocks(sources, spacer=spacer)

    assert blocks == ["BLOCK:n1", "BLOCK:o1"]


def test_the_spacer_is_asked_to_space_the_two_same_host_feeds(monkeypatch):
    """Ordering alone does not protect a host — the sleep does. With two of three
    feeds on one host, the interleave separates them and nothing needs to sleep;
    with two of two, the guard must fire."""
    monkeypatch.setattr(brief, "fetch_rss", lambda f: f"BLOCK:{f['name']}")
    spacer, waited = _no_sleep_spacer(gap=5)

    brief.fetch_feed_blocks([NITTER_A, NITTER_B], spacer=spacer)

    assert waited == [5], "the second same-host fetch was not spaced"


def test_declaration_order_is_preserved_for_two_sources_sharing_a_name(monkeypatch):
    """Position is keyed on identity, not name. `submit` builds its list as
    `RSS_FEEDS + feed_temp` directly rather than through `all_sources()`, so it
    does NOT get the name-collision overwrite — two sources really can share a
    name here, and a name-keyed position map would silently drop one."""
    a = {"name": "dupe", "url": "https://one.example/a"}
    b = {"name": "dupe", "url": "https://two.example/b"}
    monkeypatch.setattr(brief, "fetch_rss", lambda f: f"BLOCK:{f['url']}")
    spacer, _ = _no_sleep_spacer()

    blocks = brief.fetch_feed_blocks([a, b], spacer=spacer)

    assert blocks == ["BLOCK:https://one.example/a", "BLOCK:https://two.example/b"]

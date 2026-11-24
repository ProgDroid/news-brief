"""How often does an article page give usable text, per outlet? (al7.7 §6.2)

The storyline editor's `read_article` tool would fetch an article's page when
the feed summary is too thin. Capture keeps only the feed summary, and the
Google News proxy feeds store a news.google.com link, so whether fetching works
is unmeasured. This samples the most recent captured items per outlet, fetches
each page once, and reports counts per outlet. On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/storylines_fetch_probe.py [--per-outlet 15] [--days 7]

Prints outlet names and counts only: never a URL, a title or article text.
Fetches run one at a time with a pause between them. Exit codes: 0 report
printed, 2 nothing to probe (no items in the window).
"""

from __future__ import annotations

import argparse
import base64
import re
import statistics
import sys
import time
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import db  # noqa: E402

# Paragraph text at or above this many characters counts as a usable article
# (a few paragraphs). Below it is a teaser, a consent wall or a paywall stub.
USABLE_CHARS = 1500
FETCH_TIMEOUT = 15
PAUSE_SECONDS = 1.0

CATEGORIES = (
    "usable",
    "thin",
    "empty",
    "blocked",
    "http_error",
    "error",
    "google_unresolved",
)

GOOGLE_HOST = "news.google.com"
_URL_IN_BYTES = re.compile(rb"https?://[\x21-\x7e]+")


class _Paragraphs(HTMLParser):
    """Collects the text inside <p> elements, skipping script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._p_depth = 0
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        elif tag == "p":
            self._p_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "p":
            self._p_depth = max(0, self._p_depth - 1)

    def handle_data(self, data):
        if self._p_depth and not self._skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)


def paragraph_text(html: str) -> str:
    parser = _Paragraphs()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return " ".join(parser.parts)


def google_news_target(url: str) -> str | None:
    """The publisher URL inside an older-style Google News article link, when
    the link's id is base64 that embeds it. Newer ids embed no URL: None."""
    parsed = urlparse(url)
    if parsed.hostname != GOOGLE_HOST:
        return None
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not segment:
        return None
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except Exception:
        return None
    match = _URL_IN_BYTES.search(raw)
    return match.group(0).decode("ascii") if match else None


def classify(status: int | None, final_url: str, text: str) -> str:
    if status is None:
        return "error"
    if urlparse(final_url).hostname == GOOGLE_HOST:
        return "google_unresolved"
    if status in (401, 402, 403, 429, 451):
        return "blocked"
    if status >= 400:
        return "http_error"
    if len(text) >= USABLE_CHARS:
        return "usable"
    return "thin" if text else "empty"


def probe_one(url: str, get) -> tuple[str, int]:
    """(category, paragraph characters) for one item's page."""
    target = google_news_target(url) or url
    try:
        resp = get(target)
    except Exception:
        return "error", 0
    text = paragraph_text(resp.text or "") if resp.status_code < 400 else ""
    return classify(resp.status_code, resp.url or target, text), len(text)


def _default_get(url: str):
    import requests

    return requests.get(
        url,
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": brief.SOURCE_USER_AGENT},
        allow_redirects=True,
    )


def _sample_rows(days: int, per_outlet: int) -> list[tuple[str, str]]:
    sql = """
        SELECT o.name, i.url FROM (
            SELECT outlet_id, url,
                   row_number() OVER (PARTITION BY outlet_id
                                      ORDER BY created_at DESC) AS rn
            FROM items
            WHERE created_at > now() - make_interval(days => %s)
        ) i
        JOIN outlets o ON o.id = i.outlet_id
        WHERE i.rn <= %s
        ORDER BY o.name
    """
    with db.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(sql, (days, per_outlet))]


def report(results: dict[str, list[tuple[str, int]]]) -> str:
    head = ["outlet", "n", *CATEGORIES, "median_chars_usable"]
    lines = ["\t".join(head)]
    totals = defaultdict(int)
    for outlet in sorted(results):
        rows = results[outlet]
        counts = {c: 0 for c in CATEGORIES}
        for cat, _ in rows:
            counts[cat] += 1
            totals[cat] += 1
        usable = [n for cat, n in rows if cat == "usable"]
        median = int(statistics.median(usable)) if usable else 0
        lines.append(
            "\t".join(
                [outlet, str(len(rows)), *(str(counts[c]) for c in CATEGORIES)]
                + [str(median)]
            )
        )
    n = sum(totals.values())
    lines.append(
        "\t".join(["TOTAL", str(n), *(str(totals[c]) for c in CATEGORIES), "-"])
    )
    return "\n".join(lines)


def main(argv=None, *, rows=None, get=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per-outlet", type=int, default=15)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if rows is None:
        rows = _sample_rows(args.days, args.per_outlet)
    if not rows:
        print(f"fetch probe: no items in the last {args.days} days")
        return 2
    get = get or _default_get
    results: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for i, (outlet, url) in enumerate(rows):
        if i:
            sleep(PAUSE_SECONDS)
        results[outlet].append(probe_one(url, get))
    print(
        f"fetch probe: {len(rows)} items, {len(results)} outlets, "
        f"usable = at least {USABLE_CHARS} characters of paragraph text"
    )
    print(report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

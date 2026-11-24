"""How often does an article page give usable text, per outlet? (al7.7 §6.2)

The storyline editor's `read_article` tool would fetch an article's page when
the feed summary is too thin. Capture keeps only the feed summary, and the
Google News proxy feeds store a news.google.com link, so whether fetching works
is unmeasured. This samples the most recent captured items per outlet, fetches
each page once, and reports counts per outlet. On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/storylines_fetch_probe.py [--per-outlet 15] [--days 7]

Google News links are resolved to the publisher first: an older link embeds
the URL in its id; a newer one is decoded through Google's own batchexecute
call, with consent cookies so an EU host is not stopped at consent.google.com.
`google_links` / `google_decoded` say how many were Google links and how many
of those reached the publisher; an undecoded one is `google_unresolved`, never
counted as a thin article.

Prints outlet names and counts only: never a URL, a title or article text.
Fetches run one at a time with a pause between them. Exit codes: 0 report
printed, 2 nothing to probe (no items in the window).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import brief  # noqa: E402  (path shim above must run first)
import db  # noqa: E402

# Extracted text at or above this many characters counts as a usable article
# (a few paragraphs). Below it is a teaser, a consent wall or a paywall stub.
# The extractor is trafilatura; the old <p>-paragraph measure is kept as the
# `usable_by_paragraphs` comparison column.
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
BATCHEXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Google sends EU visitors to consent.google.com first; these cookies record
# the choice so the article page itself is served (round 2 red team: every
# fetch ended on consent.google.com, and the first probe counted that "thin").
GOOGLE_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}
_SIG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_URL_IN_TEXT = re.compile(r"https?://[^\"\\\s]+")


def is_google(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "google.com" or host.endswith(".google.com")


def _article_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname != GOOGLE_HOST:
        return None
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return segment or None


def signature_and_timestamp(html: str) -> tuple[str, str] | None:
    sig, ts = _SIG.search(html), _TS.search(html)
    return (sig.group(1), ts.group(1)) if sig and ts else None


def batchexecute_body(article_id: str, timestamp: str, signature: str) -> str:
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,'
        'null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id}",{timestamp},"{signature}"]'
    )
    return "f.req=" + quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))


def parse_batchexecute(text: str) -> str | None:
    """The publisher URL in a batchexecute reply, or None."""
    _, _, rest = text.partition("\n\n")
    try:
        outer = json.loads(rest)
        inner = json.loads(outer[0][2])
        if isinstance(inner[1], str) and inner[1].startswith("http"):
            return inner[1]
    except Exception:
        pass
    match = _URL_IN_TEXT.search(rest.replace("\\/", "/"))
    if match and not is_google(match.group(0)):
        return match.group(0)
    return None


def resolve_google(url: str, get, post) -> str | None:
    """The publisher URL behind a Google News link, by the old base64 id or
    by the page signature plus Google's batchexecute call. None if neither."""
    old = google_news_target(url)
    if old:
        return old
    article_id = _article_id(url)
    if not article_id:
        return None
    for page in (
        f"https://{GOOGLE_HOST}/articles/{article_id}",
        f"https://{GOOGLE_HOST}/rss/articles/{article_id}",
    ):
        try:
            found = signature_and_timestamp(get(page).text or "")
        except Exception:
            found = None
        if found:
            break
    else:
        return None
    try:
        resp = post(BATCHEXECUTE, batchexecute_body(article_id, found[1], found[0]))
    except Exception:
        return None
    return parse_batchexecute(resp.text or "")


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


def default_extract(html: str) -> str:
    """Article text by trafilatura. Imported here, not at module top, so a
    broken install fails the probe's rows, never the import of this module."""
    import trafilatura

    return trafilatura.extract(html, include_comments=False) or ""


def classify(status: int | None, final_url: str, text: str) -> str:
    if status is None:
        return "error"
    if is_google(final_url):
        return "google_unresolved"
    if status in (401, 402, 403, 429, 451):
        return "blocked"
    if status >= 400:
        return "http_error"
    if len(text) >= USABLE_CHARS:
        return "usable"
    return "thin" if text else "empty"


def probe_one(url: str, get, post=None, extract=None) -> tuple[str, int, str, int]:
    """(category, extracted characters, google, paragraph characters) for one
    item's page, where google is "" for a non-Google link, else "decoded" or
    "undecoded". The category follows the extracted text; the paragraph count
    is the old measure, kept for comparison."""
    extract = extract or default_extract
    google = ""
    target = url
    if is_google(url):
        resolved = resolve_google(url, get, post) if post else google_news_target(url)
        google = "decoded" if resolved else "undecoded"
        if not resolved:
            return "google_unresolved", 0, google, 0
        target = resolved
    try:
        resp = get(target)
    except Exception:
        return "error", 0, google, 0
    if resp.status_code >= 400:
        return classify(resp.status_code, resp.url or target, ""), 0, google, 0
    html = resp.text or ""
    try:
        text = extract(html)
    except Exception:
        return "error", 0, google, 0
    para = len(paragraph_text(html))
    return classify(resp.status_code, resp.url or target, text), len(text), google, para


def _default_get(url: str):
    import requests

    return requests.get(
        url,
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": brief.SOURCE_USER_AGENT},
        cookies=GOOGLE_COOKIES if is_google(url) else None,
        allow_redirects=True,
    )


def _default_post(url: str, body: str):
    import requests

    return requests.post(
        url,
        data=body,
        timeout=FETCH_TIMEOUT,
        headers={
            "User-Agent": brief.SOURCE_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        cookies=GOOGLE_COOKIES,
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


GOOGLE_COLUMNS = ("google_links", "google_decoded")
PARAGRAPH_COLUMN = "usable_by_paragraphs"


def report(results: dict[str, list[tuple[str, int, str, int]]]) -> str:
    head = [
        "outlet",
        "n",
        *CATEGORIES,
        *GOOGLE_COLUMNS,
        PARAGRAPH_COLUMN,
        "median_chars_usable",
    ]
    lines = ["\t".join(head)]
    totals = defaultdict(int)
    for outlet in sorted(results):
        rows = results[outlet]
        counts = {c: 0 for c in (*CATEGORIES, *GOOGLE_COLUMNS, PARAGRAPH_COLUMN)}
        for cat, _, google, para in rows:
            counts[cat] += 1
            if para >= USABLE_CHARS:
                counts[PARAGRAPH_COLUMN] += 1
            if google:
                counts["google_links"] += 1
            if google == "decoded":
                counts["google_decoded"] += 1
        for key, value in counts.items():
            totals[key] += value
        usable = [n for cat, n, _, _ in rows if cat == "usable"]
        median = int(statistics.median(usable)) if usable else 0
        lines.append(
            "\t".join(
                [outlet, str(len(rows))]
                + [
                    str(counts[c])
                    for c in (*CATEGORIES, *GOOGLE_COLUMNS, PARAGRAPH_COLUMN)
                ]
                + [str(median)]
            )
        )
    n = sum(totals[c] for c in CATEGORIES)
    lines.append(
        "\t".join(
            ["TOTAL", str(n)]
            + [str(totals[c]) for c in (*CATEGORIES, *GOOGLE_COLUMNS, PARAGRAPH_COLUMN)]
            + ["-"]
        )
    )
    return "\n".join(lines)


def main(
    argv=None, *, rows=None, get=None, post=None, sleep=time.sleep, extract=None
) -> int:
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
    post = post or _default_post
    results: dict[str, list[tuple[str, int, str, int]]] = defaultdict(list)
    for i, (outlet, url) in enumerate(rows):
        if i:
            sleep(PAUSE_SECONDS)
        results[outlet].append(probe_one(url, get, post, extract))
    print(
        f"fetch probe: {len(rows)} items, {len(results)} outlets, "
        f"usable = at least {USABLE_CHARS} characters of extracted text "
        f"(usable_by_paragraphs: the same bar on <p> text)"
    )
    print(report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

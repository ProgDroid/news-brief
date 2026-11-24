import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "storylines_fetch_probe.py"
_spec = importlib.util.spec_from_file_location("storylines_fetch_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

LONG = "Widget output fell. " * 100  # well over USABLE_CHARS
TARGET = "https://www.example.test/w"
SIG_PAGE = (
    '<c-wiz><div jscontroller="x" data-n-a-sg="SIG123" '
    'data-n-a-ts="1732400000"></div></c-wiz>'
)
REPLY = ")]}'\n\n" + json.dumps(
    [
        [
            "wrb.fr",
            "Fbv4je",
            json.dumps(["garturlres", TARGET, 1]),
            None,
            None,
            None,
            "generic",
        ],
        ["di", 42],
    ]
)


def _resp(status=200, text="", url="https://example.test/a"):
    return SimpleNamespace(status_code=status, text=text, url=url)


def _google(url: str) -> str:
    payload = b"\x08\x13\x22" + bytes([len(url)]) + url.encode() + b"\xd2\x01\x00"
    seg = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"https://news.google.com/rss/articles/{seg}?oc=5"


def test_paragraph_text_keeps_p_only_and_skips_script():
    html = (
        "<html><head><script>var p='<p>no</p>'</script></head><body>"
        "<div>menu</div><p>First <b>para</b>.</p><p>Second.</p>"
        "<style>p{}</style></body></html>"
    )
    assert probe.paragraph_text(html) == "First para . Second."


def test_google_news_target_decodes_an_embedded_url():
    url = "https://www.example.test/world/widgets-2026-11-24/"
    assert probe.google_news_target(_google(url)) == url


def test_google_news_target_none_for_other_hosts_and_opaque_ids():
    assert probe.google_news_target("https://example.test/articles/abc") is None
    opaque = "https://news.google.com/rss/articles/" + base64.urlsafe_b64encode(
        b"\x08\x13\x22\x10AU_yqLopaque"
    ).decode().rstrip("=")
    assert probe.google_news_target(opaque) is None


def test_classify_categories():
    ok = "https://example.test/a"
    assert probe.classify(200, ok, LONG) == "usable"
    assert probe.classify(200, ok, "short") == "thin"
    assert probe.classify(200, ok, "") == "empty"
    assert probe.classify(403, ok, "") == "blocked"
    assert probe.classify(429, ok, "") == "blocked"
    assert probe.classify(500, ok, "") == "http_error"
    assert probe.classify(None, ok, "") == "error"
    assert probe.classify(200, "https://news.google.com/x", LONG) == (
        "google_unresolved"
    )


def test_a_page_that_ends_on_googles_consent_wall_is_unresolved_not_thin():
    # Round 2 red team: every Google link ended on consent.google.com, and the
    # first probe counted that page's ~900 characters as a "thin" article.
    consent = "https://consent.google.com/ml?continue=x"
    assert probe.classify(200, consent, "x" * 925) == "google_unresolved"
    page = _resp(text="<p>Before you continue</p>" * 40, url=consent)
    cat, _, google = probe.probe_one("https://example.test/a", lambda u: page)
    assert (cat, google) == ("google_unresolved", "")


def test_probe_one_fetches_the_decoded_publisher_url():
    target = "https://www.example.test/widgets"
    seen = []

    def get(url):
        seen.append(url)
        return _resp(text=f"<p>{LONG}</p>", url=url)

    cat, chars, google = probe.probe_one(_google(target), get)
    assert seen == [target]
    assert cat == "usable" and chars >= probe.USABLE_CHARS
    assert google == "decoded"


def test_probe_one_unresolved_google_link_and_errors():
    shell = "https://news.google.com/rss/articles/AU_yqL"
    assert probe.probe_one(shell, lambda u: _resp(text=f"<p>{LONG}</p>", url=u)) == (
        "google_unresolved",
        0,
        "undecoded",
    )

    def boom(url):
        raise OSError("down")

    assert probe.probe_one("https://example.test/a", boom) == ("error", 0, "")
    # an error page's body is not counted as article text
    assert probe.probe_one(
        "https://example.test/a", lambda u: _resp(403, f"<p>{LONG}</p>", u)
    ) == ("blocked", 0, "")


def test_signature_timestamp_and_batchexecute_body():
    assert probe.signature_and_timestamp(SIG_PAGE) == ("SIG123", "1732400000")
    assert probe.signature_and_timestamp("<html></html>") is None
    body = probe.batchexecute_body("AU_yqLx", "1732400000", "SIG123")
    assert body.startswith("f.req=")
    decoded = json.loads(unquote(body[len("f.req=") :]))
    rpc, inner, _, _ = decoded[0][0]
    assert rpc == "Fbv4je"
    request = json.loads(inner)
    assert request[0] == "garturlreq"
    assert request[-3:] == ["AU_yqLx", 1732400000, "SIG123"]


def test_parse_batchexecute_reads_the_publisher_url():
    assert probe.parse_batchexecute(REPLY) == TARGET
    assert probe.parse_batchexecute("garbage") is None
    # a reply that only mentions Google itself resolves nothing
    assert probe.parse_batchexecute(")]}'\n\nhttps://news.google.com/x") is None


def test_new_style_google_link_is_decoded_through_batchexecute():
    link = "https://news.google.com/rss/articles/AU_yqLnew?oc=5"
    gets, posts = [], []

    def get(url):
        gets.append(url)
        if url.startswith("https://news.google.com/articles/"):
            return _resp(text=SIG_PAGE, url=url)
        return _resp(text=f"<p>{LONG}</p>", url=url)

    def post(url, body):
        posts.append((url, body))
        return _resp(text=REPLY)

    cat, chars, google = probe.probe_one(link, get, post)
    assert (cat, google) == ("usable", "decoded")
    assert gets == ["https://news.google.com/articles/AU_yqLnew", TARGET]
    assert [u for u, _ in posts] == [probe.BATCHEXECUTE]


def test_undecodable_google_link_is_never_fetched_as_an_article():
    gets = []

    def get(url):
        gets.append(url)
        return _resp(text="<html></html>", url=url)

    cat, chars, google = probe.probe_one(
        "https://news.google.com/rss/articles/AU_yqLnew",
        get,
        lambda u, b: _resp(text=""),
    )
    assert (cat, chars, google) == ("google_unresolved", 0, "undecoded")
    assert gets and all(u.startswith("https://news.google.com/") for u in gets)


def test_main_reports_counts_per_outlet_and_never_a_url_or_text(capsys):
    rows = [
        ("Widget Wire", "https://example.test/secret-path-1"),
        ("Widget Wire", "https://example.test/secret-path-2"),
        ("Gizmo Daily", "https://example.test/secret-path-3"),
        ("Gizmo Daily", "https://news.google.com/rss/articles/AU_yqLsecret"),
    ]
    pages = {
        "https://example.test/secret-path-1": _resp(text=f"<p>{LONG}</p>"),
        "https://example.test/secret-path-2": _resp(403),
        "https://example.test/secret-path-3": _resp(text="<p>Teaser only.</p>"),
        "https://news.google.com/articles/AU_yqLsecret": _resp(text=SIG_PAGE),
        TARGET: _resp(text=f"<p>{LONG}</p>", url=TARGET),
    }
    sleeps = []
    rc = probe.main(
        [],
        rows=rows,
        get=pages.__getitem__,
        post=lambda u, b: _resp(text=REPLY),
        sleep=sleeps.append,
    )
    assert rc == 0
    assert sleeps == [probe.PAUSE_SECONDS] * 3
    out = capsys.readouterr().out
    assert "secret" not in out and "example.test" not in out
    assert "Widget output" not in out and "Teaser" not in out
    lines = {line.split("\t")[0]: line.split("\t") for line in out.splitlines()}
    cols = [
        "outlet",
        "n",
        *probe.CATEGORIES,
        *probe.GOOGLE_COLUMNS,
        "median_chars_usable",
    ]
    wire = dict(zip(cols, lines["Widget Wire"]))
    assert (wire["n"], wire["usable"], wire["blocked"]) == ("2", "1", "1")
    assert (wire["google_links"], wire["google_decoded"]) == ("0", "0")
    gizmo = dict(zip(cols, lines["Gizmo Daily"]))
    assert (gizmo["n"], gizmo["thin"], gizmo["usable"]) == ("2", "1", "1")
    assert (gizmo["google_links"], gizmo["google_decoded"]) == ("1", "1")
    total = dict(zip(cols, lines["TOTAL"]))
    assert (total["n"], total["usable"], total["thin"], total["blocked"]) == (
        "4",
        "2",
        "1",
        "1",
    )
    assert (total["google_links"], total["google_decoded"]) == ("1", "1")


def test_main_with_nothing_to_probe_exits_2(capsys):
    assert probe.main(["--days", "3"], rows=[], get=None) == 2
    assert "no items in the last 3 days" in capsys.readouterr().out

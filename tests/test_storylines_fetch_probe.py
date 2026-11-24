import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "storylines_fetch_probe.py"
_spec = importlib.util.spec_from_file_location("storylines_fetch_probe", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

LONG = "Widget output fell. " * 100  # well over USABLE_CHARS


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


def test_probe_one_fetches_the_decoded_publisher_url():
    target = "https://www.example.test/widgets"
    seen = []

    def get(url):
        seen.append(url)
        return _resp(text=f"<p>{LONG}</p>", url=url)

    cat, chars = probe.probe_one(_google(target), get)
    assert seen == [target]
    assert cat == "usable" and chars >= probe.USABLE_CHARS


def test_probe_one_unresolved_google_link_and_errors():
    shell = "https://news.google.com/rss/articles/AU_yqL"
    assert probe.probe_one(shell, lambda u: _resp(text=f"<p>{LONG}</p>", url=u))[0] == (
        "google_unresolved"
    )

    def boom(url):
        raise OSError("down")

    assert probe.probe_one("https://example.test/a", boom) == ("error", 0)
    # an error page's body is not counted as article text
    assert probe.probe_one(
        "https://example.test/a", lambda u: _resp(403, f"<p>{LONG}</p>", u)
    ) == ("blocked", 0)


def test_main_reports_counts_per_outlet_and_never_a_url_or_text(capsys):
    rows = [
        ("Widget Wire", "https://example.test/secret-path-1"),
        ("Widget Wire", "https://example.test/secret-path-2"),
        ("Gizmo Daily", "https://example.test/secret-path-3"),
    ]
    pages = {
        "https://example.test/secret-path-1": _resp(text=f"<p>{LONG}</p>"),
        "https://example.test/secret-path-2": _resp(403),
        "https://example.test/secret-path-3": _resp(text="<p>Teaser only.</p>"),
    }
    sleeps = []
    rc = probe.main([], rows=rows, get=pages.__getitem__, sleep=sleeps.append)
    assert rc == 0
    assert sleeps == [probe.PAUSE_SECONDS] * 2
    out = capsys.readouterr().out
    assert "secret-path" not in out and "Widget output" not in out
    assert "Teaser" not in out
    lines = {line.split("\t")[0]: line.split("\t") for line in out.splitlines()}
    cols = ["outlet", "n", *probe.CATEGORIES, "median_chars_usable"]
    wire = dict(zip(cols, lines["Widget Wire"]))
    assert (wire["n"], wire["usable"], wire["blocked"]) == ("2", "1", "1")
    gizmo = dict(zip(cols, lines["Gizmo Daily"]))
    assert (gizmo["n"], gizmo["thin"], gizmo["median_chars_usable"]) == ("1", "1", "0")
    total = dict(zip(cols, lines["TOTAL"]))
    assert (total["n"], total["usable"], total["thin"], total["blocked"]) == (
        "3",
        "1",
        "1",
        "1",
    )


def test_main_with_nothing_to_probe_exits_2(capsys):
    assert probe.main(["--days", "3"], rows=[], get=None) == 2
    assert "no items in the last 3 days" in capsys.readouterr().out

"""The event-census labelling page: a small stdlib HTTP service.

Spec: docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md sec 5 (the
guideline, shown verbatim), 6.1 (a separate, privilege-restricted process),
6.2 (access and hardening), 6.3 (the blind pass) and 6.4 (the precision
sample). Plan: docs/superpowers/plans/2026-09-28-event-census.md, Task 6.

Runs as its own compose service under the `census_labeller` role, so it
imports only `db`, `common`, `census` and the standard library -- never
`brief`, `comprehend` or `config` -- and it must never read a settings knob:
that role cannot read `settings`. tests/test_labeller.py enforces the knob
rule (test_labeller_never_reads_a_knob); the import rule is enforced by
review only.

Environment (plain variables, not knobs):
  LABELLER_BASE_URL  required; the URL the operator's phone uses. `Host` must
                     equal its host:port and a POST's `Origin` must equal it.
  LABELLER_BIND      default 0.0.0.0
  LABELLER_PORT      default 8765
  POSTGRES_* / DATABASE_URL  the labeller role's own connection (db.conninfo).

Rendering (plan B6): the server emits a static shell -- the session header and
the sec 5 guideline, both static text -- plus the page data as
`<script type="application/json" id="data">`, with every `<` escaped. Titles in
it are raw text (`html.unescape`d once); `label.js` builds the DOM with
`createElement`/`textContent` only. A `url` is passed only for http/https.

Deviation from spec 6.2, stated: `/open` consumes the link and 303-redirects
to `/` with the session cookie, rather than rendering the page and stripping
the token with `history.replaceState`. The redirect keeps the token out of the
page URL, history and any later Referer without needing script.
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import census
import db
from common import log

DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8765
HEARTBEAT_SECONDS = 60
COOKIE_NAME = "census_session"
COOKIE_MAX_AGE = int(census.SESSION_TTL.total_seconds())
MAX_BODY_BYTES = 256 * 1024
# A stalled or slow-loris client may hold a handler thread only this long.
SOCKET_TIMEOUT_SECONDS = 30
STATIC_DIR = Path(__file__).resolve().parent / "labeller_static"
# Exact paths only: nothing under /static/ is resolved against the filesystem.
STATIC_FILES = {
    "/static/label.js": ("label.js", "text/javascript; charset=utf-8"),
    "/static/label.css": ("label.css", "text/css; charset=utf-8"),
}

# Spec 6.2, verbatim, on every response -- send_error paths included (F22).
SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)

# Spec sec 5, verbatim. `**x**` marks the spec's bold; everything else is
# escaped and rendered by the server as static text.
GUIDELINE = (
    (
        "p",
        "Put items in one group only if they report **the same occurrence**: the "
        "same action, statement or disclosure, by the same actor(s), at the same "
        "time. Different outlets, headlines, angles, sourcing or updated figures "
        "about that one occurrence still belong together. A **reaction, "
        "consequence or follow-up** is a different occurrence: leave it out of "
        "the group, even though it is the same story. Leave an item ungrouped "
        "when it matches nothing. Mark it **unsure** when you cannot tell from "
        "what is shown, even after opening the link.",
    ),
    ("p", "Boundary cases:"),
    (
        "ol",
        (
            '"Israel strikes depot near Tyre" and "Lebanon says Israeli strike '
            'killed two": **same**.',
            '"Earthquake kills 12" and "Death toll rises to 40": **same**, '
            "because the figure is an update.",
            '"Iran vows retaliation after strike" and the strike: **different**, '
            "because it is a reaction.",
            '"Stocks fall as oil jumps on Middle East strikes" and the strike: '
            "**different**, because it is its own occurrence.",
            '"Fed holds rates" and "Powell: cuts not imminent", from the same '
            "press conference: **different**. The operator ruled this on "
            "2026-09-28. It matches done-vs-said.",
            "The same story through two feeds of one outlet: **same**. This is "
            "rare, because capture deduplicates per outlet "
            "(`capture.py:45–55`).",
        ),
    ),
    (
        "p",
        "**Unsure, one rule.** An unsure item is excluded from every metric. Its "
        "group is scored on its remaining items, and it drops out only if fewer "
        "than two outlets remain. The unsure share is reported.",
    ),
)


class _BadRequest(Exception):
    """A request body that does not have the route's shape: 400."""


def _now() -> datetime:
    """The request's `now` (F13: census functions compare against it, never
    SQL now()). A function so tests can pin it."""
    return datetime.now(timezone.utc)


def _inline(text: str) -> str:
    """Escape, then turn the guideline's `**bold**` and `code` into markup."""
    out = []
    for i, part in enumerate(text.split("**")):
        part = html.escape(part, quote=False)
        pieces = part.split("`")
        part = "".join(
            f"<code>{p}</code>" if j % 2 else p for j, p in enumerate(pieces)
        )
        out.append(f"<strong>{part}</strong>" if i % 2 else part)
    return "".join(out)


def _guideline_html() -> str:
    blocks = []
    for kind, content in GUIDELINE:
        if kind == "ol":
            entries = "".join(f"<li>{_inline(c)}</li>" for c in content)
            blocks.append(f"<ol>{entries}</ol>")
        else:
            blocks.append(f"<p>{_inline(content)}</p>")
    return (
        '<details id="guideline"><summary>Guideline</summary>'
        + "".join(blocks)
        + "</details>"
    )


_GUIDELINE_HTML = _guideline_html()


def _safe_url(url: str | None) -> str | None:
    """B6: a link is passed to the page only for http and https."""
    if not url:
        return None
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def _item_json(item: dict) -> dict:
    created = item["created_at"].astimezone(timezone.utc)
    return {
        "id": item["id"],
        "title": html.unescape(item["title"] or ""),
        "url": _safe_url(item["url"]),
        "outlet": item["outlet"],
        "created_at": created.isoformat(),
    }


def render_page(data: dict) -> bytes:
    """The static shell. Only the header and the guideline are markup; all
    page content travels in the #data block and is built by label.js."""
    payload = json.dumps(data).replace("<", "\\u003c")
    header = html.escape(f"Session {data['session_no']}/{census.TOTAL_SESSIONS}")
    doc = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Event census</title>"
        '<link rel="stylesheet" href="/static/label.css">'
        "</head><body>"
        '<header id="top">'
        f'<h1 id="session">{header}</h1>'
        f"{_GUIDELINE_HTML}"
        "</header>"
        '<div id="unsaved" hidden></div>'
        '<main id="app"></main>'
        "<noscript><p>This page needs JavaScript.</p></noscript>"
        f'<script type="application/json" id="data">{payload}</script>'
        '<script src="/static/label.js"></script>'
        "</body></html>\n"
    )
    return doc.encode("utf-8")


def _page_data(conn, now: datetime) -> dict:
    # One transaction decides what to serve and stamps its `open` event
    # (D1a): the stamp claims the window against the repeat.
    task = census.serve_page(conn, now)
    data: dict = {
        "kind": task.kind,
        "session_no": task.session_no,
        "total_sessions": census.TOTAL_SESSIONS,
        "window_id": task.window_id,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
    }
    if task.kind == "blind":
        # No `detail` here: it is "<n> items", and the header shows the
        # session number only (F23, sec 4.6).
        data["items"] = [
            _item_json(i) for i in census.window_items(conn, task.window_id)
        ]
        data["assignments"] = {
            str(item_id): {"group_id": group_id, "unsure": unsure}
            for item_id, (group_id, unsure) in census.latest_assignments(
                conn, task.window_id
            ).items()
        }
    elif task.kind == "precision":
        pairs = census.precision_pairs(conn, task.window_id)
        wanted = {i for pair in pairs for i in pair}
        data["pairs"] = [list(p) for p in pairs]
        data["items"] = [
            _item_json(i)
            for i in census.window_items(conn, task.window_id)
            if i["id"] in wanted
        ]
    else:
        data["detail"] = task.detail
    return data


# ── Request body validation ──────────────────────────────────────────────────


def _int(body: dict, key: str, bits: int = 64) -> int:
    """A JSON integer that fits its column: BIGINT (64) for ids, INTEGER (32)
    for `client_seq`. Out of range is the client's error (400), not a DB
    error the client would retry forever as a 503."""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequest(f"{key} must be an integer")
    limit = 1 << (bits - 1)
    if not -limit <= value < limit:
        raise _BadRequest(f"{key} is outside the {bits}-bit integer range")
    return value


def _str(body: dict, key: str, max_len: int = 500) -> str:
    value = body.get(key)
    if not isinstance(value, str) or len(value) > max_len:
        raise _BadRequest(f"{key} must be a string of at most {max_len} characters")
    return value


def _rows(body: dict) -> list[tuple[int, int | None, bool]]:
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise _BadRequest("rows must be a list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise _BadRequest("each row must be an object")
        group_id = row.get("group_id")
        if group_id is not None:
            group_id = _int(row, "group_id")
        unsure = row.get("unsure")
        if not isinstance(unsure, bool):
            raise _BadRequest("unsure must be a boolean")
        out.append((_int(row, "item_id"), group_id, unsure))
    return out


# ── Routes: each takes (conn, body, now) and returns (status, json | None) ──


def _api_groups(conn, body, now):
    group_id = census.create_group(conn, _int(body, "window_id"), now)
    return 200, {"group_id": group_id}


def _api_assign(conn, body, now):
    window_id = _int(body, "window_id")
    tab_id = _str(body, "tab_id", max_len=64)
    if not tab_id:
        raise _BadRequest("tab_id is empty")
    client_seq = _int(body, "client_seq", bits=32)
    census.save_assignments(conn, window_id, tab_id, client_seq, _rows(body), now)
    # save_assignments does not log; the route does, after a successful save.
    # The two are separate transactions, so a Finish from another tab can
    # commit in between and make record_event refuse the event (BadWrite).
    # The labels ARE saved, so the answer is still 204 (ruling R13): a 400
    # would tell the operator "not saved" about a write that landed.
    try:
        census.record_event(conn, window_id, "action", now)
    except census.BadWrite as exc:
        log.info(f"labeller: assignment saved but its action event refused: {exc}")
    return 204, None


def _api_heartbeat(conn, body, now):
    census.record_event(conn, _int(body, "window_id"), "heartbeat", now)
    return 204, None


def _api_finish(conn, body, now):
    # finish_blind logs its own `finish` event.
    census.finish_blind(conn, _int(body, "window_id"), now)
    return 204, None


def _api_abandon(conn, body, now):
    # abandon logs its own event, and refuses an empty reason (BadWrite).
    reason = body.get("reason")
    if not isinstance(reason, str):
        raise _BadRequest("reason must be a string")
    census.abandon(conn, _int(body, "window_id"), reason, now)
    return 204, None


def _api_precision(conn, body, now):
    census.save_precision(
        conn,
        _int(body, "window_id"),
        _int(body, "item_a"),
        _int(body, "item_b"),
        _str(body, "decision", max_len=20),
        now,
    )
    return 204, None


API_ROUTES = {
    "/api/groups": _api_groups,
    "/api/assign": _api_assign,
    "/api/heartbeat": _api_heartbeat,
    "/api/finish": _api_finish,
    "/api/abandon": _api_abandon,
    "/api/precision": _api_precision,
}


class _Handler(BaseHTTPRequestHandler):
    # Set per server by make_server.
    expected_host = ""
    expected_origin = ""

    server_version = "census-labeller"
    # StreamRequestHandler applies this to the socket (M3).
    timeout = SOCKET_TIMEOUT_SECONDS
    sys_version = ""

    # ── Response plumbing ────────────────────────────────────────────────────

    def end_headers(self):
        # F22: every path, send_error's 400/404/414/501 included.
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _send(self, status, body=b"", content_type=None, headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _forbidden(self, reason: str = "session"):
        """403 whose body names the reason CLASS only (`session` or `origin`),
        so the page can tell an expired session from a mismatched address
        without the body leaking which check or value failed."""
        self._send(403, f"forbidden: {reason}".encode(), "text/plain; charset=utf-8")

    def _send_json(self, status, payload):
        if payload is None:
            self._send(status)
        else:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def log_request(self, code="-", size="-"):
        # Never the query string: /open carries the link token in it.
        path = urlsplit(self.path).path
        log.info(f"labeller {self.command} {path} {code}")

    def log_message(self, format, *args):
        log.info("labeller " + (format % args))

    # ── Checks ───────────────────────────────────────────────────────────────

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() == self.expected_host

    def _origin_ok(self) -> bool:
        return (self.headers.get("Origin") or "").lower() == self.expected_origin

    def _session_token(self) -> str | None:
        """The first `census_session=` pair of the Cookie header, parsed by
        hand. Not `http.cookies.SimpleCookie`: it silently stops at the first
        cookie it cannot tokenise (`theme=dark mode`, `a={"x":1}`, a
        name-only `b`), so another service on the same LAN host could lock
        the operator out (review I1). Tokens are `token_urlsafe`: no quoting
        to undo."""
        for raw in self.headers.get_all("Cookie") or ():
            for part in raw.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name.strip() == COOKIE_NAME and value.strip():
                    return value.strip()
        return None

    def _with_db(self, fn):
        """Run `fn(conn)` on this request's own connection (F21), closed
        before the response is written. Returns (status, payload); maps the
        census exceptions to 409/400 and anything else to a retryable 5xx."""
        try:
            with db.connect() as conn:
                return fn(conn)
        except census.WindowClosed:
            return 409, {"error": "window closed"}
        except (census.BadWrite, _BadRequest) as exc:
            return 400, {"error": str(exc)}
        except Exception:
            log.exception("labeller: request failed")
            return 503, {"error": "unavailable"}

    def _authorised(self, conn, now) -> bool:
        token = self._session_token()
        return token is not None and census.session_valid(conn, token, now)

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        if not self._host_ok():
            return self._forbidden("origin")
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/open":
            return self._open(parts.query)
        if path.startswith("/static/"):
            return self._static(path)
        # No cookie: refuse before opening a DB connection (M5).
        if self._session_token() is None:
            return self._forbidden()
        now = _now()

        def page(conn):
            if not self._authorised(conn, now):
                return 403, None
            if path != "/":
                return 404, None
            return 200, render_page(_page_data(conn, now))

        status, payload = self._with_db(page)
        if status == 403:
            return self._forbidden()
        if status == 404:
            return self.send_error(404)
        if status == 200:
            return self._send(200, payload, "text/html; charset=utf-8")
        return self._send_json(status, payload)

    def _open(self, query: str):
        token = parse_qs(query).get("t", [""])[0]
        if not token:
            return self._forbidden()
        now = _now()
        status, session = self._with_db(
            lambda conn: (200, census.open_session(conn, token, now))
        )
        if status != 200:
            # The link was not consumed (the transaction rolled back), so it
            # can be retried: say "unavailable", not "forbidden".
            return self._send_json(status, session)
        if session is None:
            return self._forbidden()
        cookie = (
            f"{COOKIE_NAME}={session}; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={COOKIE_MAX_AGE}"
        )
        self._send(303, headers=(("Location", "/"), ("Set-Cookie", cookie)))

    def _static(self, path: str):
        entry = STATIC_FILES.get(path)
        if entry is None:
            return self.send_error(404)
        name, content_type = entry
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self.send_error(404)
        self._send(200, body, content_type)

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._forbidden("origin")
        # No cookie: refuse before reading the body or opening a DB
        # connection (M5).
        if self._session_token() is None:
            return self._forbidden()
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send_error(400)
        if length > MAX_BODY_BYTES:
            return self.send_error(413)
        raw = self.rfile.read(length) if length > 0 else b""
        now = _now()

        def api(conn):
            if not self._authorised(conn, now):
                return 403, None
            route = API_ROUTES.get(path)
            if route is None:
                return 404, None
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise _BadRequest("the body is not JSON") from exc
            if not isinstance(body, dict):
                raise _BadRequest("the body must be a JSON object")
            return route(conn, body, now)

        status, payload = self._with_db(api)
        if status == 403:
            return self._forbidden()
        if status == 404:
            return self.send_error(404)
        return self._send_json(status, payload)


def make_server(base_url: str, bind: str, port: int) -> ThreadingHTTPServer:
    """A threaded server (F25: one slow phone connection must not stall the
    rest) answering only for `base_url`'s host:port and origin."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"LABELLER_BASE_URL must be an http(s) URL: {base_url!r}")
    handler = type(
        "LabellerHandler",
        (_Handler,),
        {
            "expected_host": parts.netloc.lower(),
            "expected_origin": f"{parts.scheme}://{parts.netloc}".lower(),
        },
    )
    return ThreadingHTTPServer((bind, port), handler)


def _self_check() -> int:
    """Spec 6.1: verify every grant up front, on the labeller's OWN connection
    (missing_privileges checks current_user), naming each one missing."""
    try:
        with db.connect(connect_timeout=10) as conn:
            missing = census.missing_privileges(conn)
    except Exception as exc:
        print(f"database unreachable: {exc}", file=sys.stderr, flush=True)
        return 3
    if missing:
        for grant in missing:
            print(f"missing grant: {grant}", file=sys.stderr, flush=True)
        return 3
    return 0


def main() -> int:
    base_url = os.environ.get("LABELLER_BASE_URL", "").strip()
    if not base_url:
        print("LABELLER_BASE_URL is required", file=sys.stderr, flush=True)
        return 2
    bind = os.environ.get("LABELLER_BIND", "").strip() or DEFAULT_BIND
    port_text = os.environ.get("LABELLER_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(port_text)
    except ValueError:
        print(f"LABELLER_PORT is not a port: {port_text!r}", file=sys.stderr)
        return 2

    if not os.environ.get("POSTGRES_PASSWORD", "").strip():
        print(
            "CENSUS_LABELLER_PASSWORD is unset: set it in .env to the password "
            "given to the census_labeller role by scripts/census_grants.py",
            file=sys.stderr,
            flush=True,
        )
        return 3
    code = _self_check()
    if code:
        return code

    try:
        server = make_server(base_url, bind, port)
    except (OSError, ValueError) as exc:
        print(f"labeller cannot start: {exc}", file=sys.stderr, flush=True)
        return 1
    log.info(f"labeller listening on {bind}:{port} for {base_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

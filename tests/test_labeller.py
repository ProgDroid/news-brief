"""`labeller.py`: the stdlib HTTP labelling page (plan Task 6, spec 6.1-6.4).

Two kinds of test:

- in-process: `labeller.make_server` on 127.0.0.1:0 in a thread, driven with
  `http.client` (F20: `requests` is blocked by conftest even for loopback).
  The server is built for `BASE`, a name that does not resolve; every request
  sends `Host: census.test:8765` explicitly, which is also what lets a test
  send a WRONG one. Time is pinned by patching `labeller._now` (F13).
- subprocess: `labeller.py` launched with only the spec 6.1 environment, as the
  real `census_labeller` role (spec 10, "The service really starts").

Every server is shut down in its fixture finalizer and every subprocess is
terminated in `finally` (F21): a labeller connection left idle in transaction
would hang the next fixture's DROP SCHEMA.
"""

import html
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from psycopg.conninfo import conninfo_to_dict

import census
import census_fixtures as cf
import common
import db
import labeller

pytestmark = pytest.mark.skipif(
    not db.is_configured(),
    reason="No database is configured: start a Postgres and export DATABASE_URL",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = cf.NOW
PASSWORD = "labeller-test-password"
HOST = "census.test:8765"
BASE = f"http://{HOST}"

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture()
def kb():
    with db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        c.commit()
        db.run_migrations(c)
        c.commit()
        # Roles are cluster-global and survive DROP SCHEMA; grants do not.
        census.apply_labeller_grants(c, PASSWORD)
        yield c


@pytest.fixture()
def labeller_server(kb, monkeypatch):
    monkeypatch.setattr(labeller, "_now", lambda: NOW)
    srv = labeller.make_server(BASE, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)


def _request(
    srv_or_port,
    method,
    path,
    body=None,
    cookie=None,
    host=HOST,
    origin=BASE,
    raw_body=None,
):
    port = (
        srv_or_port if isinstance(srv_or_port, int) else srv_or_port.server_address[1]
    )
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host}
    if origin is not None and method == "POST":
        headers["Origin"] = origin
    if cookie is not None:
        headers["Cookie"] = f"census_session={cookie}"
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        return Resp(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


def _login(kb, srv) -> str:
    link = census.mint_link(kb, NOW)
    r = _request(srv, "GET", f"/open?t={quote(link)}")
    assert r.status == 303, r.body
    m = re.match(r"census_session=([^;]+);", r.headers["Set-Cookie"])
    assert m, r.headers["Set-Cookie"]
    return m.group(1)


_DATA_BLOCK = re.compile(
    r'<script type="application/json" id="data">(.*?)</script>', re.DOTALL
)


def _data(r: Resp) -> dict:
    m = _DATA_BLOCK.search(r.text)
    assert m, "the page carries no #data block"
    return json.loads(m.group(1))


def _events(kb, window_id) -> list[str]:
    rows = kb.execute(
        "SELECT kind FROM census_events WHERE window_id = %s ORDER BY id",
        (window_id,),
    ).fetchall()
    kb.commit()
    return [k for (k,) in rows]


def _assert_security_headers(r: Resp):
    for name, value in SECURITY_HEADERS.items():
        assert r.headers.get(name) == value, (r.status, name, r.headers.get(name))


# ── The service really starts (subprocess, spec 10) ─────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _labeller_env(port: int, tmp_path) -> dict:
    params = conninfo_to_dict(db.conninfo())
    env = {
        "POSTGRES_HOST": params.get("host", "localhost"),
        "POSTGRES_PORT": str(params.get("port", "5432")),
        "POSTGRES_DB": params["dbname"],
        "POSTGRES_USER": census.LABELLER_ROLE,
        "POSTGRES_PASSWORD": PASSWORD,
        "LABELLER_BASE_URL": f"http://127.0.0.1:{port}",
        "LABELLER_BIND": "127.0.0.1",
        "LABELLER_PORT": str(port),
        "NEWSBRIEF_LOG_FILE": "0",
        "NEWSBRIEF_DATA_DIR": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _spawn(env) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "labeller.py"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_service_starts_as_the_labeller_role_and_refuses_without_cookie(kb, tmp_path):
    port = _free_port()
    proc = _spawn(_labeller_env(port, tmp_path))
    try:
        deadline = time.monotonic() + 10
        r = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                r = _request(port, "GET", "/", host=f"127.0.0.1:{port}")
                break
            except (ConnectionRefusedError, ConnectionResetError, OSError):
                time.sleep(0.1)
        assert proc.poll() is None, f"labeller exited: {proc.returncode}"
        assert r is not None, "labeller did not answer within 10 s"
        assert r.status == 403
        assert r.text == "forbidden"
        _assert_security_headers(r)
    finally:
        proc.terminate()
        _out, err = proc.communicate(timeout=10)
    # A 403 served by a process that could not reach the DB would still be a
    # 403; the role check is what the startup self-check proves, so it must
    # not have complained.
    assert "missing grant" not in err, err


def test_service_exits_naming_a_missing_grant(kb, tmp_path):
    kb.execute("REVOKE INSERT ON public.census_events FROM census_labeller")
    kb.commit()
    port = _free_port()
    proc = _spawn(_labeller_env(port, tmp_path))
    try:
        code = proc.wait(timeout=10)
        _out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    assert code == 3, err
    assert "missing grant: INSERT on census_events" in err, err


def test_service_exits_when_the_database_is_unreachable(kb, tmp_path):
    port = _free_port()
    env = _labeller_env(port, tmp_path)
    env["POSTGRES_PASSWORD"] = "not-the-password"
    proc = _spawn(env)
    try:
        code = proc.wait(timeout=20)
        _out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    assert code == 3, err
    assert "database unreachable" in err, err


# ── No knob reads (spec 6.1, 10) ────────────────────────────────────────────


def test_labeller_never_reads_a_knob(kb, labeller_server, monkeypatch):
    cf.prepared(kb)
    real = common.__getattr__

    def guarded(name):
        if name in common.KNOBS:
            raise AssertionError(f"the labeller read the knob {name}")
        return real(name)

    monkeypatch.setattr(common, "__getattr__", guarded)
    # Control: the patch really intercepts a knob read (conftest's
    # _stubbed_config pops every knob from the module dict at setup).
    knob = next(n for n in common.KNOBS if n not in vars(common))
    with pytest.raises(AssertionError):
        getattr(common, knob)

    statuses = []
    link = census.mint_link(kb, NOW)
    r = _request(labeller_server, "GET", f"/open?t={quote(link)}")
    statuses.append(r.status)
    cookie = re.match(r"census_session=([^;]+);", r.headers["Set-Cookie"]).group(1)

    r = _request(labeller_server, "GET", "/", cookie=cookie)
    statuses.append(r.status)
    w1 = _data(r)["window_id"]
    for path in ("/static/label.js", "/static/label.css"):
        statuses.append(_request(labeller_server, "GET", path).status)

    r = _request(
        labeller_server, "POST", "/api/groups", {"window_id": w1}, cookie=cookie
    )
    statuses.append(r.status)
    group_id = r.json()["group_id"]
    a, b = cf.cross_outlet_groups(kb, w1, 1)[0]
    rows = [{"item_id": i, "group_id": group_id, "unsure": False} for i in (a, b)]
    body = {"window_id": w1, "tab_id": "t1", "client_seq": 1, "rows": rows}
    statuses.append(
        _request(labeller_server, "POST", "/api/assign", body, cookie=cookie).status
    )
    statuses.append(
        _request(
            labeller_server, "POST", "/api/heartbeat", {"window_id": w1}, cookie=cookie
        ).status
    )
    statuses.append(
        _request(
            labeller_server, "POST", "/api/finish", {"window_id": w1}, cookie=cookie
        ).status
    )

    r = _request(labeller_server, "GET", "/", cookie=cookie)
    statuses.append(r.status)
    page = _data(r)
    assert page["kind"] == "precision"
    [[pa, pb]] = page["pairs"]
    statuses.append(
        _request(
            labeller_server,
            "POST",
            "/api/precision",
            {"window_id": w1, "item_a": pa, "item_b": pb, "decision": "same"},
            cookie=cookie,
        ).status
    )

    r = _request(labeller_server, "GET", "/", cookie=cookie)
    statuses.append(r.status)
    w2 = _data(r)["window_id"]
    assert w2 != w1
    statuses.append(
        _request(
            labeller_server,
            "POST",
            "/api/abandon",
            {"window_id": w2, "reason": "test abandon"},
            cookie=cookie,
        ).status
    )

    # open, page, js, css, groups, assign, heartbeat, finish, page
    # (precision), precision, page (window 2), abandon.
    assert len(statuses) == 12
    assert all(200 <= s < 300 or s == 303 for s in statuses), statuses


# ── Access (spec 6.2) ───────────────────────────────────────────────────────


def test_link_flow_sets_cookie_and_redirects(kb, labeller_server):
    link = census.mint_link(kb, NOW)
    r = _request(labeller_server, "GET", f"/open?t={quote(link)}")
    assert r.status == 303
    assert r.headers["Location"] == "/"
    cookie_header = r.headers["Set-Cookie"]
    assert re.fullmatch(
        r"census_session=[A-Za-z0-9_-]+; HttpOnly; SameSite=Lax; Path=/; "
        r"Max-Age=2592000",
        cookie_header,
    ), cookie_header
    token = re.match(r"census_session=([^;]+);", cookie_header).group(1)
    assert census.session_valid(kb, token, NOW)

    reuse = _request(labeller_server, "GET", f"/open?t={quote(link)}")
    assert reuse.status == 403
    assert reuse.text == "forbidden"
    assert "Set-Cookie" not in reuse.headers

    assert _request(labeller_server, "GET", "/open?t=not-a-token").status == 403
    assert _request(labeller_server, "GET", "/open").status == 403
    # The session cookie is what `/` needs; the link token is not a session.
    assert _request(labeller_server, "GET", "/", cookie=token).status == 200
    assert _request(labeller_server, "GET", "/", cookie=link).status == 403
    assert _request(labeller_server, "GET", "/").status == 403


def test_expired_link_is_forbidden(kb, labeller_server):
    link = census.mint_link(kb, NOW - census.LINK_TTL)
    r = _request(labeller_server, "GET", f"/open?t={quote(link)}")
    assert r.status == 403


def test_wrong_host_or_origin_is_forbidden(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    w1 = cf.window_id(kb, 1)
    beat = {"window_id": w1}

    # Controls: the right Host and Origin are accepted.
    assert _request(labeller_server, "GET", "/", cookie=cookie).status == 200
    assert (
        _request(labeller_server, "POST", "/api/heartbeat", beat, cookie=cookie).status
        == 204
    )

    wrong_host = _request(
        labeller_server, "GET", "/", cookie=cookie, host="evil.test:8765"
    )
    assert wrong_host.status == 403
    assert wrong_host.text == "forbidden"
    assert (
        _request(
            labeller_server, "GET", "/", cookie=cookie, host="census.test:9999"
        ).status
        == 403
    )
    assert (
        _request(
            labeller_server, "GET", "/static/label.js", host="evil.test:8765"
        ).status
        == 403
    )
    for origin in ("http://evil.test:8765", "https://census.test:8765", None):
        r = _request(
            labeller_server,
            "POST",
            "/api/heartbeat",
            beat,
            cookie=cookie,
            origin=origin,
        )
        assert r.status == 403, origin
        assert r.text == "forbidden"

    # A link opened through the wrong Host is refused BEFORE it is consumed.
    link = census.mint_link(kb, NOW)
    assert (
        _request(
            labeller_server, "GET", f"/open?t={quote(link)}", host="evil.test"
        ).status
        == 403
    )
    assert _request(labeller_server, "GET", f"/open?t={quote(link)}").status == 303

    # Exactly the one control heartbeat was logged: none of the refused ones.
    assert _events(kb, w1).count("heartbeat") == 1


def test_every_response_carries_the_security_headers(kb, labeller_server):
    cf.prepared(kb)
    link = census.mint_link(kb, NOW)
    seen = {}

    r = _request(labeller_server, "GET", f"/open?t={quote(link)}")
    seen[303] = r
    cookie = re.match(r"census_session=([^;]+);", r.headers["Set-Cookie"]).group(1)
    seen[200] = _request(labeller_server, "GET", "/", cookie=cookie)
    w1 = cf.window_id(kb, 1)
    seen[204] = _request(
        labeller_server, "POST", "/api/heartbeat", {"window_id": w1}, cookie=cookie
    )
    seen[403] = _request(labeller_server, "GET", "/")
    seen[400] = _request(
        labeller_server,
        "POST",
        "/api/heartbeat",
        raw_body=b"{not json",
        cookie=cookie,
    )
    cf.label(kb, w1, [], NOW)
    seen[409] = _request(
        labeller_server,
        "POST",
        "/api/assign",
        {"window_id": w1, "tab_id": "t", "client_seq": 1, "rows": []},
        cookie=cookie,
    )
    seen[404] = _request(labeller_server, "GET", "/no-such-page", cookie=cookie)
    seen[501] = _request(labeller_server, "PUT", "/", cookie=cookie)
    seen["static"] = _request(labeller_server, "GET", "/static/label.js")

    for expected, r in seen.items():
        if isinstance(expected, int):
            assert r.status == expected, (expected, r.status, r.body[:200])
        _assert_security_headers(r)


# ── Rendering (B6) ──────────────────────────────────────────────────────────


def test_json_carries_unescaped_text_with_no_literal_lt(kb, labeller_server):
    cf.prepared(kb)
    w1 = cf.window_id(kb, 1)
    items = census.window_items(kb, w1)
    hostile, entity = items[0]["id"], items[1]["id"]
    kb.execute(
        "UPDATE items SET title = %s WHERE id = %s",
        ("<script>alert(1)</script>", hostile),
    )
    kb.execute("UPDATE items SET title = %s WHERE id = %s", ("AT&amp;T", entity))
    kb.commit()
    cookie = _login(kb, labeller_server)

    r = _request(labeller_server, "GET", "/", cookie=cookie)
    assert r.status == 200
    block = _DATA_BLOCK.search(r.text).group(1)
    assert "<" not in block
    assert "<script>alert(1)" not in r.text
    titles = {i["id"]: i["title"] for i in json.loads(block)["items"]}
    assert titles[hostile] == "<script>alert(1)</script>"
    assert titles[entity] == "AT&T"


def test_link_scheme_filter(kb, labeller_server):
    cf.prepared(kb)
    w1 = cf.window_id(kb, 1)
    ids = [i["id"] for i in census.window_items(kb, w1)[:4]]
    urls = ["javascript:alert(1)", "", "/relative", "https://ok.example/x"]
    for item_id, url in zip(ids, urls, strict=True):
        kb.execute("UPDATE items SET url = %s WHERE id = %s", (url, item_id))
    kb.commit()
    cookie = _login(kb, labeller_server)

    page = _data(_request(labeller_server, "GET", "/", cookie=cookie))
    got = {i["id"]: i["url"] for i in page["items"]}
    assert [got[i] for i in ids] == [None, None, None, "https://ok.example/x"]
    assert "javascript:" not in json.dumps(page)


def test_label_js_uses_no_html_sinks():
    js = (REPO_ROOT / "labeller_static" / "label.js").read_text(encoding="utf-8")
    # Control: this is the real client, which builds DOM from text.
    assert "textContent" in js and "createElement" in js
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, sink
    # The only href assignment, and it sits behind a scheme check.
    assert len(re.findall(r"\.href\s*=", js)) == 1
    assert "/^https?:\\/\\//i" in js


def test_page_header_shows_the_session_number_only(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    r = _request(labeller_server, "GET", "/", cookie=cookie)
    header = re.search(r'<h1 id="session">(.*?)</h1>', r.text)
    assert header, r.text[:500]
    assert header.group(1) == "Session 1/17"
    page = _data(r)
    assert page["kind"] == "blind"
    assert page["session_no"] == 1
    # F23: no item count anywhere the page shows text; current_task's
    # "<n> items" detail is withheld from the blind page.
    assert "detail" not in page
    assert f"{len(page['items'])} items" not in r.text


def test_guideline_is_shown_verbatim_in_a_details_element(kb, labeller_server):
    cookie = _login(kb, labeller_server)
    r = _request(labeller_server, "GET", "/", cookie=cookie)
    details = re.search(r"<details[^>]*>(.*?)</details>", r.text, re.DOTALL)
    assert details
    text = html.unescape(re.sub(r"<[^>]+>", "", details.group(1)))
    text = re.sub(r"\s+", " ", text)
    for sentence in (
        "Put items in one group only if they report the same occurrence: the "
        "same action, statement or disclosure, by the same actor(s), at the "
        "same time.",
        "A reaction, consequence or follow-up is a different occurrence: leave "
        "it out of the group, even though it is the same story.",
        "Mark it unsure when you cannot tell from what is shown, even after "
        "opening the link.",
        '"Fed holds rates" and "Powell: cuts not imminent", from the same press '
        "conference: different.",
        "Unsure, one rule. An unsure item is excluded from every metric.",
    ):
        assert sentence in text, sentence


def test_status_page_when_the_census_is_not_prepared(kb, labeller_server):
    cookie = _login(kb, labeller_server)
    r = _request(labeller_server, "GET", "/", cookie=cookie)
    assert r.status == 200
    page = _data(r)
    assert page["kind"] == "not_prepared"
    assert page["detail"] == "the census is not prepared"
    assert re.search(r'<h1 id="session">Session 0/17</h1>', r.text)


# ── Writes ──────────────────────────────────────────────────────────────────


def test_routes_log_the_events_the_spec_names(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    w1 = _data(_request(labeller_server, "GET", "/", cookie=cookie))["window_id"]
    assert _events(kb, w1) == ["open"]

    gid = _request(
        labeller_server, "POST", "/api/groups", {"window_id": w1}, cookie=cookie
    ).json()["group_id"]
    item = census.window_items(kb, w1)[0]["id"]
    body = {
        "window_id": w1,
        "tab_id": "t1",
        "client_seq": 1,
        "rows": [{"item_id": item, "group_id": gid, "unsure": True}],
    }
    assert (
        _request(labeller_server, "POST", "/api/assign", body, cookie=cookie).status
        == 204
    )
    assert census.latest_assignments(kb, w1) == {item: (gid, True)}
    assert (
        _request(
            labeller_server, "POST", "/api/heartbeat", {"window_id": w1}, cookie=cookie
        ).status
        == 204
    )
    assert (
        _request(
            labeller_server, "POST", "/api/finish", {"window_id": w1}, cookie=cookie
        ).status
        == 204
    )
    # finish_blind logs its own event; the route must not add another.
    assert _events(kb, w1) == ["open", "action", "heartbeat", "finish"]
    # A heartbeat from a stale tab after Finish is refused (400), unlogged.
    assert (
        _request(
            labeller_server, "POST", "/api/heartbeat", {"window_id": w1}, cookie=cookie
        ).status
        == 400
    )
    assert _events(kb, w1).count("heartbeat") == 1


def test_assign_after_finish_is_409(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    w1 = cf.window_id(kb, 1)
    groups = cf.cross_outlet_groups(kb, w1, 1)
    cf.label(kb, w1, groups, NOW)
    before = census.latest_assignments(kb, w1)
    assert before  # control: there are blind rows to protect

    body = {
        "window_id": w1,
        "tab_id": "late-tab",
        "client_seq": 7,
        "rows": [{"item_id": groups[0][0], "group_id": None, "unsure": False}],
    }
    r = _request(labeller_server, "POST", "/api/assign", body, cookie=cookie)
    assert r.status == 409
    assert census.latest_assignments(kb, w1) == before
    assert "action" not in _events(kb, w1)
    assert (
        _request(
            labeller_server, "POST", "/api/groups", {"window_id": w1}, cookie=cookie
        ).status
        == 409
    )
    assert (
        _request(
            labeller_server, "POST", "/api/finish", {"window_id": w1}, cookie=cookie
        ).status
        == 409
    )


def test_bad_writes_are_400(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    w1 = cf.window_id(kb, 1)
    w2 = cf.window_id(kb, 2)
    foreign = census.window_items(kb, w2)[0]["id"]

    def post(path, body):
        return _request(labeller_server, "POST", path, body, cookie=cookie).status

    assert post("/api/abandon", {"window_id": w1, "reason": "   "}) == 400
    assert post("/api/abandon", {"window_id": w1}) == 400
    assert post("/api/heartbeat", {"window_id": 999999}) == 400
    assert post("/api/heartbeat", {"window_id": "1"}) == 400
    assert post("/api/groups", {}) == 400
    assert (
        post(
            "/api/assign",
            {
                "window_id": w1,
                "tab_id": "t",
                "client_seq": 1,
                "rows": [{"item_id": foreign, "group_id": None, "unsure": False}],
            },
        )
        == 400
    )
    assert (
        post(
            "/api/assign",
            {"window_id": w1, "tab_id": "t", "client_seq": 1, "rows": "nope"},
        )
        == 400
    )
    assert (
        post(
            "/api/precision",
            {"window_id": w1, "item_a": 1, "item_b": 2, "decision": "maybe"},
        )
        == 400
    )
    assert post("/api/nothing", {"window_id": w1}) == 404
    # Nothing above was written: the window is still untouched and prepared.
    assert census.latest_assignments(kb, w1) == {}
    assert _events(kb, w1) == []
    status = kb.execute(
        "SELECT status FROM census_windows WHERE id = %s", (w1,)
    ).fetchone()[0]
    kb.commit()
    assert status == "prepared"


def test_abandon_with_a_reason_abandons(kb, labeller_server):
    cf.prepared(kb)
    cookie = _login(kb, labeller_server)
    w1 = cf.window_id(kb, 1)
    r = _request(
        labeller_server,
        "POST",
        "/api/abandon",
        {"window_id": w1, "reason": "too tired"},
        cookie=cookie,
    )
    assert r.status == 204
    row = kb.execute(
        "SELECT status, abandon_reason FROM census_windows WHERE id = %s", (w1,)
    ).fetchone()
    kb.commit()
    assert row == ("abandoned", "too tired")
    assert _events(kb, w1) == ["abandon"]


def test_static_paths_are_exact(kb, labeller_server):
    js = _request(labeller_server, "GET", "/static/label.js")
    assert js.status == 200
    assert js.headers["Content-Type"].startswith("text/javascript")
    assert js.body == (REPO_ROOT / "labeller_static" / "label.js").read_bytes()
    css = _request(labeller_server, "GET", "/static/label.css")
    assert css.status == 200
    assert css.headers["Content-Type"].startswith("text/css")
    for path in (
        "/static/../census.py",
        "/static/%2e%2e/census.py",
        "/static/label.js/",
        "/static/other.js",
        "/static/",
    ):
        r = _request(labeller_server, "GET", path)
        assert r.status == 404, path
        assert b"census" not in r.body or b"import" not in r.body

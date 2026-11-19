"""Watch-file event triggers (news-brief-8lp; price rows: news-brief-vap).

The operator pushes `DATA_DIR/watch.json` from a private repository. This
module holds the pure parts of judging it: the loader, the judge's input and
request, the code-side validation of the model's answer, and the once-per-row
alert planning. Telegram, `runtime_state` and the HTTP call are wired in by
`brief.mode_watch`, which keeps everything here testable over plain data.

Design: docs/superpowers/specs/2026-11-18-watch-event-triggers-design.md.
Never commit a real watch row, and never log a row's trigger text: the file
is the operator's private list, and this repository is public.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("newsbrief")

_PRICE_CONDITIONS = ("below", "above", "above_ma200")

# A fixed trailing window, not "since the last finished run": job_runs records a
# crashed run as 'finished' too, so that window would skip items a crash never
# judged. The once-per-row alert state absorbs the overlap (spec "Window").
WINDOW_HOURS = 48
# Above this many distinct titles the job still judges but warns once: the
# signal to revisit the one-call design. Measured 392-1,030/day in Nov 2026.
INPUT_CEILING = 5000


class WatchFileError(Exception):
    """The watch file as a whole cannot be used. The job goes red on this."""


@dataclass(frozen=True)
class EventRow:
    id: str
    name: str
    trigger: str


@dataclass(frozen=True)
class PriceRow:
    id: str
    name: str
    action: str
    signal: str
    condition: str
    level: float | None


@dataclass(frozen=True)
class WatchFile:
    events: list[EventRow]
    prices: list[PriceRow]
    # (row id, or "#<index>" when the row has no usable id; reason)
    skipped: list[tuple[str, str]]


def _text(row: dict, field: str) -> str | None:
    value = row.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_row(row: object) -> EventRow | PriceRow:
    """One row, or ValueError naming what is wrong with it."""
    if not isinstance(row, dict):
        raise ValueError("row is not an object")
    rid, kind, name = _text(row, "id"), _text(row, "kind"), _text(row, "name")
    if rid is None:
        raise ValueError("no string id")
    if name is None:
        raise ValueError("no string name")
    if kind == "event":
        trigger = _text(row, "trigger")
        if trigger is None:
            raise ValueError("event row has no trigger")
        return EventRow(rid, name, trigger)
    if kind == "price":
        signal, action = _text(row, "signal"), _text(row, "action")
        condition = row.get("condition")
        if signal is None:
            raise ValueError("price row has no signal")
        if action is None:
            raise ValueError("price row has no action")
        if condition not in _PRICE_CONDITIONS:
            raise ValueError(f"unknown price condition {condition!r}")
        level = row.get("level")
        if condition == "above_ma200":
            level = None
        elif isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError(f"price condition {condition!r} needs a numeric level")
        else:
            level = float(level)
        return PriceRow(rid, name, action, signal, condition, level)
    raise ValueError(f"unknown kind {kind!r}")


def load_watch(path: Path) -> WatchFile:
    """Parse the watch file in two tiers (spec "File contract").

    File-level problems raise WatchFileError: the file cannot be trusted at
    all. A row-level problem skips only that row and names it, so one typo or
    one kind this deployment does not know yet cannot blind every other row.
    The file is never renamed or modified; the next push replaces it.
    """
    if not path.exists():
        raise WatchFileError(f"{path.name} missing — the push has not landed")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise WatchFileError(f"{path.name} unreadable: {e}") from e
    if not isinstance(data, dict):
        raise WatchFileError(f"{path.name}: top level is not an object")
    if data.get("version") != 1:
        raise WatchFileError(f"{path.name}: unknown version {data.get('version')!r}")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise WatchFileError(f"{path.name}: 'rows' is missing or not a list")

    ids = [r["id"].strip() for r in rows if isinstance(r, dict) and _text(r, "id")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise WatchFileError(f"{path.name}: duplicate row ids {', '.join(dupes)}")

    events: list[EventRow] = []
    prices: list[PriceRow] = []
    skipped: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        try:
            parsed = _parse_row(row)
        except ValueError as e:
            who = (_text(row, "id") if isinstance(row, dict) else None) or f"#{index}"
            skipped.append((who, str(e)))
            continue
        (events if isinstance(parsed, EventRow) else prices).append(parsed)
    return WatchFile(events, prices, skipped)


# ── The judge's input ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Item:
    title: str
    url: str
    outlet: str


def fetch_window_items(conn, as_of: datetime) -> list[Item]:
    """Captured items with created_at in (as_of - 48 h, as_of], oldest first.

    A naive `as_of` is UTC: the replay script takes dates from a human, and a
    local-time reading would shift the window by the host's offset.
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    rows = conn.execute(
        "SELECT i.title, i.url, o.name FROM items i "
        "JOIN outlets o ON o.id = i.outlet_id "
        "WHERE i.created_at > %s AND i.created_at <= %s "
        "ORDER BY i.created_at, i.id",
        (as_of - timedelta(hours=WINDOW_HOURS), as_of),
    ).fetchall()
    return [Item(title, url, outlet) for title, url, outlet in rows]


@dataclass(frozen=True)
class JudgeInput:
    text: str
    # Opaque per-run label -> every item that carried that title. Labels are
    # what the model cites; code maps them back to URLs, so the model never
    # sees a database id or a URL it could alter.
    labels: dict[str, list[Item]]
    n_titles: int


def build_judge_input(rows: list[EventRow], items: list[Item]) -> JudgeInput:
    """Rows and labelled headlines as one prompt block.

    Identical titles (syndicated wire copy) collapse to one label keeping every
    URL; blank titles are dropped. Labels are assigned in first-seen order.
    """
    labels: dict[str, list[Item]] = {}
    by_title: dict[str, str] = {}
    lines: list[str] = []
    for item in items:
        # Collapse all whitespace: an embedded newline could forge a label line.
        title = " ".join(item.title.split())
        if not title:
            continue
        key = title.casefold()
        label = by_title.get(key)
        if label is None:
            label = f"I{len(labels) + 1}"
            by_title[key] = label
            labels[label] = []
            lines.append(f"{label} [{item.outlet}] {title}")
        labels[label].append(item)
    row_lines = [f"- {r.id}: {r.name} — TRIGGER: {r.trigger}" for r in rows]
    text = (
        "WATCH ROWS:\n"
        + "\n".join(row_lines)
        + "\n\nHEADLINES (label [outlet] title):\n"
        + ("\n".join(lines) or "(none)")
    )
    return JudgeInput(text, labels, len(labels))


# ── The call and the code-side check of its answer ───────────────────────────

MAX_LINKS = 3
_VERDICTS = ("triggered", "not", "unclear")

_EMIT_VERDICTS_TOOL = {
    "name": "emit_verdicts",
    "description": "Record exactly one verdict for every watch row.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "row_id": {"type": "string"},
                        "verdict": {"type": "string", "enum": list(_VERDICTS)},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Headline labels (I1, I2, ...) that "
                            "support the verdict. Empty if none.",
                        },
                        "reason": {"type": "string", "description": "One sentence."},
                    },
                    "required": ["row_id", "verdict", "evidence", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
}

_SYSTEM = (
    "You judge whether specific events have happened, using only the headlines "
    "you are given. You never use outside knowledge to decide that an event "
    "happened."
)

_USER_TEMPLATE = """For each watch row below, decide from the headlines whether its \
TRIGGER has happened, then call emit_verdicts with one verdict per row.

- triggered: a headline reports that the trigger has happened. Cite its label.
- unclear: a headline suggests it may have happened, or nearly has, but does \
not establish it. Cite the label.
- not: no headline reports it. Cite nothing.

Cite only labels from the list. A plan, a threat, a negotiation or a forecast \
is not the event itself.

{block}
"""


@dataclass(frozen=True)
class Verdict:
    row_id: str
    verdict: str  # "triggered" | "not" | "unclear" | "invalid"
    evidence: list[Item]
    reason: str


def build_judge_request(rows: list[EventRow], ji: JudgeInput, *, model: str) -> dict:
    """The Messages payload for one forced-tool call covering every row."""
    return {
        "model": model,
        "max_tokens": 512 + 300 * len(rows),
        # Explicit, never the default: adaptive thinking eats max_tokens and
        # truncates the tool call (memory newsbrief-model-config).
        "thinking": {"type": "disabled"},
        "system": _SYSTEM,
        "tools": [_EMIT_VERDICTS_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_verdicts"},
        "messages": [{"role": "user", "content": _USER_TEMPLATE.format(block=ji.text)}],
    }


def _pick_evidence(groups: list[list[Item]]) -> list[Item]:
    """Up to MAX_LINKS distinct URLs, one per cited headline per round, so the
    syndicated copies of the first headline cannot crowd out a second one."""
    picked: list[Item] = []
    seen: set[str] = set()
    for depth in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if depth < len(group) and group[depth].url not in seen:
                seen.add(group[depth].url)
                picked.append(group[depth])
                if len(picked) == MAX_LINKS:
                    return picked
    return picked


def _check_one(raw: dict, ji: JudgeInput) -> Verdict:
    row_id = raw["row_id"]
    verdict = raw.get("verdict")
    reason = str(raw.get("reason") or "").strip()
    if verdict not in _VERDICTS:
        return Verdict(row_id, "invalid", [], f"unknown verdict {verdict!r}")
    labels = raw.get("evidence") or []
    if not isinstance(labels, list):
        return Verdict(row_id, "invalid", [], "evidence is not a list")
    # isinstance first: an unhashable label would raise on the dict lookup.
    unknown = [lb for lb in labels if not isinstance(lb, str) or lb not in ji.labels]
    if unknown:
        # Never drop an unknown label and carry on: a row whose evidence the
        # model invented is not a row we judged (memory
        # never-give-a-model-raw-database-ids).
        return Verdict(row_id, "invalid", [], f"cited unknown labels {unknown}")
    evidence = _pick_evidence([ji.labels[lb] for lb in dict.fromkeys(labels)])
    if verdict == "triggered" and not evidence:
        verdict = "unclear"
    return Verdict(row_id, verdict, evidence, reason)


def validate_verdicts(
    resp: dict, rows: list[EventRow], ji: JudgeInput
) -> list[Verdict]:
    """Exactly one Verdict per row, in row order. Never raises on a bad answer:
    whatever the model got wrong becomes an `invalid` verdict, which is
    reported, never read as `not`."""
    raw = None
    for block in resp.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == "emit_verdicts":
            raw = (block.get("input") or {}).get("verdicts")
            break
    if not isinstance(raw, list):
        why = f"no usable emit_verdicts answer (stop_reason={resp.get('stop_reason')})"
        return [Verdict(r.id, "invalid", [], why) for r in rows]

    wanted = {r.id for r in rows}
    found: dict[str, Verdict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        row_id = entry.get("row_id")
        if not isinstance(row_id, str) or row_id not in wanted or row_id in found:
            continue  # unknown row ids are ignored; the first verdict wins
        found[row_id] = _check_one(entry, ji)
    return [
        found.get(r.id) or Verdict(r.id, "invalid", [], "no verdict returned")
        for r in rows
    ]


def judge_watch(
    conn, rows: list[EventRow], as_of: datetime, *, post, model: str
) -> tuple[JudgeInput, list[Verdict]]:
    """Judge `rows` against the 48 h of items before `as_of`.

    The one function the nightly job and the replay script share, so a replay
    measures production rather than a copy of it. `post` sends a Messages
    payload and returns the decoded response. No items at all raises: an
    empty window means capture is down, and judging it would report `not`
    for news nobody looked at.
    """
    items = fetch_window_items(conn, as_of)
    ji = build_judge_input(rows, items)
    if ji.n_titles == 0:
        raise WatchFileError(
            f"no captured items in the {WINDOW_HOURS} h to {as_of.isoformat()}: "
            "capture may be down"
        )
    resp = post(build_judge_request(rows, ji, model=model))
    usage = resp.get("usage") or {}
    # Before validating: on a truncated tool call the stop_reason is the one
    # datum that tells truncation from a bad answer.
    log.info(
        f"Watch judge: rows={len(rows)} titles={ji.n_titles} "
        f"stop_reason={resp.get('stop_reason')} "
        f"usage(in={usage.get('input_tokens')} out={usage.get('output_tokens')})"
    )
    return ji, validate_verdicts(resp, rows, ji)


# ── Alerting, once per row ───────────────────────────────────────────────────

# One runtime_state key per row id: {"hash": trigger_hash, "strength": 1|2}.
STATE_PREFIX = "watch.alerted."


def strength(v: Verdict) -> int:
    """2 = triggered, 1 = unclear with evidence, 0 = nothing to alert on."""
    if v.verdict == "triggered":
        return 2
    if v.verdict == "unclear" and v.evidence:
        return 1
    return 0


def trigger_hash(row: EventRow) -> str:
    return hashlib.sha256(row.trigger.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Alert:
    row: EventRow
    verdict: Verdict
    key: str
    value: dict


def keep_ids(wf: WatchFile) -> set[str]:
    """Row ids still present in the file: every parsed row, plus rows the
    loader SKIPPED as malformed that carry an id. A skipped row keeps its alert
    state, so fixing a typo does not re-ring an old alert (red-team,
    2026-11-19). Positional skips ("#3") have no id to keep."""
    ids = {r.id for r in wf.events} | {r.id for r in wf.prices}
    return ids | {who for who, _ in wf.skipped if not who.startswith("#")}


def plan_alerts(
    rows: list[EventRow],
    verdicts: list[Verdict],
    state: dict,
    keep_ids: set[str] | None = None,
) -> tuple[list[Alert], list[str]]:
    """(alerts to send, stale keys to clear). User decision 2026-11-18: a row
    alerts the first time it reaches a strength and stays quiet until its
    trigger text changes or the verdict gets stronger. A weaker verdict sends
    nothing and keeps the stored strength. A key is stale only when its id is
    not in `keep_ids` (default: the ids of `rows`)."""
    by_id = {v.row_id: v for v in verdicts}
    alerts: list[Alert] = []
    for row in rows:
        v = by_id.get(row.id)
        level = strength(v) if v else 0
        if level == 0:
            continue
        key = STATE_PREFIX + row.id
        seen = state.get(key) if isinstance(state.get(key), dict) else {}
        h = trigger_hash(row)
        if seen.get("hash") == h and level <= seen.get("strength", 0):
            continue
        alerts.append(Alert(row, v, key, {"hash": h, "strength": level}))
    ids = keep_ids if keep_ids is not None else {r.id for r in rows}
    live = {STATE_PREFIX + i for i in ids}
    stale = sorted(k for k in state if k.startswith(STATE_PREFIX) and k not in live)
    return alerts, stale


def format_alert(a: Alert) -> str:
    """Telegram HTML for one alert. Every interpolated field is escaped."""
    e = html.escape
    label = "TRIGGERED" if a.verdict.verdict == "triggered" else "UNCLEAR"
    lines = [
        f"🔔 <b>Watch {e(a.row.id)}: {e(a.row.name)}</b>. {label}",
        e(a.verdict.reason),
    ]
    for item in a.verdict.evidence:
        lines.append(
            f'• <a href="{e(item.url, quote=True)}">{e(item.outlet)}: {e(item.title)}</a>'
        )
    return "\n".join(lines)


def format_problems(
    skipped: list[tuple[str, str]],
    invalid: list[Verdict],
    unknown: "list[PriceVerdict] | tuple" = (),
) -> str | None:
    """One message for everything the job could not judge this run, or None.
    Sent every run, deliberately not once-per-row: the file needs fixing.
    `unknown` holds price verdicts in state "unknown" (PriceVerdict)."""
    if not skipped and not invalid and not unknown:
        return None
    e = html.escape
    lines = ["⚠️ <b>Watch: rows not judged this run</b>"]
    lines += [f"• skipped {e(who)}: {e(why)}" for who, why in skipped]
    lines += [f"• couldn't judge {e(v.row_id)}: {e(v.reason)}" for v in invalid]
    lines += [f"• price unknown {e(v.row_id)}: {e(v.detail)}" for v in unknown]
    return "\n".join(lines)


# ── Price rows (news-brief-vap): judged by code, never the model ─────────────
# The rules are the portfolio repo's watch_check.py, so a level that fires on
# the operator's PC fires here on the same day. Design:
# docs/superpowers/specs/2026-11-19-watch-price-triggers-design.md.

MA_WINDOW = 200
# A last completed bar older than this is a stale or delisted symbol: UNKNOWN,
# never "not fired". Five days covers a weekend plus a holiday.
STALE_DAYS = 5


@dataclass(frozen=True)
class PriceVerdict:
    row_id: str
    state: str  # "fired" | "not" | "unknown"
    detail: str


def _as_utc(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def completed_bars(
    bars: list[tuple[date, float]], as_of: datetime
) -> list[tuple[date, float]]:
    """Bars dated before as_of's UTC date, oldest first. A bar dated on the run
    day may still be forming (futures and FX trade overnight; yfinance also
    appends a live same-day row), so it is never judged."""
    today = _as_utc(as_of).astimezone(timezone.utc).date()
    return sorted(b for b in bars if b[0] < today)


def symbol_usable(bars: list[tuple[date, float]] | None, as_of: datetime) -> bool:
    """A fetched series with a completed bar no older than STALE_DAYS."""
    if not bars:
        return False
    done = completed_bars(bars, as_of)
    today = _as_utc(as_of).astimezone(timezone.utc).date()
    return bool(done) and (today - done[-1][0]).days <= STALE_DAYS


def evaluate_price(
    row: PriceRow, bars: list[tuple[date, float]] | None, as_of: datetime
) -> PriceVerdict:
    """One price row against its symbol's bars (None = the fetch failed)."""
    if bars is None:
        return PriceVerdict(row.id, "unknown", f"{row.signal}: fetch failed")
    done = completed_bars(bars, as_of)
    if not done:
        return PriceVerdict(row.id, "unknown", f"{row.signal}: no completed closes")
    last_day, last = done[-1]
    age = (_as_utc(as_of).astimezone(timezone.utc).date() - last_day).days
    if age > STALE_DAYS:
        return PriceVerdict(
            row.id,
            "unknown",
            f"{row.signal}: stale, last close {last_day} ({age} days)",
        )
    if row.condition == "above_ma200":
        if len(done) < MA_WINDOW:
            return PriceVerdict(
                row.id, "unknown", f"{row.signal}: only {len(done)} closes"
            )
        ma = sum(v for _, v in done[-MA_WINDOW:]) / MA_WINDOW
        detail = (
            f"{row.signal} {last:.2f} vs 200dma {ma:.2f} "
            f"({100 * (last / ma - 1):+.1f}%) @ {last_day}"
        )
        return PriceVerdict(row.id, "fired" if last > ma else "not", detail)
    level = float(row.level)
    fired = last < level if row.condition == "below" else last > level
    detail = (
        f"{row.signal} {last:.2f} vs {row.condition} {level:g} "
        f"({100 * (last / level - 1):+.1f}%) @ {last_day}"
    )
    return PriceVerdict(row.id, "fired" if fired else "not", detail)


# ── Price alerts: on crossing, re-arm when the row stops firing ──────────────

# One runtime_state key per price row: {"hash": price_hash, "fired": bool}.
PRICE_STATE_PREFIX = "watch.price."


def price_hash(row: PriceRow) -> str:
    """Identity of what a price row watches; an edit re-arms the row."""
    level = "" if row.level is None else repr(float(row.level))
    ident = f"{row.signal}|{row.condition}|{level}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PriceAlert:
    row: PriceRow
    verdict: PriceVerdict
    key: str
    value: dict


def plan_price_alerts(
    rows: list[PriceRow],
    verdicts: list[PriceVerdict],
    state: dict,
    keep_ids: set[str],
) -> tuple[list[PriceAlert], dict, list[str]]:
    """(alerts to send, re-arm updates to store, stale keys to clear).

    User decision 2026-11-19: alert the day a row starts firing, stay quiet
    while it stays fired, re-arm when it stops. An edited row re-arms. UNKNOWN
    touches nothing: a data gap neither re-arms nor fires.
    """
    by_id = {v.row_id: v for v in verdicts}
    alerts: list[PriceAlert] = []
    updates: dict = {}
    for row in rows:
        v = by_id.get(row.id)
        if v is None or v.state == "unknown":
            continue
        key = PRICE_STATE_PREFIX + row.id
        seen = state.get(key) if isinstance(state.get(key), dict) else {}
        h = price_hash(row)
        armed = seen.get("hash") != h or not seen.get("fired")
        if v.state == "fired":
            if armed:
                alerts.append(PriceAlert(row, v, key, {"hash": h, "fired": True}))
        elif not armed:
            updates[key] = {"hash": h, "fired": False}
        elif seen and seen.get("hash") != h:
            updates[key] = {"hash": h, "fired": False}
    live = {PRICE_STATE_PREFIX + i for i in keep_ids}
    stale = sorted(
        k for k in state if k.startswith(PRICE_STATE_PREFIX) and k not in live
    )
    return alerts, updates, stale


def format_price_alert(a: PriceAlert) -> str:
    """Telegram HTML for one FIRED price row. Every field is escaped."""
    e = html.escape
    return (
        f"📈 <b>Watch {e(a.row.id)}: {e(a.row.name)}</b>. FIRED\n{e(a.verdict.detail)}"
    )


# ── The fetcher: Yahoo v8 chart, cleaned the way yfinance cleans it ──────────

YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _exchange_tz(meta: dict):
    """The listing's own timezone, which is how yfinance dates daily bars.

    Probed on Yahoo 2026-11-19: FX bars are stamped at London midnight, i.e.
    23:00 UTC under BST, so a UTC date puts every summer FX bar a day early
    (review I1). US futures are stamped at New York midnight. Falls back to
    UTC only when Yahoo names no timezone; a named zone that cannot load is
    an error, never a silent UTC (the image carries tzdata for this).
    """
    name = meta.get("exchangeTimezoneName")
    if not name:
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def fetch_adj_closes(symbol: str, *, get=None) -> list[tuple[date, float]]:
    """A year of daily adjusted closes, oldest first, in native units.

    Reproduces what yfinance's auto_adjust=True hands watch_check.py:
    `adjclose`, falling back to `close` per bar where it is missing; None bars
    dropped; one bar per UTC date (the last wins); and NO unit conversion, so a
    GBp listing stays in pence, as the operator's levels are. Raises
    ValueError on any HTTP or shape failure. `get` defaults to requests.get
    (imported lazily) and is injectable, so tests never touch the network.
    """
    if get is None:
        import requests

        get = requests.get
    try:
        resp = get(
            _CHART_URL.format(symbol=symbol),
            params={"interval": "1d", "range": "1y"},
            headers=YAHOO_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        node = resp.json()["chart"]["result"][0]
        stamps = node.get("timestamp") or []
        ind = node["indicators"]
        closes = ind["quote"][0]["close"]
        adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or [None] * len(stamps)
        tz = _exchange_tz(node.get("meta") or {})
    except Exception as e:
        raise ValueError(f"{symbol}: Yahoo chart unusable: {e}") from e
    if not (len(stamps) == len(closes) == len(adj)):
        raise ValueError(f"{symbol}: Yahoo chart arrays differ in length")
    by_day: dict[date, float] = {}
    for ts, c, a in zip(stamps, closes, adj):
        value = a if a is not None else c
        if value is None:
            continue
        by_day[datetime.fromtimestamp(ts, tz=tz).date()] = float(value)
    return sorted(by_day.items())

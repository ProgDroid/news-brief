"""Dated memory for the brief (storylines phase 1, spec section 3).

Two jobs: give the brief a window of recent dated claims, and record which
evidence each day's brief added for each thesis key. This module owns ALL
`thesis_evidence` SQL; callers pass a connection and never write the SQL
themselves. There is no DELETE from that table, by design.

The window half is pure text work with no database: `brief_sections` parses an
archived brief into its sections, and `build_window` turns the last week of
briefs into the dated headline block that goes into the brief prompt.
"""

import html
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger("newsbrief")

DIRECTIONS = ("supports", "challenges")

DATED_WINDOW_MAX_CHARS = 12_000
ENTRY_MAX_CHARS = 200

_TAG_RE = re.compile(r"<[^>]*>")
_SECTION_HEADINGS = (
    ("TOP STORIES", "top"),
    ("THREADS THAT MOVED", "threads"),
    ("WATCH / FORWARD", "watch"),
    ("POSITION SIGNALS", "positions"),
)
_WINDOW_TAGS = (("top", "top"), ("threads", "thread"), ("watch", "watch"))
_WINDOW_HEADING = (
    "## THE LAST 7 DAYS — ALREADY REPORTED\n"
    "Headlines, threads and forward-looks from the past week's briefs, each with "
    "the date it ran (MM-DD, UTC). Use them for continuity: when today's news "
    'follows from one of these, say so with its date ("follows from 11-14") '
    "instead of explaining it again. Do not repeat a development listed here "
    "unless something about it changed today, and then lead with what changed. "
    "A forward-look listed here that has come due is worth one line on how it "
    "turned out."
)


@dataclass(frozen=True)
class EvidenceRow:
    thesis_key: str
    direction: str  # "supports" | "challenges"
    line: str


def save_brief_evidence(
    conn, on_date: str, rows: list[EvidenceRow], *, model: str
) -> int:
    """Insert brief-sourced rows dated `on_date` (YYYY-MM-DD); return the count written."""
    for r in rows:
        conn.execute(
            "INSERT INTO thesis_evidence (thesis_key, on_date, direction, line, source, model)"
            " VALUES (%s, %s, %s, %s, 'brief', %s)",
            (r.thesis_key, on_date, r.direction, r.line, model),
        )
    return len(rows)


def has_brief_evidence(conn, on_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM thesis_evidence WHERE on_date = %s AND source = 'brief' LIMIT 1",
        (on_date,),
    ).fetchone()
    return row is not None


def recent_evidence(conn, thesis_key: str, *, since: str) -> list[dict]:
    """Rows for one key with on_date >= since, newest first."""
    rows = conn.execute(
        "SELECT on_date, direction, line, source FROM thesis_evidence"
        " WHERE thesis_key = %s AND on_date >= %s ORDER BY on_date DESC, id DESC",
        (thesis_key, since),
    ).fetchall()
    return [
        {
            "on_date": d.isoformat(),
            "direction": direction,
            "line": line,
            "source": source,
        }
        for d, direction, line, source in rows
    ]


def keys_with_evidence(conn, *, since: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT thesis_key FROM thesis_evidence WHERE on_date >= %s ORDER BY thesis_key",
        (since,),
    ).fetchall()
    return [r[0] for r in rows]


@dataclass(frozen=True)
class WindowResult:
    block: str  # "" when nothing qualifies
    days: int
    lines: int
    chars: int


def _is_heading(line: str) -> bool:
    if line[0] in "•-*" or line[0].isdigit() or len(line) > 80:
        return False
    letters = [c for c in line if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def brief_sections(text: str) -> dict[str, list[str]]:
    """Parse a brief (raw HTML or the tag-stripped archive) into its sections.

    Keys are "top", "threads", "watch", "positions", present only when the
    section has entries. An entry is its lines joined with newline, bullet
    marker stripped. Any other all-caps heading closes the current section.
    """
    text = html.unescape(_TAG_RE.sub("", text))
    out: dict[str, list[str]] = {}
    section: str | None = None
    entry: list[str] | None = None
    new_entry_next = (
        False  # a non-bullet line right after a heading or blank starts one
    )

    def flush() -> None:
        nonlocal entry
        if section is not None and entry:
            out.setdefault(section, []).append("\n".join(entry))
        entry = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            new_entry_next = True
            continue
        if _is_heading(line):
            flush()
            section = next((k for h, k in _SECTION_HEADINGS if h in line), None)
            new_entry_next = True
            continue
        if section is None:
            continue
        if line[0] == "•" or line.startswith("- "):
            flush()
            entry = [line[1:].strip()]
        elif new_entry_next or entry is None:
            flush()
            entry = [line]
        else:
            entry.append(line)
        new_entry_next = False
    flush()
    return out


def _cut(line: str) -> str:
    if len(line) <= ENTRY_MAX_CHARS:
        return line
    return line[: ENTRY_MAX_CHARS - 1] + "…"


def _block(day_lines: list[list[str]]) -> str:
    return "\n".join([_WINDOW_HEADING, *(ln for day in day_lines for ln in day)])


def build_window(briefs: list[tuple[str, str]], today: str) -> WindowResult:
    """The dated headline block for the prompt: briefs from today-7 to today-1.

    `briefs` is (YYYY-MM-DD, text) oldest first. Over DATED_WINDOW_MAX_CHARS the
    oldest whole days go first; if the newest day alone is still over, its
    trailing lines go.
    """
    today_d = date.fromisoformat(today)
    lo, hi = today_d - timedelta(days=7), today_d - timedelta(days=1)
    days: list[list[str]] = []
    for day, text in briefs:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        if not lo <= d <= hi:
            continue
        sections = brief_sections(text)
        mmdd = d.strftime("%m-%d")
        lines = [
            _cut(f"{mmdd} [{tag}] {entry.split(chr(10), 1)[0]}")
            for key, tag in _WINDOW_TAGS
            for entry in sections.get(key, [])
        ]
        if lines:
            days.append(lines)
    while len(days) > 1 and len(_block(days)) > DATED_WINDOW_MAX_CHARS:
        days.pop(0)
    while days and len(days[0]) > 1 and len(_block(days)) > DATED_WINDOW_MAX_CHARS:
        days[0].pop()
    if not days:
        return WindowResult("", 0, 0, 0)
    block = _block(days)
    return WindowResult(block, len(days), sum(len(d) for d in days), len(block))


# ── Evidence extraction (spec section 5.3) ───────────────────────────────────

EVIDENCE_LINE_MAX = 300

_EVIDENCE_SYSTEM = (
    "You turn the POSITION SIGNALS section of a finished investment brief into "
    "dated evidence for the reader's theses. Each thesis is given only as a "
    "label and its key (a ticker, or cluster:<name>). Record a row only where "
    "the section itself says a development confirms or challenges that thesis; "
    "never infer one the section does not state. One row per development per "
    "thesis, its line a single plain sentence under 300 characters. If nothing "
    "in the section bears on a listed thesis, return no rows."
)
_EVIDENCE_USER = (
    "## THESES\n{theses}\n\n## POSITION SIGNALS\n{section}\n\nCall emit_evidence once."
)


@dataclass(frozen=True)
class EvidenceResult:
    status: str  # "no_section" | "no_theses" | "ok" | "no_answer"
    rows: list[EvidenceRow]
    problems: list[str]


def build_evidence_request(section: str, labels: dict[str, str], *, model: str) -> dict:
    """One forced-tool Messages payload. The model sees labels, never raw keys."""
    tool = {
        "name": "emit_evidence",
        "description": "Record the evidence the section gives for each thesis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "thesis": {"type": "string", "enum": list(labels)},
                            "direction": {"type": "string", "enum": list(DIRECTIONS)},
                            "line": {"type": "string"},
                        },
                        "required": ["thesis", "direction", "line"],
                    },
                }
            },
            "required": ["rows"],
        },
    }
    theses = "\n".join(f"{label} = {key}" for label, key in labels.items())
    return {
        "model": model,
        "max_tokens": 2048,
        # Explicit, never the default: adaptive thinking eats max_tokens.
        "thinking": {"type": "disabled"},
        "system": _EVIDENCE_SYSTEM,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "emit_evidence"},
        "messages": [
            {
                "role": "user",
                "content": _EVIDENCE_USER.format(theses=theses, section=section),
            }
        ],
    }


def validate_evidence(
    resp: dict, labels: dict[str, str]
) -> tuple[list[EvidenceRow], list[str]]:
    """(rows, problems). Never raises on a bad answer. An unknown label is a
    problem, never mapped through: a row naming a thesis we did not offer is
    not evidence for one."""
    raw = None
    for block in resp.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == "emit_evidence":
            raw = (block.get("input") or {}).get("rows")
            break
    if not isinstance(raw, list):
        return [], [f"no emit_evidence answer (stop_reason={resp.get('stop_reason')})"]

    rows: list[EvidenceRow] = []
    problems: list[str] = []
    seen: set[EvidenceRow] = set()
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            problems.append(f"row {i}: not an object")
            continue
        label = entry.get("thesis")
        if not isinstance(label, str) or label not in labels:
            problems.append(f"row {i}: unknown thesis label {label!r}")
            continue
        direction = entry.get("direction")
        if direction not in DIRECTIONS:
            problems.append(f"row {i}: bad direction {direction!r}")
            continue
        line = entry.get("line")
        line = line.strip() if isinstance(line, str) else ""
        if not line:
            problems.append(f"row {i}: empty line")
            continue
        if len(line) > EVIDENCE_LINE_MAX:
            line = line[: EVIDENCE_LINE_MAX - 1] + "…"
        row = EvidenceRow(labels[label], direction, line)
        if row in seen:
            continue
        seen.add(row)
        rows.append(row)
    return rows, problems


def extract_thesis_evidence(
    brief_text: str, keys: list[str], *, post, model: str
) -> EvidenceResult:
    """POSITION SIGNALS of a finished brief -> evidence rows for `keys`.

    `post` sends a Messages payload and returns the decoded response. No
    section or no keys means no model call.
    """
    entries = brief_sections(brief_text).get("positions")
    if not entries:
        return EvidenceResult("no_section", [], [])
    if not keys:
        return EvidenceResult("no_theses", [], [])
    labels = {f"T{i}": k for i, k in enumerate(keys, 1)}
    resp = post(build_evidence_request("\n".join(entries), labels, model=model))
    usage = resp.get("usage") or {}
    # Before validating: on a truncated tool call the stop_reason is the one
    # datum that tells truncation from a bad answer.
    log.info(
        f"Evidence response: stop_reason={resp.get('stop_reason')} "
        f"usage(in={usage.get('input_tokens')} out={usage.get('output_tokens')})"
    )
    rows, problems = validate_evidence(resp, labels)
    no_answer = bool(problems) and problems[0].startswith("no emit_evidence answer")
    return EvidenceResult("no_answer" if no_answer else "ok", rows, problems)

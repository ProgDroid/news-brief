"""Storylines: pure functions (plan 2a). No I/O, no SQL; the store owns that.

This task holds the seeding pair, a request builder and a validator. The
attach functions follow below.
"""

import html
import re
from dataclasses import dataclass

import requests

from storyline_store import Storyline

SEED_TOOL = "propose_storylines"
SEED_MAX = 25
# ~25 items of up to 80 + 400 characters is about 3-4k tokens; leave headroom.
SEED_MAX_TOKENS = 8000
NAME_MAX = 80
DESCRIPTION_MAX = 400

_SEED_SYSTEM = (
    "You draft the initial set of storylines for a news-brief reader. A "
    "storyline is one developing situation that news will keep adding to over "
    "weeks, such as a negotiation, a policy fight, a sector squeeze or a "
    "company saga; it is not a one-day event and not a whole asset class. "
    "Draft about 20 storylines that together cover the topics, the pinned "
    "subjects, the investment theses and the watched triggers below. Give each "
    "a short specific name and a description of what is unfolding and what "
    "would move it. Set pinned to true for any storyline that covers a pinned "
    "subject. Never reuse a name from the existing storylines list. Call the "
    "tool once."
)


@dataclass(frozen=True)
class Proposed:
    name: str
    description: str
    pinned: bool


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else "(none)"


def build_seed_request(
    topics: list[dict],
    pins: list[str],
    theses: dict[str, str],
    watch_lines: list[str],
    live_names: list[str],
    *,
    model: str,
    max_count: int,
) -> dict:
    """One forced-tool Messages payload that asks for the seed storylines."""
    tool = {
        "name": SEED_TOOL,
        "description": "Propose the initial storylines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "storylines": {
                    "type": "array",
                    "maxItems": max_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "maxLength": NAME_MAX},
                            "description": {
                                "type": "string",
                                "maxLength": DESCRIPTION_MAX,
                            },
                            "pinned": {"type": "boolean"},
                        },
                        "required": ["name", "description", "pinned"],
                    },
                }
            },
            "required": ["storylines"],
        },
    }
    user = (
        "## TOPICS\n"
        + _bullets([f"{t.get('label', '')}: {t.get('search', '')}" for t in topics])
        + "\n\n## PINNED SUBJECTS\n"
        + _bullets(list(pins))
        + "\n\n## THESES\n"
        + _bullets([f"{k}: {v}" for k, v in theses.items()])
        + "\n\n## WATCHED TRIGGERS\n"
        + _bullets(list(watch_lines))
        + "\n\n## EXISTING STORYLINES (do not reuse these names)\n"
        + _bullets(list(live_names))
        + f"\n\nPropose at most {max_count} storylines. Call {SEED_TOOL} once."
    )
    return {
        "model": model,
        "max_tokens": SEED_MAX_TOKENS,
        # Explicit, never the default: adaptive thinking eats max_tokens.
        "thinking": {"type": "disabled"},
        "system": _SEED_SYSTEM,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": SEED_TOOL},
        "messages": [{"role": "user", "content": user}],
    }


def validate_seed(
    resp: dict, *, max_count: int, live_names: list[str]
) -> tuple[list[Proposed], list[str]]:
    """(proposals, problems). Never raises on a bad answer; a bad item is
    dropped with one problem string, which never quotes the item's text."""
    raw = None
    if isinstance(resp, dict):
        for block in resp.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == SEED_TOOL
            ):
                inp = block.get("input")
                raw = inp.get("storylines") if isinstance(inp, dict) else None
                break
    if not isinstance(raw, list):
        stop = resp.get("stop_reason") if isinstance(resp, dict) else None
        return [], [f"no tool_use block (stop_reason={stop})"]

    seen = {n.strip().lower() for n in live_names}
    out: list[Proposed] = []
    problems: list[str] = []
    for i, entry in enumerate(raw, 1):
        if len(out) >= max_count:
            problems.append(f"item {i}: beyond max_count={max_count}")
            continue
        if not isinstance(entry, dict):
            problems.append(f"item {i}: not an object")
            continue
        name = entry.get("name")
        desc = entry.get("description")
        name = name.strip() if isinstance(name, str) else ""
        desc = desc.strip() if isinstance(desc, str) else ""
        if not name or len(name) > NAME_MAX:
            problems.append(f"item {i}: empty or over-long name")
            continue
        if not desc or len(desc) > DESCRIPTION_MAX:
            problems.append(f"item {i}: empty or over-long description")
            continue
        if name.lower() in seen:
            problems.append(f"item {i}: duplicate name")
            continue
        seen.add(name.lower())
        out.append(Proposed(name, desc, entry.get("pinned") is True))
    return out, problems


# --- attach (spec 8.1) ------------------------------------------------------

ATTACH_TOOL = "attach_articles"
ATTACH_BATCH = 60
ATTACH_TEXT_MAX = 600
ATTACH_MAX_PER_ARTICLE = 3
ATTACH_MAX_TOKENS = 4000
_LABEL_ECHO_MAX = 40

_ATTACH_SYSTEM = (
    "You file news articles under storylines. Each article below carries a "
    "label (A1, A2, ...) and each storyline a label (S1, S2, ...). Answer every "
    "article exactly once. Attach an article to a storyline only where the "
    "article materially develops that storyline: new facts, a decision, a "
    "reversal or a number that moves it. A passing mention, the same sector or "
    "the same country is not enough. An empty list is the common answer. Give "
    f"at most {ATTACH_MAX_PER_ARTICLE} storylines per article. Use only the "
    "labels shown. Call the tool once."
)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def plain_text(title: str, body: str | None, limit: int = ATTACH_TEXT_MAX) -> str:
    """`title — body` as plain text: entities decoded, tags stripped, whitespace
    collapsed, cut to `limit`."""

    def clean(s: str | None) -> str:
        s = _TAG_RE.sub(" ", html.unescape(s or ""))
        return _WS_RE.sub(" ", s.replace("\xa0", " ")).strip()

    t, b = clean(title), clean(body)
    return (f"{t} — {b}" if b else t)[:limit]


def build_attach_request(
    storylines: list[Storyline], articles: list[dict], *, model: str
) -> tuple[dict, dict[str, int], dict[str, int]]:
    """(payload, s_labels, a_labels). The model sees S1.. and A1.. only."""
    s_labels = {f"S{i}": s.id for i, s in enumerate(storylines, 1)}
    a_labels = {f"A{i}": a["id"] for i, a in enumerate(articles, 1)}
    tool = {
        "name": ATTACH_TOOL,
        "description": "Record which storylines each article materially develops.",
        "input_schema": {
            "type": "object",
            "properties": {
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "article": {"type": "string"},
                            "storylines": {
                                "type": "array",
                                "maxItems": ATTACH_MAX_PER_ARTICLE,
                                "items": {"type": "string", "enum": list(s_labels)},
                            },
                        },
                        "required": ["article", "storylines"],
                    },
                }
            },
            "required": ["answers"],
        },
    }
    s_block = "\n".join(
        f"{lab}: {s.name} — {s.description}" for lab, s in zip(s_labels, storylines)
    )
    a_block = "\n".join(
        f"{lab}: {plain_text(a['title'], a.get('body'))}"
        for lab, a in zip(a_labels, articles)
    )
    user = (
        f"## STORYLINES\n{s_block}\n\n## ARTICLES\n{a_block}\n\n"
        f"Answer every article once. Call {ATTACH_TOOL} once."
    )
    payload = {
        "model": model,
        "max_tokens": ATTACH_MAX_TOKENS,
        # Explicit, never the default: adaptive thinking eats max_tokens.
        "thinking": {"type": "disabled"},
        "system": _ATTACH_SYSTEM,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": ATTACH_TOOL},
        "messages": [{"role": "user", "content": user}],
    }
    return payload, s_labels, a_labels


@dataclass(frozen=True)
class AttachResult:
    decided: dict[int, list[int]]
    problems: dict[int, str]
    stray: list[str]
    whole: str | None


def _echo(x) -> str:
    return str(x)[:_LABEL_ECHO_MAX]


def validate_attach(
    resp: dict, s_labels: dict[str, int], a_labels: dict[str, int]
) -> AttachResult:
    """Map the answer back to ids. Never raises; an unknown label is a problem
    for that article only, never a fall-through."""
    stop = resp.get("stop_reason") if isinstance(resp, dict) else None
    answers = None
    if isinstance(resp, dict) and stop == "tool_use":
        content = resp.get("content")
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == ATTACH_TOOL
            ):
                inp = block.get("input")
                answers = inp.get("answers") if isinstance(inp, dict) else None
                break
    if not isinstance(answers, list):
        return AttachResult({}, {}, [], f"unusable response (stop_reason={stop})")

    by_article: dict[str, list] = {}
    stray: list[str] = []
    for entry in answers:
        if not isinstance(entry, dict):
            continue
        label = entry.get("article")
        if not isinstance(label, str) or label not in a_labels:
            stray.append(_echo(label))
            continue
        by_article.setdefault(label, []).append(entry.get("storylines"))

    decided: dict[int, list[int]] = {}
    problems: dict[int, str] = {}
    for label, item_id in a_labels.items():
        given = by_article.get(label, [])
        if not given:
            problems[item_id] = "no answer"
            continue
        if len(given) > 1:
            problems[item_id] = "answered twice"
            continue
        raw = given[0]
        if not isinstance(raw, list):
            problems[item_id] = "malformed answer"
            continue
        bad = next(
            (x for x in raw if not isinstance(x, str) or x not in s_labels), None
        )
        if bad is not None or any(not isinstance(x, str) for x in raw):
            problems[item_id] = f"unknown storyline label {_echo(bad)}"
            continue
        distinct = list(dict.fromkeys(raw))
        if len(distinct) > ATTACH_MAX_PER_ARTICLE:
            problems[item_id] = f"more than {ATTACH_MAX_PER_ARTICLE} storylines"
            continue
        decided[item_id] = [s_labels[x] for x in distinct]
    return AttachResult(decided, problems, stray, None)


def classify_error(exc: BaseException) -> str:
    """transient (retry next pass), config (stop and alert), request (judged
    nothing, deferred) or unknown (deferred)."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return "transient"
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None)
        if not isinstance(code, int):
            return "unknown"
        if code == 429 or code >= 500:
            return "transient"
        if code in (401, 403, 404):
            return "config"
        return "request"
    return "unknown"

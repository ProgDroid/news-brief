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
    "Each storyline follows exactly one situation: never merge two situations "
    "into one storyline, and a name that joins two subjects with 'and' is two "
    "storylines. An investment thesis or a watched trigger is not a storyline: "
    "cover it with the situation whose news would move it, and describe that "
    "situation, not the trade. Draft about 20 storylines from the topics, the "
    "pinned subjects, the investment theses and the watched triggers below; if "
    "they need more situations than that, keep the ones that will draw the "
    "most news and leave the rest out rather than merge them. Give each a "
    f"short specific name of at most {NAME_MAX} characters and a description, "
    f"of at most {DESCRIPTION_MAX} characters, of what is unfolding and what "
    "kind of news counts as a development; anything longer is discarded. Set "
    "pinned to true for any storyline that covers a pinned subject. Never "
    "reuse a name from the existing storylines list. Call the tool once."
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
    # strict (news-brief-bcr): unconstrained, the model returned `storylines`
    # as a JSON STRING holding {"storylines": [...]} on 2026-11-23, and a correct
    # answer of 22 items was dropped. Grammar-constrained sampling makes that
    # shape ungenerable. The strict subset 400s on maxItems/maxLength, so the
    # count lives in the prompt and every limit in validate_seed.
    tool = {
        "name": SEED_TOOL,
        "description": "Propose the initial storylines.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "storylines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "pinned": {"type": "boolean"},
                        },
                        "required": ["name", "description", "pinned"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["storylines"],
            "additionalProperties": False,
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
    shape = None
    if isinstance(resp, dict):
        for block in resp.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == SEED_TOOL
            ):
                inp = block.get("input")
                if not isinstance(inp, dict):
                    shape = f"input type={type(inp).__name__}"
                else:
                    raw = inp.get("storylines")
                    shape = f"storylines type={type(raw).__name__}"
                break
    if not isinstance(raw, list):
        if shape is not None:
            # The block IS there. Saying "no tool_use block" here sent the
            # 2026-11-23 diagnosis after a missing block (news-brief-bcr).
            # The type name only: never the content.
            return [], [f"{SEED_TOOL} input has no storylines list ({shape})"]
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
    # strict for the reason the seed tool is (news-brief-bcr): the same nested
    # array shape. The per-article cap is in the system prompt and enforced in
    # validate_attach, since the strict subset 400s on maxItems.
    tool = {
        "name": ATTACH_TOOL,
        "description": "Record which storylines each article materially develops.",
        "strict": True,
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
                                "items": {"type": "string", "enum": list(s_labels)},
                            },
                        },
                        "required": ["article", "storylines"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["answers"],
            "additionalProperties": False,
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
    shape = None
    if isinstance(resp, dict) and stop == "tool_use":
        content = resp.get("content")
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == ATTACH_TOOL
            ):
                inp = block.get("input")
                if not isinstance(inp, dict):
                    shape = f"input type={type(inp).__name__}"
                else:
                    answers = inp.get("answers")
                    shape = f"answers type={type(answers).__name__}"
                break
    if not isinstance(answers, list):
        if shape is not None:
            why = f"{ATTACH_TOOL} input has no answers list ({shape})"
            return AttachResult({}, {}, [], why)
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


# ---- offline attach check (spec 8.2): pure metrics over runs ----
# A run is {item id: set of storyline ids}, holding only the items it decided.

CHECK_ILL_POSED_BELOW = 0.65
CHECK_AGREEMENT_SLACK = 0.10
CHECK_AGREEMENT_FLOOR = 0.55
CHECK_RATE_SLACK = 0.15


def jaccard_mean(a: dict[int, set[int]], b: dict[int, set[int]]) -> tuple[float, int]:
    """Mean Jaccard over items in both runs where at least one set is non-empty,
    and that subset's size. Empty subset: (nan, 0)."""
    scores = []
    for item_id in a.keys() & b.keys():
        x, y = a[item_id], b[item_id]
        if x or y:
            scores.append(len(x & y) / len(x | y))
    if not scores:
        return float("nan"), 0
    return sum(scores) / len(scores), len(scores)


def attach_rate(run: dict[int, set[int]]) -> float:
    """The share of decided items with a non-empty set (nan when none)."""
    if not run:
        return float("nan")
    return sum(1 for s in run.values() if s) / len(run)


def differ_kinds(h: dict[int, set[int]], s: dict[int, set[int]]) -> dict[str, int]:
    """Over items in both runs whose sets differ: h a proper subset of s
    (`less`), a proper superset (`more`), or anything else (`elsewhere`)."""
    kinds = {"less": 0, "more": 0, "elsewhere": 0}
    for item_id in h.keys() & s.keys():
        x, y = h[item_id], s[item_id]
        if x == y:
            continue
        kinds["less" if x < y else "more" if x > y else "elsewhere"] += 1
    return kinds


def check_verdict(
    j_ss: float, j_hs: float, rate_h: float, rate_s1: float
) -> tuple[str, list[str]]:
    """ILL-POSED when Sonnet disagrees with itself (Haiku is not judged); PASS
    only when both bars hold; otherwise FAIL. The list names each bar."""
    if not j_ss >= CHECK_ILL_POSED_BELOW:  # also true for nan
        return "ILL-POSED", [
            f"J(S1,S2)={j_ss:.2f} is below {CHECK_ILL_POSED_BELOW:.2f}: "
            "the storylines are not well posed, Haiku is not judged"
        ]
    floor = max(j_ss - CHECK_AGREEMENT_SLACK, CHECK_AGREEMENT_FLOOR)
    rate_floor = rate_s1 - CHECK_RATE_SLACK
    ok_j = j_hs >= floor
    ok_r = rate_h >= rate_floor
    reasons = [
        f"J(S1,S2)={j_ss:.2f} is at least {CHECK_ILL_POSED_BELOW:.2f}",
        f"J(H,S1)={j_hs:.2f} {'meets' if ok_j else 'misses'} the floor {floor:.2f}",
        f"H attach rate {rate_h:.2f} {'meets' if ok_r else 'misses'} "
        f"the floor {rate_floor:.2f}",
    ]
    return ("PASS" if ok_j and ok_r else "FAIL"), reasons

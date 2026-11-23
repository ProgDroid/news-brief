"""Storylines: pure functions (plan 2a). No I/O, no SQL; the store owns that.

This task holds the seeding pair, a request builder and a validator. The
attach functions arrive in a later task.
"""

from dataclasses import dataclass

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

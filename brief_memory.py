"""Standing-claim ledger: gives the daily brief multi-day memory of facts it has
already established, so it stops re-explaining them. DESCRIPTIVE only — never
affects trading. Flag-gated by BRIEF_MEMORY_ENABLED; fail-safe (any error leaves
the brief unaffected and the prior ledger intact)."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import requests

from common import ANTHROPIC_HEADERS, DATA_DIR, _write_json_atomic, log

BRIEF_MEMORY_FILE = DATA_DIR / "brief_memory.json"
# Size of the WORKING SET — the window of claims sent to the reconcile model and
# rendered into the brief. It is a PROMPT BUDGET, not a storage limit: storage is
# unbounded and claims leave only by TTL. The two were the same number until the
# 2026-08-29 replay showed the cap, not the TTL, was losing claims (56 of 70
# resolutions arrived inside the claim's own TTL window). See select_working_set.
WORKING_SET_SIZE = 25
RETIRE_AFTER_DAYS = 7
HIGH_SEVERITY_BONUS_DAYS = 7  # extra retention days a "high" claim earns (=> 14d TTL)
_SEVERITY_RANK = {"low": 0, "normal": 1, "high": 2}  # working-set ordering
_VALID_SEVERITY = frozenset(_SEVERITY_RANK)
_DEFAULT_SEVERITY = "normal"
# Claim lifecycle. QUARANTINED: written on every row, read by nothing. Rendering,
# TTL retirement and working-set ordering all ignore it deliberately, because the
# 2026-08-29 replay measured contradiction detection at ~61% precision and the
# restatement guard that lifts it (news-brief-93u) is gated on the gold set
# (news-brief-jx9.7). Detections accumulate as evidence meanwhile; nothing acts on
# them. news-brief-jx9.5 and news-brief-jx9.6 are the first readers.
_VALID_STATUS = frozenset({"standing", "challenged", "broken"})
_DEFAULT_STATUS = "standing"
# Wording overlap at which two claims count as the same fact. See _is_duplicate_claim.
DEDUP_SIMILARITY = 0.85
_DEDUP_STOPWORDS = frozenset(
    "a an and are as at be been by for from had has have in is it its of on or "
    "that the this to was were will with".split()
)
_NUMBER_RE = re.compile(r"\d[\d.,]*")
_WORD_RE = re.compile(r"[a-z0-9]+")
RECONCILE_MODEL = "claude-haiku-4-5-20251001"
# A full working set serialises to ~2400 output tokens, so the old 2048
# budget truncated the JSON array before its closing "]" — the parser then
# misreported the cut-off as "no JSON array". Give generous headroom and bound
# the model's output (see _RECONCILE_TEMPLATE) so it stays well inside this.
RECONCILE_MAX_TOKENS = 4096


def is_enabled() -> bool:
    return os.environ.get("BRIEF_MEMORY_ENABLED", "0") == "1"


def empty_ledger() -> dict:
    return {"version": 1, "claims": []}


def load_ledger(path: Path = BRIEF_MEMORY_FILE) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("claims"), list):
                return data
        return empty_ledger()
    except Exception as e:
        log.warning(f"Brief-memory ledger unreadable ({path}); starting empty: {e}")
        return empty_ledger()


def save_ledger(ledger: dict, path: Path = BRIEF_MEMORY_FILE) -> None:
    _write_json_atomic(path, ledger)


def _max_id_num(ledger: dict) -> int:
    nums = []
    for c in ledger.get("claims", []):
        m = re.match(r"c-(\d+)$", str(c.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0)


def _claim_fingerprint(text: str) -> tuple[frozenset, frozenset]:
    """(content words, numeric tokens) for duplicate detection. Numbers are held
    apart from words because they are the highest-signal discriminator here:
    "...raised to 1.0%" and "...raised to 1.5%" share every other token. Function
    words are dropped so that an added article cannot swing a short claim."""
    lowered = str(text or "").lower()
    numbers = frozenset(n.rstrip(".,") for n in _NUMBER_RE.findall(lowered))
    words = frozenset(
        w
        for w in _WORD_RE.findall(lowered)
        if w not in _DEDUP_STOPWORDS and not w[0].isdigit()
    )
    return words, numbers


def _is_duplicate_claim(a: str, b: str) -> bool:
    """True when two claim texts assert the same thing. Any difference in numbers
    blocks a merge outright, and the wording bar is high, because the two errors
    are not symmetric: a duplicate row wastes a working-set slot, a false merge
    destroys one of the two assertions with no record that it existed."""
    a_words, a_nums = _claim_fingerprint(a)
    b_words, b_nums = _claim_fingerprint(b)
    if a_nums != b_nums or not a_words or not b_words:
        return False
    return len(a_words & b_words) / len(a_words | b_words) >= DEDUP_SIMILARITY


def _find_duplicate(text: str, rows) -> dict | None:
    for row in rows:
        if _is_duplicate_claim(text, row.get("claim", "")):
            return row
    return None


def _reaffirm(base: dict, mc: dict, today: str) -> None:
    """Fold today's restatement of a claim into its existing row, in place."""
    base["claim"] = mc.get("claim", base.get("claim", ""))
    base["topic"] = mc.get("topic", base.get("topic", ""))
    base["last_reaffirmed"] = today
    base["restate_count"] = base.get("restate_count", 0) + 1
    base["source_count"] = max(
        base.get("source_count", 0) or 0,
        mc.get("source_count", 0) or 0,
    )
    new_sev = _coerce_severity(mc.get("severity"))
    base["severity"] = new_sev or base.get("severity", _DEFAULT_SEVERITY)
    _apply_status(base, mc.get("status"), mc.get("broken_by"), today)


def _days_between(d_old: str, d_new: str) -> int:
    try:
        a = datetime.strptime(d_old, "%Y-%m-%d")
        b = datetime.strptime(d_new, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return 0  # unparseable date -> never retire on this basis


def merge_ledger(
    prior: dict,
    model_claims: list[dict],
    today: str,
    *,
    retire_after_days: int = RETIRE_AFTER_DAYS,
) -> dict:
    by_id = {c["id"]: c for c in prior.get("claims", []) if "id" in c}
    next_num = _max_id_num(prior) + 1
    returned = set()
    result = []
    for mc in model_claims:
        cid = mc.get("id")
        if cid and cid in by_id:
            # An echoed id is authoritative — never second-guess it with similarity.
            base = dict(by_id[cid])
            _reaffirm(base, mc, today)
            result.append(base)
            returned.add(cid)
            continue
        if not mc.get("claim"):
            continue
        # No id: the model believes this is new. Check it really is, because it
        # frequently restates an existing fact in fresh words (the 2026-08-29
        # replay minted 816 rows in 90 days, three of them the same claim on one
        # day). A duplicate only wastes a working-set slot, but a false merge
        # destroys an assertion, so _is_duplicate_claim is deliberately strict.
        twin = _find_duplicate(mc["claim"], result)
        if twin is not None:
            log.info(f"Brief-memory: dropped duplicate of {twin['id']} in one reply")
            continue
        twin = _find_duplicate(
            mc["claim"], [c for c in by_id.values() if c.get("id") not in returned]
        )
        if twin is not None:
            log.info(f"Brief-memory: reused {twin['id']} for a reworded claim")
            base = dict(twin)
            _reaffirm(base, mc, today)
            result.append(base)
            returned.add(base["id"])
            continue
        row = {
            "id": f"c-{next_num:04d}",
            "claim": mc["claim"],
            "topic": mc.get("topic", ""),
            "first_seen": today,
            "last_reaffirmed": today,
            "restate_count": 1,
            "source_count": mc.get("source_count", 0) or 0,
            "severity": _coerce_severity(mc.get("severity")) or _DEFAULT_SEVERITY,
        }
        _apply_status(row, mc.get("status"), mc.get("broken_by"), today)
        result.append(row)
        next_num += 1
    for c in prior.get("claims", []):
        if c.get("id") not in returned:
            result.append(dict(c))
    result = [
        c
        for c in result
        if _days_between(c["last_reaffirmed"], today) - _ttl_bonus(c.get("severity"))
        <= retire_after_days
    ]
    # Stored in working-set order (severity first, then recency) so the window is
    # a prefix. Storage is UNBOUNDED — a claim leaves only by TTL, never by being
    # crowded out. See WORKING_SET_SIZE.
    result.sort(
        key=lambda c: (_severity_rank(c.get("severity")), c["last_reaffirmed"]),
        reverse=True,
    )
    return {"version": 1, "claims": result}


def select_working_set(ledger: dict, limit: int = WORKING_SET_SIZE) -> list[dict]:
    """The window of claims the model and the reader actually see: highest
    severity first, then most recently reaffirmed. Everything outside it stays in
    storage untouched — merge_ledger keeps any claim the model did not return."""
    claims = sorted(
        ledger.get("claims", []),
        key=lambda c: (_severity_rank(c.get("severity")), c.get("last_reaffirmed", "")),
        reverse=True,
    )
    return claims[:limit]


_RECONCILE_SYSTEM = (
    "You maintain a compact memory of durable facts a daily market brief has "
    "already told its reader, so tomorrow's brief stops re-explaining them."
)

_RECONCILE_TEMPLATE = """Below is the CURRENT memory (JSON), TODAY'S BRIEF, and
TODAY'S SOURCE HEADLINES (the outlets that ran each story today, grouped by SOURCE).

Return ONLY a JSON array of the durable facts the reader now knows after today's
brief. Rules:
- A durable fact is something that should NOT be re-explained tomorrow unless it
  materially changes: one-time events already reported (e.g. a rate hike), and
  standing analytical frames/theses. NOT ephemeral daily price moves.
- For a fact already in CURRENT memory that is still relevant, include it and
  ECHO its existing "id". You may reword its "claim" if today refined it.
- For a genuinely NEW durable fact, include it with NO "id". Before you do, check
  CURRENT memory for the same fact stated in different words and echo that id
  instead — a reworded restatement is not a new fact. Never return the same fact
  twice in one reply.
- Omit facts that are no longer relevant.
- For each fact, set "source_count" to the number of DISTINCT outlets in TODAY'S
  SOURCE HEADLINES (the "SOURCE:" blocks) whose headline supports that fact. Count
  outlets, not headlines. Use 0 when the fact is not covered in today's headlines,
  when no source headlines are provided, or when you are unsure.
- For each fact, set "severity" to one of "low", "normal", or "high". "high" =
  a major standing development the reader must not have re-explained (wars,
  leadership or regime changes, major policy-regime shifts, market-structural
  events); "normal" = a typical durable fact (use this by default); "low" = a
  true but minor, low-stakes detail. When unsure, use "normal".
- For each fact, set "status" to one of "standing", "challenged", or "broken".
  "standing" = the fact still holds (use this by default); "challenged" = today's
  brief reports something that puts it in doubt without settling it; "broken" =
  today's brief reports something that directly contradicts it. When in doubt,
  use "standing". Two things are NOT breaks. A restatement, escalation or
  confirmation is not a break, however much more strongly it is worded — "the
  rate decision was executed at 1.0%" does not break "the rate was raised to
  1.0%". And absence of mention is not contradiction: a fact nobody wrote about
  today is still "standing". When you mark a fact "broken", set "broken_by" to a
  short phrase naming what contradicted it, and do NOT reword its "claim" — the
  original wording is what the reader was told, and it is what the break is
  measured against.
- Return at most {max_claims} items — keep only the most important durable facts,
  and keep each "claim" to one terse sentence (no more than ~30 words).
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>", "source_count": <integer>, "severity": "<low|normal|high>", "status": "<standing|challenged|broken>", "broken_by": "<what contradicted it; omit unless broken>"}}.
Output the JSON array and nothing else.

CURRENT memory:
{current}

TODAY'S BRIEF:
{brief}

TODAY'S SOURCE HEADLINES:
{source_index}
"""


def _corroboration_cue(source_count) -> str:
    """Coarse, reader-facing confidence cue derived from the peak source_count.
    Thresholds: 1 single-source / 2-3 corroborated / >=4 widely corroborated."""
    try:
        n = int(source_count)
    except (TypeError, ValueError):
        return ""
    if n >= 4:
        return "widely corroborated"
    if n >= 2:
        return "corroborated"
    if n == 1:
        return "single-source"
    return ""


def render_established_block(ledger: dict) -> str:
    claims = select_working_set(ledger)
    if not claims:
        return ""
    rows = []
    for c in claims:
        cue = _corroboration_cue(c.get("source_count"))
        tag = f" ({cue})" if cue else ""
        rows.append(f"  • [{c.get('topic') or 'general'}]{tag} {c['claim']}")
    lines = "\n".join(rows)
    return (
        "## ESTABLISHED — THE READER ALREADY KNOWS THESE\n"
        "Reference each in at most one clause, and only if still relevant. Do NOT "
        "re-explain or restate them as news. Lead every section with what has "
        "CHANGED since. The parenthetical is how broadly the fact was corroborated "
        "across outlets: lean on 'widely corroborated' facts with confidence and "
        "treat 'single-source' ones more tentatively.\n\n" + lines + "\n"
    )


def build_reconcile_prompt(
    ledger: dict, brief_text: str, source_index: str = ""
) -> str:
    current = json.dumps(select_working_set(ledger), indent=2)
    return _RECONCILE_TEMPLATE.format(
        current=current,
        brief=brief_text,
        max_claims=WORKING_SET_SIZE,
        source_index=source_index.strip() or "(no source index available)",
    )


def _coerce_severity(v) -> str | None:
    """Canonical severity ('low'/'normal'/'high'), or None to omit/default to normal.
    Case-insensitive; anything non-string or outside the enum returns None."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VALID_SEVERITY:
            return s
    return None


def _coerce_status(v) -> str | None:
    """Canonical claim status ('standing'/'challenged'/'broken'), or None to leave
    the row's existing status alone. Case-insensitive; anything non-string or
    outside the enum returns None."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VALID_STATUS:
            return s
    return None


def _apply_status(row: dict, proposed, broken_by, today: str) -> None:
    """Write status/broke_on/broken_by onto a claim row.

    broke_on is stamped on the first transition out of 'standing' and never
    rewritten: it records when the ledger learned the claim had failed, which a
    later reaffirmation must not move. An unrecognised or absent status leaves
    whatever the row already had (new rows default to 'standing')."""
    prior = _coerce_status(row.get("status")) or _DEFAULT_STATUS
    row["status"] = _coerce_status(proposed) or prior
    if row["status"] != _DEFAULT_STATUS and not row.get("broke_on"):
        row["broke_on"] = today
    if isinstance(broken_by, str) and broken_by.strip():
        row["broken_by"] = broken_by.strip()


def _ttl_bonus(severity) -> int:
    """Extra retention days a claim's severity buys. Only 'high' extends life."""
    return HIGH_SEVERITY_BONUS_DAYS if severity == "high" else 0


def _severity_rank(severity) -> int:
    """Cap-eviction ordering: high > normal > low; unknown/missing -> normal."""
    return _SEVERITY_RANK.get(severity, _SEVERITY_RANK[_DEFAULT_SEVERITY])


def _coerce_source_count(v) -> int | None:
    """Best-effort non-negative int, or None to omit. bool is rejected (it is an
    int subclass but never a real count)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, str) and v.strip().lstrip("+").isdigit():
        return max(0, int(v.strip()))
    return None


def parse_reconcile_response(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"no JSON array in reconcile response: {text[:200]!r}")
    data = json.loads(m.group())
    if not isinstance(data, list):
        raise ValueError("reconcile response is not a JSON list")
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("claim"):
            entry = {k: item[k] for k in ("id", "claim", "topic") if k in item}
            sc = _coerce_source_count(item.get("source_count"))
            if sc is not None:
                entry["source_count"] = sc
            sev = _coerce_severity(item.get("severity"))
            if sev is not None:
                entry["severity"] = sev
            st = _coerce_status(item.get("status"))
            if st is not None:
                entry["status"] = st
            bb = item.get("broken_by")
            if isinstance(bb, str) and bb.strip():
                entry["broken_by"] = bb.strip()
            out.append(entry)
    return out


def _messages_call(system: str, user: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": RECONCILE_MODEL,
            "max_tokens": RECONCILE_MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("stop_reason") == "max_tokens":
        # Don't return a half-written array for the parser to choke on — surface
        # the real cause so the fail-safe logs it as truncation, not a parse bug.
        raise ValueError(
            "reconcile response truncated (stop_reason=max_tokens); "
            "ledger/output exceeded the max_tokens budget"
        )
    blocks = body.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def reconcile_ledger(
    prior: dict, brief_text: str, today: str, *, call=None, source_index: str = ""
) -> dict:
    caller = call or _messages_call
    try:
        text = caller(
            _RECONCILE_SYSTEM,
            build_reconcile_prompt(prior, brief_text, source_index),
        )
        return merge_ledger(prior, parse_reconcile_response(text), today)
    except Exception as e:
        log.warning(f"Brief-memory reconcile failed; keeping prior ledger: {e}")
        return prior

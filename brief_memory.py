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
# Where a claim came from (spec 4.1). 'extracted' is source-grounded and is the
# only kind a propagation rule, thesis or calibration aggregate may ever read;
# 'authored' is the brief's own interpretation, persisted and scorable but never
# readable back as established fact. ALSO QUARANTINED for now: the 4.1 render
# filter is not wired, because this is an unmeasured classifier in the same
# prompt where news-brief-47q shows severity came back degenerate ('high' 25/25),
# and a uniformly-'authored' result would silently empty the ESTABLISHED block.
# Wire the filter once the gold set (news-brief-jx9.7) shows real variance.
_VALID_ORIGIN = frozenset({"extracted", "authored"})
_DEFAULT_ORIGIN = "extracted"
# Claim admission (news-brief-93u). Spec 3.3: "Market levels are Observation
# rows; a claim may cite them but must not be one." The prompt has forbidden
# ephemeral price levels since this feature shipped and admitted them anyway
# (6 of the 23 gold-set rows), so the rule is enforced here rather than merely
# restated there. Unlike severity/status/origin this is NOT stored on the row:
# everything that survives the guard is a claim, so the column would be uniform
# on every read, which spec 12.2 rates worse than a missing one.
_VALID_KIND = frozenset({"claim", "observation"})
_DEFAULT_KIND = "claim"
# Wording overlap at which two claims count as the same fact. See _is_duplicate_claim.
DEDUP_SIMILARITY = 0.85
_DEDUP_STOPWORDS = frozenset(
    "a an and are as at be been by for from had has have in is it its of on or "
    "that the this to was were will with".split()
)
_NUMBER_RE = re.compile(r"\d[\d.,]*")
_WORD_RE = re.compile(r"[a-z0-9]+")
RECONCILE_MODEL = "claude-haiku-4-5-20251001"
# BUMP THIS whenever _RECONCILE_TEMPLATE changes in a way that could move the
# boundary the model draws on any field. Rows carry it so a later audit can tell
# which prompt produced a verdict; without it a prompt change is indistinguishable
# from a change in the world. Version 1 is the template as of the Epic 1 repair —
# rows written before this existed carry no prompt_version at all, and that
# absence is itself the correct reading.
# v4 (news-brief-93u): added the "kind" rubric — claim vs observation — that the
# claim-admission guard in merge_ledger enforces. v3 (news-brief-jx9.2 / 47q):
# recalibrated the severity rubric with worked examples across all three tiers
# plus an over-marking warning, and added the "driver" rule. v2 added "origin".
# v1 was the Epic 1 repair template — status rules, restatement/absence negative
# cases, id-reuse pressure.
PROMPT_VERSION = 4
# A full working set serialises to ~2400 output tokens, so the old 2048
# budget truncated the JSON array before its closing "]" — the parser then
# misreported the cut-off as "no JSON array". Give generous headroom and bound
# the model's output (see _RECONCILE_TEMPLATE) so it stays well inside this.
# Raised 4096 -> 8192 when Epic 1 added status/broken_by/origin/driver to the
# reply schema: a worst-case full working set measured ~3.7k output tokens, i.e.
# under 400 tokens of headroom. Truncation here fails SAFE (the prior ledger is
# kept) but silently loses a day of memory, and this repo has hit max_tokens
# truncation four separate times. test_reconcile_budget_keeps_headroom_for_a_
# full_working_set trips if another field erodes the margin again.
RECONCILE_MAX_TOKENS = 8192


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


def _dropped_numbers(stored: str, rewritten: str) -> frozenset:
    """Numbers the STORED claim asserted that a rewrite no longer carries.

    Dropped, never merely different: adding a number is what refinement looks
    like ("raised to 1.0%" -> "raised to 1.0% on June 16"), and treating that as
    a contradiction would break the rewording the prompt is right to allow. A
    number the claim previously asserted going missing is the other thing —
    "Colombia holds 6 pts" coming back as "both teams hold 4 pts" withdraws an
    assertion, and on the 2026-08-31 gold-set runs that separated the classes
    cleanly on admissible rows (fires on gs-08/09/10/16, all true breaks, and
    on nothing else).
    """
    if not rewritten or rewritten == stored:
        return frozenset()
    return _claim_fingerprint(stored)[1] - _claim_fingerprint(rewritten)[1]


def _find_duplicate(text: str, rows) -> dict | None:
    for row in rows:
        if _is_duplicate_claim(text, row.get("claim", "")):
            return row
    return None


def _stamp_provenance(row: dict, extractor_model: str, prompt_version: int) -> None:
    """Record which extractor and which prompt version last wrote this row.

    Stamped on every write, not just the first: a row's content is only ever as
    recent as the extractor that last touched it, and that is what a later audit
    needs to know. It is deliberately NOT covered by the jx9.5 text freeze — a
    broken claim keeps its original wording, but the verdict that broke it came
    from whichever model is named here."""
    row["extractor_model"] = extractor_model
    row["prompt_version"] = prompt_version


def _reaffirm(base: dict, mc: dict, today: str) -> None:
    """Fold today's restatement of a claim into its existing row, in place."""
    # Claim text is immutable once the claim is not standing — INCLUDING on the
    # very reply that breaks it, which is exactly how the 2026-08-29 replay
    # destroyed the Patriot assertion: it marked the claim broken and rewrote it
    # into a description of its own reversal, so the ledger read back as though
    # the reversal had itself been reversed. The original wording is what the
    # reader was told and what the break is measured against; the reversal
    # belongs in broken_by. Rewording stays correct for refining a claim that is,
    # and remains, standing.
    was = _coerce_status(base.get("status")) or _DEFAULT_STATUS
    now = _coerce_status(mc.get("status")) or was
    proposed = mc.get("status")
    evidence = mc.get("broken_by")
    if was == _DEFAULT_STATUS and now == _DEFAULT_STATUS:
        rewritten = mc.get("claim", base.get("claim", ""))
        # An unmarked rewrite is a contradiction, not a refinement (jx9.9). The
        # freeze above is conditioned on a field the MODEL sets, so keeping
        # status "standing" and editing the claim to match the new facts walks
        # straight through it — which is what three gold-set runs measured it
        # doing on EVERY true break it scored "standing". The ledger then
        # self-corrects with no trace it was ever wrong, and an accountability
        # record that quietly edits its own history cannot be audited at all.
        # Challenged rather than broken: a dropped number can be innocent
        # compression, and challenged is still read by nothing.
        dropped = _dropped_numbers(base.get("claim", ""), rewritten)
        if dropped:
            log.info(
                f"Brief-memory: refused an unmarked rewrite of {base.get('id')} "
                f"— it withdraws {', '.join(sorted(dropped))}: {rewritten[:120]}"
            )
            proposed = "challenged"
            evidence = evidence or f"unmarked rewrite: {rewritten}"
        else:
            base["claim"] = rewritten
    base["topic"] = mc.get("topic", base.get("topic", ""))
    base["last_reaffirmed"] = today
    base["restate_count"] = base.get("restate_count", 0) + 1
    base["source_count"] = max(
        base.get("source_count", 0) or 0,
        mc.get("source_count", 0) or 0,
    )
    new_sev = _coerce_severity(mc.get("severity"))
    base["severity"] = new_sev or base.get("severity", _DEFAULT_SEVERITY)
    # Sourcing can arrive later, so an authored claim may legitimately be
    # promoted to extracted; omitting the field leaves it as it was.
    base["origin"] = _coerce_origin(mc.get("origin")) or base.get(
        "origin", _DEFAULT_ORIGIN
    )
    drv = mc.get("driver")
    if isinstance(drv, str) and drv.strip():
        base["driver"] = drv.strip()
    _apply_status(base, proposed, evidence, today)


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
    extractor_model: str = RECONCILE_MODEL,
    prompt_version: int = PROMPT_VERSION,
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
            _stamp_provenance(base, extractor_model, prompt_version)
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
            _stamp_provenance(base, extractor_model, prompt_version)
            result.append(base)
            returned.add(base["id"])
            continue
        # Claim-admission guard. Judged only on genuinely NEW rows: both paths
        # above resolve to a row already in the ledger, and reaffirming one is
        # not admitting it, so a late "observation" label must not retro-evict a
        # standing claim (jx9.5 froze those). Fails OPEN on a missing or
        # unrecognised label — a model that quietly stops emitting the field
        # would otherwise empty the ledger silently. Absence is caught loudly
        # instead, as a variance field in scripts/score_gold_set.py.
        if (_coerce_kind(mc.get("kind")) or _DEFAULT_KIND) == "observation":
            log.info(
                "Brief-memory: rejected an observation rather than admitting it "
                f"as a durable claim: {mc['claim'][:120]}"
            )
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
            "origin": _coerce_origin(mc.get("origin")) or _DEFAULT_ORIGIN,
        }
        if isinstance(mc.get("driver"), str) and mc["driver"].strip():
            row["driver"] = mc["driver"].strip()
        _apply_status(row, mc.get("status"), mc.get("broken_by"), today)
        _stamp_provenance(row, extractor_model, prompt_version)
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
- For each fact, set "severity" to "low", "normal" or "high", using these worked
  examples so the boundary is not guesswork:
    high = a war starting or ending, a leadership or regime change, a central
      bank changing its policy REGIME, a market-structural break.
    normal = a scheduled rate decision landing where expected, a named
      negotiation opening or continuing, a sanctions package, an earnings-path
      revision. THIS IS THE COMMON CASE — use it by default.
    low = a single official's remark, a minor procedural step, a true but
      low-stakes detail no reader would act on.
  An ongoing war is not "high" every day it runs: the escalation is "high", the
  continuation is "normal". If you are marking more than a few facts "high" you
  are miscalibrated.
- For each fact, set "driver" to a short phrase naming the MECHANISM behind it —
  what is producing it or why it holds ("Hormuz transit risk", "BOJ policy
  normalisation"). Omit it when there is no clear mechanism. Unlike the fact
  itself, a driver MAY be restated in later briefs whenever it explains a move.
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
- For each fact, set "origin" to "extracted" or "authored". "extracted" = the
  fact is grounded in what today's sources actually reported (use this by
  default); "authored" = it is the brief's own interpretation, framing or
  forward-looking read rather than something a source stated. "Iran and Oman are
  negotiating a shipping framework" is extracted; "this represents a genuine
  escalation ladder" is authored. Interpretation is wanted, not banned — mark it
  honestly rather than dropping it. When in doubt, use "extracted".
- For each fact, set "kind" to "claim" or "observation". "claim" = an assertion
  that can still be true or false tomorrow: an event that happened, a decision
  taken, or a standing thesis about how something works. "observation" = a
  measurement of where a market sat on one day — a price, a level, a percentage
  move, a spread, a one-day divergence. A fact may CITE a level; it must not BE
  one. "Japan's 10-year yield held around 2.88% on Thursday" is an observation:
  when the yield prints 2.70% nothing has been contradicted, the number was
  simply superseded. "Brent surged 1.2% on Sunday, the first real repricing of
  the escalation" is also an observation — one day's move with a label attached.
  But "the market is pricing a contained war, not a closed strait; the repricing
  trigger remains an Iranian tanker hit" IS a claim: it says how the market will
  behave and names what would prove it wrong. The test is whether the fact would
  still mean something a week from now with its numbers stripped out. When in
  doubt, use "claim".
- Return at most {max_claims} items — keep only the most important durable facts,
  and keep each "claim" to one terse sentence (no more than ~30 words).
Each array item: {{"id": "<existing id, omit if new>", "claim": "<short fact>", "topic": "<short label>", "source_count": <integer>, "severity": "<low|normal|high>", "status": "<standing|challenged|broken>", "broken_by": "<what contradicted it; omit unless broken>", "origin": "<extracted|authored>", "kind": "<claim|observation>", "driver": "<short mechanism phrase; omit if none>"}}.
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
        driver = str(c.get("driver") or "").strip()
        note = f" — driver: {driver}" if driver else ""
        rows.append(f"  • [{c.get('topic') or 'general'}]{tag} {c['claim']}{note}")
    lines = "\n".join(rows)
    # The old header ("ESTABLISHED — THE READER ALREADY KNOWS THESE") was adopted
    # by the model as vocabulary: it began shipping literal "(established)" tags
    # meaning "I know this and am not telling you". The instruction was also
    # purely suppressive, while MARKET PULSE simultaneously asks the model to
    # explain moves — and yesterday's driver is by construction not today's news,
    # so the two directives contradicted each other. Hence the explicit
    # permission to restate a driver.
    return (
        "## BACKGROUND ALREADY REPORTED\n"
        "Previous briefs already reported these. Do not re-report them as if they "
        "were today's news, and never emit a bare marker such as '(established)' "
        "or '(as noted)' in place of an explanation — if something is worth "
        "mentioning, write it plainly in the prose. You MAY restate the DRIVER of "
        "any of these whenever it explains something today: a mechanism that is "
        "still operating is not old news, and an unexplained move is worse than a "
        "repeated explanation. Lead with what has CHANGED. The parenthetical is "
        "how broadly the fact was corroborated across outlets: lean on 'widely "
        "corroborated' facts with confidence and treat 'single-source' ones more "
        "tentatively.\n\n" + lines + "\n"
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


def _coerce_origin(v) -> str | None:
    """Canonical origin ('extracted'/'authored'), or None to leave the row's
    existing origin alone. Case-insensitive; anything outside the enum is None."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _VALID_ORIGIN:
            return s
    return None


def _coerce_kind(v) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v if v in _VALID_KIND else None


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
            org = _coerce_origin(item.get("origin"))
            if org is not None:
                entry["origin"] = org
            knd = _coerce_kind(item.get("kind"))
            if knd is not None:
                entry["kind"] = knd
            drv = item.get("driver")
            if isinstance(drv, str) and drv.strip():
                entry["driver"] = drv.strip()
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

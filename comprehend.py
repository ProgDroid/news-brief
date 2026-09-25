"""Comprehension: read captured items, decide which are material, extract.

Spec: docs/superpowers/specs/2026-09-04-comprehension-pipeline-design.md

A supervisor job child, deliberately SEPARATE from capture. Capture must stay
continuous and cheap because feeds are windows and a missed item is gone
forever; comprehension can be lazy and batched because a captured item can be
reprocessed indefinitely. Concretely, an unbounded model call inside capture's
600s bound would trip the supervisor's overlap alert 48 times a day.

Nothing reads what this writes. entities/events/event_entities/assertions ship
QUARANTINED, per the rule Epic 1 converged on: quarantine is the default for an
unmeasured field, and measurement is what lifts it. The only thing that can say
whether this works is scripts/score_comprehension.py.
"""

import html
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

import common
import config
from common import log

# Bump on any material change to the triage prompt. A prompt change that is not
# versioned is indistinguishable from a change in the world.
TRIAGE_PROMPT_VERSION = 1
INTEGRATE_PROMPT_VERSION = 2

# A pass must not outlive its own fire time: supervisor's _due_jobs loop alerts
# on any job still running at its next fire. Hourly schedule, 40-minute bound.
DEADLINE_SECONDS = 2400


@dataclass
class Tally:
    """What one pass did. Returned AND logged, because a bare count is
    unattributable: 0 rows written is ambiguous across "nothing was captured",
    "everything was immaterial" and "every model call failed"."""

    enabled: bool = False
    items_seen: int = 0
    triaged_by_rules: int = 0
    triaged_by_model: int = 0
    sampled: int = 0
    material: int = 0
    immaterial: int = 0
    # Structural outcomes of the free rules (spec 2026-09-25 4.4-4.5).
    stale: int = 0
    quote_pages: int = 0
    failed_triage: int = 0
    failed_integration: int = 0
    # Items whose batch died on the network rather than on its contents. Held
    # APART from failed_integration on purpose: no verdict about these items was
    # ever obtained, so counting them as extraction failures misattributes a
    # host fault as a weak extractor -- the same confound that made
    # news-brief-uer a gate-validity bug rather than deferrable polish.
    deferred_transport: int = 0
    # Items whose batch died on the RESPONSE rather than on the network: the
    # model answered and the answer could not be read. Apart from
    # deferred_transport because the two implicate different fixes -- a host
    # fault is waited out, an unreadable response is a prompt or a parser
    # problem -- and apart from failed_integration for the reason above: no
    # verdict about these items was obtained either (news-brief-h8p).
    deferred_response: int = 0
    # Items charged an attempt only because their defer budget was exhausted.
    # The signal that a shape fault has stopped being bad luck and started
    # being a property of the item, which is the one case where charging a
    # no-verdict failure is the right answer.
    defer_cap_hit: int = 0
    gave_up_triage: int = 0
    gave_up_integration: int = 0
    # Material items past the horizon, never to be integrated (spec D4). A
    # standing count, like gave_up_integration. Not a failure: the policy
    # under a budget is that old items give way.
    aged_out: int = 0
    entities_created: int = 0
    entities_resolved: int = 0
    events_created: int = 0
    events_matched: int = 0
    assertions_written: int = 0
    # Distinguishes "one item failed" from "four items were collateral" -- the
    # exact confusion a batch-wide transaction would have produced.
    items_lost_to_savepoint: int = 0
    # Well-formed extractions that carried nothing. NOT a failure: spec 12.2
    # measured real stock-quote and market-summary junk in the corpus, so an
    # empty sample arm is a finding about the corpus, not about the pipeline.
    # Counted separately because folding it into failed_integration makes the
    # failure counters unattributable (news-brief-uer).
    empty_extraction: int = 0
    instrument_entity_refused: int = 0
    candidate_cap_hit: int = 0
    # A `candidate` the model supplied that matched no offered label. Not a
    # failure -- the row falls back to the new-entity/new-event path -- but it
    # is model NON-COMPLIANCE, and a fallback that left no trace would repeat
    # exactly what made failed_integration=172 unattributable.
    unmapped_candidate: int = 0
    # New events the model declined to give a `commitment_state`. NOT a
    # failure: the field is a property of commitments and a factual report has
    # none. Counted because it is now a field that is sometimes absent, and
    # this repo's rule is that an unmeasured field gets measured -- without it
    # nobody can tell a rare edge case from most of the corpus (migration 0011).
    commitment_omitted: int = 0
    # Extractions carrying events but no entities. NOT a failure and NOT empty:
    # the model answered, it just found nothing nameable to attach the event to.
    # Terminal rather than retried, because the answer is deterministic
    # (news-brief-bqa.17).
    entityless_extraction: int = 0
    # Batches whose `items` arrived as a JSON STRING rather than an array and
    # were recovered. Model NON-COMPLIANCE, not a failure -- counted so a
    # healthy failure count cannot hide it continuing.
    items_json_string: int = 0
    # Batches whose `items` arrived wrapped a SECOND time -- {"items": {"items":
    # [...]}} -- and were recovered. Measured 2026-09-10 (news-brief-19i). Held
    # apart from items_json_string because they are different non-compliances
    # with different fixes: one is an encoding mistake, this is a nesting one,
    # and a single counter could not say which was continuing.
    items_double_wrapped: int = 0
    # Why the pass stopped early, or "" if it did not. An account-level failure
    # (billing, auth) or a refusal to spend (unpriced model) stops the WHOLE
    # pass without charging any item: nothing about the items was judged.
    aborted: str = ""
    spent_usd: float = 0.0
    budget_exhausted: bool = False
    budget_balance_usd: float | None = None
    failures: dict = field(default_factory=dict)


class NoEntitySurvived(ValueError):
    """Every entity the model returned was refused by `_resolve_entity`.

    A named subclass rather than a bare ValueError so `failures` can name it.
    This class fails the SAME item on every pass and is therefore certain to be
    retired at the ceiling, which is a different operational fact from an item
    that failed once and would succeed on a retry.
    """


def _note(tally, cause: str, n: int = 1) -> None:
    """Attribute a failure by cause, so `failures` can say WHICH one fired.

    Tolerates a None tally: `parse_integration_response` is called without one
    by `scripts/inspect_integration.py` and by the pure-function tests.
    """
    if tally is None:
        return
    tally.failures[cause] = tally.failures.get(cause, 0) + n


def _absence(obj: dict, field_name: str) -> str:
    """ "missing" when the model omitted the field, "unknown" when it sent a
    value the enum forbids. Keyed on membership, not on a None check, so an
    explicit null counts as present-but-wrong rather than as an omission.

    The two implicate different fixes, which is the whole point of splitting
    them: `missing` means the tool schema does not require the field and the
    model is obeying it exactly as published, `unknown` means the model
    invented a value its own declared enum rules out.
    """
    return "missing" if field_name not in obj else "unknown"


def _log_rejected_value(label: str, obj: dict, field_name: str) -> None:
    """Name the offending value, but only when there IS one.

    The value stays OUT of the failure key: a model can emit arbitrary strings
    and an unbounded key space would make `failures` unreadable exactly when it
    matters most. The key stays at four bounded outcomes; the value goes here.
    """
    if field_name in obj:
        log.warning(f"Comprehend: rejected {label} {obj[field_name]!r}")


# How many keys of a refused container the diagnostic may name. Twelve because
# the question it answers is "which shape is this", and every candidate shape
# is distinguishable inside a dozen keys; a batch is 5 items, so an
# items-keyed-by-index dict fits whole.
_SHAPE_KEY_CAP = 12


def _shape_of(value) -> str:
    """Name a refused value's SHAPE without printing its content.

    `items type=dict` recurred nine times on 2026-09-10 and the logs could not
    say whether the dict was items keyed by index, one item emitted bare, or
    the array nested a level deeper -- three different recoveries, so the fix
    could only have been guessed (news-brief-19i).

    Keys and types, never values: the keys settle the shape question exactly,
    while a truncated dump of news text answers it only by luck and puts
    unbounded article content in the log.
    """
    if isinstance(value, dict):
        keys = sorted(map(str, value))
        more = len(keys) - _SHAPE_KEY_CAP
        # Named, not silently cut. A truncated list of keys reads exactly like
        # a complete one, which is how an eyeballed audit ends up measuring the
        # renderer rather than the data.
        tail = f" +{more} more" if more > 0 else ""
        inner = next(iter(value.values()), None)
        return (
            f"dict keys={keys[:_SHAPE_KEY_CAP]}{tail} "
            f"first_value={type(inner).__name__}"
        )
    if isinstance(value, list):
        head = type(value[0]).__name__ if value else "empty"
        return f"list len={len(value)} first={head}"
    if isinstance(value, str):
        return f"str len={len(value)}"
    return type(value).__name__


def _is_transient(exc: BaseException) -> bool:
    """True when the failure obtained no verdict AND is plausibly temporary.

    Only these may be retried without charging `integrate_attempts`, which is a
    ONE-WAY DOOR at 3. A ~90s DNS fault on 2026-09-08 failed three whole batches
    and spent an attempt on every item in them, for a fault that said nothing
    about any of those items; because `pending_integration` is `ORDER BY i.id
    DESC` that loss lands on the NEWEST corpus rather than at random. 429 and
    5xx sit here with the connection faults: they are the server declining to
    answer, which is likewise not a statement about what was asked.

    The set is deliberately narrow, because the failure mode of being too
    liberal is quieter than the bug it fixes. A 4xx other than 429 means the
    request itself was refused and will be refused identically next hour, so
    deferring it forever would re-pay an 8192-token generation every pass while
    nothing ever retired it.
    """
    if not isinstance(exc, requests.RequestException):
        return False
    resp = getattr(exc, "response", None)
    if resp is None:
        return True
    return resp.status_code == 429 or resp.status_code >= 500


_ACCOUNT_STATUSES = {401: "auth", 402: "billing", 403: "auth"}


def _account_failure(exc: BaseException) -> str | None:
    """ "billing" or "auth" when the failure is about the ACCOUNT, else None.

    news-brief-0rg: on 2026-09-25 the balance ran out, every call returned
    402, and _is_transient -- which spares only 429 and 5xx -- charged each
    one to the ITEM. Triage gave items up after 3 passes and integration
    retired them after 13, for a fault that said nothing about any of them.
    Every later call in the pass would fail identically, so the pass stops.
    """
    if not isinstance(exc, requests.RequestException):
        return None
    resp = getattr(exc, "response", None)
    kind = _ACCOUNT_STATUSES.get(getattr(resp, "status_code", None))
    if kind:
        return kind
    # The DOCUMENTED empty-balance error is 402 billing_error, but the widely
    # reported one is 400 invalid_request_error "credit balance is too low".
    # Which one the host got on 2026-09-25 was never recorded (only the status
    # was logged), so match the body as well; but only for a 400 -- a 429 or
    # 5xx whose body happens to mention "credit balance" is still a transient
    # server fault (_is_transient), not a statement about the account.
    if getattr(resp, "status_code", None) != 400:
        return None
    try:
        body = resp.json().get("error") or {}
        message = body.get("message", "")
    except Exception:
        return None
    if "credit balance" in str(message).lower():
        return "billing"
    return None


ABORT_STATE_KEY = "comprehend_abort"


def _abort(
    conn, tally: Tally, reason: str, exc: BaseException | None = None, now=None
) -> Tally:
    """Stop the pass WITHOUT charging anything, and say why.

    Logs the response BODY, not just the status: an HTTPError stringifies to a
    status and a URL, and that is why nobody can now say whether 2026-09-25's
    empty balance came back as 400 or 402 (http-error-body-is-the-diagnosis).

    Persists the reason so the monitor -- a fresh process every hour, with no
    memory of this pass -- can say ONCE that every pass is failing (spec
    4.2-4.3): an abort never charges an item, so nothing else about this run
    is otherwise visible outside the log. `now` is the caller's clock when
    reachable (all three call sites are inside run(), where it is already in
    scope); a direct call with none falls back to reading the clock itself.
    """
    conn.commit()
    tally.aborted = reason
    body = getattr(getattr(exc, "response", None), "text", "") or ""
    log.error(
        f"Comprehend: pass aborted ({reason}); no item was charged. body={body[:500]!r}"
    )
    config.set_runtime_state(
        {
            ABORT_STATE_KEY: {
                "reason": reason,
                "at": (now or datetime.now(timezone.utc)).isoformat(),
            }
        }
    )
    log.info(f"Comprehend: {tally}")
    return tally


def _timed_post(request: dict, label: str, timeout: int, max_attempts: int) -> dict:
    """Post via brief's HTTP path, logging how long it actually took.

    The elapsed time is the point. Both timeouts below are opening guesses, and
    a guess is only defensible while something is measuring it -- this is what
    lets the host retune the knobs from real durations instead of from a
    token-rate estimate. Logged on failure too: a call that timed out is
    precisely the one whose duration you want.
    """
    import brief

    started = time.monotonic()
    try:
        resp = brief._post_messages(request, timeout=timeout, max_attempts=max_attempts)
    except Exception:
        log.warning(
            f"Comprehend: {label} call failed after "
            f"{time.monotonic() - started:.1f}s (timeout={timeout}s)"
        )
        raise
    # INPUT tokens, not just output. This logged `out=` alone until 2026-09-10,
    # which meant the number that actually sizes the bill was never written
    # down: on 2026-09-08 this pipeline spent 5.14M Sonnet tokens in a day, 51x
    # its own baseline, and the only instrument that could say so was the
    # billing console three days later.
    #
    # The cache counters are here for the reason the API docs give: a zero
    # cache_read across repeated identical prefixes is the signature of a silent
    # invalidator, and a cache that quietly stops being read is indistinguishable
    # from one that was never configured -- unless someone is counting.
    #
    # `.get` throughout, never `[...]`: telemetry must not be able to break the
    # call it measures. A missing field would otherwise turn a cosmetic gap into
    # a failed integration batch.
    usage = resp.get("usage") or {}
    log.info(
        f"Comprehend: {label} call took {time.monotonic() - started:.1f}s "
        f"(timeout={timeout}s) stop_reason={resp.get('stop_reason')} "
        f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
        f"cache_read={usage.get('cache_read_input_tokens')} "
        f"cache_write={usage.get('cache_creation_input_tokens')}"
    )
    return resp


def call_triage(request: dict) -> dict:
    """The network seam for triage. Tests monkeypatch this."""
    return _timed_post(
        request, "triage", int(common.COMPREHEND_TRIAGE_TIMEOUT), max_attempts=2
    )


def call_integration(request: dict) -> dict:
    """The network seam for integration. Tests monkeypatch this.

    ONE HTTP attempt, deliberately. item_triage.integrate_attempts < 3 already
    gates the integration SELECT and the failure paths increment it, so a
    failed item is retried on the next hourly pass. Retrying here as well
    multiplies against that ceiling -- up to six charged 8192-token
    generations for one persistently-bad item -- and, unlike the item-level
    retry, it spends this pass's DEADLINE_SECONDS, shrinking how many other
    items get processed at all (news-brief-wvt).
    """
    return _timed_post(
        request,
        "integration",
        int(common.COMPREHEND_INTEGRATE_TIMEOUT),
        max_attempts=1,
    )


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# $ per million tokens, (input, output). Source: the Anthropic model table
# (claude-api skill, cached 2026-06-24), checked 2026-09-25. Cache tokens use
# the documented multipliers. A model missing from this table REFUSES to run
# (UnpricedModel): pricing an unknown model at $0 would make the budget an
# accepted-and-inert config the moment a settings row named a new model.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
BATCH_FACTOR = 0.5
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


class UnpricedModel(RuntimeError):
    """A model with no entry in PRICES_PER_MTOK."""


def price_of(model: str) -> tuple[float, float]:
    try:
        return PRICES_PER_MTOK[model]
    except KeyError:
        raise UnpricedModel(model) from None


def cost_usd(model: str, usage: dict, *, batch: bool = False) -> float:
    """Dollars for one call's `usage`. Raises UnpricedModel before anything else."""
    pin, pout = price_of(model)
    if not usage:
        log.warning(f"Comprehend: {model} response carried no usage; costed at $0")
        return 0.0
    tokens_in = (
        (usage.get("input_tokens") or 0) * pin
        + (usage.get("cache_creation_input_tokens") or 0) * pin * _CACHE_WRITE_MULT
        + (usage.get("cache_read_input_tokens") or 0) * pin * _CACHE_READ_MULT
    )
    usd = (tokens_in + (usage.get("output_tokens") or 0) * pout) / 1_000_000
    return usd * (BATCH_FACTOR if batch else 1.0)


def record_spend(conn, stage, model, usage, *, batch_id=None, batch=False) -> float:
    usd = cost_usd(model, usage, batch=batch)
    conn.execute(
        "INSERT INTO comprehend_spend "
        "  (stage, model, input_tokens, output_tokens, usd, batch_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            stage,
            model,
            (usage or {}).get("input_tokens") or 0,
            (usage or {}).get("output_tokens") or 0,
            usd,
            batch_id,
        ),
    )
    return usd


BUDGET_STATE_KEY = "comprehend_budget"


def accrue(state, now, allowance: float, max_days: float) -> dict:
    """The bucket after accruing up to `now`. Pure.

    Continuous accrual at allowance/day, capped at allowance * max_days. An
    unreadable state (a hand-edited row, a type drift) starts over at one
    day's allowance rather than crashing every pass. A timestamp in the future
    (clock moved back) accrues nothing rather than a negative amount.
    """
    # min(), not bare allowance: without it a first pass under a MAX_DAYS < 1
    # (a fractional cap, tightened during an incident) would open above the
    # cap it is meant to respect.
    fresh = {
        "balance_usd": min(float(allowance), float(allowance) * float(max_days)),
        "at": now.isoformat(),
    }
    try:
        balance = float(state["balance_usd"])
        if not math.isfinite(balance):
            # A hand-edited or corrupted `inf`/`nan` row must be treated the
            # same as an unreadable one -- reinitialised, not carried forward
            # into an arithmetic op that turns the whole bucket non-finite.
            raise ValueError("non-finite balance_usd")
        then = datetime.fromisoformat(state["at"])
        # INSIDE the try: a hand-edited naive timestamp parses fine and then
        # raises TypeError (aware minus naive) on the subtraction, on every pass.
        days = max(0.0, (now - then).total_seconds() / 86400)
    except (TypeError, KeyError, ValueError):
        return fresh
    out = dict(state)
    out["balance_usd"] = min(allowance * max_days, balance + allowance * days)
    out["at"] = now.isoformat()
    return out


class Budget:
    """One pass's view of the bucket. Every debit is persisted immediately,
    so a crash mid-pass cannot un-spend what was spent."""

    def __init__(self, state: dict) -> None:
        self.state = state

    @property
    def balance(self) -> float:
        return float(self.state["balance_usd"])

    def can_spend(self) -> bool:
        return self.balance > 0

    def debit(self, usd: float) -> None:
        self.state["balance_usd"] = self.balance - usd
        config.set_runtime_state({BUDGET_STATE_KEY: self.state})

    def mark_exhausted(self, now) -> None:
        # The UTC DATE, overwritten each time: the alert is at most once per
        # day the budget ran out (spec 4.3, revised). A once-per-episode key
        # cleared by "a full day banked" could fire once and then never again,
        # because spend runs at about the allowance.
        self.state["exhausted_on"] = now.date().isoformat()
        config.set_runtime_state({BUDGET_STATE_KEY: self.state})


def _finite_or_default(value: float, knob_name: str) -> float:
    """A non-finite or negative operator value falls back to the KNOBS
    default rather than reaching accrue(), where inf/nan would make the
    bucket non-finite forever (an unbounded budget, silently)."""
    if not math.isfinite(value) or value < 0:
        default = common.KNOBS[knob_name].default
        log.warning(
            f"Comprehend: {knob_name}={value!r} is not a usable non-negative "
            f"number; using its default {default}"
        )
        return float(default)
    return value


def _allowance() -> tuple[float, float]:
    return (
        _finite_or_default(
            float(common.COMPREHEND_DAILY_BUDGET_USD), "COMPREHEND_DAILY_BUDGET_USD"
        ),
        _finite_or_default(
            float(common.COMPREHEND_BUDGET_MAX_DAYS), "COMPREHEND_BUDGET_MAX_DAYS"
        ),
    )


def open_budget(now) -> Budget:
    allowance, max_days = _allowance()
    state = accrue(
        config.runtime_state().get(BUDGET_STATE_KEY), now, allowance, max_days
    )
    config.set_runtime_state({BUDGET_STATE_KEY: state})
    return Budget(state)


def pause_budget(now) -> None:
    """A disabled pass moves the clock without accruing: time spent off
    must not bank budget that the flip back on would then release at once."""
    state = config.runtime_state().get(BUDGET_STATE_KEY)
    if isinstance(state, dict):
        config.set_runtime_state({BUDGET_STATE_KEY: {**state, "at": now.isoformat()}})


def run(conn, now=None) -> Tally:
    """One full pass. Bounded by DEADLINE_SECONDS.

    Commit boundaries are load-bearing: one transaction per micro-batch, one
    savepoint per item inside it (spec section 6.4).
    """
    now = now or datetime.now(timezone.utc)
    tally = Tally(enabled=bool(common.COMPREHEND_ENABLED))
    if not tally.enabled:
        log.info(
            "Comprehend: disabled by COMPREHEND_ENABLED; no items read "
            "(budget clock paused)"
        )
        pause_budget(now)
        return tally

    # Resolved ONCE, here, and threaded through every use below. The settings
    # cache TTL is 60s and a pass can run up to DEADLINE_SECONDS (40 minutes),
    # so re-resolving at each call site let an operator's mid-pass model edit
    # steer LATER calls in this same pass to a different model than the one
    # just priced -- reviewer's probe (I1): a mid-pass switch to an unpriced
    # model produced 0 ledger rows, a $0 debit, and 3 items marked `failed`,
    # because UnpricedModel was raised by record_spend AFTER the paid call had
    # already happened. One resolution per pass makes the price check above
    # and every request/ledger/provenance write below agree by construction.
    triage_model = _triage_model()
    integrate_model = _integrate_model()

    # Refuse to spend on a model we cannot price, BEFORE any call (spec 4.3).
    for model in {triage_model, integrate_model}:
        try:
            price_of(model)
        except UnpricedModel:
            return _abort(conn, tally, f"unpriced_model:{model}", now=now)

    budget = open_budget(now)
    deadline = time.monotonic() + DEADLINE_SECONDS
    index = SurfaceIndex.build(conn)
    outlets = dict(conn.execute("SELECT id, name FROM outlets").fetchall())

    pending = pending_triage(
        conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_MAX_ITEMS)
    )
    tally.items_seen = len(pending)

    # --- Triage: rules half first, so the model never sees what a lookup answered.
    undecided = []
    for item in pending:
        # Rule order (spec 4.5): stale, quote page, tracked topic, then the model.
        if item["stale"]:
            record_triage(
                conn, item["id"], "stale", "stale", None, TRIAGE_PROMPT_VERSION
            )
            tally.stale += 1
            continue
        if common.is_quote_page(item.get("title")):
            record_triage(
                conn,
                item["id"],
                "immaterial",
                "quote_page",
                None,
                TRIAGE_PROMPT_VERSION,
            )
            tally.triaged_by_rules += 1
            tally.immaterial += 1
            tally.quote_pages += 1
            continue
        hit = triage_by_rules(item, index)
        if hit:
            record_triage(
                conn, item["id"], "material", hit.reason, None, TRIAGE_PROMPT_VERSION
            )
            tally.triaged_by_rules += 1
            tally.material += 1
        else:
            undecided.append(item)
    conn.commit()

    # --- Triage: model half, on the remainder only.
    for batch in _chunk(undecided, int(common.COMPREHEND_TRIAGE_BATCH)):
        if time.monotonic() >= deadline:
            break
        if not budget.can_spend():
            tally.budget_exhausted = True
            budget.mark_exhausted(now)
            break
        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        try:
            resp = call_triage(build_triage_request(payload, model=triage_model))
            usd = record_spend(conn, "triage", triage_model, resp.get("usage") or {})
            tally.spent_usd += usd
            budget.debit(usd)
            verdicts = parse_triage_response(resp, {it["id"] for it in batch})
        except Exception as exc:
            kind = _account_failure(exc)
            if kind:
                return _abort(conn, tally, kind, exc, now=now)
            tally.failed_triage += len(batch)
            log.warning("Comprehend: triage batch failed", exc_info=True)
            for it in batch:
                record_triage(
                    conn,
                    it["id"],
                    "failed",
                    "error",
                    triage_model,
                    TRIAGE_PROMPT_VERSION,
                )
            conn.commit()
            continue
        for it in batch:
            material = verdicts.get(it["id"], False)
            record_triage(
                conn,
                it["id"],
                "material" if material else "immaterial",
                "topical" if material else "none",
                triage_model,
                TRIAGE_PROMPT_VERSION,
            )
            tally.triaged_by_model += 1
            tally.material += int(material)
            tally.immaterial += int(not material)
        conn.commit()

    # --- The unconfounded control arm.
    for item_id in select_sampled(
        conn, TRIAGE_PROMPT_VERSION, int(common.COMPREHEND_SAMPLE_PER_DAY)
    ):
        record_triage(conn, item_id, "material", "sampled", None, TRIAGE_PROMPT_VERSION)
        conn.execute(
            "UPDATE item_triage SET sampled_at = now() "
            "WHERE item_id = %s AND triage_prompt_version = %s",
            (item_id, TRIAGE_PROMPT_VERSION),
        )
        tally.sampled += 1
    conn.commit()

    # --- Integration.
    # published_at is carried because write_extraction needs it for
    # events.occurred_at. Without it every created event is invisible to
    # candidate_events and corroboration is impossible.
    material_items = pending_integration(conn, int(common.COMPREHEND_MAX_ITEMS))
    published = {it["id"]: it["published_at"] for it in material_items}

    for batch in _chunk(material_items, int(common.COMPREHEND_INTEGRATE_BATCH)):
        if time.monotonic() >= deadline:
            break
        if not budget.can_spend():
            tally.budget_exhausted = True
            budget.mark_exhausted(now)
            break

        # Quote pages triaged `material` before this deploy still reach here:
        # is_quote_page is otherwise consulted only in the triage rules loop
        # above, so a page triaged under an older code version keeps its old
        # verdict forever (I2, reviewer finding -- the runbook's step-4
        # recovery SQL re-queues exactly this population). Reclassify for
        # free, the same pair the triage rules loop already writes for a page
        # seen for the first time; 0014's CHECK constraints allow
        # (immaterial, quote_page), and record_triage's ON CONFLICT overwrites
        # the existing (material, tracked_entity) row rather than erroring.
        quote_page_ids = {
            it["id"] for it in batch if common.is_quote_page(it.get("title"))
        }
        if quote_page_ids:
            for it in batch:
                if it["id"] in quote_page_ids:
                    record_triage(
                        conn,
                        it["id"],
                        "immaterial",
                        "quote_page",
                        None,
                        TRIAGE_PROMPT_VERSION,
                    )
                    tally.quote_pages += 1
            conn.commit()
            batch = [it for it in batch if it["id"] not in quote_page_ids]
            if not batch:
                continue

        payload = [dict(it, outlet=outlets.get(it["outlet_id"], "?")) for it in batch]
        hits = [
            sf
            for it in batch
            for sf in index.match(f"{clean(it['title'])}\n{clean(it['body'])}")
            if sf.entity_id is not None
        ]
        # Most-recent-first (spec §6), same as the event cap's ORDER BY DESC:
        # truncation should drop the least likely candidates. Entity ids are
        # monotonically increasing, so `id DESC` IS newest-first, and it is
        # unambiguous -- unlike index insertion order, which is index-build
        # order (SurfaceIndex.build's SELECT has no ORDER BY, so DB order is
        # arbitrary) followed by this-run's add_entity appends. Keeping the
        # FIRST N of insertion order means an entity THIS RUN just created is
        # the first one dropped, defeating the mid-run index refresh that
        # exists specifically to surface it as a candidate.
        ranked = sorted(dict.fromkeys(sf.entity_id for sf in hits), reverse=True)
        if len(ranked) > CANDIDATE_ENTITY_CAP:
            tally.candidate_cap_hit += 1
        entity_ids = ranked[:CANDIDATE_ENTITY_CAP]
        cand_entities = (
            [
                {"id": r[0], "name": r[1], "type": r[2]}
                for r in conn.execute(
                    "SELECT id, name, type FROM entities WHERE id = ANY(%s)",
                    (entity_ids,),
                ).fetchall()
            ]
            if entity_ids
            else []
        )
        cand_events = candidate_events(
            conn, entity_ids, [it["title"] for it in batch], tally
        )

        try:
            resp = call_integration(
                build_integration_request(
                    payload, cand_entities, cand_events, model=integrate_model
                )
            )
            usd = record_spend(
                conn, "integration", integrate_model, resp.get("usage") or {}
            )
            tally.spent_usd += usd
            budget.debit(usd)
            extractions = parse_integration_response(
                resp,
                {it["id"] for it in batch},
                label_map(_ENTITY_LABEL, cand_entities),
                label_map(_EVENT_LABEL, cand_events),
                tally,
            )
        except Exception as exc:
            kind = _account_failure(exc)
            if kind:
                return _abort(conn, tally, kind, exc, now=now)
            # A network fault is a statement about the host, not about the news.
            # Charging it would let three unrelated outages retire an item that
            # was never once judged -- see _is_transient (news-brief-bqa.13).
            if _is_transient(exc):
                tally.deferred_transport += len(batch)
                _note(tally, "transport", len(batch))
                log.warning(
                    f"Comprehend: integration batch deferred, no attempt charged: "
                    f"{type(exc).__name__}: {exc}"
                )
                conn.commit()
                continue
            # Nor did THIS batch obtain a verdict. The model answered and the
            # answer could not be read, which is a statement about the response
            # -- the prompt, the tool schema, the parser -- and not about any
            # item in the batch. Measured 2026-09-10: nine batches lost to
            # `items type=dict`, 45 items charged for it.
            #
            # But the deferral is BUDGETED, unlike transport's. A network fault
            # clears on its own; a response the model mangles because of what
            # this item contains does not, and an unbudgeted defer would re-pay
            # an 8192-token generation every hour with nothing able to retire
            # it. So the ceiling converts the failure back into a charge, and
            # the three-strike door still closes behind it.
            #
            # Charge FIRST, then defer. The other order walks a row onto the
            # ceiling and charges it in the same pass, which raises the
            # effective ceiling by one every time and never reaches it.
            ids = [it["id"] for it in batch]
            ceiling = int(common.COMPREHEND_MAX_DEFERS)
            charged = conn.execute(
                "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
                "WHERE item_id = ANY(%s) AND integrate_defers >= %s "
                "RETURNING item_id",
                (ids, ceiling),
            ).fetchall()
            deferred = conn.execute(
                "UPDATE item_triage SET integrate_defers = integrate_defers + 1 "
                "WHERE item_id = ANY(%s) AND integrate_defers < %s "
                "RETURNING item_id",
                (ids, ceiling),
            ).fetchall()
            if charged:
                tally.failed_integration += len(charged)
                tally.defer_cap_hit += len(charged)
                _note(tally, f"batch_capped:{type(exc).__name__}", len(charged))
            if deferred:
                tally.deferred_response += len(deferred)
                _note(tally, f"batch:{type(exc).__name__}", len(deferred))
            log.warning(
                f"Comprehend: integration batch failed; "
                f"{len(deferred)} deferred, {len(charged)} charged at the "
                f"defer ceiling of {ceiling}",
                exc_info=True,
            )
            conn.commit()
            continue

        # A row _validate_item rejected never reaches write_batch, so nothing
        # else advances it: it satisfies pending_integration's predicate every
        # pass and `ORDER BY i.id DESC` keeps it among the newest items until
        # it ages past the 14-day horizon, re-paying its share of the call
        # each pass in that window with no operator-visible signal. Charging
        # it an attempt lets the ceiling of 3 retire it first, which is the
        # same treatment a whole-batch failure already gets.
        dropped = [
            it["id"]
            for it in batch
            if it["id"] not in {e["item_id"] for e in extractions}
        ]
        if dropped:
            tally.failed_integration += len(dropped)
            # Counts EVERY absent item, while the `validate:*` keys attribute
            # only the subset _validate_item actually saw and rejected. The
            # difference is therefore items the model never returned at all --
            # a distinct defect, and one no single counter could have named.
            _note(tally, "dropped", len(dropped))
            conn.execute(
                "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
                "WHERE item_id = ANY(%s)",
                (dropped,),
            )

        for e in extractions:
            e["published_at"] = published.get(e["item_id"])
        write_batch(conn, extractions, index, tally, model=integrate_model)
        conn.commit()

    tally.gave_up_integration = conn.execute(
        "SELECT count(*) FROM item_triage WHERE integrate_attempts >= 3"
    ).fetchone()[0]
    # aged_out counts only material items pending_integration will never pick
    # up at all (R13: extracted into aged_out_count, called from both here and
    # budget_verdict -- see that function's docstring for the IS NULL note).
    tally.aged_out = aged_out_count(conn)
    tally.gave_up_triage = conn.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'failed' AND attempts >= 3"
    ).fetchone()[0]
    tally.budget_balance_usd = round(budget.balance, 4)
    # A clean pass (nothing aborted) retracts a previous abort: the state key
    # is the monitor's only memory across the hourly process restart, and
    # leaving it set after recovery would keep telling the operator about a
    # failure that stopped happening (capture.liveness contract).
    #
    # R19 (kept as-is on review): this clears on ANY pass that reaches this
    # line, not only one that spent money -- a pass with nothing to do is
    # "clean" too. That is a deliberate trade, not an oversight: a premature
    # clear fails LOUD, not silent. If the account is still actually broken,
    # the very next pass with work to do aborts again, and because
    # _alert_once's key was cleared it simply re-sends -- worst case, one
    # duplicate Telegram message. The stricter alternative ("only a
    # successfully PAID call clears") would have to special-case
    # unpriced_model, which never spends by design, against billing/auth,
    # which do -- more state to get wrong for a failure mode that costs at
    # most a repeat.
    if ABORT_STATE_KEY in config.runtime_state():
        config.clear_runtime_state([ABORT_STATE_KEY])
    log.info(f"Comprehend: {tally}")
    return tally


def retirement(conn) -> tuple[str, str] | None:
    """(episode key, message) when items have been retired or are one failure
    from it, else None.

    The key names the SITUATION rather than the check, so a caller that
    remembers the last key it sent speaks once per change instead of once per
    monitor run -- the difference between one message and one every hour until
    someone looks (news-brief-bqa.15).

    No rate and no threshold. `capture.liveness` refuses to alert on quality
    RATES because the rate separating a bad day from a broken feed has not been
    measured, and a guessed one is indistinguishable to the operator from a
    measured one. That reasoning holds here and the fix is to report neither:
    both numbers below are exact counts -- of an irreversible event, and of the
    population one failure away from it.

    The at-risk half is the actionable one. `gave_up_integration` can only ever
    report a loss that has already happened; the manual reset the operator
    would run works while the items are still alive.
    """
    # Deliberately the same shape as Tally.gave_up_integration: an alert that
    # counted a different population than the log line would make the two
    # disagree with no way to tell which was wrong.
    retired = conn.execute(
        "SELECT count(*) FROM item_triage WHERE integrate_attempts >= 3"
    ).fetchone()[0]
    # The predicate the integration select ACTUALLY reads, plus the last
    # strike. A guard testing a predicate its consumer does not read counts
    # items in no danger: one that failed twice and then SUCCEEDED is never
    # offered again, so it can never take a third strike.
    at_risk = conn.execute(
        "SELECT count(*) FROM item_triage "
        "WHERE triage_prompt_version = %s AND verdict = 'material' "
        "  AND integrate_attempts = 2 "
        "  AND (integrated_at IS NULL OR integrate_prompt_version < %s)",
        (TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION),
    ).fetchone()[0]
    if not retired and not at_risk:
        return None
    return (
        f"retired:{retired}|risk:{at_risk}",
        f"Comprehension has permanently retired {retired} item(s) after three "
        f"failed integration attempts, and {at_risk} more are one failure "
        f"away. Recover the survivors with: UPDATE item_triage SET "
        f"integrate_attempts = 0 WHERE integrate_attempts > 0;",
    )


_ABORT_ADVICE = {
    "billing": "The Anthropic balance is empty. Top it up; nothing was charged "
    "to any item, and the next pass after that resumes on its own.",
    "auth": "The API key was refused (401/403). Check ANTHROPIC_API_KEY on the host.",
}


def abort_verdict(state: dict) -> tuple[str, str] | None:
    """(episode key, message) while comprehension is refusing to run at all --
    an empty balance, a refused key, or an unpriced model -- each of which
    stops EVERY pass before charging an item, which is otherwise silent
    (spec 4.2-4.3). `state` is a `config.runtime_state()` snapshot; the caller
    owns dedup against the last key sent."""
    a = state.get(ABORT_STATE_KEY)
    if not isinstance(a, dict) or not a.get("reason"):
        return None
    reason = a["reason"]
    if reason.startswith("unpriced_model:"):
        model = reason.split(":", 1)[1]
        advice = (
            f"Model {model!r} has no price in comprehend.PRICES_PER_MTOK, "
            "so comprehension refuses to spend on it. Add its price, or "
            "point the model settings row back at a priced model."
        )
    else:
        advice = _ABORT_ADVICE.get(reason, "See the comprehend log.")
    return (f"abort:{reason}", f"Comprehension is stopping every pass: {advice}")


def budget_verdict(conn, state: dict, now) -> tuple[str, str] | None:
    """(episode key, message) on the UTC day the budget ran dry (spec 4.3).

    Keyed on the day, not on `exhausted_on` alone: `!= today` (not `is None`)
    is what makes yesterday's exhaustion silent again at midnight UTC, so a
    fresh key is available for a fresh episode without a manual reset.
    """
    b = state.get(BUDGET_STATE_KEY)
    today = now.date().isoformat()
    if not isinstance(b, dict) or b.get("exhausted_on") != today:
        return None
    allowance, max_days = _allowance()
    # Budget-starved items must never be silent (spec 4.3, revised): the day's
    # structural outcomes ride along with the one alert.
    #
    # R18: NOT `created_at >= %s::date`. Postgres casts a bare date to
    # timestamptz in the SESSION TimeZone, and db.connect() never sets one --
    # on a non-UTC host "today" would silently shift to that session's
    # midnight instead of UTC midnight. `today` here is already derived from
    # `now`, which every caller in this module treats as UTC, so the boundary
    # must be built the same way: a tz-aware UTC midnight computed in Python,
    # compared with no cast at all.
    today_start_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    stale_today = conn.execute(
        "SELECT count(*) FROM item_triage WHERE verdict = 'stale' AND created_at >= %s",
        (today_start_utc,),
    ).fetchone()[0]
    # R13: the same query run() tallies, through the one function -- never a
    # second copy of it (reconstruction-drifts-from-production).
    aged_out = aged_out_count(conn)
    balance = accrue(b, now, allowance, max_days)["balance_usd"]
    untriaged = conn.execute(
        "SELECT count(*) FROM items i LEFT JOIN item_triage t "
        "  ON t.item_id = i.id AND t.triage_prompt_version = %s WHERE t.id IS NULL",
        (TRIAGE_PROMPT_VERSION,),
    ).fetchone()[0]
    # Spec 4.3 asks for BOTH counts. Through the production predicate, never
    # a copy of it (reconstruction-drifts-from-production).
    awaiting = len(pending_integration(conn, 1_000_000))
    return (
        f"budget:{today}",
        f"Comprehension's budget ran out today ({today}). "
        f"Balance ${balance:.2f} of ${allowance:.2f}/day (cap {max_days:g} days); "
        f"{untriaged} items untriaged, {awaiting} awaiting integration; "
        f"{stale_today} went stale today and {aged_out} have aged out unintegrated. "
        "It resumes as the allowance accrues. To spend more, raise the "
        "COMPREHEND_DAILY_BUDGET_USD settings row.",
    )


def pending_triage(conn, version: int, limit: int) -> list[dict]:
    """Items with no verdict at this version, or a retryable failure.

    NEWEST first (spec 2026-09-25 D4). This was oldest-first on the argument
    that "nothing reads this layer yet, so completeness beats recency", which
    assumed a small backlog. A 12-day pause left 24,045 items, drained at
    $0.0027 each, oldest first. Under a budget, something must give when
    arrivals outrun it, and a KB that lags the news cannot corroborate current
    events -- so the OLD items give, and the horizon marks them stale.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at, "
        "  coalesce(i.published_at, i.created_at) "
        "    < now() - make_interval(days => %s) AS stale "
        "FROM items i "
        "LEFT JOIN item_triage t "
        "  ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.id IS NULL OR (t.verdict = 'failed' AND t.attempts < 3) "
        "ORDER BY i.id DESC "
        "LIMIT %s",
        # CANDIDATE_WINDOW_DAYS is defined further down the module; module-level
        # names resolve at call time, so the forward reference is fine.
        (CANDIDATE_WINDOW_DAYS, version, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
            "stale": r[5],
        }
        for r in rows
    ]


def pending_integration(conn, limit: int) -> list[dict]:
    """The ONE definition of what integration picks up next.

    scripts/inspect_integration.py used to carry its own copy of this SELECT,
    which is how a diagnostic ends up probing a query production no longer
    runs (news-brief-bqa.26). Call this; never re-derive it.
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.body, i.outlet_id, i.published_at FROM items i "
        "JOIN item_triage t ON t.item_id = i.id AND t.triage_prompt_version = %s "
        "WHERE t.verdict = 'material' AND t.integrate_attempts < 3 "
        "  AND (t.integrated_at IS NULL OR t.integrate_prompt_version < %s) "
        "  AND coalesce(i.published_at, i.created_at) "
        "      >= now() - make_interval(days => %s) "
        "ORDER BY i.id DESC LIMIT %s",
        (TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION, CANDIDATE_WINDOW_DAYS, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "body": r[2] or "",
            "outlet_id": r[3],
            "published_at": r[4],
        }
        for r in rows
    ]


def aged_out_count(conn) -> int:
    """Material items pending_integration will never pick up at all -- the
    un-integrated half of D4's horizon policy (R13). One query, called from
    both run()'s tally and budget_verdict(); never a copy of it
    (reconstruction-drifts-from-production).

    integrated_at IS NULL, not "integrate_prompt_version < current": a row
    integrated under an OLDER prompt version is already in the KB, so counting
    it here would make every INTEGRATE_PROMPT_VERSION bump read as mass loss
    (R9).
    """
    return conn.execute(
        "SELECT count(*) FROM item_triage t JOIN items i ON i.id = t.item_id "
        "WHERE t.triage_prompt_version = %s AND t.verdict = 'material' "
        "  AND t.integrate_attempts < 3 AND t.integrated_at IS NULL "
        "  AND coalesce(i.published_at, i.created_at) "
        "      < now() - make_interval(days => %s)",
        (TRIAGE_PROMPT_VERSION, CANDIDATE_WINDOW_DAYS),
    ).fetchone()[0]


# Surface forms that are also ordinary English words. A form on this list never
# matches, whatever entity claims it. Short and hand-maintained on purpose: a
# large stop-list hides a matcher that is too loose.
STOP_FORMS = frozenset(
    {
        "will",
        "may",
        "can",
        "us",
        "it",
        "he",
        "she",
        "they",
        "the",
        "and",
        "for",
        "was",
        "are",
        "has",
        "had",
        "new",
        "one",
        "two",
        "all",
        "any",
    }
)

# Below this length a form must match case-sensitively. Acronyms are real
# entities (US, EU, UN, IMF); lowercased they collide with common words.
_CASE_SENSITIVE_BELOW = 4


def clean(text: str | None) -> str:
    """Decode HTML entities and collapse whitespace.

    Measured on production 2026-09-05: item bodies carry literal `&nbsp;`.
    Reuters items read `...facing 10 years in jail&nbsp;&nbsp;Reuters`. Without
    unescaping, a surface form spanning an entity boundary silently fails to
    match -- and a silent miss in the tracked half presents as "the KB did not
    find that interesting", not as an error.

    Used by BOTH the matcher and the prompt builders, so the model never sees
    entity noise either.
    """
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def form_matches(form: str, text: str) -> bool:
    """Word-boundary match, never substring.

    Substring matching is a recorded failure elsewhere in this repo: MU matched "Musk"
    """
    form = (form or "").strip()
    if not form or not text:
        return False
    # The stop-list check is CASE-SENSITIVE for short forms, and that is not a
    # detail. STOP_FORMS holds lowercase words, and a short form already relies
    # on case to disambiguate -- so lowercasing before the lookup would stop
    # `US` (the country) because `us` (the pronoun) is on the list, and the same
    # for EU, UN and every other acronym that is also a common word. The short
    # form `us` is still stopped; the entity `US` is not.
    if (form if len(form) < _CASE_SENSITIVE_BELOW else form.lower()) in STOP_FORMS:
        return False
    flags = 0 if len(form) < _CASE_SENSITIVE_BELOW else re.IGNORECASE
    return re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text, flags) is not None


@dataclass(frozen=True)
class SurfaceForm:
    form: str
    reason: str  # tracked_entity | tracked_claim | tracked_story
    entity_id: int | None = None


class SurfaceIndex:
    """Every trackable surface form, held in memory for one run.

    A full scan of `entities` per run, deliberately: the word-boundary and
    case rules above do not express well in SQL, and one scan per run is
    cheaper than a per-mention query. This is why there is NO GIN index on
    entities.aliases -- an index with no reader is dead weight.
    """

    def __init__(self, forms: list[SurfaceForm]) -> None:
        self._forms = list(forms)

    @classmethod
    def build(cls, conn) -> "SurfaceIndex":
        forms: list[SurfaceForm] = []
        for eid, name, aliases in conn.execute(
            "SELECT id, name, aliases FROM entities"
        ).fetchall():
            for f in [name, *(aliases or [])]:
                forms.append(SurfaceForm(f, "tracked_entity", eid))
        for (topic,) in conn.execute(
            "SELECT DISTINCT topic FROM claims "
            "WHERE topic IS NOT NULL AND status IN ('standing', 'challenged') "
            "AND retired_on IS NULL"
        ).fetchall():
            forms.append(SurfaceForm(topic, "tracked_claim", None))
        for (name,) in conn.execute(
            "SELECT name FROM stories WHERE state <> 'closed'"
        ).fetchall():
            forms.append(SurfaceForm(name, "tracked_story", None))
        return cls(forms)

    def add_entity(self, entity_id: int, name: str, aliases: list[str]) -> None:
        """Called after each batch's writes. Built once per run, an entity
        created in batch 1 is invisible to batch 3, so events attached to it
        cannot be offered as candidates -- which depresses events_matched, the
        numerator of the corroboration gate. The gate would then fail for a
        caching reason while the matcher worked."""
        for f in [name, *(aliases or [])]:
            self._forms.append(SurfaceForm(f, "tracked_entity", entity_id))

    def match(self, text: str) -> list[SurfaceForm]:
        """Every distinct hit, deduplicated by entity (or by form where there
        is no entity). Order is stable: entity hits first, in insertion order."""
        seen: set = set()
        out: list[SurfaceForm] = []
        for sf in self._forms:
            key = ("e", sf.entity_id) if sf.entity_id is not None else ("f", sf.form)
            if key in seen:
                continue
            if form_matches(sf.form, text):
                seen.add(key)
                out.append(sf)
        return out


# The rules half decides MATERIAL only for what the operator chose to track.
# An entity mention used to be enough, and the entity set is the KB itself: it
# grows with every pass, so the rule's precision FELL as the pipeline worked,
# until 86% of items were material (spec 2026-09-25 D5). Entity-only items now
# go to the model, which judges the subject.
_RULE_REASONS = frozenset({"tracked_claim", "tracked_story"})


def triage_by_rules(item: dict, index: SurfaceIndex) -> SurfaceForm | None:
    """The tracked half. A database lookup, no model call.

    Reads title AND body. The body is the RSS blurb -- capture.py stores
    entry.get("summary"), not article text -- so it is short and there is no
    window to choose.
    """
    text = f"{clean(item.get('title'))} {clean(item.get('body'))}"
    for hit in index.match(text):
        if hit.reason in _RULE_REASONS:
            return hit
    return None


# Sized from the batch: 25 items x ~25 output tokens plus schema overhead. A
# tight max_tokens is what truncated the signals call twice; leave headroom and
# check stop_reason regardless.
TRIAGE_MAX_TOKENS = 2048

_TRIAGE_TOOL = {
    "name": "emit_triage",
    "description": "Return one materiality verdict per input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "material": {"type": "boolean"},
                    },
                    "required": ["id", "material"],
                },
            }
        },
        "required": ["items"],
    },
}

_TRIAGE_SYSTEM = """You are triaging news items for a geopolitics and macro \
knowledge base. For each item, answer one question: does it belong to that \
domain -- international relations, conflict, statecraft, energy, trade, \
central banks, sovereign risk, or the markets those move?

Judge the SUBJECT, not the importance. A minor development in the domain is \
material; a major story outside it is not. Sport, entertainment, crime and \
consumer news are not material unless they carry a stated geopolitical or \
macro consequence.

Return a verdict for every id you were given, and no others."""


def _triage_model() -> str:
    """An unset NEWSBRIEF_TRIAGE_MODEL means "follow MODEL"."""
    return common.TRIAGE_MODEL or common.MODEL


def build_triage_request(items: list[dict], *, model: str | None = None) -> dict:
    """`model` lets run() pass the value it froze at pass start (I1); a direct
    caller (tests, scripts) that omits it gets the live resolver, unchanged."""
    lines = []
    for it in items:
        # clean() so the model never sees `&nbsp;` noise either. Measured
        # 2026-09-05: 41% of captured volume is Google News proxy items whose
        # body is the headline restated with entity separators.
        body = clean(it.get("body"))
        lines.append(
            f"- id={it['id']} outlet={it.get('outlet', '?')} "
            f"title={clean(it['title'])!r} lead={body[:300]!r}"
        )
    return {
        "model": model if model is not None else _triage_model(),
        "max_tokens": TRIAGE_MAX_TOKENS,
        # Forced-tool extraction on a tight budget: thinking OFF. Omitting the
        # field runs ADAPTIVE thinking on Sonnet 5, which spends max_tokens.
        "thinking": {"type": "disabled"},
        "system": _TRIAGE_SYSTEM,
        "tools": [_TRIAGE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_triage"},
        "messages": [{"role": "user", "content": "ITEMS:\n" + "\n".join(lines)}],
    }


def _is_id(x) -> bool:
    """A real integer id, excluding the values Python quietly counts as one.

    `bool` subclasses `int`, so `isinstance(True, int)` is True and `True == 1`;
    and `1.0 == 1`, so a float passes a set-membership test against integer ids.
    Either would bind a hallucinated value onto a real row -- and because the
    resulting id IS real, nothing downstream errors.
    """
    return isinstance(x, int) and not isinstance(x, bool)


def parse_triage_response(resp: dict, offered_ids: set[int]) -> dict[int, bool]:
    """id -> material. Drops ids that were never offered.

    stop_reason is checked FIRST: a truncated reply produces a parse error that
    reads as a broken parser, and this repo has misdiagnosed that four times.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("triage response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_triage":
            rows = block.get("input", {}).get("items")
            if not isinstance(rows, list):
                raise ValueError("emit_triage input missing 'items' list")
            out: dict[int, bool] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                rid, mat = r.get("id"), r.get("material")
                if _is_id(rid) and isinstance(mat, bool) and rid in offered_ids:
                    out[rid] = mat
            return out
    raise ValueError("no emit_triage tool_use block in response")


def record_triage(conn, item_id, verdict, reason, triage_model, version) -> None:
    """Insert a verdict, or bump `attempts` on a retry at the same version.

    ON CONFLICT rather than a plain INSERT because a failed row is retried, and
    the unique key would otherwise make the first failure permanent until
    someone bumped the prompt version.
    """
    conn.execute(
        "INSERT INTO item_triage "
        "  (item_id, verdict, reason, triage_model, triage_prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_id, triage_prompt_version) DO UPDATE SET "
        "  verdict = EXCLUDED.verdict, reason = EXCLUDED.reason, "
        "  triage_model = EXCLUDED.triage_model, "
        "  attempts = item_triage.attempts + 1",
        (item_id, verdict, reason, triage_model, version),
    )


def select_sampled(conn, version: int, per_day: int) -> list[int]:
    """Items BOTH halves rejected, promoted to material as the §5.3 control.

    Why this exists, and it is not coverage: a model-judged `topical` set is
    confounded with the triage model's own view of the domain. Measure enum
    variance over topical rows alone and there is no way to separate "the
    extractor works" from "triage picked items its sibling finds easy". A
    recency sample is not confounded, so these rows are the control the other
    arms are read against.

    Cheap because it is capped: removing the model triage half entirely and
    integrating by recency was considered and rejected -- it moves integration
    from ~120/day to ~1,200/day in the expensive tier.

    PER DAY, NOT PER RUN. The schedule is hourly, so a bare LIMIT would draw
    the cap on every fire: 20 becomes 480/day in the EXPENSIVE tier, turning a
    ~17% control-arm overhead into ~400% -- the same order of cost error as the
    proposal this arm was chosen over.

    The cap counts PROMOTIONS today via `sampled_at`, not `created_at`:
    `created_at` records when the row was TRIAGED, and `record_triage`'s
    ON CONFLICT never rewrites it on promotion. A row triaged yesterday and
    promoted today would be invisible to today's budget under `created_at`,
    letting the cap silently unbind across every UTC day boundary.

    `reason = 'none'` restricts the pool to items the MODEL judged immaterial;
    a structural reject (quote_page) would replace the control with junk.
    """
    used = conn.execute(
        "SELECT count(*) FROM item_triage "
        "WHERE reason = 'sampled' AND sampled_at >= date_trunc('day', now())"
    ).fetchone()[0]
    remaining = max(0, per_day - used)
    if remaining == 0:
        return []
    rows = conn.execute(
        "SELECT item_id FROM item_triage "
        "WHERE triage_prompt_version = %s AND verdict = 'immaterial' "
        "  AND reason = 'none' "
        "ORDER BY item_id DESC LIMIT %s",
        (version, remaining),
    ).fetchall()
    return [r[0] for r in rows]


# Prompt-budget choices, not measurements. Ordered most-recent-first so
# truncation drops the least likely candidates, and a cap reached is counted.
CANDIDATE_ENTITY_CAP = 40
CANDIDATE_EVENT_CAP = 30
CANDIDATE_WINDOW_DAYS = 14

INTEGRATE_MAX_TOKENS = 8192


def candidate_events(
    conn, entity_ids: list[int], titles: list[str], tally: Tally, *, as_of=None
) -> list[dict]:
    """Events sharing entities with this batch, BEST LEXICAL MATCH FIRST.

    Ranked by trigram similarity between the batch's item titles and the
    event's summary, recency breaking ties. Matching one of these is the only
    way corroboration is ever recorded, so this ordering decides whether the
    event layer can justify itself at all (spec 8.2).

    THE ORDERING HAS CHANGED TWICE, AND THE NUMBERS ARE WHY.

    Recency was the ranking until 2026-09-08 and was the worst arm available:
    for a hub entity -- Iran carried 578 events in one 7-day window -- 30
    recency slots cover a few hours, so a duplicate reported the same evening
    was never shown to the model. Measured 13% batched recall@30.

    Entity overlap replaced it and roughly doubled that, to 28%. But the
    comparison that chose it was confounded: the evaluation set was the top
    800 pairs by title similarity, which is the signal the arm it lost to
    ranks on, so the lexical arm was measured only where it was strongest.

    That confound was removed on 2026-09-09 (news-brief-bqa.22): the bake-off
    now samples WITHIN each similarity band, holding the selection constant
    across arms. Lexical ranking did not merely survive it -- it won by more,
    and it won in EVERY band, including the lowest one where lexical DETECTION
    is worthless and where the review had specifically predicted it would
    underperform. In production's batched shape, on 418 pairs:

        pg_trgm          184/418 = 44.0%      <- this ordering
        entity overlap   115/418 = 27.5%      +16.5pp, z=4.98, p=6.4e-07
        recency           56/418 = 13.4%

    Token Jaccard scored 48.8%, and that margin over pg_trgm is +4.8pp at
    z=1.39, p=0.165 -- NOT distinguishable from zero. It is not taken, because
    it must rank a pool fetched into Python, and the only thing available to
    bound that pool is recency: it would reintroduce the exact burial above at
    a larger n, in exchange for a gain the measurement cannot confirm exists.

    RANKING IN SQL IS THEREFORE STILL DELIBERATE, and now cheap: Postgres
    orders the full in-window set through pg_trgm (migration 0012, a trusted
    contrib extension present in the running image), so there is no pool, no
    truncation, and no function crossing the production/diagnostic boundary.

    `titles` is REQUIRED and ranks against the WHOLE batch, by best match to
    any of its items. One candidate list serves every item in the batch, so
    ranking on the first title alone would bury every other item's story --
    recency's failure wearing a different hat. Empty titles offer NOTHING
    rather than falling back to an unranked list, which would silently restore
    recency ordering with no downstream signal that it had happened.

    `as_of` defaults to now() and exists so a diagnostic can ask "what would
    have been offered THEN?" through this function rather than a copy of it.
    scripts/probe_corroboration.py reimplemented this ORDER BY, its comment
    said the two must mirror each other, and it silently stopped mirroring
    anything the first time the ranking changed -- reporting a retired ranking
    as though it were live (news-brief-bqa.26). Mirror the predicate, never
    re-derive it. In production as_of is now(), so `created_at < as_of`
    excludes only events created inside the current transaction, which are
    this batch's own and must not be offered to it.

    Returns id and summary and NOTHING ELSE. events.type and
    commitment_state are scored by the pre-registered gate; sending them here
    would make every attached assertion inherit the framing by echo, and the
    gate would measure the prompt rather than the model.
    """
    titles = [t for t in (titles or []) if t]
    if not entity_ids or not titles:
        return []
    rows = conn.execute(
        # CROSS JOIN unnest multiplies each event by the batch's titles and the
        # GROUP BY collapses it back, so `best` is the strongest match to ANY
        # item. GROUP BY also supplies the de-duplication SELECT DISTINCT used
        # to provide: an event reachable through two of the batch's entities
        # appears once.
        "SELECT e.id, e.summary, max(similarity(t.title, e.summary)) AS best "
        "FROM events e "
        "JOIN event_entities ee ON ee.event_id = e.id "
        "CROSS JOIN unnest(%s::text[]) AS t(title) "
        "WHERE ee.entity_id = ANY(%s) "
        "  AND e.occurred_at >= coalesce(%s::timestamptz, now()) "
        "                       - make_interval(days => %s) "
        "  AND e.created_at < coalesce(%s::timestamptz, now()) "
        "GROUP BY e.id, e.summary, e.occurred_at "
        # `e.id DESC` last so the order is TOTAL. Two events with an equal
        # score and an identical occurred_at would otherwise return in whatever
        # order the plan happened to produce, making the offered list
        # irreproducible for a fixed corpus -- which defeats both debugging and
        # any later gold set built against it.
        "ORDER BY best DESC, e.occurred_at DESC, e.id DESC "
        "LIMIT %s",
        (
            titles,
            list(entity_ids),
            as_of,
            CANDIDATE_WINDOW_DAYS,
            as_of,
            CANDIDATE_EVENT_CAP + 1,
        ),
    ).fetchall()
    if len(rows) > CANDIDATE_EVENT_CAP:
        tally.candidate_cap_hit += 1
        rows = rows[:CANDIDATE_EVENT_CAP]
    return [{"id": r[0], "summary": r[1]} for r in rows]


_INTEGRATE_SYSTEM = """You extract structured knowledge from news items for a \
geopolitics and macro knowledge base.

For each item, return:
  entities   -- the actors involved. Prefer an id from CANDIDATE ENTITIES when \
one refers to the same real-world actor; otherwise propose a new entity.
  events     -- what happened. Prefer an id from CANDIDATE EVENTS when the item \
reports the SAME event another outlet already reported; otherwise propose a new \
one. Matching an existing event is how corroboration is recorded, so match \
whenever the underlying occurrence is the same, even if the wording differs.
  assertion  -- how this outlet stands behind each event.

ONE ENTITY PER REAL-WORLD ACTOR. A company and its equity line are the SAME \
entity: use type 'company' and put the ticker in aliases. Never create a \
separate entity of type 'instrument' for a company's shares.

Distinguish what was DONE from what was SAID. "Trump declared the ceasefire \
over" is a statement; whether the ceasefire is over is a separate matter.

CANDIDATE LABELS. Candidates are listed with labels like ENT1 and EVT1. Set \
`candidate` to one of those labels ONLY to refer to that exact listed item. \
OMIT `candidate` entirely for anything new -- never invent or number a label \
yourself. Entities are deduplicated by name automatically, so you do not need \
an id to link two mentions of the same actor across items."""

_INTEGRATE_TOOL = {
    "name": "emit_extraction",
    "description": "Structured extraction for each input item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer"},
                        "entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate": {
                                        "type": "string",
                                        "description": (
                                            "A label from CANDIDATE ENTITIES, "
                                            "e.g. 'ENT1'. Omit entirely for a "
                                            "new entity; never invent a label."
                                        ),
                                    },
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": [
                                            "country",
                                            "institution",
                                            "company",
                                            "person",
                                            "instrument",
                                        ],
                                    },
                                    "aliases": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                # Two shapes, one object. A matched entity needs
                                # only its label; a NEW one needs what
                                # _validate_item actually demands. This object
                                # published NO required list at all, so a model
                                # omitting `name` or `type` was obeying the
                                # schema exactly and lost the whole item for it
                                # (news-brief-bqa.16). `anyOf`, not `oneOf`: it
                                # is the conditional the structured-output
                                # schema subset supports, so the contract still
                                # holds if this tool ever goes `strict`.
                                "anyOf": [
                                    {"required": ["candidate"]},
                                    {"required": ["name", "type"]},
                                ],
                            },
                        },
                        "events": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "candidate": {
                                        "type": "string",
                                        "description": (
                                            "A label from CANDIDATE EVENTS, "
                                            "e.g. 'EVT1'. Omit entirely for a "
                                            "new event; never invent a label."
                                        ),
                                    },
                                    "summary": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": ["action", "statement", "disclosure"],
                                    },
                                    "commitment_state": {
                                        "type": "string",
                                        "enum": [
                                            "in_force",
                                            "committed",
                                            "intended",
                                            "proposed",
                                        ],
                                    },
                                    "standing": {
                                        "type": "string",
                                        "enum": [
                                            "verified",
                                            "official",
                                            "reported",
                                            "attributed",
                                            "alleged",
                                        ],
                                    },
                                },
                                # As above. `standing` is asked of BOTH shapes
                                # -- it is how the item relates to the event,
                                # not a property of the event -- while summary
                                # and type are demanded only of a new one.
                                "anyOf": [
                                    {"required": ["candidate", "standing"]},
                                    {"required": ["summary", "type", "standing"]},
                                ],
                            },
                        },
                    },
                    "required": ["item_id", "entities", "events"],
                },
            }
        },
        "required": ["items"],
    },
}


def _integrate_model() -> str:
    return common.INTEGRATE_MODEL or common.MODEL


_ENTITY_LABEL = "ENT"
_EVENT_LABEL = "EVT"


def label_map(prefix: str, candidates: list[dict]) -> dict[str, int]:
    """Opaque label -> real id. The ONE definition of the label scheme.

    Raw database ids used to be sent to the model as `candidate_id`. `entities.id`
    is BIGSERIAL from 1, and a model numbering its own extractions 1, 2, 3
    produces values indistinguishable from real ids the moment the KB holds any
    rows -- so a parser liberal enough to survive a cold start would bind an
    assertion to an unrelated entity, silently and permanently. A label space
    the model cannot accidentally land in removes that ambiguity structurally
    rather than by instruction (news-brief-bqa.11).
    """
    return {f"{prefix}{i}": c["id"] for i, c in enumerate(candidates, 1)}


def _resolve_label(raw, labels: dict[str, int], tally) -> int | None:
    """The real id an offered label names, or None meaning "not a reference".

    None covers three cases that must NOT be distinguished by the caller: the
    field was omitted (compliance), it held a non-string, or it held a string
    naming nothing offered. All three fall through to the new-entity path.
    Rejecting instead would deadlock the cold start -- with an empty KB nothing
    can map, so no entity is ever created, so there are never any candidates.

    Only a supplied-but-unmatched value is counted: an omitted field is the
    documented way to say "this is new" and is not non-compliance.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and raw in labels:
        return labels[raw]
    if tally is not None:
        tally.unmapped_candidate += 1
    return None


def build_integration_request(
    items: list[dict],
    entities: list[dict],
    events: list[dict],
    *,
    model: str | None = None,
) -> dict:
    """`model` lets run() pass the value it froze at pass start (I1); a direct
    caller (tests, scripts) that omits it gets the live resolver, unchanged."""
    # Labels, never raw ids. Rendered from the SAME label_map the parser accepts
    # against, so what is offered and what is understood cannot drift apart.
    ent_lines = (
        "\n".join(
            f"- {label} {e['name']} ({e['type']})"
            for label, e in zip(label_map(_ENTITY_LABEL, entities), entities)
        )
        or "(none)"
    )
    # label and summary ONLY. See candidate_events.
    ev_lines = (
        "\n".join(
            f"- {label} {e['summary']}"
            for label, e in zip(label_map(_EVENT_LABEL, events), events)
        )
        or "(none)"
    )
    item_lines = "\n".join(
        f"- item_id={it['id']} outlet={it.get('outlet', '?')} "
        f"title={clean(it['title'])!r}\n  body={clean(it.get('body'))!r}"
        for it in items
    )
    # NO PROMPT CACHING HERE, AND IT IS NOT AN OVERSIGHT. The cacheable prefix
    # is `system` + `tools` = 2,796 chars, about 777 tokens (measured
    # 2026-09-10). Claude Sonnet 5's minimum cacheable prefix is 1024 tokens, so
    # a `cache_control` marker would be accepted and then silently do nothing --
    # no error, `cache_creation_input_tokens: 0`, and a saving that exists only
    # on paper.
    #
    # Worth ~$0.50/day if it worked, at ~320 calls a day. Revisit only if the
    # prefix crosses 1024 tokens or the model changes: Claude Opus 5's minimum
    # is 512, where this prefix WOULD cache. The per-model minimums are not
    # monotonic across generations, so check the current one rather than
    # assuming a newer model is more permissive.
    #
    # The request shape is already cache-ready if that day comes: caching is a
    # prefix match rendered tools -> system -> messages, and every volatile
    # value (candidates, item text) is in `messages`, after everything static.
    return {
        "model": model if model is not None else _integrate_model(),
        "max_tokens": INTEGRATE_MAX_TOKENS,
        "thinking": {"type": "disabled"},
        "system": _INTEGRATE_SYSTEM,
        "tools": [_INTEGRATE_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_extraction"},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"CANDIDATE ENTITIES:\n{ent_lines}\n\n"
                    f"CANDIDATE EVENTS:\n{ev_lines}\n\n"
                    f"ITEMS:\n{item_lines}"
                ),
            }
        ],
    }


_ENTITY_TYPES = {"country", "institution", "company", "person", "instrument"}
_EVENT_TYPES = {"action", "statement", "disclosure"}
_COMMITMENT = {"in_force", "committed", "intended", "proposed"}
_STANDING = {"verified", "official", "reported", "attributed", "alleged"}


def parse_integration_response(
    resp: dict,
    offered_item_ids: set[int],
    entity_labels: dict[str, int],
    event_labels: dict[str, int],
    tally=None,
) -> list[dict]:
    """Validated extractions, one per item. Drops an item WHOLE on any defect.

    Dropping the whole item rather than the bad part is deliberate: a
    half-written extraction is a claim about the world that no source made, and
    the item can be retried.

    An unrecognised `candidate` is NOT such a defect. It is treated as "this is
    new" rather than as a reference, because a label the model invented cannot
    collide with a real id -- see `label_map`. It is counted, not silently
    absorbed.
    """
    if resp.get("stop_reason") == "max_tokens":
        raise ValueError("integration response truncated at max_tokens; not parsed")
    for block in resp.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_extraction":
            rows = block.get("input", {}).get("items")
            # The model sometimes SERIALISES the array instead of emitting it:
            # `input keys=['items'] items type=str`, measured 2026-09-08 at 35
            # items per pass -- 12% of the corpus, whole batches at a time, and
            # re-paid every hour. The content is well formed; only the encoding
            # is wrong, so discarding the batch spends an 8192-token generation
            # to throw away a correct answer.
            #
            # Recovery is deliberately narrow. `json.loads('"x"')` SUCCEEDS and
            # yields a string, so the shape is re-checked below rather than
            # inferred from the parse -- otherwise the comprehension would
            # iterate over characters. A string that is not JSON at all stays a
            # batch failure and keeps its attempt.
            #
            # parse_triage_response has the same shape and is deliberately NOT
            # changed: failed_triage has been 0 across every measured pass, so
            # there is nothing to fix there and a speculative copy would be an
            # untested branch.
            if isinstance(rows, str):
                try:
                    rows = json.loads(rows)
                except ValueError:
                    pass
                else:
                    # Counted, never silently absorbed: a recovery leaving no
                    # trace hides continuing non-compliance behind a suddenly
                    # healthy failure count.
                    if tally is not None:
                        tally.items_json_string += 1
            # The model also WRAPS THE ARRAY TWICE: measured 2026-09-10 as
            # `items shape=dict keys=['items'] first_value=list`, i.e.
            # input={"items": {"items": [...]}}. Same family as the JSON-string
            # case above -- the content is well formed and only the packaging is
            # wrong, so refusing the batch spends an 8192-token generation to
            # discard a correct answer.
            #
            # Narrow for the same reason that one is. Unwrapping ANY single-key
            # dict would turn a genuinely malformed response into a silent empty
            # extraction, which advances the item and loses it for good; the key
            # must be `items` and its value must already be a list.
            #
            # parse_triage_response is again deliberately left alone:
            # failed_triage has been 0 across every measured pass, including
            # 2026-09-10, so there is nothing to fix and a speculative copy
            # would be an untested branch.
            if (
                isinstance(rows, dict)
                and list(rows) == ["items"]
                and isinstance(rows["items"], list)
            ):
                rows = rows["items"]
                if tally is not None:
                    tally.items_double_wrapped += 1
            if not isinstance(rows, list):
                # Name what arrived. This fired 35 times in the 2026-09-08
                # 11:00 pass -- 12% of items, whole batches at a time -- and
                # the bare message could not say whether `items` was absent,
                # was a dict, or arrived under another key. An error that
                # cannot distinguish those is the same unactionable shape as
                # an HTTPError that stringifies to a status code.
                #
                # The type alone was not enough either. `items type=dict`
                # recurred nine times on 2026-09-10 and no log line anywhere
                # said whether the dict was an item keyed by index, a single
                # item emitted bare, or the array nested one level deeper --
                # three different recoveries, and the host logs could not
                # discriminate them (news-brief-19i). So name the SHAPE.
                #
                # Keys, not content: a bounded key list answers the shape
                # question exactly, while a truncated dump of news text would
                # cut mid-structure and answer it only by luck.
                raise ValueError(
                    "emit_extraction input missing 'items' list; "
                    f"input keys={sorted(block.get('input', {}))} "
                    f"items type={type(rows).__name__} "
                    f"items shape={_shape_of(rows)}"
                )
            return [
                p
                for r in rows
                if isinstance(r, dict)
                and (
                    p := _validate_item(
                        r, offered_item_ids, entity_labels, event_labels, tally
                    )
                )
            ]
    raise ValueError("no emit_extraction tool_use block in response")


def _validate_item(
    row, item_ids, entity_labels, event_labels, tally=None
) -> dict | None:
    item_id = row.get("item_id")
    if not _is_id(item_id) or item_id not in item_ids:
        _note(tally, "validate:item_id")
        return None

    entities = []
    for e in row.get("entities") or []:
        if not isinstance(e, dict):
            _note(tally, "validate:entity_shape")
            return None
        cid = _resolve_label(e.get("candidate"), entity_labels, tally)
        if cid is not None:
            entities.append({"candidate_id": cid})
            continue
        name, etype = e.get("name"), e.get("type")
        if not (isinstance(name, str) and name.strip()):
            _note(tally, "validate:entity_name")
            return None
        # MISSING vs UNKNOWN, because they implicate different fixes: missing
        # means the tool schema does not require the field and the model is
        # obeying it (news-brief-bqa.16), unknown means the model invented a
        # value the schema's own enum forbids. `not in e` rather than a None
        # check, so an explicit null reads as present-but-wrong.
        if etype not in _ENTITY_TYPES:
            _note(tally, f"validate:entity_type_{_absence(e, 'type')}")
            _log_rejected_value("entity type", e, "type")
            return None
        aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
        entities.append({"name": name.strip(), "type": etype, "aliases": aliases})

    events = []
    for ev in row.get("events") or []:
        if not isinstance(ev, dict) or ev.get("standing") not in _STANDING:
            _note(tally, "validate:event_shape")
            return None
        cid = _resolve_label(ev.get("candidate"), event_labels, tally)
        if cid is not None:
            events.append({"candidate_id": cid, "standing": ev["standing"]})
            continue
        summary, etype = ev.get("summary"), ev.get("type")
        commitment = ev.get("commitment_state")
        if not (isinstance(summary, str) and summary.strip()):
            _note(tally, "validate:event_summary")
            return None
        # Split, and split again by missing/unknown. The merged
        # `validate:event_enums` key ranked this cause first (73 of 108
        # failures on 2026-09-08) but could not choose a fix between them:
        # four predicates arriving under one label.
        if etype not in _EVENT_TYPES:
            _note(tally, f"validate:event_type_{_absence(ev, 'type')}")
            _log_rejected_value("event type", ev, "type")
            return None
        # ABSENT is a legitimate answer, INVALID is not. `commitment_state` is
        # a property of commitments, and for a factual report there is none --
        # measured 2026-09-08, the model supplied `type` on 100% of events and
        # omitted this on 38%, dropping those items WHOLE for a field the tool
        # schema never required. An explicit null says the same thing as an
        # omission, so both read as absent; rejecting one spelling and not the
        # other would reintroduce the bug for the same answer (bqa.16, 0011).
        if commitment is None:
            if tally is not None:
                tally.commitment_omitted += 1
        elif commitment not in _COMMITMENT:
            _note(tally, "validate:commitment_unknown")
            _log_rejected_value("commitment_state", ev, "commitment_state")
            return None
        events.append(
            {
                "summary": summary.strip(),
                "type": etype,
                "commitment_state": commitment,
                "standing": ev["standing"],
            }
        )

    # An empty list here is NOT a defect: every member that survived the loops
    # above was well-formed, so reaching this point with nothing means the item
    # genuinely had nothing to extract. Returning None would collapse that into
    # the malformed case, and run() would charge it three integration calls
    # before the ceiling retired it (news-brief-uer). write_extraction owns the
    # empty branch, because marking the item done is the only thing that stops
    # it being re-offered.
    return {"item_id": item_id, "entities": entities, "events": events}


def _resolve_entity(
    conn, spec: dict, tally: Tally, *, model: str | None = None
) -> int | None:
    """A candidate id, or an upserted new entity. None means refused.

    `model` lets write_extraction pass through the value run() froze at pass
    start (I1); a direct caller (tests) that omits it gets the live resolver.
    """
    if "candidate_id" in spec:
        tally.entities_resolved += 1
        return spec["candidate_id"]

    name, etype = spec["name"], spec["type"]
    # bqa.9 item 5, enforced where the code can see it. The prompt states the
    # rule too, but jx9.5 showed a guard on a model-supplied field gets walked
    # around by the model choosing the other value.
    if etype == "instrument":
        shadowed = conn.execute(
            "SELECT 1 FROM entity_instruments ei JOIN entities e ON e.id = ei.entity_id "
            "WHERE lower(ei.symbol) = lower(%s) AND e.type = 'company' LIMIT 1",
            (name,),
        ).fetchone()
        if shadowed:
            tally.instrument_entity_refused += 1
            log.warning(
                f"Comprehend: refused instrument entity {name!r}; a company "
                f"already maps that symbol (one entity per company)"
            )
            return None

    row = conn.execute(
        "INSERT INTO entities (name, type, aliases, extractor_model, prompt_version) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (lower(name), type) DO NOTHING RETURNING id",
        (
            name,
            etype,
            spec.get("aliases") or [],
            model if model is not None else _integrate_model(),
            INTEGRATE_PROMPT_VERSION,
        ),
    ).fetchone()
    if row:
        tally.entities_created += 1
        return row[0]
    tally.entities_resolved += 1
    return conn.execute(
        "SELECT id FROM entities WHERE lower(name) = lower(%s) AND type = %s",
        (name, etype),
    ).fetchone()[0]


def write_extraction(
    conn,
    extraction: dict,
    index: SurfaceIndex,
    tally: Tally,
    *,
    model: str | None = None,
) -> bool:
    """Write one item's extraction inside its OWN savepoint.

    capture.store_items already established this pattern and documented why:
    each entry gets its own savepoint so neither a duplicate nor a rejected
    entry can lose the entries around it. Here the payload is far more
    expensive to re-derive, so the argument is stronger, not weaker.

    `model` lets write_batch pass through the value run() froze at pass start
    (I1) for every provenance write below (entities, events, assertions); a
    direct caller (tests) that omits it gets the live resolver, unchanged.

    Returns True if the item was integrated.
    """
    item_id = extraction["item_id"]
    # A new event has no unique key to ON CONFLICT against, so re-running the
    # same extraction (a retry, a re-queued item) would otherwise mint a
    # second event and a second assertion every time. integrated_at alone is
    # NOT the right guard, though: spec 4.2's pending predicate is
    # `integrated_at IS NULL OR integrate_prompt_version < :current`, and
    # 0009's comment names exactly the failure a version-blind guard would
    # reintroduce -- "bumping the integration prompt left integrated_at set
    # so nothing could re-extract". Scope the guard to the CURRENT version:
    # bumping INTEGRATE_PROMPT_VERSION must still re-integrate. A NULL
    # integrate_prompt_version makes `NULL >= n` NULL (never true), so a row
    # that was never integrated always falls through and (re-)writes.
    already = conn.execute(
        "SELECT 1 FROM item_triage WHERE item_id = %s "
        "AND triage_prompt_version = %s AND verdict = 'material' "
        "AND integrated_at IS NOT NULL AND integrate_prompt_version >= %s",
        (item_id, TRIAGE_PROMPT_VERSION, INTEGRATE_PROMPT_VERSION),
    ).fetchone()
    if already:
        return True

    # Nothing to extract. Mark it done and charge it nothing: it is neither a
    # model failure nor a parser failure, and leaving integrated_at NULL is the
    # shape this repo has hit three times -- a row a predicate keeps
    # re-selecting that nothing advances. Under `pending_integration`'s `ORDER
    # BY i.id DESC` plus the 14-day horizon, an unmarked empty extraction stays
    # among the NEWEST un-integrated items, so it would be re-paid every pass
    # until it ages past the horizon -- bounded, but by up to 14 days of hourly
    # calls, not by attempts.
    # Note this is NOT the same as "every entity was refused": that path went
    # through resolution and had work rejected, so it keeps its attempt.
    if not extraction["entities"] and not extraction["events"]:
        conn.execute(
            "UPDATE item_triage SET integrated_at = now(), "
            "  integrate_prompt_version = %s "
            "WHERE item_id = %s AND triage_prompt_version = %s "
            "  AND verdict = 'material'",
            (INTEGRATE_PROMPT_VERSION, item_id, TRIAGE_PROMPT_VERSION),
        )
        tally.empty_extraction += 1
        return True

    # Events but NO entities. Refusing to write is correct: candidate_events
    # retrieves BY entity id, so an entity-less event can never be offered as a
    # candidate or matched -- it would inflate events_created while never
    # touching events_matched, depressing the corroboration ratio the
    # pre-registered gate reads. That is the same by-construction trap the
    # occurred_at comment below describes.
    #
    # What was WRONG (news-brief-bqa.17) was refusing by raising inside the
    # savepoint and charging an attempt. The outcome is DETERMINISTIC for this
    # item at this prompt version, so it failed identically every pass and
    # burned three 8192-token generations to reach a verdict available on the
    # first. Terminal and uncharged instead -- the model answered, it just
    # answered "no entities".
    #
    # NoEntitySurvived below keeps its attempt and keeps its name: entities
    # WERE offered there and every one was refused, which is work rejected
    # rather than work never done. The comment above the empty branch always
    # claimed that distinction; until now nothing enforced it, because
    # `not entity_ids` is true in both cases.
    if not extraction["entities"]:
        conn.execute(
            "UPDATE item_triage SET integrated_at = now(), "
            "  integrate_prompt_version = %s "
            "WHERE item_id = %s AND triage_prompt_version = %s "
            "  AND verdict = 'material'",
            (INTEGRATE_PROMPT_VERSION, item_id, TRIAGE_PROMPT_VERSION),
        )
        tally.entityless_extraction += 1
        return True

    resolved_model = model if model is not None else _integrate_model()
    try:
        with conn.transaction():
            entity_ids = []
            for spec in extraction["entities"]:
                eid = _resolve_entity(conn, spec, tally, model=resolved_model)
                if eid is not None:
                    entity_ids.append(eid)
                    if "name" in spec:
                        index.add_entity(eid, spec["name"], spec.get("aliases") or [])
            if not entity_ids:
                raise NoEntitySurvived("no entity survived resolution")

            for ev in extraction["events"]:
                if "candidate_id" in ev:
                    event_id = ev["candidate_id"]
                    tally.events_matched += 1
                else:
                    # occurred_at is NOT optional here, and omitting it is fatal
                    # rather than untidy. candidate_events filters
                    # `occurred_at >= now() - interval`, which is FALSE for
                    # NULL -- so an event created without one can never be
                    # offered as a candidate, events_matched stays 0, and the
                    # corroboration floor fails BY CONSTRUCTION while the
                    # matcher is working perfectly.
                    #
                    # It is taken from the item's published_at, an observed fact
                    # capture already stores, rather than extracted: a model
                    # guess here would be one more unmeasured field, and article
                    # publication is a good enough proxy for a 14-day window.
                    event_id = conn.execute(
                        "INSERT INTO events (summary, type, commitment_state, "
                        "  occurred_at, extractor_model, prompt_version) "
                        "VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s) RETURNING id",
                        (
                            ev["summary"],
                            ev["type"],
                            # NULL means "not a commitment". 0011 dropped the
                            # NOT NULL; the CHECK still rejects a non-member.
                            ev.get("commitment_state"),
                            extraction.get("published_at"),
                            resolved_model,
                            INTEGRATE_PROMPT_VERSION,
                        ),
                    ).fetchone()[0]
                    tally.events_created += 1

                for eid in entity_ids:
                    conn.execute(
                        "INSERT INTO event_entities (event_id, entity_id) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (event_id, eid),
                    )
                # source_relationship is deliberately NOT written: no rubric
                # exists for it, and bqa.8 owns its fate.
                written = conn.execute(
                    "INSERT INTO assertions (item_id, event_id, standing, "
                    "  extractor_model, prompt_version) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (item_id, event_id) DO NOTHING RETURNING id",
                    (
                        item_id,
                        event_id,
                        ev["standing"],
                        resolved_model,
                        INTEGRATE_PROMPT_VERSION,
                    ),
                ).fetchone()
                if written:
                    tally.assertions_written += 1

            conn.execute(
                "UPDATE item_triage SET integrated_at = now(), "
                "  integrate_prompt_version = %s "
                "WHERE item_id = %s AND triage_prompt_version = %s "
                "  AND verdict = 'material'",
                (INTEGRATE_PROMPT_VERSION, item_id, TRIAGE_PROMPT_VERSION),
            )
    except Exception as exc:
        tally.items_lost_to_savepoint += 1
        tally.failed_integration += 1
        _note(tally, f"savepoint:{type(exc).__name__}")
        log.warning(
            f"Comprehend: item {item_id} rolled back its savepoint", exc_info=True
        )
        conn.execute(
            "UPDATE item_triage SET integrate_attempts = integrate_attempts + 1 "
            "WHERE item_id = %s",
            (item_id,),
        )
        return False
    return True


def write_batch(
    conn, extractions, index: SurfaceIndex, tally: Tally, *, model: str | None = None
) -> int:
    """One transaction for the batch, one savepoint per item inside it.

    The OUTER `conn.transaction()` is load-bearing and must not be removed as
    redundant. db.connect() sets autocommit=False, so psycopg's
    `conn.transaction()` is a real transaction when it is the outermost block
    and a SAVEPOINT only when one is already open. Without this wrapper the
    behaviour of write_extraction depends on whether the caller happens to have
    an open transaction -- which differs between a test that just committed and
    the run loop, which has an open SELECT. A test would then assert semantics
    production never uses.

    `model` lets run() pass through the value it froze at pass start (I1); a
    direct caller (tests) that omits it gets the live resolver, unchanged.
    """
    with conn.transaction():
        return sum(
            1
            for e in extractions
            if write_extraction(conn, e, index, tally, model=model)
        )

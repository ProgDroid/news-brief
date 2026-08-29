"""Score the break-detection gold set against the LIVE reconcile prompt.

This is a manual gate, not a CI test. It spends money: one Haiku call per gold-set
item (23 today), against `brief_memory._RECONCILE_TEMPLATE` as it currently stands.
CI has no ANTHROPIC_API_KEY, so nothing here runs there — `tests/test_gold_set.py`
covers the fixture's schema and this module's arithmetic without the network.

    python scripts/score_gold_set.py            # score every item
    python scripts/score_gold_set.py --limit 3  # cheap smoke run
    python scripts/score_gold_set.py --dry-run  # build prompts, call nothing

WHAT IT MEASURES

Each item is one isolated pair: a single standing claim in CURRENT memory, and the
clause that the seed replay believed contradicted it as TODAY'S BRIEF. The question
is only "does the prompt call this a break?" — with retention, crowding-out and
competing stories all removed. The seed replay measured the end-to-end system; this
measures the judgment. A change that improves the judgment can still be invisible
end-to-end if the claim was evicted before the contradiction arrived.

WHAT THE BASELINE IS

The seed detector marked every row in this fixture "broken" — that is why they are
in it. So its precision is exactly the share of rows labelled `true_break`, and its
recall is 1.0, both computed here rather than asserted. Beating it means converting
`false_break` rows to something other than "broken" WITHOUT losing `true_break`
rows. Read the per-item table: n is 21 classifiable, so one relabel moves the
aggregate about five points.

Enum variance is reported alongside, because a field that comes back uniform looks
populated and carries no information — severity was `high` on 25/25 live claims
while scoring as "complete".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv() -> None:
    """common.py binds ANTHROPIC_API_KEY at import, so .env has to land first.
    setdefault, so a key already in the environment always wins."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

import brief_memory  # noqa: E402

GOLD_SET_PATH = REPO_ROOT / "tests" / "fixtures" / "gold_set_breaks.json"

CLASSIFIABLE = ("true_break", "false_break")
VALID_LABELS = frozenset(CLASSIFIABLE + ("unclear",))

TRANSPORTS = ("auto", "rest", "sdk")

# Fields whose values are checked for variance across everything the model returns.
VARIANCE_FIELDS = ("severity", "status", "origin")


def sdk_call(system: str, user: str) -> str:
    """Same model, same budget, same prompt as brief_memory._messages_call — only the
    transport differs.

    The production path signs requests with x-api-key from the environment. This box
    keeps no key on disk and authenticates through an `ant auth login` profile, which
    only the SDK's zero-arg client resolves; the 2026-08-29 replay ran the same way.
    `anthropic` is a local dev dependency and deliberately absent from requirements,
    so it is imported here rather than at module scope: CI must still be able to
    import this module to test the arithmetic.
    """
    import anthropic

    resp = anthropic.Anthropic().messages.create(
        model=brief_memory.RECONCILE_MODEL,
        max_tokens=brief_memory.RECONCILE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "max_tokens":
        # Mirrors the production guard: a half-written array must surface as
        # truncation, not as a parse bug several layers down.
        raise ValueError("reconcile response truncated (stop_reason=max_tokens)")
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def choose_call(transport: str = "auto"):
    """Pick a transport. `auto` prefers the production REST path and falls back to the
    SDK only when no API key is set, so a normal run exercises the real code path."""
    if transport not in TRANSPORTS:
        raise ValueError(
            f"unknown transport {transport!r}; expected one of {TRANSPORTS}"
        )
    if transport == "rest":
        return brief_memory._messages_call
    if transport == "sdk":
        return sdk_call
    return (
        brief_memory._messages_call if os.environ.get("ANTHROPIC_API_KEY") else sdk_call
    )


def load_gold_set(path: Path = GOLD_SET_PATH) -> dict:
    """Read the fixture. Raises rather than returning an empty set: a scorer that
    silently reports 0/0 on a missing file is the failure mode this gate exists to
    catch elsewhere."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"gold set at {path} has no items")
    return doc


def build_probe_ledger(item: dict) -> dict:
    """A one-claim ledger holding just this item's claim, standing."""
    return {
        "version": 1,
        "claims": [
            {
                "id": "c-0001",
                "claim": item["claim"],
                "topic": item.get("topic") or "general",
                "source_count": 2,
                "severity": item.get("seed_severity") or "normal",
                "status": "standing",
                "origin": "extracted",
                "first_seen": item["first_seen"],
                "last_reaffirmed": item["first_seen"],
            }
        ],
    }


def build_probe_brief(item: dict) -> str:
    """The contradicting clause, presented as the whole of today's brief.

    This is the most compressed honest form of the later event: it is what the seed
    extractor itself wrote as `broken_by`. The real brief prose lives under
    from-server/ and cannot be committed, so the fixture has to be self-contained.
    The trade is that the probe is easier than production — no distractors — which
    is what makes it a clean read on the guard rather than on retention.
    """
    return f"## TODAY\n\n{item['broken_by']}\n"


def probe(item: dict, call=None) -> dict:
    """Run one item through the live template. Returns the model's row for the claim.

    `predicted` is None when the call or parse failed, or when the model dropped the
    claim entirely — three outcomes that are NOT "the model said standing", so they
    are excluded from precision rather than scored as correct rejections.
    """
    caller = call or choose_call()
    prompt = brief_memory.build_reconcile_prompt(
        build_probe_ledger(item), build_probe_brief(item)
    )
    out = {
        "id": item["id"],
        "label": item["label"],
        "predicted": None,
        "row": None,
        "error": None,
    }
    try:
        rows = brief_memory.parse_reconcile_response(
            caller(brief_memory._RECONCILE_SYSTEM, prompt)
        )
    except Exception as e:  # noqa: BLE001 - the failure itself is the datum
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    match = next((r for r in rows if r.get("id") == "c-0001"), None)
    if match is None:
        out["error"] = "claim not returned (dropped, or re-emitted without its id)"
        return out
    out["row"] = match
    out["predicted"] = match.get("status") or brief_memory._DEFAULT_STATUS
    return out


def score(results: list[dict]) -> dict:
    """Precision and recall over the classifiable rows, plus the two numbers that
    actually decide whether a guard helped: false positives converted, true breaks
    lost."""
    scored = [
        r for r in results if r["label"] in CLASSIFIABLE and r["predicted"] is not None
    ]
    tp = sum(
        1 for r in scored if r["label"] == "true_break" and r["predicted"] == "broken"
    )
    fp = sum(
        1 for r in scored if r["label"] == "false_break" and r["predicted"] == "broken"
    )
    fn = sum(
        1 for r in scored if r["label"] == "true_break" and r["predicted"] != "broken"
    )
    tn = sum(
        1 for r in scored if r["label"] == "false_break" and r["predicted"] != "broken"
    )
    gold_true = sum(1 for r in results if r["label"] == "true_break")
    gold_class = sum(1 for r in results if r["label"] in CLASSIFIABLE)
    return {
        "n_items": len(results),
        "n_scored": len(scored),
        "n_unclear": sum(1 for r in results if r["label"] == "unclear"),
        "n_errors": sum(1 for r in results if r["error"]),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": (tp / (tp + fp)) if (tp + fp) else None,
        "recall": (tp / (tp + fn)) if (tp + fn) else None,
        # The seed detector called every row broken, so its precision is the gold
        # positive rate and its recall is 1.0 by construction.
        "baseline_precision": (gold_true / gold_class) if gold_class else None,
        "baseline_recall": 1.0 if gold_true else None,
        "converted": tn,
        "lost": fn,
    }


def field_variance(rows: list[dict], fields=VARIANCE_FIELDS) -> dict:
    """Distinct-value counts per enum field. `degenerate` marks a field that came
    back with a single value across the whole run: populated, complete, useless."""
    out = {}
    for f in fields:
        vals = [r[f] for r in rows if isinstance(r, dict) and r.get(f) is not None]
        counts = Counter(vals)
        out[f] = {
            "n": len(vals),
            "distinct": len(counts),
            "counts": dict(counts),
            "degenerate": len(counts) == 1 and len(vals) > 1,
            "absent": len(vals) == 0,
        }
    return out


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def format_report(doc: dict, results: list[dict], scores: dict, variance: dict) -> str:
    meta = doc.get("meta", {})
    lines = [
        "=" * 78,
        "BREAK-DETECTION GOLD SET",
        "=" * 78,
        f"fixture      : {len(doc['items'])} items, labelled {meta.get('labelled_on', '?')}",
        f"scored under : {brief_memory.RECONCILE_MODEL}  prompt_version={brief_memory.PROMPT_VERSION}",
        "",
        f"{'id':<7}{'gold':<13}{'predicted':<12}note",
        "-" * 78,
    ]
    for r in results:
        note = ""
        if r["error"]:
            note = r["error"][:44]
        elif r["label"] == "true_break" and r["predicted"] != "broken":
            note = "LOST a real break"
        elif r["label"] == "false_break" and r["predicted"] != "broken":
            note = "converted"
        elif r["label"] == "false_break":
            note = "still a false positive"
        lines.append(
            f"{r['id']:<7}{r['label']:<13}{str(r['predicted'] or '-'):<12}{note}"
        )

    lines += [
        "-" * 78,
        "",
        "=" * 78,
        "SCORE",
        "=" * 78,
        f"scored {scores['n_scored']}/{scores['n_items']}   "
        f"unclear (excluded) {scores['n_unclear']}   errors {scores['n_errors']}",
        f"tp {scores['tp']}   fp {scores['fp']}   fn {scores['fn']}   tn {scores['tn']}",
        "",
        f"precision  {_pct(scores['precision']):>8}   baseline {_pct(scores['baseline_precision'])}",
        f"recall     {_pct(scores['recall']):>8}   baseline {_pct(scores['baseline_recall'])}",
        "",
        f"false positives converted : {scores['converted']}",
        f"true breaks lost          : {scores['lost']}   <- any value here is a regression",
    ]
    if scores["n_errors"]:
        lines.append(
            f"NOTE {scores['n_errors']} item(s) errored and were excluded, not scored as "
            "correct rejections."
        )

    lines += [
        "",
        "=" * 78,
        "ENUM VARIANCE (a uniform field looks populated and says nothing)",
        "=" * 78,
        "CAVEAT: the probe hands the model a ledger row already carrying the seed's",
        "severity, so severity variance here largely measures ECHO, not calibration.",
        "Only status is judged from scratch. Read severity from live ledger rows.",
    ]
    for f, v in variance.items():
        flag = (
            "  <- DEGENERATE"
            if v["degenerate"]
            else ("  <- ABSENT" if v["absent"] else "")
        )
        lines.append(
            f"{f:<10} n={v['n']:<4} distinct={v['distinct']}  {v['counts']}{flag}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0, help="score only the first N items")
    ap.add_argument(
        "--dry-run", action="store_true", help="build prompts, make no API calls"
    )
    ap.add_argument(
        "--json", type=Path, default=None, help="also write raw results here"
    )
    ap.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="auto",
        help="auto uses the production REST path when ANTHROPIC_API_KEY is set, "
        "else the SDK against an `ant auth login` profile",
    )
    args = ap.parse_args(argv)

    doc = load_gold_set()
    items = doc["items"][: args.limit] if args.limit else doc["items"]
    caller = choose_call(args.transport)

    if args.dry_run:
        sample = brief_memory.build_reconcile_prompt(
            build_probe_ledger(items[0]), build_probe_brief(items[0])
        )
        print(
            f"{len(items)} items would be scored via {caller.__name__}. "
            f"Prompt for {items[0]['id']}:\n"
        )
        print(sample)
        return 0

    results = []
    for i, item in enumerate(items, 1):
        r = probe(item, call=caller)
        results.append(r)
        print(
            f"  [{i}/{len(items)}] {r['id']} -> {r['predicted'] or r['error']}",
            flush=True,
        )

    scores = score(results)
    variance = field_variance([r["row"] for r in results if r["row"]])
    print()
    print(format_report(doc, results, scores, variance))

    if args.json:
        args.json.write_text(
            json.dumps(
                {"results": results, "scores": scores, "variance": variance}, indent=2
            ),
            encoding="utf-8",
        )
        print(f"\nraw results -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

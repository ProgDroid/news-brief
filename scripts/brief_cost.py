"""Price the brief from the host log (news-brief-al7.3, storylines spec §11).

Re-prices up to 30 days of `Batch result:` and `Realtime brief:` log lines.
It does NOT use the logged `est_batch_cost`, which is hard-coded at older
batch rates; it parses the tokens and prices them with
`comprehend.cost_usd`, so it follows `comprehend.PRICES_PER_MTOK`.

On the host:

    docker compose run --rm --entrypoint python newsbrief \
        scripts/brief_cost.py [--model claude-sonnet-5]
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import comprehend  # noqa: E402  (path shim above must run first)
import common  # noqa: E402

# The figure in brief.py's comment on its web-search cost; not verified
# against current pricing, and labelled so in the output.
WEB_SEARCH_USD = 0.01
SPAN_TARGET_DAYS = 30
ROTATIONS = 5

_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
_MODE = re.compile(r"\[([^\]]+)\]")
_IN = re.compile(r"\bin=(\d+)")
_OUT = re.compile(r"\bout=(\d+)")
_SEARCHES = re.compile(r"\bsearches=(\d+)")
_USAGE = re.compile(r"usage\(([^)]*)\)")


@dataclass(frozen=True)
class CostLine:
    day: str
    mode: str
    path: str  # "batch" or "realtime"
    input_tokens: int
    output_tokens: int
    searches: int


def _parse_one(line: str) -> CostLine | None:
    batch = line.find("Batch result:")
    realtime = line.find("Realtime brief:")
    if batch >= 0:
        path, at = "batch", batch
    elif realtime >= 0:
        path, at = "realtime", realtime
    else:
        return None
    day = _DAY.match(line)
    usage = _USAGE.search(line, at)
    if not day or not usage:
        return None
    body = usage.group(1)
    tokens_in, tokens_out = _IN.search(body), _OUT.search(body)
    if not tokens_in or not tokens_out:
        return None
    searches = _SEARCHES.search(body)
    mode = _MODE.search(line[:at])
    return CostLine(
        day.group(1),
        mode.group(1) if mode else "manual",
        path,
        int(tokens_in.group(1)),
        int(tokens_out.group(1)),
        int(searches.group(1)) if searches else 0,
    )


def parse_cost_lines(lines: Iterable[str]) -> list[CostLine]:
    """Cost lines in log order. A (day, mode) batch result logged twice counts
    once, the later line winning, since a rerun replaces the brief. Realtime
    runs are each a real full-price call, so none is dropped."""
    out: list[CostLine] = []
    batch_at: dict[tuple[str, str], int] = {}
    for line in lines:
        c = _parse_one(line)
        if c is None:
            continue
        if c.path == "batch":
            key = (c.day, c.mode)
            if key in batch_at:
                out[batch_at[key]] = c
                continue
            batch_at[key] = len(out)
        out.append(c)
    return out


def price(c: CostLine, model: str) -> float:
    usage = {"input_tokens": c.input_tokens, "output_tokens": c.output_tokens}
    tokens = comprehend.cost_usd(model, usage, batch=c.path == "batch")
    return tokens + c.searches * WEB_SEARCH_USD


def _money(x: float) -> str:
    return f"${x:.2f}"


def summarise(costs: list[CostLine], model: str) -> str:
    if not costs:
        return "no cost lines found in the log"
    days = sorted({c.day for c in costs})
    first, last = days[0], days[-1]
    span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    out = [
        f"model={model} span={first}..{last}",
        f"web_search=${WEB_SEARCH_USD:.2f}/req (unverified)",
    ]
    if span < SPAN_TARGET_DAYS:
        out.append(f"SHORT: only {span} days in the log")
    by_mode: dict[str, list[float]] = defaultdict(list)
    realtime: list[float] = []
    for c in costs:
        (realtime if c.path == "realtime" else by_mode[c.mode]).append(price(c, model))
    for mode in sorted(by_mode):
        v = by_mode[mode]
        mean = statistics.mean(v)
        out.append(
            f"{mode}: days={len(v)} mean={_money(mean)} "
            f"median={_money(statistics.median(v))} min={_money(min(v))} "
            f"max={_money(max(v))} projected_30d={_money(mean * SPAN_TARGET_DAYS)}"
        )
    out.append(f"realtime_runs={len(realtime)} total={_money(sum(realtime))}")
    return "\n".join(out)


def _log_files(data_dir: Path) -> list[Path]:
    """Oldest first: newsbrief.log.5 .. .1, then the live file."""
    names = [f"newsbrief.log.{n}" for n in range(ROTATIONS, 0, -1)] + ["newsbrief.log"]
    return [p for p in (data_dir / n for n in names) if p.is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=None, help="default: common.MODEL")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    model = args.model or common.MODEL
    files = _log_files(common.DATA_DIR)
    if not files:
        print(f"no log found under {common.DATA_DIR}")
        return 1

    def lines():
        for p in files:
            with open(p, encoding="utf-8", errors="replace") as fh:
                yield from fh

    print(summarise(parse_cost_lines(lines()), model))
    return 0


if __name__ == "__main__":
    sys.exit(main())

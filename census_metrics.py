"""Pure metrics, sizing and sampling for the event census.

No I/O: standard library only (no numpy/scipy -- they are not in the image,
and the t-quantile tables below stand in for scipy.stats.t.ppf). This module
has no importer yet; census.py (session state) and the readout consume these
exact names and signatures.

Spec: docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md sec
4.4-4.6 (the metric, sizing and go/no-go) and 6.4 (the precision sample).
"""

import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime

SEED_BOOT = 20260928

# The pre-registered thresholds (spec sec 4.5 go/no-go, 6.4 precision sample,
# active-minutes idle cap). Defined once, here; census.py re-exports them.
GO_MIN_MEAN_GROUPS = 8
GO_MAX_MEDIAN_MINUTES = 80
PRECISION_PAIRS = 10
IDLE_CAP_MINUTES = 5

# Two-sided 97.5th percentile and one-sided 80th percentile of Student's t,
# keyed by degrees of freedom (1..15 -- df = K-1 for K up to 16 windows).
T975: dict[int, float] = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
}
T80: dict[int, float] = {
    1: 1.376,
    2: 1.061,
    3: 0.978,
    4: 0.941,
    5: 0.920,
    6: 0.906,
    7: 0.896,
    8: 0.889,
    9: 0.883,
    10: 0.879,
    11: 0.876,
    12: 0.873,
    13: 0.870,
    14: 0.868,
    15: 0.866,
}


@dataclass(frozen=True)
class Assignment:
    item_id: int
    outlet_id: int
    group_id: int | None
    unsure: bool


@dataclass(frozen=True)
class GoNoGo:
    ok: bool
    mean_m: float
    median_minutes: float
    values: tuple[int, ...]
    reason: str


def groups(assignments: list[Assignment]) -> list[frozenset[int]]:
    """Partition items into groups, unsure items removed first.

    `group_id is None` means a singleton. A group all of whose items were
    unsure simply never appears, because it is only ever built from what
    survives the filter.
    """
    sure = [a for a in assignments if not a.unsure]
    singles: list[frozenset[int]] = []
    buckets: dict[int, set[int]] = {}
    for a in sure:
        if a.group_id is None:
            singles.append(frozenset({a.item_id}))
        else:
            buckets.setdefault(a.group_id, set()).add(a.item_id)
    return singles + [frozenset(items) for items in buckets.values()]


def multi_outlet_groups(assignments: list[Assignment]) -> list[frozenset[int]]:
    """The groups (per `groups`) with items from at least two outlets."""
    outlet_of = {a.item_id: a.outlet_id for a in assignments if not a.unsure}
    return [g for g in groups(assignments) if len({outlet_of[i] for i in g}) >= 2]


def confirms(
    group: frozenset[int],
    outlet_of: dict[int, int],
    events_of: dict[int, set[int]],
) -> bool:
    """True when some event id is shared by two of the group's items from
    different outlets."""
    items = sorted(group)
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if outlet_of[a] == outlet_of[b]:
                continue
            if events_of.get(a, set()) & events_of.get(b, set()):
                return True
    return False


def pooled_share(
    windows: list[list[frozenset[int]]],
    outlet_of: dict[int, int],
    events_of: dict[int, set[int]],
) -> float:
    """Sigma confirmed / Sigma multi-outlet groups over all windows.

    Each group counts once, so a busy window weighs what its groups weigh.
    `nan` when there are no multi-outlet groups anywhere.
    """
    confirmed = 0
    total = 0
    for window_groups in windows:
        for g in window_groups:
            total += 1
            if confirms(g, outlet_of, events_of):
                confirmed += 1
    return confirmed / total if total else float("nan")


def _directly_linked(a: int, b: int, events_of: dict[int, set[int]]) -> bool:
    """Two items are linked by the system iff they share an event id --
    direct only, never transitive. A system that links x-y and y-z has NOT
    linked x-z (R7 controller ruling, 2026-09-28)."""
    return bool(events_of.get(a, set()) & events_of.get(b, set()))


def pair_recall(
    windows: list[list[frozenset[int]]],
    outlet_of: dict[int, int],
    events_of: dict[int, set[int]],
) -> float:
    """Of the cross-outlet pairs inside each true (multi-outlet) group, the
    share also directly linked (sharing an event) by the system. No
    transitive closure: a pair counts only when that specific pair shares an
    event, never via a third item."""
    hits = 0
    total = 0
    for window_groups in windows:
        for g in window_groups:
            items = sorted(g)
            for i, a in enumerate(items):
                for b in items[i + 1 :]:
                    if outlet_of[a] == outlet_of[b]:
                        continue
                    total += 1
                    if _directly_linked(a, b, events_of):
                        hits += 1
    return hits / total if total else float("nan")


def bcubed_recall(
    windows: list[list[frozenset[int]]],
    outlet_of: dict[int, int],
    events_of: dict[int, set[int]],
) -> float:
    """Standard B-cubed recall of the system's direct links against the true
    (multi-outlet) groups: for each item i in a group G(i), the share of
    G(i) that is i itself or directly linked to i, averaged over items.

    This is outlet-agnostic by design (`outlet_of` is accepted for interface
    symmetry with `pair_recall` but not used) and a secondary clustering
    metric only -- the cross-outlet headline is `confirms`/`pooled_share`.
    Like `pair_recall`, linkage is direct only, never transitive: two
    same-outlet items that each bridge to a third item are not counted as
    linked to each other just because that third item links both.
    """
    total = 0.0
    count = 0
    for window_groups in windows:
        for g in window_groups:
            for i in g:
                linked = sum(
                    1 for j in g if j == i or _directly_linked(i, j, events_of)
                )
                total += linked / len(g)
                count += 1
    return total / count if count else float("nan")


def _resolve(labels: dict[int, int | None]) -> dict[int, object]:
    """A `None` group is a unique cluster per item (F11): substitute a
    per-item sentinel so two `None` items are never treated as the same
    cluster."""
    return {
        item: (group if group is not None else ("__singleton__", item))
        for item, group in labels.items()
    }


def pairwise_agreement(
    ref: dict[int, int | None],
    other: dict[int, int | None],
) -> tuple[float, float, float]:
    """Precision, recall and F1 of `other`'s same-cluster pairs against
    `ref`'s. `nan`, never ZeroDivisionError, when a denominator has no
    same-group pairs. The caller has already removed unsure items."""
    items = sorted(set(ref) & set(other))
    r = _resolve({i: ref[i] for i in items})
    o = _resolve({i: other[i] for i in items})

    tp = fp = fn = 0
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            same_ref = r[a] == r[b]
            same_other = o[a] == o[b]
            if same_ref and same_other:
                tp += 1
            elif same_other:
                fp += 1
            elif same_ref:
                fn += 1

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    if math.isnan(precision) or math.isnan(recall) or (precision + recall) == 0:
        f1 = float("nan")
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def adjusted_rand_index(
    a: dict[int, int | None],
    b: dict[int, int | None],
) -> float:
    """The adjusted Rand index between two labellings of the same items.

    `None` is resolved per `_resolve` (F11), so an all-singleton labelling
    does not collapse `None` items into one cluster. The degenerate case
    where both labellings are a single cluster, or both are fully singleton,
    is special-cased (matching scikit-learn): that partition is unique, so
    the two labellings must already be identical, and the general formula
    would otherwise divide 0 by 0.

    No items is `nan`, deliberately unlike scikit-learn's 1.0: there is
    nothing to agree on, and `item_bootstrap_interval` drops NaN replicates
    rather than counting an empty resample as perfect agreement.
    """
    items = sorted(set(a) & set(b))
    n = len(items)
    if n == 0:
        return float("nan")

    ra = _resolve({i: a[i] for i in items})
    rb = _resolve({i: b[i] for i in items})

    clusters_a: dict[object, set[int]] = {}
    clusters_b: dict[object, set[int]] = {}
    for i in items:
        clusters_a.setdefault(ra[i], set()).add(i)
        clusters_b.setdefault(rb[i], set()).add(i)

    n_classes = len(clusters_a)
    n_clusters = len(clusters_b)
    # Both all-singleton or both one-cluster: that partition is unique, so the
    # labellings are identical and agreement is perfect. Special-cased because
    # the general formula's denominator is 0 there. This is the ONLY way the
    # denominator reaches 0 (proved exhaustively for n <= 5 in
    # test_ari_denominator_is_never_zero_past_the_special_cases).
    if (n_classes == n_clusters == 1) or (n_classes == n_clusters == n):
        return 1.0

    def c2(k: int) -> int:
        return k * (k - 1) // 2

    contingency: dict[tuple[object, object], int] = {}
    for i in items:
        key = (ra[i], rb[i])
        contingency[key] = contingency.get(key, 0) + 1

    index = sum(c2(v) for v in contingency.values())
    sum_a = sum(c2(len(v)) for v in clusters_a.values())
    sum_b = sum(c2(len(v)) for v in clusters_b.values())
    total_pairs = c2(n)
    expected = (sum_a * sum_b) / total_pairs if total_pairs else 0.0
    max_index = 0.5 * (sum_a + sum_b)
    return (index - expected) / (max_index - expected)


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-sorted sequence
    (numpy's default method) -- the standard-library stand-in used by both
    `item_bootstrap_interval` and `deciles`."""
    if not ordered:
        return float("nan")
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


MIN_BOOTSTRAP_REPLICATES = 100


def item_bootstrap_intervals(
    stat,
    a: dict,
    b: dict,
    reps: int = 2000,
    seed: int = SEED_BOOT,
) -> list[tuple[float | None, float | None, int]]:
    """Like `item_bootstrap_interval`, for a `stat(a_sub, b_sub)` returning a
    sequence of components: each resample is scored ONCE and every component
    gets its own `(lo, hi, n_dropped)`, NaN replicates dropped per component."""
    items = sorted(set(a) & set(b))
    n = len(items)
    rng = random.Random(seed)
    values: list[list[float]] | None = None
    dropped: list[int] = []
    for _ in range(reps):
        sample = [items[rng.randrange(n)] for _ in range(n)]
        a_sub = {idx: a[item] for idx, item in enumerate(sample)}
        b_sub = {idx: b[item] for idx, item in enumerate(sample)}
        row = stat(a_sub, b_sub)
        if values is None:
            values = [[] for _ in row]
            dropped = [0] * len(row)
        for k, v in enumerate(row):
            if math.isnan(v):
                dropped[k] += 1
            else:
                values[k].append(v)
    out: list[tuple[float | None, float | None, int]] = []
    for vals, d in zip(values or [], dropped):
        if len(vals) < min(MIN_BOOTSTRAP_REPLICATES, reps):
            out.append((None, None, d))
            continue
        vals.sort()
        out.append((_percentile(vals, 2.5), _percentile(vals, 97.5), d))
    return out


def item_bootstrap_interval(
    stat,
    a: dict,
    b: dict,
    reps: int = 2000,
    seed: int = SEED_BOOT,
) -> tuple[float | None, float | None, int]:
    """Resample items with replacement, recompute `stat(a_sub, b_sub)` each
    time, and return `(lo, hi, n_dropped)`: the 2.5th and 97.5th percentiles
    of the defined replicates, and how many were NaN and dropped.

    A NaN replicate (a resample with no same-cluster pair to score) cannot be
    ordered, so leaving it in `sort()` scrambles the whole list and the
    percentiles come out wrong. Fewer than `MIN_BOOTSTRAP_REPLICATES`
    survivors gives `(None, None, n_dropped)`.
    """
    return item_bootstrap_intervals(
        lambda x, y: (stat(x, y),), a, b, reps=reps, seed=seed
    )[0]


def mde(m_values: list[int], rho: float, d: float = 0.5) -> float:
    """The minimum detectable difference (points) for the pooled estimator,
    clustered by window."""
    k = len(m_values)
    if k < 2:
        raise ValueError("mde needs at least 2 windows")
    m_bar = statistics.mean(m_values)
    cv = statistics.pstdev(m_values) / m_bar
    de = 1 + ((1 + cv**2) * m_bar - 1) * rho
    crit = T975[k - 1] + T80[k - 1]
    return 100 * crit * math.sqrt(d * de / (k * m_bar))


def go_no_go(
    m_values: list[int],
    active_minutes: list[float],
    min_mean_groups: float = GO_MIN_MEAN_GROUPS,
    max_median_minutes: float = GO_MAX_MEDIAN_MINUTES,
) -> GoNoGo:
    """Sec 4.5's pre-registered go/no-go after windows 1 and 2."""
    mean_m = statistics.mean(m_values)
    median_minutes = statistics.median(active_minutes)
    mean_ok = mean_m >= min_mean_groups
    minutes_ok = median_minutes <= max_median_minutes

    if mean_ok and minutes_ok:
        reason = "mean multi-outlet groups per window and median active minutes both clear the floor"
    else:
        reasons = []
        if not mean_ok:
            reasons.append(
                f"mean multi-outlet groups per window {mean_m:.2f} < {min_mean_groups}"
            )
        if not minutes_ok:
            reasons.append(
                f"median active minutes {median_minutes:.2f} > {max_median_minutes}"
            )
        reason = "; ".join(reasons)

    return GoNoGo(
        ok=mean_ok and minutes_ok,
        mean_m=mean_m,
        median_minutes=median_minutes,
        values=tuple(m_values),
        reason=reason,
    )


def split_share(gaps_hours: list[float]) -> float:
    """The expected split share under 6-hour windows, E[min(g/6, 1)]."""
    if not gaps_hours:
        return float("nan")
    return statistics.mean(min(g / 6, 1.0) for g in gaps_hours)


def deciles(values: list[float]) -> tuple[float, ...]:
    """The nine cut points (10th, 20th, ..., 90th percentile) of `values`."""
    if not values:
        return ()
    ordered = sorted(values)
    return tuple(_percentile(ordered, p) for p in range(10, 100, 10))


def gap_band(share: float) -> str:
    """Sec 4.3's pre-registered band: below 40% proceeds outright, 40-50%
    proceeds labelled within-6h-only, above 50% stops."""
    if share > 0.5:
        return "stop"
    if share >= 0.4:
        return "within_6h_only"
    return "proceed"


def draw_precision_sample(
    groups: list[frozenset[int]],
    outlet_of: dict[int, int],
    rng: random.Random,
    n: int = PRECISION_PAIRS,
) -> list[tuple[int, int]]:
    """Sec 6.4's blind precision sample: `min(n, len(groups))` multi-outlet
    groups drawn without replacement, one cross-outlet pair per group."""
    ordered = sorted(groups, key=min)
    chosen = rng.sample(ordered, min(n, len(ordered)))

    pairs: list[tuple[int, int]] = []
    for g in chosen:
        items = sorted(g)
        cross_pairs = [
            (a, b)
            for i, a in enumerate(items)
            for b in items[i + 1 :]
            if outlet_of[a] != outlet_of[b]
        ]
        a, b = rng.choice(cross_pairs)
        pairs.append((min(a, b), max(a, b)))
    return pairs


def precision_estimate(
    per_window: list[tuple[int, int]],
) -> tuple[float, float | None, float | None]:
    """The group-weighted blind precision, pooled over groups (not averaged
    over windows), with a window-clustered interval. `(p, None, None)` below
    two windows."""
    k = len(per_window)
    total_yes = sum(yes for yes, _asked in per_window)
    total_asked = sum(asked for _yes, asked in per_window)
    p = total_yes / total_asked if total_asked else float("nan")

    if k < 2:
        return p, None, None
    if not total_asked:
        # every window asked nothing: no estimate, and the variance below
        # would divide by zero
        return p, None, None

    var = (
        (k / (k - 1))
        * sum((yes - p * asked) ** 2 for yes, asked in per_window)
        / (total_asked**2)
    )
    half = T975[k - 1] * math.sqrt(var)
    return p, max(0.0, p - half), min(1.0, p + half)


def active_minutes(
    stamps: list[datetime], idle_cap_minutes: float = IDLE_CAP_MINUTES
) -> float:
    """Sort the stamps and sum the consecutive gaps of at most
    `idle_cap_minutes`, dropping idle time between bursts of activity."""
    if len(stamps) < 2:
        return 0.0
    ordered = sorted(stamps)
    total = 0.0
    for prev, curr in zip(ordered, ordered[1:]):
        gap = (curr - prev).total_seconds() / 60
        if gap <= idle_cap_minutes:
            total += gap
    return total

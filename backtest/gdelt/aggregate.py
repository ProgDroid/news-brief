# backtest/gdelt/aggregate.py
"""Classify GDELT events (Middle-East conflict) and fold them into daily
accumulators. Pure stdlib. Accumulator fields are SUMS so daily records merge
associatively when several calendar days snap onto one trading day."""

from dataclasses import dataclass

from backtest.gdelt.events import GdeltEvent

# FIPS 10-4 codes for the energy/Hormuz theatre. Tunable. NOTE FIPS != ISO:
# Iraq=IZ, Yemen=YM, Saudi=SA, Iran=IR, Israel=IS, Syria=SY, Lebanon=LE.
MIDEAST_FIPS: frozenset[str] = frozenset({"IR", "IZ", "IS", "SA", "SY", "YM", "LE"})

# CAMEO root codes for material/violent conflict: 18 assault, 19 fight,
# 20 unconventional mass violence.
CONFLICT_ROOT_CODES: frozenset[str] = frozenset({"18", "19", "20"})


def in_region(ev: GdeltEvent, geo: frozenset[str]) -> bool:
    return ev.geo_country in geo


def is_conflict(ev: GdeltEvent) -> bool:
    """Material/verbal conflict: QuadClass 3 or 4, OR a violent CAMEO root."""
    return ev.quad_class in (3, 4) or ev.event_root_code in CONFLICT_ROOT_CODES


@dataclass(frozen=True)
class GdeltDaily:
    date: str  # ISO; calendar day, later snapped to a trading day
    conflict_mentions: float  # sum NumMentions over region CONFLICT events
    n_conflict_events: int
    n_region_events: int
    tone_weighted_sum: float  # sum(AvgTone * NumMentions) over region events
    goldstein_weighted_sum: float  # sum(Goldstein * NumMentions) over region events
    mention_weight: float  # sum(NumMentions) over region events (tone/gold denom)

    def signal(self, field: str) -> float:
        if field == "conflict_mentions":
            return self.conflict_mentions
        w = self.mention_weight
        if field == "mean_tone":
            return self.tone_weighted_sum / w if w else 0.0
        if field == "mean_goldstein":
            return self.goldstein_weighted_sum / w if w else 0.0
        raise ValueError(f"unknown signal field: {field}")


def aggregate_events(
    date_iso: str, events: list[GdeltEvent], geo: frozenset[str]
) -> GdeltDaily:
    cm = mw = tws = gws = 0.0
    nc = nr = 0
    for ev in events:
        if not in_region(ev, geo):
            continue
        nr += 1
        tws += ev.avg_tone * ev.num_mentions
        gws += ev.goldstein * ev.num_mentions
        mw += ev.num_mentions
        if is_conflict(ev):
            cm += ev.num_mentions
            nc += 1
    return GdeltDaily(date_iso, cm, nc, nr, tws, gws, mw)


def fold_daily(events: list[GdeltEvent], geo: frozenset[str]) -> dict[str, GdeltDaily]:
    """Group region events by their event date into daily accumulators."""
    by_date: dict[str, list[GdeltEvent]] = {}
    for ev in events:
        by_date.setdefault(ev.date, []).append(ev)
    return {d: aggregate_events(d, evs, geo) for d, evs in by_date.items()}


def merge_daily(a: GdeltDaily, b: GdeltDaily, *, date_iso: str) -> GdeltDaily:
    return GdeltDaily(
        date=date_iso,
        conflict_mentions=a.conflict_mentions + b.conflict_mentions,
        n_conflict_events=a.n_conflict_events + b.n_conflict_events,
        n_region_events=a.n_region_events + b.n_region_events,
        tone_weighted_sum=a.tone_weighted_sum + b.tone_weighted_sum,
        goldstein_weighted_sum=a.goldstein_weighted_sum + b.goldstein_weighted_sum,
        mention_weight=a.mention_weight + b.mention_weight,
    )

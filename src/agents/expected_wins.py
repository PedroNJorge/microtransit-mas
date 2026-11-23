"""Expected-wins decision for an idle driver choosing where to wait (pure functions; the brain policy "expected").

A driver at hub cell c asks, for every other hub H it could drive to: is waiting at H expected to pay, in euros, more than
waiting here, after paying for the unpaid drive?

    wins(H)   = demand(H) x chance(H)          demand = requests waiting + arrivals expected over the horizon (HubLoad)
    chance(H) = 1 / (supply(H) + 1)            the free drivers there (cruisers heading in included) share the requests equally,
                                               and the mover would be one more of them
    chance(c) = 1 / max(supply(c), 1)          here the driver is already counted in supply(c)
    gain(H)   = value x (wins(H) - wins(c)) - cost_per_km x distance_km(H) - margin       move only if gain(H) > 0

`value` is what a won trip is worth to the driver: its mean net earnings per trip (payment - km cost), a running mean that
starts from a prior (VALUE_PRIOR for PRIOR_TRIPS trips) and moves to the driver's own experience. Idle time is free, as in the
income metric. This is a behavioural model of the win chance (equal shares), not an estimate from data."""
from typing import Dict, List, Optional, Sequence

VALUE_PRIOR = 2.0          # EUR net per won trip before the driver has any experience (about 2.9 EUR revenue - 0.7 EUR km, calibration runs)
PRIOR_TRIPS = 5            # weight of the prior, in trips


def win_chance(supply: float, includes_me: bool) -> float:
    """Chance of winning one request among the free drivers of a cell, all with equal chances."""
    return 1.0 / max(supply, 1.0) if includes_me else 1.0 / (max(supply, 0.0) + 1.0)


def value_per_win(net_sum: float, trips: int, prior: float = VALUE_PRIOR, prior_trips: int = PRIOR_TRIPS) -> float:
    """Running mean of net earnings per trip with a prior of `prior_trips` trips worth `prior` each (never negative)."""
    return max((prior * prior_trips + net_sum) / (prior_trips + max(trips, 0)), 0.0)


def move_gains(hubs: Sequence[str], own: str, demands: Sequence[float], supplies: Sequence[float],
               dists_m: Sequence[float], value: float, per_km: float, margin: float) -> Dict[str, float]:
    """gain(H) in euros for every hub except `own` whose distance is known. Empty inputs (no load heard): no gains."""
    if not demands or own not in hubs:
        return {}
    i = list(hubs).index(own)
    stay = demands[i] * win_chance(supplies[i], True)
    out: Dict[str, float] = {}
    for h, d, s, m in zip(hubs, demands, supplies, dists_m):
        if h == own or m < 0:
            continue
        out[h] = value * (d * win_chance(s, False) - stay) - per_km * m / 1000.0 - margin
    return out

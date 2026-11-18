"""
src/common/pooling.py   Pure pooling maths (Milestone 9). No agents, no I/O, no clock.

The last-mile problem is a degenerate Dial-a-Ride Problem (DARP): every pickup is at the hub, so
only the drop-off sequence is a decision, and a vehicle carries at most `max_size` (<= 4) riders.
At that size EXACT methods are cheap and provably optimal, so this module uses them instead of a
heuristic:

  evaluate_group   exact: enumerate every drop-off order, keep the best order that respects every
                   rider's detour tolerance and the pickup time windows (DARP side constraints).
  best_partition   exact: set-partition DP over bitmasks - which riders share a vehicle, so that
                   the total cost (vehicle time + rider detours) is minimal.  Optimal for n <= MAX_BATCH.
  insert_rider     the incremental operator of the spec: insert ONE new rider into an existing
                   route at the cheapest feasible position (marginal detour per Environmental
                   Model 2.1: detour(r) = route_time_with_r - route_time_without_r).
  greedy_partition insertion-based baseline, used in tests to show exact <= greedy.

Solver interface: a solver is any callable (network, hub, reqs, now, **kw) -> Partition.  `SOLVERS`
maps a name to one; an OR-Tools CP-SAT/routing backend for the full multi-hub DARP can be added with
`register_solver` without touching the protocol.

Two different detour notions, both in SECONDS, never ratios:
  * experienced detour of rider i  = ride time of i on the shared route - direct hub->dest time.
    This is what `TripRequest.detour_tolerance` bounds (it is what the rider feels).
  * marginal detour of a new rider = route time with it - route time without it (2.1); used to
    choose where to insert and reported in `GroupPlan.marginal`.
"""
import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from common.auction import round_cents

MAX_BATCH = 8                       # exact DP is 3^n; 8 riders -> 6561 sub-problems, instant


@dataclass(frozen=True)
class GroupPlan:
    """A feasible shared trip for a group of requests."""
    request_ids: Tuple[str, ...]
    stops: Tuple[str, ...]                       # hub first, then drop-offs in service order
    route_time: float                            # seconds, hub -> last drop-off
    ride: Dict[str, float] = field(compare=False, default_factory=dict)       # request_id -> seconds on board
    direct: Dict[str, float] = field(compare=False, default_factory=dict)     # request_id -> direct seconds
    detour: Dict[str, float] = field(compare=False, default_factory=dict)     # request_id -> experienced detour
    marginal: Dict[str, float] = field(compare=False, default_factory=dict)   # request_id -> 2.1 marginal detour
    departure: float = 0.0                       # earliest common pickup instant inside every window

    @property
    def total_detour(self) -> float:
        return sum(self.detour.values())

    def cost(self, detour_weight: float = 1.0) -> float:
        """What the partition minimises: vehicle seconds + weighted rider detour seconds."""
        return self.route_time + detour_weight * self.total_detour


@dataclass(frozen=True)
class Partition:
    groups: Tuple[GroupPlan, ...]
    unserved: Tuple[str, ...] = ()               # request_ids no feasible group could take
    cost: float = 0.0

    def group_of(self, request_id: str) -> Optional[GroupPlan]:
        return next((g for g in self.groups if request_id in g.request_ids), None)


# ---- windows ------------------------------------------------------------------------------------

def common_departure(reqs: Sequence, now: float) -> Optional[float]:
    """Earliest instant >= now at which every rider can be picked up, or None if the windows
    have no common instant (compatibility filter, part 1)."""
    d = max([now] + [r.time_window_start for r in reqs])
    return d if d <= min(r.time_window_end for r in reqs) else None


# ---- exact group evaluation ---------------------------------------------------------------------

def _simulate(network, hub: str, order: Sequence[str], t: float
              ) -> Optional[Tuple[Dict[str, float], float]]:
    """Arrival offset (seconds after leaving the hub) at each stop of `order`; None if unreachable."""
    clock, here, arrivals = t, hub, {}
    for node in order:
        leg = network.travel_time(here, node, clock)
        if math.isinf(leg):
            return None
        clock += leg
        arrivals.setdefault(node, clock - t)            # first visit = drop-off instant
        here = node
    return arrivals, clock - t


def evaluate_group(network, hub: str, reqs: Sequence, now: float,
                   respect_windows: bool = True) -> Optional[GroupPlan]:
    """Best feasible drop-off order for exactly these requests, or None.

    Feasible = windows share an instant (unless respect_windows=False) AND every rider's
    experienced detour <= its tolerance.  Among feasible orders the winner minimises
    (route_time + sum of detours), ties broken by the stop sequence, so the result is deterministic.
    """
    reqs = list(reqs)
    if not reqs:
        return None
    dep = common_departure(reqs, now) if respect_windows else now
    if dep is None:
        return None
    dests = sorted({r.destination_home for r in reqs})
    direct = {}
    for r in reqs:
        d = network.travel_time(hub, r.destination_home, dep)
        if math.isinf(d):
            return None
        direct[r.request_id] = d
    best, best_key = None, None
    for perm in itertools.permutations(dests):
        sim = _simulate(network, hub, perm, dep)
        if sim is None:
            continue
        arrivals, total = sim
        ride = {r.request_id: arrivals[r.destination_home] for r in reqs}
        detour = {k: max(0.0, ride[k] - direct[k]) for k in ride}
        if any(detour[r.request_id] > r.detour_tolerance + 1e-9 for r in reqs):
            continue
        key = (total + sum(detour.values()), perm)
        if best_key is None or key < best_key:
            best_key, best = key, (perm, total, ride, detour)
    if best is None:
        return None
    perm, total, ride, detour = best
    marginal = {}
    for r in reqs:                                       # route time with r - route time without r
        rest = sorted({x.destination_home for x in reqs if x.request_id != r.request_id})
        if not rest:
            marginal[r.request_id] = total
            continue
        sub = [n for n in perm if n in rest]
        sim = _simulate(network, hub, sub, dep)
        marginal[r.request_id] = total - sim[1] if sim else 0.0
    return GroupPlan(tuple(sorted(r.request_id for r in reqs)), (hub, *perm), total, ride, direct,
                     detour, marginal, dep)


# ---- insertion operator (the spec's "insertion heuristic", made exact for one insertion) ------------

def insert_rider(network, hub: str, route: Sequence[str], new_dest: str, now: float
                 ) -> Optional[Tuple[List[str], float]]:
    """Cheapest insertion of `new_dest` into `route` (drop-offs only, hub excluded).

    Tries every position and returns (new_route, marginal_detour) where marginal_detour is
    route_time_with - route_time_without (Environmental Model 2.1), or None if unreachable.
    A destination already on the route costs nothing (the vehicle passes there anyway)."""
    route = list(route)
    base = _simulate(network, hub, route, now) if route else ({}, 0.0)
    if base is None:
        return None
    if new_dest in route:
        return route, 0.0
    best = None
    for i in range(len(route) + 1):
        cand = route[:i] + [new_dest] + route[i:]
        sim = _simulate(network, hub, cand, now)
        if sim is None:
            continue
        key = (sim[1] - base[1], cand)
        if best is None or key < best:
            best = key
    return (best[1], best[0]) if best else None


# ---- partitioning -------------------------------------------------------------------------------

def best_partition(network, hub: str, reqs: Sequence, now: float, max_size: int = 4,
                   detour_weight: float = 1.0, respect_windows: bool = True) -> Partition:
    """Exact set-partition: split `reqs` into vehicle loads of <= max_size minimising total cost.

    Riders who cannot share (windows, tolerance, capacity) end up alone; a rider who cannot even
    travel alone (unreachable / window already closed) is reported in `unserved`.
    Optimal for len(reqs) <= MAX_BATCH (callers cap the batch)."""
    reqs = sorted(reqs, key=lambda r: r.request_id)
    n = len(reqs)
    if n > MAX_BATCH:
        raise ValueError(f"exact partition limited to {MAX_BATCH} requests (got {n})")
    plans: Dict[int, GroupPlan] = {}
    for mask in range(1, 1 << n):
        members = [reqs[i] for i in range(n) if mask >> i & 1]
        if len(members) > max_size:
            continue
        plan = evaluate_group(network, hub, members, now, respect_windows)
        if plan is not None:
            plans[mask] = plan
    unserved = [reqs[i].request_id for i in range(n) if (1 << i) not in plans]
    serve = [i for i in range(n) if (1 << i) in plans]
    full = sum(1 << i for i in serve)
    INF = (math.inf, ())
    best: Dict[int, Tuple[float, tuple]] = {0: (0.0, ())}

    def solve(mask: int) -> Tuple[float, tuple]:
        if mask in best:
            return best[mask]
        low = mask & -mask
        res = INF
        sub = mask
        while sub:
            if sub & low and sub in plans:
                rest_cost, rest_groups = solve(mask ^ sub)
                if rest_cost < math.inf:
                    cand = (plans[sub].cost(detour_weight) + rest_cost, (sub,) + rest_groups)
                    if cand < res:
                        res = cand
            sub = (sub - 1) & mask
        best[mask] = res
        return res

    cost, masks = solve(full)
    groups = tuple(sorted((plans[m] for m in masks), key=lambda g: g.request_ids))
    return Partition(groups, tuple(sorted(unserved)), cost if groups else 0.0)


def greedy_partition(network, hub: str, reqs: Sequence, now: float, max_size: int = 4,
                     detour_weight: float = 1.0, respect_windows: bool = True) -> Partition:
    """Baseline: take requests in id order and insert each into the first group that stays feasible."""
    open_groups: List[List] = []
    unserved: List[str] = []
    for r in sorted(reqs, key=lambda x: x.request_id):
        for g in open_groups:
            if len(g) < max_size and evaluate_group(network, hub, g + [r], now, respect_windows):
                g.append(r)
                break
        else:
            if evaluate_group(network, hub, [r], now, respect_windows):
                open_groups.append([r])
            else:
                unserved.append(r.request_id)
    plans = tuple(evaluate_group(network, hub, g, now, respect_windows) for g in open_groups)
    return Partition(plans, tuple(sorted(unserved)), sum(p.cost(detour_weight) for p in plans))


SOLVERS: Dict[str, Callable[..., Partition]] = {"exact": best_partition, "greedy": greedy_partition}


def register_solver(name: str, fn: Callable[..., Partition]) -> None:
    """Plug in another backend (e.g. OR-Tools CP-SAT for the multi-hub DARP)."""
    SOLVERS[name] = fn


# ---- fares --------------------------------------------------------------------------------------

def split_fare_weighted(final_fare: float, weights: Dict[str, float]) -> Dict[str, float]:
    """Split final_fare (EUR) in whole cents proportionally to `weights` (largest-remainder method),
    summing EXACTLY to the total.  All-zero weights fall back to an equal split.  Deterministic:
    ties go to the smaller id."""
    ids = sorted(weights)
    if not ids:
        return {}
    total = round_cents(final_fare)
    w = [max(0.0, float(weights[i])) for i in ids]
    s = sum(w)
    if s <= 0:
        w, s = [1.0] * len(ids), float(len(ids))
    exact = [total * x / s for x in w]
    base = [int(math.floor(e + 1e-9)) for e in exact]
    left = total - sum(base)
    order = sorted(range(len(ids)), key=lambda i: (-(exact[i] - base[i]), ids[i]))
    for i in order[:left]:
        base[i] += 1
    return {ids[i]: base[i] / 100 for i in range(len(ids))}


def fare_ceiling(max_fares: Dict[str, float], weights: Dict[str, float]) -> float:
    """Largest total fare whose weighted split gives nobody more than their own max_fare
    (the auction reserve of a pool).  One cent of slack per extra rider covers rounding."""
    ids = sorted(max_fares)
    if len(ids) == 1:
        return max_fares[ids[0]]
    s = sum(max(0.0, weights[i]) for i in ids)
    shares = {i: (max(0.0, weights[i]) / s if s > 0 else 1.0 / len(ids)) for i in ids}
    cents = min((round_cents(max_fares[i]) - 1) / shares[i] for i in ids if shares[i] > 0)
    return max(0, math.floor(cents + 1e-9)) / 100


def reference_solo_fare(direct_seconds: float, base: float, per_second: float, max_fare: float) -> float:
    """Posted solo tariff used to show the pooling discount: base + rate x direct ride time,
    never above the rider's own maximum."""
    return min(max_fare, round_cents(base + per_second * direct_seconds) / 100)

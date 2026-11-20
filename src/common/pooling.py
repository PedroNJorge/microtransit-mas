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
from common.settings import DWELL_DROPOFF

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
    wait: Dict[str, float] = field(compare=False, default_factory=dict)       # request_id -> seconds already waited for the batch
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
                   respect_windows: bool = True, count_wait: bool = True) -> Optional[GroupPlan]:
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
    waited = {r.request_id: (max(0.0, now - r.created_at) if count_wait and len(reqs) > 1 and r.created_at > 0 else 0.0) for r in reqs}
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
        # What sharing costs a rider: the extra ride AND the time spent waiting for the batch (a rider alone is not
        # batched). The wait is already spent when the plan is made, so it limits who may share but is not in the
        # objective below (that would count a sunk cost).
        if any(detour[r.request_id] + waited[r.request_id] > r.detour_tolerance + 1e-9 for r in reqs):
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
                     detour, marginal, waited, dep)


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


# ---- footprint (vehicle distance, CO2) ---------------------------------------------------------------------------

def footprint(network, hub: str, stops: Sequence[str], dests: Sequence[str], now: float) -> Optional[Tuple[float, float]]:
    """(metres driven on the shared route hub -> stops, metres if each destination had its own vehicle from the hub).
    The loaded leg only: both are measured from the hub, so the difference is what sharing saved. None if unreachable."""
    shared = network.route_length([hub, *stops], now) if stops else 0.0
    if shared is None:
        return None
    alone = 0.0
    for d in dests:
        leg = network.route_length([hub, d], now) if d != hub else 0.0
        if leg is None:
            return None
        alone += leg
    return shared, alone


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
    """Posted solo tariff used to show the pooling discount: base + rate x direct ride time (rate = distance and time folded
    into one EUR/s figure, common.costs.rate_per_second),
    never above the rider's own maximum."""
    return min(max_fare, round_cents(base + per_second * direct_seconds) / 100)


# ---- late join: a rider joins a pool whose driver has not reached the hub yet (item 4) ------------------------------
@dataclass
class LateJoinTerms:
    plan: GroupPlan                         # the new shared trip (existing riders + the joiner)
    extra_seconds: float                    # extra driving the joiner causes
    extra_pay: float                        # paid to the driver on top of the old total, EUR
    fares: Dict[str, float]                 # request_id -> fare after the join (existing riders: never above before)
    total: float                            # new total fare = old total + extra_pay


def late_join_terms(network, hub: str, existing: Sequence, new_req, old_fares: Dict[str, float], now: float,
                    cost_per_second: float, markup: float, max_size: int) -> Optional[LateJoinTerms]:
    """Can `new_req` join the pool of `existing` requests, and on what terms? None if not.

    Feasible = the group (existing + joiner) is feasible as a whole: windows overlap at `now` (the driver's expected arrival)
    and every rider's detour stays within ITS OWN tolerance (the existing riders agreed to a detour only up to that, they
    are not asked again). The driver is paid for the extra driving at its variable cost plus `markup`. The new total is
    split like any pool (by direct time); an existing rider pays the smaller of its old fare and its new share, so
    nobody's fare rises, and the joiner pays what is left, which is never below the driver's extra pay. If the joiner's
    fare is above its max_fare the join is not offered."""
    existing = list(existing)
    if not existing or len(existing) + 1 > max_size:
        return None
    # No batch wait here: nobody waits for a batch, the driver is already coming; only the detour counts against tolerance.
    before = evaluate_group(network, hub, existing, now, respect_windows=False, count_wait=False)
    plan = evaluate_group(network, hub, existing + [new_req], now, count_wait=False)
    if before is None or plan is None:
        return None
    extra_seconds = max(0.0, plan.route_time - before.route_time)
    extra_pay = round((extra_seconds + DWELL_DROPOFF) * cost_per_second * (1.0 + markup), 2)   # + the joiner's drop-off stop
    old_total = round(sum(old_fares.values()), 2)
    total = round(old_total + extra_pay, 2)
    shares = split_fare_weighted(total, {rid: max(1.0, d) for rid, d in plan.direct.items()})
    fares = {rid: min(old_fares[rid], shares[rid]) for rid in old_fares}
    fares[new_req.request_id] = round(total - sum(fares.values()), 2)
    if fares[new_req.request_id] > new_req.max_fare + 1e-9 or fares[new_req.request_id] <= 0:
        return None
    return LateJoinTerms(plan, extra_seconds, extra_pay, fares, total)

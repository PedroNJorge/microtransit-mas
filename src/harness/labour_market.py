"""Exit and entry of drivers (optional, `"labour_market": {...}` in a scenario).

Every `check_every` sim seconds the market looks at each driver's rolling net hourly earnings (revenue - km cost, per duty hour,
over the last 2 h):
  * exit  - a driver on duty for at least `min_tenure` whose earnings stayed below exit_ratio x wage for `consecutive` checks
            in a row leaves with probability `p_exit` per check, but only if it is idle with no lock, and never below `min_fleet`;
  * entry - if the average of the eligible drivers is above entry_ratio x wage, one reserve driver (an offline agent started
            with the scenario) comes on duty at the hub with the largest deficit (`common.cells.deficit`: waiting + arrivals over the horizon - supply).
            Optional second signal (`entry_wait`, off by default): when at least `entry_min_waiting` riders have been waiting for
            a pickup longer than `entry_wait` seconds, `max_enter` reserve drivers come on duty at once, whatever the income.
            A shortage shows in waiting times, not in income (fares do not rise), so income alone reacts late.
Both rules read the same quantity with a gap between them (default 0.65 and 1.10 x wage), so the fleet settles where earnings
sit inside the band. These are behavioural rules, not estimated from data. The decision is a pure function (`step`), the loop
(`run`) only reads the agents and flips them."""
import asyncio
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from common import clock
from common.cells import deficit
from common.settings import DRIVER_COST_PER_KM, DRIVER_WAGE_TARGET_PER_HOUR


@dataclass
class LabourRules:
    check_every: float = 1800.0
    exit_ratio: float = 0.65
    entry_ratio: float = 1.10
    p_exit: float = 0.15
    min_tenure: float = 3600.0
    consecutive: int = 2
    min_fleet: int = 3
    reserve: int = 0                       # extra offline drivers that can enter (the market's room to grow)
    entry_wait: Optional[float] = None     # seconds a rider has waited for a pickup that counts as a shortage (None: signal off)
    entry_min_waiting: int = 3             # how many such riders trigger entry
    max_enter: int = 2                     # drivers that enter at once on the waiting signal
    wage: float = DRIVER_WAGE_TARGET_PER_HOUR


@dataclass
class DriverView:
    name: str
    idle: bool
    tenure: float                          # seconds on duty
    net_per_hour: float                    # rolling revenue - km cost, per duty hour


@dataclass
class Decision:
    exits: List[str] = field(default_factory=list)
    enter: int = 0                         # reserve drivers to bring on duty now


def step(views: List[DriverView], reserve_left: int, rules: LabourRules, streaks: Dict[str, int],
         rng: random.Random, long_waiting: int = 0) -> Decision:
    """One check. `streaks` (consecutive checks below the exit line, per driver) is updated in place; `long_waiting` is the
    number of riders who have waited for a pickup longer than rules.entry_wait."""
    d = Decision()
    eligible = [v for v in views if v.tenure >= rules.min_tenure]
    for v in eligible:
        streaks[v.name] = streaks.get(v.name, 0) + 1 if v.net_per_hour < rules.exit_ratio * rules.wage else 0
    active = len(views)
    for v in eligible:
        if active - len(d.exits) <= rules.min_fleet:
            break
        if streaks[v.name] >= rules.consecutive and v.idle and rng.random() < rules.p_exit:
            d.exits.append(v.name)
    if eligible and sum(v.net_per_hour for v in eligible) / len(eligible) > rules.entry_ratio * rules.wage:
        d.enter = 1
    if rules.entry_wait is not None and long_waiting >= rules.entry_min_waiting:
        d.enter = max(d.enter, rules.max_enter)
    d.enter = min(d.enter, max(reserve_left, 0))
    return d


def parse_rules(d: dict) -> LabourRules:
    from harness.scenario import ScenarioError
    allowed = set(LabourRules.__dataclass_fields__) - {"wage"}
    bad = set(d) - allowed
    if bad:
        raise ScenarioError(f"labour_market: unknown keys {sorted(bad)} (allowed: {sorted(allowed)})")
    ints = ("consecutive", "min_fleet", "reserve", "entry_min_waiting", "max_enter")
    r = LabourRules(**{k: (None if v is None else int(v) if k in ints else float(v)) for k, v in d.items()})
    if r.check_every <= 0 or r.min_tenure < 0 or r.consecutive < 1 or r.reserve < 0 or r.min_fleet < 0:
        raise ScenarioError("labour_market: check_every > 0, min_tenure >= 0, consecutive >= 1, reserve >= 0, min_fleet >= 0")
    if (r.entry_wait is not None and r.entry_wait <= 0) or r.entry_min_waiting < 1 or r.max_enter < 1:
        raise ScenarioError("labour_market: entry_wait > 0 (or null), entry_min_waiting >= 1, max_enter >= 1")
    if not (0 <= r.p_exit <= 1) or r.exit_ratio < 0 or r.entry_ratio < r.exit_ratio:
        raise ScenarioError("labour_market: 0 <= p_exit <= 1 and 0 <= exit_ratio <= entry_ratio")
    return r


def entry_hubs(h, now: float, k: int) -> List[str]:
    """`k` hubs for entrants: the hubs ordered by deficit (`common.cells.deficit`, the drivers' own signal), largest first,
    cycling through them if k exceeds their number."""
    ranked = sorted(((deficit(ld.waiting, ld.rate, ld.supply), a.hub_id)
                     for a in h.agents.values() if a.role == "microhub" for ld in [a.load_snapshot(now)]), reverse=True)
    return [ranked[i % len(ranked)][1] for i in range(k)] if ranked else []


def waiting_riders(h, longer_than: float) -> int:
    """Riders who requested a trip and have not boarded yet, for more than `longer_than` seconds."""
    now = clock.now()
    n = 0
    for a in h.agents.values():
        tr = getattr(a, "trip", None) if a.role == "rider" else None
        if tr is not None and not getattr(tr, "boarded_at", 0) and now - tr.requested_at > longer_than:
            n += 1
    return n


async def run(h, rules: LabourRules, seed: int = 0) -> None:
    """Background task of the harness. Events go to `h.market_log` as (sim time, "exit"|"enter", driver, fleet after)."""
    rng = random.Random(seed)
    streaks: Dict[str, int] = {}
    k = 1
    while True:
        while clock.now() < k * rules.check_every:
            await asyncio.sleep(0.25)
        now = k * rules.check_every
        k += 1
        drivers = [a for a in h.agents.values() if a.role == "driver"]
        on = [a for a in drivers if a.online_from is not None and a.online_until is None]
        views = []
        for a in on:
            net, hours = a.rolling_net(now)
            views.append(DriverView(a.jid.user, a.activity == "idle" and a.lock.state.name == "FREE" and a.assignment is None,
                                    now - a.online_from, net / hours if hours > 0 else 0.0))
        reserve = [a for a in drivers if a.online_from is None]
        long_wait = waiting_riders(h, rules.entry_wait) if rules.entry_wait is not None else 0
        dec = step(views, len(reserve), rules, streaks, rng, long_wait)
        by_name = {a.jid.user: a for a in on}
        left = [n for n in dec.exits if by_name[n].go_offline(now)]
        fleet = len(on) - len(left)
        for name in left:
            h.market_log.append((now, "exit", name, fleet))
        if dec.enter and reserve:
            for drv, hub in zip(reserve[:dec.enter], entry_hubs(h, now, dec.enter)):
                if drv.go_online(now, hub):
                    fleet += 1
                    h.market_log.append((now, "enter", drv.jid.user, fleet))

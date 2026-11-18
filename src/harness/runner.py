"""
src/harness/runner.py
Plays the DYNAMIC part of a scenario on top of a started Harness: request streams, road
blockages, traffic, hub failures and demand forecasts (see the schema in harness/scenario.py).

    plan_demand(scenario, network)  -> [PlannedRequest]     pure, seeded: same scenario = same plan
    ScenarioRunner(harness).run()   -> executes plan + events in sim time, returns a summary

Requests go hub -> covered node (a trip starts at its hub). They are issued by the scenario's
RIDER agents, one trip at a time per rider: if every rider is busy when a request is due, the
request waits up to `RIDER_WAIT` sim seconds and is then dropped (counted as `skipped`), so give
the scenario at least as many riders as the biggest burst.

Roads: `block_road` and `traffic` are Disruptions on the shared TransitNetwork (closed, or an
edge-time multiplier), so drivers re-plan around them (Driver.drive) and every later route/ETA
sees them. `kill_hub` stops the hub agent for good (its XMPP session closes).
"""
import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from spade.behaviour import OneShotBehaviour

from common import clock
from common.graph import TransitNetwork
from common.messages import make_inform
from common.ontology import DemandForecast, Disruption, DisruptionType, Protocol
from harness.scenario import DemandSpec, EventSpec, Scenario, ScenarioError

logger = logging.getLogger(__name__)

RIDER_WAIT = 60.0

# Heavy traffic is a Disruption with a severity multiplier. `CONGESTION` is the precise type; an older
# ontology.py without it falls back to WEATHER (same effect on travel times: slower, still open).
TRAFFIC_TYPE = getattr(DisruptionType, "CONGESTION", None) or DisruptionType.WEATHER


class SendForecast(OneShotBehaviour):
    """The Environment tells one hub what to expect (DemandForecastProtocol). Defined here, not in
    environment.py, so the runner works with an unmodified EnvironmentAgent."""

    def __init__(self, jid: str, fc: DemandForecast):
        super().__init__()
        self.jid_to, self.fc = jid, fc

    async def run(self):
        await self.send(make_inform(self.jid_to, Protocol.DEMAND_FORECAST, self.fc,
                                    f"fc-{self.fc.hub_id}-{int(self.fc.horizon_start)}"))


@dataclass(frozen=True)
class PlannedRequest:
    at: float
    spec_id: str
    hub: str
    destination: str
    max_fare: float
    window: float
    detour_tolerance: float
    pooling: bool
    subsidy: bool


def covered_nodes(net: TransitNetwork, hub: str) -> List[str]:
    """Nodes the hub covers (except the hub node itself), sorted."""
    return sorted(n for n in net.g.nodes if n != hub and hub in net.hubs_covering(n, 0.0))


def plan_demand(sc: Scenario, net: TransitNetwork) -> List[PlannedRequest]:
    rng = random.Random(f"{sc.seed}:demand")
    cache: Dict[str, List[str]] = {}
    plan: List[PlannedRequest] = []
    for d in sc.demand:
        for h in d.hubs:
            if h not in cache:
                cache[h] = covered_nodes(net, h)
            if not cache[h]:
                raise ScenarioError(f"demand {d.id!r}: hub {h!r} covers no node, nothing to request")
        times = ([d.start + (i + 0.5) * (d.end - d.start) / d.count for i in range(d.count)]
                 if d.pattern == "even" else sorted(rng.uniform(d.start, d.end) for _ in range(d.count)))
        for i, t in enumerate(times):
            hub = d.hubs[i % len(d.hubs)] if d.pattern == "even" else rng.choice(d.hubs)
            plan.append(PlannedRequest(t, d.id, hub, rng.choice(cache[hub]), d.max_fare, d.window,
                                       d.detour_tolerance, rng.random() < d.pooling, rng.random() < d.subsidy))
    plan.sort(key=lambda r: (r.at, r.spec_id, r.hub, r.destination))
    return plan


def route_edges(net: TransitNetwork, hubs: List[str]) -> List[str]:
    """Edge ids ('a|b') on the shortest paths between every pair of the given hubs."""
    out = set()
    for i, a in enumerate(hubs):
        for b in hubs[i + 1:]:
            for src, dst in ((a, b), (b, a)):
                res = net.shortest_path(src, dst, 0.0)
                if res:
                    out.update(f"{u}|{v}" for u, v in zip(res[0], res[0][1:]))
    return sorted(out)


def event_disruptions(ev: EventSpec, net: TransitNetwork, seed: int) -> List[Disruption]:
    """The Disruption objects a block_road / traffic event adds (not applied here)."""
    p = ev.params
    if ev.type == "block_road":
        w = p["where"]
        if "edges" in w:
            edges = list(w["edges"])
        else:
            pool = route_edges(net, w["among"])
            if not pool:
                raise ScenarioError(f"event {ev.id!r}: no route between hubs {w['among']}")
            edges = random.Random(f"{seed}:{ev.id}").sample(pool, min(w["random_on_routes"], len(pool)))
        if p["both_directions"]:
            edges += [f"{b}|{a}" for a, b in (e.split("|") for e in edges) if net.g.has_edge(b, a)]
        edges = sorted(set(edges))
        return [Disruption(ev.id, DisruptionType.ROAD_CLOSURE, edges, ev.at, ev.at + p["duration"], closed=True)]
    # traffic
    w = p["where"]
    if w == "all":
        edges = [f"{a}|{b}" for a, b in net.g.edges]
    else:
        near = {n for h in w["near"] for n in net.g.nodes if net.travel_time(h, n, 0.0) <= w["radius"]}
        edges = [f"{a}|{b}" for a, b in net.g.edges if a in near and b in near]
    if not edges:
        raise ScenarioError(f"event {ev.id!r}: no road selected")
    prof = p["profile"]
    out = []
    for k, (off, sev) in enumerate(prof):
        end = prof[k + 1][0] if k + 1 < len(prof) else p["duration"]
        out.append(Disruption(f"{ev.id}-{k + 1}", TRAFFIC_TYPE, edges, ev.at + off,
                              ev.at + end, severity=sev))
    return out


@dataclass
class RunSummary:
    issued: int = 0
    skipped: int = 0
    events: List[Tuple[float, str]] = field(default_factory=list)


class ScenarioRunner:
    def __init__(self, harness):
        self.h = harness
        self.sc: Scenario = harness.scenario
        self.net: TransitNetwork = harness.network
        self.summary = RunSummary()
        self.killed: set = set()                        # hub ids stopped by a kill_hub event
        self._tasks: List[asyncio.Task] = []

    # ---- helpers ---------------------------------------------------------------------
    def _note(self, text: str) -> None:
        self.summary.events.append((clock.now(), text))
        logger.info("t=%.0f %s", clock.now(), text)

    def _riders(self):
        return [a for a in self.h.agents.values() if a.role == "rider"]

    async def _at(self, t: float) -> None:
        await clock.sim_sleep(t - clock.now())

    # ---- demand ----------------------------------------------------------------------
    async def _demand(self, plan: List[PlannedRequest]) -> None:
        riders = self._riders()
        for r in plan:
            await self._at(r.at)
            rider, deadline = None, clock.now() + RIDER_WAIT
            while rider is None:
                rider = next((x for x in riders if x.state == "IDLE" and x.trip is None), None)
                if rider is None:
                    if clock.now() >= deadline:
                        break
                    await clock.sim_sleep(2.0)
            if rider is None:
                self.summary.skipped += 1
                continue
            req = rider.request_trip(r.destination, r.max_fare, r.window, r.detour_tolerance, hub_id=r.hub,
                                     pooling_willingness=r.pooling, subsidy_eligible=r.subsidy)
            if req is None:
                self.summary.skipped += 1
            else:
                self.summary.issued += 1

    # ---- events ----------------------------------------------------------------------
    async def _event(self, ev: EventSpec) -> None:
        await self._at(ev.at)
        if ev.type == "kill_hub":
            for hub in ev.params["hubs"]:
                agent = self.h.agents.get(self.sc.jid(hub))
                self.killed.add(hub)
                if agent is not None and agent.is_alive():
                    await agent.stop()
                self._note(f"{ev.id}: hub {hub} killed")
            return
        for d in event_disruptions(ev, self.net, self.sc.seed):
            self.net.add_disruption(d)
            kind = "closed" if d.closed else f"x{d.severity:g}"
            self._note(f"{ev.id}: {d.disruption_id} {kind} on {len(d.affected_edges)} edges "
                       f"until t={d.end_time:.0f}")

    async def _forecast(self, f) -> None:
        await self._at(f.at)
        env = next((a for a in self.h.agents.values() if a.role == "environment"), None)
        if env is None:
            return
        fc = DemandForecast(f.hub, f.at, f.at + f.horizon, f.expected)
        for to in f.to:
            env.add_behaviour(SendForecast(self.sc.jid(to), fc))
        self._note(f"forecast: {f.expected:g} requests expected at {f.hub} until t={f.at + f.horizon:.0f}, told {f.to}")

    # ---- run -------------------------------------------------------------------------
    def _on_done(self, task: asyncio.Task) -> None:
        """A failed event/demand task must be LOUD, not a silent never-retrieved exception."""
        if not task.cancelled() and task.exception() is not None:
            logger.error("scenario task %s failed: %r", task.get_name(), task.exception(),
                         exc_info=task.exception())

    def start(self) -> None:
        """Schedule everything (call once, right after Harness.start)."""
        plan = plan_demand(self.sc, self.net)
        if plan:
            self._tasks.append(asyncio.create_task(self._demand(plan)))
        for ev in self.sc.events:
            self._tasks.append(asyncio.create_task(self._event(ev), name=f'event:{ev.id}'))
        for f in self.sc.forecasts:
            self._tasks.append(asyncio.create_task(self._forecast(f)))
        for t in self._tasks:
            t.add_done_callback(self._on_done)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def finished(self) -> bool:
        """Every demand stream has issued its requests and every event/forecast has played out."""
        return bool(self._tasks) and all(t.done() for t in self._tasks)

    def errors(self) -> List[BaseException]:
        return [t.exception() for t in self._tasks if t.done() and not t.cancelled() and t.exception()]


def report(h) -> str:
    """Plain-text outcome summary of a run (riders' trips, repositions, ledger)."""
    from collections import Counter
    riders = [a for a in h.agents.values() if a.role == "rider"]
    out = Counter(t.outcome for r in riders for t in r.trips)
    busy = sum(1 for r in riders if r.trip is not None)
    lines = [f"riders: {sum(out.values())} trips ended " + ", ".join(f"{k}={v}" for k, v in sorted(out.items()))
             + (f"; {busy} still in progress" if busy else "")]
    run = getattr(h, "runner", None)
    if run is not None and (run.summary.issued or run.summary.skipped):
        lines.append(f"demand: {run.summary.issued} requests issued, {run.summary.skipped} skipped (no idle rider)")
    metrics = [m for a in h.agents.values() if a.role == "microhub" for m in a.pool_metrics]
    if metrics:
        shared_riders = sum(m.riders for m in metrics if m.riders > 1)
        pools = [m for m in metrics if m.riders > 1]
        total = sum(m.riders for m in metrics)
        shared, alone = sum(m.shared_m for m in metrics) / 1000, sum(m.alone_m for m in metrics) / 1000
        g = next((a.emission_g_per_km for a in h.agents.values() if a.role == "microhub"), 0.0)
        lines.append(f"pooling: {shared_riders} of {total} matched riders shared a vehicle ({len(pools)} pools"
                     + (f", {shared_riders / len(pools):.1f} riders each" if pools else "") + ")")
        lines.append(f"vehicle distance, loaded leg: {shared:.1f} km shared vs {alone:.1f} km if everyone rode alone: "
                     f"{alone - shared:.1f} km saved, {(alone - shared) * g / 1000:.2f} kg CO2 ({g:.0f} g/km)")
    joined = sum(len(a.late_joined) for a in h.agents.values() if a.role == "microhub")
    if joined:
        lines.append(f"late joins: {joined} rider(s) joined a pool whose driver was still on the way to the hub")
    pay = next((a for a in h.agents.values() if a.role == "payment"), None)
    if pay is not None and (pay.ledger.pooling_spent or pay.ledger.pooling_shortfall):
        lines.append(f"pooling incentive: {pay.ledger.pooling_spent:.2f} EUR paid to riders of shared trips from the "
                     f"pooling fund" + (f" ({pay.ledger.pooling_shortfall:.2f} EUR unpaid, fund empty)"
                                         if pay.ledger.pooling_shortfall else ""))
    why = Counter(t.reason or "?" for r in riders for t in r.trips if t.outcome not in ("completed",))
    if why:
        lines.append("not completed, by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(why.items())))
    waits = [t.boarded_at - t.requested_at for r in riders for t in r.trips if t.outcome == "completed"]
    if waits:
        lines.append(f"wait request -> boarding (completed trips): mean {sum(waits) / len(waits):.0f}s, "
                     f"max {max(waits):.0f}s")
    hubs = [a for a in h.agents.values() if a.role == "microhub"]
    repo = Counter(c.outcome for a in hubs for c in a.reposition_log)
    if repo:
        lines.append("repositions: " + ", ".join(f"{k}={v}" for k, v in sorted(repo.items())))
    lines.append("hubs alive: " + ", ".join(f"{a.hub_id}={'yes' if a.is_alive() else 'NO'}" for a in hubs))
    return "\n".join(lines)
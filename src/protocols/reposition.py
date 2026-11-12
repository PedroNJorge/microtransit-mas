"""
src/protocols/reposition.py   RepositionProtocol (Hub -> Driver) and the hub's rebalancing heuristic

One FSMBehaviour per request, run by the MicrohubAgent (initiator):

    REQUEST -> AWAIT_REPLY -+-> AWAIT_ARRIVAL -> END
                            +------------------> END      (refuse / failure / silence)

  REQUEST         request(RepositionRequest) -> the driver (thread = a fresh reposition id)
  AWAIT_REPLY     agree | refuse(reason) | silence for T_reposition
  AWAIT_ARRIVAL   inform(RepositionRequest) = "arrived" | failure(reason) | silence until
                  agreed_at + travel_time * ARRIVAL_SLACK + T_reposition
  END             hand the finished context back to the hub (on_reposition_finished)

Messages
  Hub -> Driver        request(RepositionRequest)
  Driver -> Hub        agree(RepositionRequest) | refuse(reason)
  Driver -> Hub        inform(RepositionRequest) on arrival | failure(reason) if it cannot get there
  Driver -> every hub  inform(DriverStatus) on DriverStatusProtocol when it leaves (REPOSITIONING)
                       and again when it has arrived (IDLE at the new node). This is the broadcast:
                       every hub learns where the driver is, not only the one that asked.

The DRIVER side already lives in agents/driver.py (`on_reposition`, `_Reposition`); it is the
participant of this protocol and is not duplicated here. It refuses with: unknown_hub, locked,
busy, unreachable, incentive_too_low, malformed. A driver already moving for another hub answers
`busy`, which is how two hubs that want the same driver are sorted out without a coordinator.

A refusal is final for this request: the hub does not renegotiate, it puts the driver on a cooldown
(RepositionRules.cooldown) and the next rebalancing round may pick someone else.

The heuristic (`plan_repositions`, a pure function; the hub gathers its inputs)
  A staging point is HOT when its predicted demand exceeds `demand_threshold`. It should then hold
  ceil(demand / requests_per_driver) drivers (never more than its capacity). Drivers already there
  or on their way count. For the missing ones the hub picks, among its own idle drivers, the
  closest one (<= max_travel seconds) that is NOT standing on an equally hot or hotter point,
  so a driver is never taken from a point that needs it more (no ping-pong). Hottest point first,
  at most `max_moves` drivers per round. The incentive covers the driver's assumed driving cost.

RepositionPlanner is the trigger: every `t_rebalance` sim seconds it asks the hub for a plan and
starts one FSM per move.

Robustness: an exception in any state is logged, recorded in the context and routed to END,
because SPADE kills a whole FSM on an unhandled error and the hub would never learn the outcome.
Every step is appended to `ctx.trace` and logged as one line.
"""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional

from spade.behaviour import CyclicBehaviour, FSMBehaviour, State
from spade.message import Message

from common import clock
from common.auction import ceil_cents
from common.messages import make_request
from common.ontology import Perf, Protocol, RepositionRequest, StagingPoint
from common.settings import (
    DRIVER_COST_PER_SECOND, REPOSITION_COOLDOWN, REPOSITION_DEMAND_THRESHOLD,
    REPOSITION_INCENTIVE_MARGIN, REPOSITION_MAX_MOVES, REPOSITION_MAX_TRAVEL,
    REPOSITION_MIN_SHIFT_LEFT, REPOSITION_REQUESTS_PER_DRIVER,
)
from protocols.payment_common import bare, expect

logger = logging.getLogger(__name__)

REQUEST, AWAIT_REPLY, AWAIT_ARRIVAL, END = "REQUEST", "AWAIT_REPLY", "AWAIT_ARRIVAL", "END"

# RepositionContext.outcome
ARRIVED = "arrived"          # the driver reported it is at the staging point
REFUSED = "refused"          # refuse(reason): the driver said no
TIMEOUT = "timeout"          # no answer to the request
FAILED = "failed"            # failure(reason): the driver agreed but could not get there
NO_ARRIVAL = "no_arrival"    # the driver agreed and then went silent
ERROR = "error"              # a bug or a malformed message broke the FSM

ARRIVAL_SLACK = 2.0          # the driver may take this many times the planned travel time
                             # (traffic, re-routes around closed roads) before it counts as lost

# The heuristic
@dataclass(frozen=True)
class RepositionRules:
    """Tunable parameters of the heuristic (defaults: common/settings.py)."""
    demand_threshold: float = REPOSITION_DEMAND_THRESHOLD
    requests_per_driver: float = REPOSITION_REQUESTS_PER_DRIVER
    max_travel: float = REPOSITION_MAX_TRAVEL
    incentive_per_second: float = DRIVER_COST_PER_SECOND       # EUR per second of driving
    incentive_margin: float = REPOSITION_INCENTIVE_MARGIN
    cooldown: float = REPOSITION_COOLDOWN
    min_shift_left: float = REPOSITION_MIN_SHIFT_LEFT
    max_moves: int = REPOSITION_MAX_MOVES

    def __post_init__(self):
        if self.requests_per_driver <= 0:
            raise ValueError("requests_per_driver must be > 0")
        if self.max_moves < 0 or self.incentive_per_second < 0 or self.incentive_margin < 0:
            raise ValueError("max_moves, incentive_per_second and incentive_margin must be >= 0")

@dataclass(frozen=True)
class RepositionPlan:
    """One move chosen by the heuristic."""
    driver_jid: str
    staging_point_id: str
    from_node: str
    to_node: str
    travel_time: float          # seconds, as the hub computed it
    incentive: float            # EUR offered to the driver

def plan_repositions(idle: Mapping[str, str],
                     points: Iterable[StagingPoint],
                     demand: Callable[[StagingPoint], float],
                     travel_time: Callable[[str, str], float],
                     supply: Mapping[str, int],
                     rules: RepositionRules = RepositionRules()) -> List[RepositionPlan]:
    """Which idle drivers should go to which hot staging points.

    idle         driver JID -> node it stands on (only drivers the hub may move)
    points       candidate staging points
    demand       predicted demand at a staging point (same unit as rules.demand_threshold)
    travel_time  seconds between two nodes, math.inf if unreachable
    supply       staging point id -> drivers already there or on their way
    """
    points = list(points)
    dem = {sp.id: demand(sp) for sp in points}
    local: Dict[str, float] = {}                     # node -> demand of the hottest point on it
    for sp in points:
        local[sp.location] = max(local.get(sp.location, 0.0), dem[sp.id])
    hot = sorted((sp for sp in points if dem[sp.id] > rules.demand_threshold),
                 key=lambda sp: (-dem[sp.id], sp.id))
    free = dict(idle)
    have = dict(supply)
    plans: List[RepositionPlan] = []
    for sp in hot:
        wanted = min(math.ceil(dem[sp.id] / rules.requests_per_driver), sp.capacity)
        for _ in range(max(0, wanted - have.get(sp.id, 0))):
            if len(plans) >= rules.max_moves:
                return plans
            best = None                                  # (travel_time, jid)
            for jid, node in free.items():
                if node == sp.location or local.get(node, 0.0) >= dem[sp.id]:
                    continue                             # already there / would strip an equal need
                tt = travel_time(node, sp.location)
                if math.isinf(tt) or tt > rules.max_travel:
                    continue
                if best is None or (tt, jid) < best:
                    best = (tt, jid)
            if best is None:
                break
            tt, jid = best
            incentive = ceil_cents(rules.incentive_per_second * tt * rules.incentive_margin) / 100.0
            plans.append(RepositionPlan(jid, sp.id, free.pop(jid), sp.location, tt, incentive))
            have[sp.id] = have.get(sp.id, 0) + 1
    return plans

# The FSM
@dataclass
class RepositionContext:
    """Everything one reposition request knows. Created by the hub, filled in by the states."""
    plan: RepositionPlan
    thread: str
    hub_id: str = ""
    t_reply: float = 30.0                    # how long to wait for agree / refuse
    request: Optional[RepositionRequest] = None
    outcome: str = ""
    reason: str = ""                         # the driver's refuse / failure text
    error: str = ""
    started_at: float = 0.0
    agreed_at: float = 0.0
    finished_at: float = 0.0
    trace: List[dict] = field(default_factory=list)

def note(ctx: RepositionContext, state: str, event: str, **details) -> None:
    ctx.trace.append({"t": clock.now(), "state": state, "event": event, **details})
    extra = " ".join(f"{k}={v}" for k, v in details.items())
    logger.info("[%s] %-13s %s %s", ctx.thread, state, event, extra)

class _RepositionState(State):
    name = ""

    def __init__(self, ctx: RepositionContext):
        super().__init__()
        self.ctx = ctx

    async def run(self):
        try:
            await self.step()
        except Exception as e:                       # see module docstring
            logger.exception("%s: reposition %s failed in %s", self.agent.jid, self.ctx.thread, self.name)
            self.ctx.error = f"{type(e).__name__}: {e}"
            if not self.ctx.outcome:
                self.ctx.outcome = ERROR
            note(self.ctx, self.name, "error", error=self.ctx.error)
            if self.name != END:
                self.set_next_state(END)

    async def step(self):
        raise NotImplementedError

    async def next_from_driver(self, deadline: float) -> Optional[Message]:
        """The driver's next message on this thread, or None once the sim-time deadline has passed.
        Waits in slices of at most one real second so the behaviour stays killable."""
        while True:
            remaining = deadline - clock.now()
            if remaining <= 0:
                return None
            msg = await self.receive(timeout=min(1.0, max(clock.to_real(remaining), 0.001)))
            if msg is None:
                continue
            if bare(msg.sender) != self.ctx.plan.driver_jid:
                note(self.ctx, self.name, "ignored: not the driver", sender=bare(msg.sender))
                continue
            return msg

    def finish(self, outcome: str, reason: str = "") -> None:
        self.ctx.outcome, self.ctx.reason = outcome, reason
        self.set_next_state(END)

class _Request(_RepositionState):
    name = REQUEST

    async def step(self):
        ctx, p = self.ctx, self.ctx.plan
        ctx.started_at = clock.now()
        ctx.request = RepositionRequest(ctx.hub_id, p.driver_jid, p.staging_point_id, p.incentive)
        await self.send(make_request(p.driver_jid, Protocol.REPOSITION, ctx.request, ctx.thread))
        note(ctx, REQUEST, "request sent", driver=p.driver_jid, to=p.staging_point_id,
             travel=round(p.travel_time, 1), incentive=p.incentive)
        self.set_next_state(AWAIT_REPLY)

class _AwaitReply(_RepositionState):
    name = AWAIT_REPLY

    async def step(self):
        ctx = self.ctx
        deadline = clock.now() + ctx.t_reply
        while True:
            msg = await self.next_from_driver(deadline)
            if msg is None:
                note(ctx, AWAIT_REPLY, "no answer")
                self.finish(TIMEOUT)
                return
            perf = msg.get_metadata("performative")
            if perf == Perf.AGREE:
                ctx.agreed_at = clock.now()
                note(ctx, AWAIT_REPLY, "agree")
                self.set_next_state(AWAIT_ARRIVAL)
                return
            if perf in (Perf.REFUSE, Perf.FAILURE):
                reason = msg.body or "-"
                note(ctx, AWAIT_REPLY, perf, reason=reason)
                self.finish(REFUSED if perf == Perf.REFUSE else FAILED, reason)
                return
            note(ctx, AWAIT_REPLY, "ignored", performative=perf)

class _AwaitArrival(_RepositionState):
    name = AWAIT_ARRIVAL

    async def step(self):
        ctx = self.ctx
        deadline = ctx.agreed_at + ctx.plan.travel_time * ARRIVAL_SLACK + ctx.t_reply
        while True:
            msg = await self.next_from_driver(deadline)
            if msg is None:
                note(ctx, AWAIT_ARRIVAL, "driver never arrived")
                self.finish(NO_ARRIVAL)
                return
            perf = msg.get_metadata("performative")
            if perf == Perf.FAILURE:
                note(ctx, AWAIT_ARRIVAL, "failure", reason=msg.body or "-")
                self.finish(FAILED, msg.body or "-")
                return
            if perf == Perf.INFORM:
                try:
                    done = expect(msg, RepositionRequest)
                except ValueError as e:
                    note(ctx, AWAIT_ARRIVAL, "ignored: malformed inform", error=e)
                    continue
                if done.staging_point_id == ctx.plan.staging_point_id:
                    note(ctx, AWAIT_ARRIVAL, "arrived", seconds=round(clock.now() - ctx.agreed_at, 1))
                    self.finish(ARRIVED)
                    return
                note(ctx, AWAIT_ARRIVAL, "ignored: another staging point", at=done.staging_point_id)
            else:
                note(ctx, AWAIT_ARRIVAL, "ignored", performative=perf)

class _End(_RepositionState):
    name = END

    async def step(self):
        ctx = self.ctx
        ctx.finished_at = clock.now()
        if not ctx.outcome:
            ctx.outcome = ERROR
        note(ctx, END, "reposition closed", outcome=ctx.outcome, reason=ctx.reason or "-",
             seconds=round(ctx.finished_at - ctx.started_at, 2))
        self.agent.on_reposition_finished(ctx)

class RepositionFSM(FSMBehaviour):
    """One reposition request. Register with make_template(Protocol.REPOSITION, thread=ctx.thread)."""

    def __init__(self, ctx: RepositionContext):
        self.ctx = ctx
        super().__init__()

    def setup(self):
        c = self.ctx
        self.add_state(REQUEST, _Request(c), initial=True)
        self.add_state(AWAIT_REPLY, _AwaitReply(c))
        self.add_state(AWAIT_ARRIVAL, _AwaitArrival(c))
        self.add_state(END, _End(c))
        for a, b in [(REQUEST, AWAIT_REPLY), (AWAIT_REPLY, AWAIT_ARRIVAL), (AWAIT_REPLY, END),
                     (AWAIT_ARRIVAL, END), (REQUEST, END)]:
            self.add_transition(a, b)

class RepositionPlanner(CyclicBehaviour):
    """Every hub.t_rebalance sim seconds: hub.rebalance(now) (heuristic + one FSM per move)."""

    def __init__(self):
        super().__init__()
        self._next: Optional[float] = None

    async def run(self):
        await asyncio.sleep(0.05)                    # short slices keep the behaviour killable
        hub, now = self.agent, clock.now()
        if self._next is None:
            self._next = now + hub.t_rebalance
        if now < self._next:
            return
        self._next = now + hub.t_rebalance
        try:
            hub.rebalance(now)
        except Exception:                            # a bad round must not switch rebalancing off
            logger.exception("%s: rebalancing round failed", hub.jid)
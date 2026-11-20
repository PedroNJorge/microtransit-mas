"""
src/agents/driver.py   DriverAgent (Role: Driver, Sub-org: Transit Ops). Milestone 6.

Protocols (driver side)
  MatchAuctionProtocol   contractor: cfp -> propose | refuse(reason); accept-proposal -> confirm |
                         refuse; reject-proposal -> release the lock; cancel -> abort.
  DriverStatusProtocol   inform(DriverStatus) to every known hub on each state change (CC-07) and
                         every T_status (heartbeat).
  TripExecutionProtocol  after a firm commitment: drive to the pickup, tell the riders
                         (DriverArriving), wait for RiderReady (T_no_show), board
                         (RiderBoarded), drive to the drop-offs (RiderDroppedOff), report
                         TripCompleted to the hub, go back to idle at the last drop-off.
  RepositionProtocol     request(RepositionRequest) -> agree | refuse; inform on arrival.

The provisional lock (CC-01..CC-09) is common/lock.py::DriverLock, a pure state machine. This
class only turns messages into lock calls and lock decisions into messages. Everything that
touches the lock runs inside ONE behaviour (the contractor), or in the trip runner AFTER the lock
is firm, so there are no interleavings to reason about.

Pricing (truthful, see docs/auction_mechanism.md): the bid price IS the driver's cost
    c = base_cost + cost_per_km * km + cost_per_second * (driving seconds + boarding/alighting seconds)
rounded up to the cent, never a guess at what others bid. Under the second-score rule that is
a dominant strategy. A driver whose cost exceeds the CFP's reserve refuses (it could not be paid
at least its cost, individual rationality).

The ETA in the bid is computed here from the driver's own position, but the hub recomputes it
from the last DriverStatus (CC-10) and its value is the one used for evaluation.

Simplification: drivers share the in-process TransitNetwork the Environment updates, instead of
rebuilding it from TrafficUpdate messages (the edge_weights table is received but not used).
"""
import asyncio
import logging
import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.ws_server import broadcast_event
from common.auction import ceil_cents
from common.cells import deficit
from common.costs import rate_per_second
from common.policy import PolicyBook
from common.lock import DriverLock, LockReason, LockState
from common.messages import make_inform, make_message, make_propose, make_refuse, make_template
from common.ontology import (
    BrainPercept, BidPolicy, CruiseCommand, DemandSignal, HubLoad,
    AuctionOutcome, Bid, CFP, Policy, Rating, DriverArriving, DriverState, DriverStatus, FailureReason, MatchResult,
    LateJoinDecision, LateJoinRequest, Perf, Protocol, RepositionRequest, ReputationScore, RiderBoarded, RiderDroppedOff, RiderReady, Settlement,
    TripCompleted, TripFailed, parse_payload,
)
from common.settings import (
    DRIVER_BASE_COST, DRIVER_CAPACITY, DRIVER_COST_PER_KM, DRIVER_COST_PER_SECOND, DWELL_DROPOFF, DWELL_PICKUP,
    T_JOIN_HOLD, T_LOAD, T_NO_SHOW, T_PROVISIONAL, T_STATUS,
)
from protocols.payment_common import bare

logger = logging.getLogger(__name__)

# refuse reasons (plain text bodies of a `refuse`)
LOCKED = LockReason.LOCKED                  # CC-09
REPOSITIONING, OFFLINE, NO_NETWORK = "repositioning", "offline", "no_network"
NO_CAPACITY, SHIFT_ENDING, UNREACHABLE = "no_capacity", "shift_ending", "unreachable"
TOO_LATE, DETOUR_TOO_LARGE, OVER_RESERVE = "too_late", "detour_too_large", "over_reserve"
UNKNOWN_HUB, MALFORMED = "unknown_hub", "malformed"


class _TripCancelled(Exception):
    pass


class _Unreachable(Exception):
    pass


@dataclass
class Offer:
    """What the driver offered for one pool (kept until the lock is gone)."""
    cfp: CFP
    bid: Bid
    route: List[str]                    # stops: pickup + drop-offs in driving order
    route_time: float


@dataclass
class Assignment:
    """A firm commitment: one pool, the hub that awarded it and the plan."""
    hub: str
    match: MatchResult
    offer: Offer
    cancelled: bool = False
    boarded: List[str] = field(default_factory=list)       # rider JIDs
    riders: List[tuple] = field(default_factory=list)      # (rider JID, request id, destination, that rider's pool id)
    route: List[str] = field(default_factory=list)         # stop order in force, pickup first ([] = offer.route)
    frozen: bool = False                                   # arrived at the pickup: nobody can join any more
    pending: Dict[str, "PendingJoin"] = field(default_factory=dict)   # provisional late joiners by their pool id
    no_shows: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)


@dataclass
class PendingJoin:
    """A late joiner the driver accepted but the hub has not confirmed yet: what to undo if it never is."""
    rider: tuple
    previous_route: List[str]
    deadline: float


# ---------------------------------------------------------------------------------------------
class LateJoinHandler(CyclicBehaviour):
    """LateJoinProtocol, driver side: request(LateJoinRequest) -> agree (applied provisionally) | refuse(reason);
    inform(LateJoinDecision) -> keep it or undo it. The driver is free to refuse: it must still be on the way to the
    pickup, have a seat, and be paid at least its own variable cost of the extra driving plus its markup."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d, sender = self.agent, bare(msg.sender)
        try:
            p = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        a = d.assignment
        if isinstance(p, LateJoinRequest) and msg.get_metadata("performative") == Perf.REQUEST:
            reason = d.late_join_problem(a, sender, p)
            if reason:
                d.log(f"late join {p.join_pool_id} into {p.pool_id} refused: {reason}")
                await self.send(make_message(sender, Perf.REFUSE, Protocol.LATE_JOIN, reason, p.join_pool_id))
                return
            rider = (p.rider_jid, p.request_id, p.destination, p.join_pool_id)
            a.pending[p.join_pool_id] = PendingJoin(rider, list(a.route or a.offer.route),
                                                    clock.now() + d.t_join_hold)
            a.riders.append(rider)
            a.route = list(p.route)
            d.log(f"late join {p.join_pool_id} into {p.pool_id} agreed: +{p.extra_seconds:.0f}s for +{p.extra_pay:.2f}")
            await self.send(make_message(sender, Perf.AGREE, Protocol.LATE_JOIN, "agreed", p.join_pool_id))
        elif isinstance(p, LateJoinDecision) and a is not None and sender == a.hub and a.match.pool_id == p.pool_id:
            if p.confirmed:
                a.pending.pop(p.join_pool_id, None)
            else:
                d.undo_late_join(a, p.join_pool_id)


# ---------------------------------------------------------------------------------------------
class AuctionContractor(CyclicBehaviour):
    """All MatchAuctionProtocol traffic, one message at a time."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d = self.agent
        try:
            await d.on_auction_message(self, msg)
        except Exception:                                    # never let one message kill the behaviour
            logger.exception("%s: error handling %s", d.jid, msg.get_metadata("performative"))


class Housekeeping(CyclicBehaviour):
    """Lock expiry (CC-02) and the DriverStatus heartbeat."""

    async def run(self):
        d = self.agent
        await asyncio.sleep(0.05)
        now = clock.now()
        expired = d.lock.tick(now)
        if expired:
            d.log(f"provisional lock on {expired.pool_id} expired")
            d.offers.pop(expired.pool_id, None)
        await d.announce(self, force=now - d.last_status_at >= d.t_status)
        if d.brain_jid:
            await d.send_percept(self)


class BrainLink(CyclicBehaviour):
    """Commands from this driver's own BDI brain (only that JID is obeyed)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is not None:
            try:
                await self.agent.on_brain_command(self, msg)
            except Exception:
                logger.exception("%s: error handling a brain command", self.agent.jid)


class DemandListener(CyclicBehaviour):
    """A hub tells us about a request we were not called for (we were busy or too far)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d = self.agent
        try:
            sig = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(sig, DemandSignal) and bare(msg.sender) in d.hub_jids:
            d.observe_signal(sig, clock.now())
        elif isinstance(sig, HubLoad) and bare(msg.sender) in d.hub_jids:
            d.loads[sig.hub_id] = (sig, clock.now())


class _Cruise(OneShotBehaviour):
    def __init__(self, probe: Assignment, target: str):
        super().__init__()
        self.probe, self.target = probe, target

    async def run(self):
        d = self.agent
        while d._cruise_busy:                                       # the previous move ends at its next junction
            await asyncio.sleep(0.02)
        if self.probe.cancelled:
            return
        d._cruise_busy = True
        try:
            d.activity = "cruising"
            await d.announce(self)
            await d.drive(self.target, self.probe)
        except (_TripCancelled, _Unreachable):
            pass
        finally:
            d._cruise_busy = False
            if d.cruise is self.probe:                              # not replaced, not overtaken by a job
                d.cruise, d.cruise_target = None, ""
                if d.activity == "cruising":
                    d.activity = "idle"
            await d.announce(self)


class RepositionHandler(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is not None:
            await self.agent.on_reposition(self, msg)


class SettlementReceiver(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        try:
            s = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(s, Settlement) and self.agent.payment_jid and bare(msg.sender) == self.agent.payment_jid:
            self.agent.payouts.append(s)
            self.agent.payout_times.append(clock.now())


class ReputationReceiver(CyclicBehaviour):
    """ReputationProtocol: the reputation agent informs the ratee (here: the driver) of its published score."""
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        try:
            score = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        d = self.agent
        if isinstance(score, ReputationScore) and (not d.reputation_jid or bare(msg.sender) == d.reputation_jid):
            d.reputation_score = score


class PolicyReceiver(CyclicBehaviour):
    """PolicyProtocol (RI-04). The fare cap reaches the driver through the CFP reserve; the driver
    only acknowledges, and refuses what it cannot accept (stale, conflicting, malformed, not from the Regulator)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d = self.agent
        try:
            policy = parse_payload(msg, strict=True)
            if not isinstance(policy, Policy):
                raise ValueError("not a Policy")
            if not d.regulator_jid or bare(msg.sender) != d.regulator_jid:
                reason = "unknown_regulator"
            else:
                ack = d.policies.offer(policy)
                reason = "" if ack.ok else ack.reason
        except (ValueError, TypeError):
            reason = "malformed"
        await self.send(make_message(str(msg.sender), Perf.REFUSE if reason else Perf.AGREE,
                                     Protocol.POLICY, reason or None, msg.thread))


class CompletionReceiver(CyclicBehaviour):
    """The hub marked the trip completed -> now (ID-08) the driver may rate the riders it drove."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d = self.agent
        try:
            done = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        a = d.finished_trips.get(getattr(done, "pool_id", ""))
        if not (isinstance(done, TripCompleted) and a is not None and bare(msg.sender) == a.hub
                and done.pool_id not in d.rated and d.reputation_jid):
            return
        d.rated.add(done.pool_id)
        for rider in a.dropped:
            score = int(min(5, max(1, d.rate_riders_with(a))))
            r = Rating(f"trip-{done.pool_id}", d.me, rider, score)
            await self.send(make_inform(d.reputation_jid, Protocol.REPUTATION, r, r.trip_id))


class TripRunner(OneShotBehaviour):
    """TripExecutionProtocol, driver side. Runs only while the lock is FIRM."""

    def __init__(self, assignment: Assignment):
        super().__init__()
        self.a = assignment

    async def run(self):
        d, a = self.agent, self.a
        pool = a.match.pool_id
        try:
            await d.run_trip(self, a)
        except _TripCancelled:
            d.log(f"trip {pool} cancelled by the hub")
        except _Unreachable as e:
            d.log(f"trip {pool} failed: {e}")
            await self.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                         TripFailed(pool, FailureReason.DRIVER_FAILED, str(d.jid.bare)), pool))
            d.lock.abort(pool, a.hub, clock.now(), "trip_failed")
        except Exception:
            logger.exception("%s: trip %s crashed", d.jid, pool)
            await self.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                         TripFailed(pool, FailureReason.DRIVER_FAILED, str(d.jid.bare)), pool))
            d.lock.abort(pool, a.hub, clock.now(), "trip_crashed")
        finally:
            d.assignment = None
            d.activity = "idle"
            d.passengers = 0
            await d.announce(self)


# ---------------------------------------------------------------------------------------------
def stop_needed(stop: str, boarding, dropped) -> bool:
    """Does anybody still on board get off at `stop`? The planned route has a stop for every rider of the pool, including
    one who did not show up: driving there would be empty, unpaid km, shown on the map as a trip with nobody inside."""
    return any(dest == stop and jid not in dropped for jid, _, dest, _ in boarding)


class DriverAgent(BaseAgent):
    role = "driver"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, start_hub: str = "", network=None,
                 hub_jids: Sequence[str] = (), capacity: int = DRIVER_CAPACITY,
                 shift_end: float = 1e12, base_cost: float = DRIVER_BASE_COST,
                 cost_per_second: float = DRIVER_COST_PER_SECOND, cost_per_km: float = DRIVER_COST_PER_KM,
                 t_provisional: float = T_PROVISIONAL,
                 t_status: float = T_STATUS, t_no_show: float = T_NO_SHOW,
                 dwell_pickup: float = DWELL_PICKUP, dwell_dropoff: float = DWELL_DROPOFF,
                 payment_jid: Optional[str] = None, regulator_jid: Optional[str] = None,
                 reputation_jid: Optional[str] = None,
                 rate_riders_with: Optional[Callable[['Assignment'], int]] = None,
                 brain_jid: Optional[str] = None, t_percept: float = 30.0, t_obs_window: float = 600.0,
                 return_policy: str = "none", **kwargs):
        super().__init__(jid, password, **kwargs)
        self.start_hub = start_hub                    # hub id where this driver starts its shift
        # fixed baseline (no BDI): after a trip drive back to `start_hub` / the `nearest_hub`; `none` = wait where dropped off
        self.return_policy = return_policy
        self.returns: List[str] = []                  # hubs this policy sent the driver to (history)
        self.network = network
        self.hub_jids: List[str] = [bare(j) for j in hub_jids]
        self.capacity, self.shift_end = capacity, shift_end
        self.base_cost, self.cost_per_second, self.cost_per_km = base_cost, cost_per_second, cost_per_km
        self.t_status, self.t_no_show = t_status, t_no_show
        self.dwell_pickup, self.dwell_dropoff = dwell_pickup, dwell_dropoff
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.regulator_jid = bare(regulator_jid) if regulator_jid else None
        self.reputation_jid = bare(reputation_jid) if reputation_jid else None
        self.rate_riders_with = rate_riders_with or (lambda a: 5)
        self.policies = PolicyBook()
        self.finished_trips: Dict[str, Assignment] = {}
        self.rated: set = set()
        self.reputation_score: Optional[ReputationScore] = None   # newest published score about this driver
        self.location: str = start_hub
        self.activity = "idle"                        # idle | repositioning | to_pickup | on_trip | offline
        self.passengers = 0
        self.lock = DriverLock(t_provisional)
        self.offers: Dict[str, Offer] = {}
        self.assignment: Optional[Assignment] = None
        self.payouts: List[Settlement] = []
        self.payout_times: List[float] = []           # sim time each payout arrived (aligned with payouts), for the metrics
        self.odo_m = {"loaded": 0.0, "pickup": 0.0, "other": 0.0}      # metres driven, by what the vehicle was doing
        self.odo_s = {"loaded": 0.0, "pickup": 0.0, "other": 0.0}      # seconds driven, same split
        # empty driving without a rider, by cause: "return" = going to the NEAREST hub (back after a drop-off), "move" = going to
        # another hub because the brain finds it better, "reposition" = a hub asked for it
        self.odo_sub_m = {"return": 0.0, "move": 0.0, "reposition": 0.0}
        self.odo_sub_s = {"return": 0.0, "move": 0.0, "reposition": 0.0}
        self.cruise_reason = "return"
        self.other_log: List[Tuple[float, float, float]] = []         # (time, metres, seconds) of empty driving
        self.trip_log: Dict[str, dict] = {}           # pool id -> pickup/loaded metres and seconds, and when it ended
        self.completed: List[str] = []                # pool ids
        self.status_log: List[DriverStatus] = []
        self.last_status_at = -math.inf
        self._announced = None
        # BDI layer (optional): the brain gets percepts and may command `cruise` moves while idle
        self.brain_jid = bare(brain_jid) if brain_jid else None
        self.t_percept, self.t_obs_window = t_percept, t_obs_window
        self.obs: deque = deque()                      # (time, hub_id, callees, pool_id) of every CFP seen
        self._obs_pools: set = set()
        self.cruise: Optional[Assignment] = None       # the current self-directed move (only the `cancelled` flag is used)
        self.cruise_target: str = ""
        self.loads: Dict[str, Tuple[HubLoad, float]] = {}              # hub id -> (latest HubLoad, received at)
        self._cruise_busy = False
        self._last_percept = (None, -math.inf)
        self.late_join = True                          # may accept riders into a pool it is already driving to
        self.t_join_hold = T_JOIN_HOLD                 # sim s it waits at the hub for a provisional joiner to be confirmed
        self.bid_markup = 0.0                          # opportunity-cost markup set by the brain (0 = bid at cost)
        self.markups: List[float] = []                 # history of markups the brain set
        self.cruises: List[str] = []                   # hubs the brain sent this driver to (history)
        self._trip_inbox: Dict[str, List] = {}

    async def setup(self):
        await super().setup()
        self.add_behaviour(AuctionContractor(), make_template(Protocol.MATCH_AUCTION))
        self.add_behaviour(Housekeeping())
        self.add_behaviour(LateJoinHandler(), make_template(Protocol.LATE_JOIN))
        self.add_behaviour(RepositionHandler(), make_template(Protocol.REPOSITION, Perf.REQUEST))
        self.add_behaviour(SettlementReceiver(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
        self.add_behaviour(ReputationReceiver(), make_template(Protocol.REPUTATION, Perf.INFORM))
        self.add_behaviour(PolicyReceiver(), make_template(Protocol.POLICY, Perf.REQUEST))
        self.add_behaviour(CompletionReceiver(), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))
        if self.brain_jid:
            self.add_behaviour(BrainLink(), make_template(Protocol.DRIVER_BRAIN, Perf.REQUEST))
            self.add_behaviour(DemandListener(), make_template(Protocol.DRIVER_BRAIN, Perf.INFORM))

    # ---- small helpers -------------------------------------------------------------------
    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.jid.user, text)

    @property
    def me(self) -> str:
        return str(self.jid.bare)

    @property
    def state(self) -> DriverState:
        if self.activity == "offline":
            return DriverState.OFFLINE
        if self.lock.state == LockState.PROVISIONAL:
            return DriverState.BIDDING
        if self.lock.state == LockState.FIRM:
            return {"to_pickup": DriverState.EN_ROUTE_PICKUP,
                    "on_trip": DriverState.ON_TRIP}.get(self.activity, DriverState.COMMITTED)
        if self.activity == "repositioning":
            return DriverState.REPOSITIONING
        return DriverState.IDLE

    def status(self) -> DriverStatus:
        return DriverStatus(self.me, self.location, self.state, self.capacity, self.passengers,
                            self.shift_end, self.base_cost,
                            self.cruise_target if self.activity == "cruising" else "")

    async def announce(self, beh, force: bool = False) -> None:
        """DriverStatusProtocol (CC-07): send the status to every hub when the state changed
        (or on the heartbeat)."""
        sig = (self.state, self.lock.holder.pool_id if self.lock.holder else None,
               self.location if (self.state != DriverState.IDLE or self.activity == "cruising") else None,
               self.cruise_target if self.activity == "cruising" else "")
        if not force and sig == self._announced:
            return
        self._announced = sig
        self.last_status_at = clock.now()
        self.broadcast_viz()
        st = self.status()
        self.status_log.append(st)
        for hub in self.hub_jids:
            await beh.send(make_message(hub, Perf.INFORM, Protocol.DRIVER_STATUS, st))

    # ---- map viewer (WebSocket) --------------------------------------------------------------
    def viz_payload(self) -> Optional[dict]:
        """What the map shows for this driver: DRIVER_UPDATE {driver_id, state, lat, lon, passengers, capacity,
        rider_ids}. None if the position has no coordinates."""
        node = self.network.g.nodes.get(self.location) if self.network is not None else None
        if not node or node.get("lat") is None or node.get("lon") is None:
            return None
        state = self.state.value if hasattr(self.state, "value") else str(self.state)
        # Colour priority: a driver in an auction or on a job shows that (BIDDING, COMMITTED, EN_ROUTE_PICKUP, ON_TRIP),
        # even while it is also cruising; REPOSITIONING and CRUISING only show for a driver that is otherwise free.
        if self.state == DriverState.IDLE and self.activity == "cruising":
            state = "CRUISING"                            # self-directed move by the BDI brain / return policy
        on_board = []
        if self.assignment is not None:
            on_board = [j.split("@")[0] for j in self.assignment.boarded if j not in self.assignment.dropped]
        return {"driver_id": self.jid.user, "state": state, "lat": float(node["lat"]), "lon": float(node["lon"]),
                "passengers": self.passengers, "capacity": self.capacity, "rider_ids": on_board}

    def broadcast_viz(self) -> None:
        """Fire-and-forget; never blocks or breaks the simulation (no loop = a unit test = nothing to send)."""
        payload = self.viz_payload()
        if payload is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(broadcast_event("DRIVER_UPDATE", payload))

    # ---- bidding ---------------------------------------------------------------------------
    def calculate_bid(self, cfp: CFP, now: float):
        """-> (Offer, None) or (None, refuse_reason). No side effects."""
        net = self.network
        if net is None:
            return None, NO_NETWORK
        if cfp.passenger_count > self.capacity - self.passengers:             # GC-01
            return None, NO_CAPACITY
        try:
            eta = net.travel_time(self.location, cfp.pickup_node, now)
            planned = net.best_stop_order(cfp.pickup_node, list(cfp.rider_destinations), now)
            if cfp.stop_order and sorted(set(cfp.stop_order)) == sorted(set(cfp.rider_destinations) - {cfp.pickup_node}):
                routed = net.route_through([cfp.pickup_node, *cfp.stop_order], now)     # the hub's exact plan
                planned = ([cfp.pickup_node, *cfp.stop_order], routed[1]) if routed else None
        except KeyError:
            return None, UNREACHABLE
        if planned is None or math.isinf(eta):
            return None, UNREACHABLE
        stops, route_time = planned
        if now + eta + route_time > self.shift_end:                            # GC-04
            return None, SHIFT_ENDING
        if any(now + eta > w[1] for w in cfp.rider_time_windows.values()):     # GC-05
            return None, TOO_LATE
        detour = 0.0
        for dest in set(cfp.rider_destinations):
            if dest == cfp.pickup_node:
                continue
            extra = net.detour_seconds(cfp.pickup_node, dest, stops, now)
            if extra is None:
                return None, UNREACHABLE
            detour = max(detour, extra)
        if detour > cfp.max_detour + 1e-9:                                     # GC-06
            return None, DETOUR_TOO_LARGE
        eta_m = net.route_length([self.location, cfp.pickup_node], now) if self.location != cfp.pickup_node else 0.0
        route_m = net.route_length(stops, now + eta) if len(stops) > 1 else 0.0
        if eta_m is None or route_m is None:
            return None, UNREACHABLE
        dwell = DWELL_PICKUP + DWELL_DROPOFF * len({s for s in stops[1:] if s != cfp.pickup_node})
        cost = (self.base_cost + self.cost_per_km * (eta_m + route_m) / 1000.0
                + self.cost_per_second * (eta + route_time + dwell))
        price = ceil_cents(cost * (1.0 + self.bid_markup)) / 100.0               # cost + opportunity cost
        if price > cfp.max_total_fare + 1e-9:                                  # IR: not below cost
            return None, OVER_RESERVE
        bid = Bid(str(uuid.uuid4()), self.me, cfp.pool_id, price, eta, detour,
                  self.capacity - self.passengers, cfp.attempt)
        return Offer(cfp, bid, stops, route_time), None

    async def on_auction_message(self, beh, msg) -> None:
        perf, hub = msg.get_metadata("performative"), bare(msg.sender)
        now = clock.now()
        expired = self.lock.tick(now)
        if expired:
            self.offers.pop(expired.pool_id, None)
        try:
            payload = parse_payload(msg, strict=False)
        except (ValueError, TypeError):
            payload = None
        pool = msg.thread or ""
        if self.hub_jids and hub not in self.hub_jids:
            if perf == Perf.CFP:
                await beh.send(make_refuse(hub, pool, UNKNOWN_HUB))
            return
        if perf == Perf.CFP:
            if not isinstance(payload, CFP):
                await beh.send(make_refuse(hub, pool, MALFORMED))
            else:
                await self.handle_cfp(beh, hub, payload, now)
        elif perf == Perf.ACCEPT and isinstance(payload, MatchResult):
            await self.handle_accept(beh, hub, payload, now)
        elif perf == Perf.REJECT:
            if self.lock.release(pool, hub, now, "rejected"):                  # CC-04
                self.offers.pop(pool, None)
                self.log(f"reject-proposal for {pool}: lock released")
        elif perf == Perf.CANCEL:
            await self.handle_cancel(hub, pool, now)
        await self.announce(beh)

    async def handle_cfp(self, beh, hub: str, cfp: CFP, now: float) -> None:
        pool = cfp.pool_id
        self.observe(cfp, now)
        if self.activity == "offline":
            await beh.send(make_refuse(hub, pool, OFFLINE)); return
        if self.lock.blocks(pool, hub, cfp.attempt, now):                      # CC-09
            self.log(f"cfp {pool} from {hub}: refuse(locked)")
            await beh.send(make_refuse(hub, pool, LOCKED)); return
        if self.activity == "repositioning":
            await beh.send(make_refuse(hub, pool, REPOSITIONING)); return
        if self.lock.holds(pool, hub) and pool in self.offers and cfp.attempt == self.offers[pool].cfp.attempt:
            await beh.send(make_propose(hub, self.offers[pool].bid)); return   # duplicate CFP: same bid
        offer, reason = self.calculate_bid(cfp, now)
        if offer is None:
            self.log(f"cfp {pool}: refuse({reason})")
            await beh.send(make_refuse(hub, pool, reason)); return
        decision = self.lock.acquire(pool, hub, offer.bid.bid_id, cfp.attempt, now)
        if not decision.ok:
            await beh.send(make_refuse(hub, pool, decision.reason)); return
        self.offers[pool] = offer
        self.log(f"cfp {pool}: bid {offer.bid.price:.2f} eta={offer.bid.eta:.0f}s (lock until "
                 f"t={self.lock.holder.expires_at:.0f})")
        await beh.send(make_propose(hub, offer.bid))

    async def handle_accept(self, beh, hub: str, match: MatchResult, now: float) -> None:
        pool = match.pool_id
        if bare(match.driver_id) != self.me:
            await beh.send(make_refuse(hub, pool, "not_addressed")); return
        decision = self.lock.commit(pool, hub, now)                            # CC-03
        offer = self.offers.get(pool)
        if not decision.ok or offer is None:
            self.log(f"accept {pool} refused: {decision.reason or 'no_offer'}")
            await beh.send(make_refuse(hub, pool, decision.reason or LockReason.NO_LOCK)); return
        if decision.replay:
            await beh.send(make_message(hub, Perf.CONFIRM, Protocol.MATCH_AUCTION, match, pool)); return
        if self.cruise is not None:                                            # a job beats a cruise: stop at the next junction
            self.cruise.cancelled = True
            self.cruise, self.cruise_target = None, ""
        self.assignment = Assignment(hub, match, offer)
        self.activity = "to_pickup"
        self.log(f"committed to {pool}, fare {match.final_fare:.2f}")
        await beh.send(make_message(hub, Perf.CONFIRM, Protocol.MATCH_AUCTION, match, pool))
        # no thread filter: a rider that joined late talks on its own pool id (run_trip checks the sender)
        self.add_behaviour(TripRunner(self.assignment), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))

    async def handle_cancel(self, hub: str, pool: str, now: float) -> None:
        a = self.assignment
        if a and a.match.pool_id == pool and a.hub == hub:
            if self.activity == "on_trip":
                return                                                         # too late to void
            a.cancelled = True
        if self.lock.abort(pool, hub, now, "cancelled"):
            self.offers.pop(pool, None)
            self.log(f"{pool} cancelled by {hub}: lock released")

    # ---- BDI layer: perception and commands ----------------------------------------------------
    def observe(self, cfp: CFP, now: float) -> None:
        """Every call for proposals is a sighting of demand at its hub (and of how many drivers it called)."""
        if cfp.pool_id not in self._obs_pools:
            self._obs_pools.add(cfp.pool_id)
            self.obs.append((now, cfp.hub_id, max(1.0, cfp.constraints.get("callees", 1.0)), cfp.pool_id))

    def observe_signal(self, sig: DemandSignal, now: float) -> None:
        if sig.pool_id not in self._obs_pools:
            self._obs_pools.add(sig.pool_id)
            self.obs.append((now, sig.hub_id, max(1.0, sig.callees), sig.pool_id))

    def pressures(self, now: float) -> Dict[str, float]:
        """hub -> requests per available driver seen in the last t_obs_window seconds."""
        while self.obs and now - self.obs[0][0] > self.t_obs_window:
            _, _, _, pool = self.obs.popleft()
            self._obs_pools.discard(pool)
        out: Dict[str, float] = {}
        for _, hub, callees, _ in self.obs:
            out[hub] = out.get(hub, 0.0) + 1.0 / callees
        return out

    def percept(self, now: float) -> BrainPercept:
        pressures = self.pressures(now)
        known = set(self.network.hubs) if self.network is not None else set()   # the brain sees every hub
        hubs = sorted(set(pressures) | known | ({self.start_hub} if self.start_hub else set()))
        etas = []
        for h in hubs:
            try:
                t = self.network.travel_time(self.location, h, now) if self.network else math.inf
            except KeyError:
                t = math.inf
            etas.append(-1.0 if math.isinf(t) else round(t, 1))
        if self.activity == "cruising":
            state = "cruising"
        elif self.lock.state == LockState.FIRM or self.activity not in ("idle", "cruising"):
            state = "busy"
        else:
            state = "idle"                                       # a provisional lock is a bid in flight, not a job
        at_hub = self.location if self.location in hubs else ""
        deficits = []
        if any(now - at <= 3 * T_LOAD for _, at in self.loads.values()):
            for h in hubs:
                load = self.loads.get(h)
                fresh = load is not None and now - load[1] <= 3 * T_LOAD
                deficits.append(round(deficit(load[0].waiting, load[0].rate, load[0].supply), 2) if fresh else 0.0)
        return BrainPercept(self.me, state, self.location, at_hub, self.start_hub, hubs, etas,
                            [round(pressures.get(h, 0.0), 3) for h in hubs], now, deficits)

    async def send_percept(self, beh, force: bool = False) -> None:
        now = clock.now()
        p = self.percept(now)
        sig = (p.state, p.at_hub)
        if not force and sig == self._last_percept[0] and now - self._last_percept[1] < self.t_percept:
            return
        self._last_percept = (sig, now)
        await beh.send(make_message(self.brain_jid, Perf.INFORM, Protocol.DRIVER_BRAIN, p))

    async def on_brain_command(self, beh, msg) -> None:
        if bare(msg.sender) != self.brain_jid:
            return
        try:
            cmd = parse_payload(msg, strict=True)
            if not isinstance(cmd, (CruiseCommand, BidPolicy)):
                raise ValueError("not a brain command")
        except (ValueError, TypeError):
            await beh.send(make_message(msg.sender, Perf.REFUSE, Protocol.DRIVER_BRAIN, MALFORMED, msg.thread))
            return
        if isinstance(cmd, BidPolicy):                                          # pricing needs no lock: it only shapes future bids
            self.bid_markup = cmd.markup
            self.markups.append(cmd.markup)
            await beh.send(make_message(msg.sender, Perf.AGREE, Protocol.DRIVER_BRAIN, cmd, msg.thread))
            return
        now = clock.now()
        if self.lock.state != LockState.FREE or self.activity not in ("idle", "cruising"):
            reason = LOCKED                                                     # busy: the brain is told and re-plans
        elif not self.network or not self.network.has_node(cmd.hub_id):
            reason = UNREACHABLE
        elif cmd.hub_id == self.cruise_target:
            reason = "already_going"
        elif math.isinf(self.network.travel_time(self.location, cmd.hub_id, now)):
            reason = UNREACHABLE
        else:
            reason = ""
        if reason:
            await beh.send(make_message(msg.sender, Perf.REFUSE, Protocol.DRIVER_BRAIN, reason, msg.thread))
            return
        if self.cruise is not None:
            self.cruise.cancelled = True                                        # redirected: the old move stops at its next junction
        self.cruise = Assignment("", None, None)
        self.cruise_target = cmd.hub_id
        self.cruise_reason = "return" if cmd.hub_id == self.nearest_hub(now) else "move"
        self.cruises.append(cmd.hub_id)
        self.log(f"brain: cruising to {cmd.hub_id}")
        await beh.send(make_message(msg.sender, Perf.AGREE, Protocol.DRIVER_BRAIN, cmd, msg.thread))
        self.add_behaviour(_Cruise(self.cruise, cmd.hub_id))

    # ---- trip execution ----------------------------------------------------------------------
    async def drive(self, to: str, a: Assignment) -> None:
        """Move node by node (a closed road triggers a re-plan; no route -> _Unreachable)."""
        net = self.network
        waited = 0
        while self.location != to:
            res = net.shortest_path(self.location, to, clock.now())
            if res is None:
                if waited >= 3:
                    raise _Unreachable(f"no route {self.location} -> {to}")
                waited += 1
                await clock.sim_sleep(30.0)
                continue
            path, _ = res
            for nxt in path[1:]:
                if a.cancelled:
                    raise _TripCancelled()
                dt = net.effective_time(self.location, nxt, clock.now())
                if dt is None:
                    break                                   # road closed: re-plan from here
                await clock.sim_sleep(dt)
                self.record_hop(self.location, nxt, dt, a)
                self.location = nxt
                self.broadcast_viz()
            if a.cancelled:
                raise _TripCancelled()

    def nearest_hub(self, now: float) -> str:
        """The hub node closest in travel time ('' if unknown): going there is a 'return', anywhere else a 'move'."""
        best, best_t = "", math.inf
        hubs = set(self.network.hubs) if self.network is not None else set()          # hub NODE ids (hub_jids are JIDs)
        for h in sorted(hubs | ({self.start_hub} if self.start_hub else set())):
            try:
                t = self.network.travel_time(self.location, h, now)
            except (KeyError, AttributeError):
                continue
            if t < best_t:
                best, best_t = h, t
        return best

    @property
    def cost_rate(self) -> float:
        """EUR per second of driving at the nominal urban speed (km and time folded together)."""
        return rate_per_second(self.cost_per_km, self.cost_per_second)

    def record_hop(self, frm: str, to: str, seconds: float, a: Assignment) -> None:
        """Odometer for the metrics: what this hop was (loaded / on the way to a pickup / anything else) and how long."""
        kind = {"on_trip": "loaded", "to_pickup": "pickup"}.get(self.activity, "other")
        try:
            metres = float(self.network.g[frm][to].get("length", 0.0))
        except (KeyError, AttributeError):
            metres = 0.0
        self.odo_m[kind] += metres
        self.odo_s[kind] += seconds
        if kind == "other":
            self.other_log.append((clock.now(), metres, seconds))
            sub = "reposition" if self.activity == "repositioning" else self.cruise_reason
            self.odo_sub_m[sub] += metres
            self.odo_sub_s[sub] += seconds
        if a is not None and a.match is not None and kind != "other":
            row = self.trip_log.setdefault(a.match.pool_id, {"pickup_m": 0.0, "loaded_m": 0.0, "pickup_s": 0.0,
                                                             "loaded_s": 0.0, "done": None})
            row[kind + "_m"] += metres
            row[kind + "_s"] += seconds

    async def run_trip(self, beh, a: Assignment) -> None:
        pool, cfp = a.match.pool_id, a.offer.cfp
        while self._cruise_busy:                                               # let the cruise stop first
            await asyncio.sleep(0.02)
        a.riders = [(j, r, dest, pool) for j, r, dest in
                    zip(cfp.rider_jids, cfp.rider_time_windows.keys(), cfp.rider_destinations)]
        await self.announce(beh)
        await self.drive(cfp.pickup_node, a)
        # arrived. Joiners the hub has not confirmed yet get a little time (T_join_hold), then they are undone; after this
        # nobody can join any more (no await between the last check and the freeze).
        while a.pending and not a.cancelled:
            for jp, pend in list(a.pending.items()):
                if clock.now() >= pend.deadline:
                    self.undo_late_join(a, jp)
                    await beh.send(make_message(a.hub, Perf.INFORM, Protocol.LATE_JOIN,
                                                LateJoinDecision(pool, jp, False, "not_confirmed"), jp))
            if a.pending:
                await clock.sim_sleep(2.0)
        a.frozen = True
        riders = list(a.riders)
        # tell the riders and wait for them (T_no_show, FE-05)
        eta = 0.0
        for jid, req, _, rpool in riders:
            await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        DriverArriving(req, rpool, self.me, eta), rpool))
        ready: set = set()
        deadline = clock.now() + self.t_no_show
        while len(ready) < len(riders) and clock.now() < deadline:
            m = await beh.receive(timeout=max(min(clock.to_real(deadline - clock.now()), 1.0), 0.001))
            if m is None:
                if a.cancelled:
                    raise _TripCancelled()
                continue
            try:
                p = parse_payload(m, strict=True)
            except (ValueError, TypeError):
                continue
            if isinstance(p, RiderReady) and bare(m.sender) in [r[0] for r in riders]:
                ready.add(bare(m.sender))
        if a.cancelled:
            raise _TripCancelled()
        a.no_shows = [jid for jid, _, _, _ in riders if jid not in ready]
        for jid, _, _, rpool in riders:
            if jid in a.no_shows:                                  # FE-05: report, do not wait longer
                await beh.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                            TripFailed(rpool, FailureReason.RIDER_CANCELLED, jid), rpool))
        boarding = [r for r in riders if r[0] in ready]
        if not boarding:
            self.lock.abort(pool, a.hub, clock.now(), "no_show")
            self.log(f"{pool}: nobody showed up, back to idle")
            return
        await clock.sim_sleep(self.dwell_pickup)
        for jid, req, _, rpool in boarding:
            a.boarded.append(jid)
            await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        RiderBoarded(req, rpool, self.me, clock.now()), rpool))
        self.passengers = len(boarding)
        self.activity = "on_trip"
        await self.announce(beh)
        # drive the planned order, dropping off as we go
        for stop in (a.route or a.offer.route)[1:]:
            if not stop_needed(stop, boarding, a.dropped):
                continue                                           # nobody on board goes there (a no-show's stop, or all dropped)
            await self.drive(stop, a)
            await clock.sim_sleep(self.dwell_dropoff)
            for jid, req, dest, rpool in boarding:
                if dest == stop and jid not in a.dropped:
                    a.dropped.append(jid)
                    self.passengers -= 1
                    await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                                RiderDroppedOff(req, rpool, self.me, clock.now()), rpool))
        for jid, req, dest, rpool in boarding:                     # drop-off at the pickup itself
            if jid not in a.dropped:
                a.dropped.append(jid)
                self.passengers -= 1
                await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                            RiderDroppedOff(req, rpool, self.me, clock.now()), rpool))
        done = TripCompleted(trip_id=f"trip-{pool}", pool_id=pool, driver_id=self.me,
                             rider_ids=list(a.dropped), completed_at=clock.now(),
                             rider_fares={j: a.match.rider_fares[j] for j in a.dropped
                                          if j in a.match.rider_fares})
        await beh.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION, done, pool))
        self.lock.finish(pool, clock.now())
        self.offers.pop(pool, None)
        self.completed.append(pool)
        self.trip_log.setdefault(pool, {"pickup_m": 0.0, "loaded_m": 0.0, "pickup_s": 0.0, "loaded_s": 0.0})["done"] = clock.now()
        self.finished_trips[pool] = a
        self.log(f"{pool} completed, idle at {self.location}")
        self.return_after_trip()

    def late_join_problem(self, a, sender: str, p) -> str:
        """Why this driver does not take a late joiner ('' = it does)."""
        if not self.late_join or a is None or a.match is None or sender != a.hub or a.match.pool_id != p.pool_id:
            return "not_assigned"
        if a.frozen or self.activity != "to_pickup":
            return "too_late"
        if len(a.riders) + 1 > self.capacity:
            return "full"
        need = (p.extra_seconds + DWELL_DROPOFF) * self.cost_rate * (1.0 + self.bid_markup)
        if p.extra_pay + 1e-9 < need:
            return "underpaid"
        return ""

    def undo_late_join(self, a, join_pool_id: str) -> bool:
        """Take a provisional joiner back out (cancelled, or never confirmed). Only possible before the pickup."""
        pend = a.pending.pop(join_pool_id, None)
        if pend is None:
            return False
        if pend.rider in a.riders:
            a.riders.remove(pend.rider)
        a.route = list(pend.previous_route)
        self.log(f"late join {join_pool_id} undone")
        return True

    def return_target(self) -> str:
        """Hub the fixed return policy sends an idle driver to ('' = stay)."""
        if self.return_policy == "start_hub":
            return self.start_hub
        if self.return_policy == "nearest_hub" and self.network is not None:
            now = clock.now()
            best, best_t = "", math.inf
            for h in sorted(self.network.hubs):
                try:
                    t = self.network.travel_time(self.location, h, now)
                except KeyError:
                    continue
                if t < best_t:
                    best, best_t = h, t
            return best
        return ""

    def return_after_trip(self) -> None:
        target = self.return_target()
        if not target or target == self.location or self.brain_jid:
            return
        if self.cruise is not None:
            self.cruise.cancelled = True
        self.cruise = Assignment("", None, None)
        self.cruise_target = target
        self.cruise_reason = "return"
        self.returns.append(target)
        self.log(f"returning to {target}")
        self.add_behaviour(_Cruise(self.cruise, target))

    # ---- repositioning ------------------------------------------------------------------------
    async def on_reposition(self, beh, msg) -> None:
        hub = bare(msg.sender)
        try:
            req = parse_payload(msg, strict=True)
            if not isinstance(req, RepositionRequest) or msg.get_metadata("performative") != Perf.REQUEST:
                return
        except (ValueError, TypeError):
            await beh.send(make_message(hub, Perf.REFUSE, Protocol.REPOSITION, MALFORMED, msg.thread))
            return
        now, thread = clock.now(), msg.thread
        reason = ""
        if self.hub_jids and hub not in self.hub_jids:
            reason = UNKNOWN_HUB
        elif self.lock.state != LockState.FREE or self.activity in ("to_pickup", "on_trip"):
            reason = LOCKED                                           # GC-13
        elif self.activity not in ("idle", "cruising"):
            reason = "busy"
        else:                                                         # a self-directed cruise (BDI) yields to the hub's request
            sp = self.network.staging_points.get(req.staging_point_id) if self.network else None
            target = sp.location if sp else (req.staging_point_id if self.network and
                                             self.network.has_node(req.staging_point_id) else "")
            secs = self.network.travel_time(self.location, target, now) if target else math.inf
            if not target or math.isinf(secs):
                reason = UNREACHABLE
            elif req.incentive + 1e-9 < self.cost_rate * secs:
                reason = "incentive_too_low"
        if reason:
            await beh.send(make_message(hub, Perf.REFUSE, Protocol.REPOSITION, reason, thread))
            return
        if self.cruise is not None:
            self.cruise.cancelled = True                              # stops at its next junction; _Reposition waits for it
        self.activity = "repositioning"
        await beh.send(make_message(hub, Perf.AGREE, Protocol.REPOSITION, req, thread))
        await self.announce(beh)
        self.add_behaviour(_Reposition(hub, req, target, thread))


class _Reposition(OneShotBehaviour):
    def __init__(self, hub: str, req: RepositionRequest, target: str, thread: str):
        super().__init__()
        self.hub, self.req, self.target, self.thread = hub, req, target, thread

    async def run(self):
        d = self.agent
        probe = Assignment(self.hub, None, None)                     # only the `cancelled` flag is used
        while d._cruise_busy:                                        # a cancelled cruise ends at its next junction
            await asyncio.sleep(0.02)
        try:
            await d.drive(self.target, probe)
        except _Unreachable:
            d.activity = "idle"
            await d.announce(self)
            await self.send(make_message(self.hub, Perf.FAILURE, Protocol.REPOSITION, "unreachable", self.thread))
            return
        d.activity = "idle"
        await d.announce(self)
        await self.send(make_message(self.hub, Perf.INFORM, Protocol.REPOSITION, self.req, self.thread))

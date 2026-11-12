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
    c = base_cost + cost_per_second * (seconds to the pickup + seconds of the route)
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
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.auction import ceil_cents
from common.policy import PolicyBook
from common.lock import DriverLock, LockReason, LockState
from common.messages import make_inform, make_message, make_propose, make_refuse, make_template
from common.ontology import (
    AuctionOutcome, Bid, CFP, Policy, Rating, DriverArriving, DriverState, DriverStatus, FailureReason, MatchResult,
    Perf, Protocol, RepositionRequest, RiderBoarded, RiderDroppedOff, RiderReady, Settlement,
    TripCompleted, TripFailed, parse_payload,
)
from common.settings import (
    DRIVER_BASE_COST, DRIVER_CAPACITY, DRIVER_COST_PER_SECOND, DWELL_DROPOFF, DWELL_PICKUP,
    T_NO_SHOW, T_PROVISIONAL, T_STATUS,
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
    no_shows: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)


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
class DriverAgent(BaseAgent):
    role = "driver"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, start_hub: str = "", network=None,
                 hub_jids: Sequence[str] = (), capacity: int = DRIVER_CAPACITY,
                 shift_end: float = 1e12, base_cost: float = DRIVER_BASE_COST,
                 cost_per_second: float = DRIVER_COST_PER_SECOND, t_provisional: float = T_PROVISIONAL,
                 t_status: float = T_STATUS, t_no_show: float = T_NO_SHOW,
                 dwell_pickup: float = DWELL_PICKUP, dwell_dropoff: float = DWELL_DROPOFF,
                 payment_jid: Optional[str] = None, regulator_jid: Optional[str] = None,
                 reputation_jid: Optional[str] = None,
                 rate_riders_with: Optional[Callable[['Assignment'], int]] = None, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.start_hub = start_hub                    # hub id where this driver starts its shift
        self.network = network
        self.hub_jids: List[str] = [bare(j) for j in hub_jids]
        self.capacity, self.shift_end = capacity, shift_end
        self.base_cost, self.cost_per_second = base_cost, cost_per_second
        self.t_status, self.t_no_show = t_status, t_no_show
        self.dwell_pickup, self.dwell_dropoff = dwell_pickup, dwell_dropoff
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.regulator_jid = bare(regulator_jid) if regulator_jid else None
        self.reputation_jid = bare(reputation_jid) if reputation_jid else None
        self.rate_riders_with = rate_riders_with or (lambda a: 5)
        self.policies = PolicyBook()
        self.finished_trips: Dict[str, Assignment] = {}
        self.rated: set = set()
        self.location: str = start_hub
        self.activity = "idle"                        # idle | repositioning | to_pickup | on_trip | offline
        self.passengers = 0
        self.lock = DriverLock(t_provisional)
        self.offers: Dict[str, Offer] = {}
        self.assignment: Optional[Assignment] = None
        self.payouts: List[Settlement] = []
        self.completed: List[str] = []                # pool ids
        self.status_log: List[DriverStatus] = []
        self.last_status_at = -math.inf
        self._announced = None
        self._trip_inbox: Dict[str, List] = {}

    async def setup(self):
        await super().setup()
        self.add_behaviour(AuctionContractor(), make_template(Protocol.MATCH_AUCTION))
        self.add_behaviour(Housekeeping())
        self.add_behaviour(RepositionHandler(), make_template(Protocol.REPOSITION, Perf.REQUEST))
        self.add_behaviour(SettlementReceiver(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
        self.add_behaviour(PolicyReceiver(), make_template(Protocol.POLICY, Perf.REQUEST))
        self.add_behaviour(CompletionReceiver(), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))

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
                            self.shift_end, self.base_cost)

    async def announce(self, beh, force: bool = False) -> None:
        """DriverStatusProtocol (CC-07): send the status to every hub when the state changed
        (or on the heartbeat)."""
        sig = (self.state, self.lock.holder.pool_id if self.lock.holder else None,
               self.location if self.state != DriverState.IDLE else None)
        if not force and sig == self._announced:
            return
        self._announced = sig
        self.last_status_at = clock.now()
        st = self.status()
        self.status_log.append(st)
        for hub in self.hub_jids:
            await beh.send(make_message(hub, Perf.INFORM, Protocol.DRIVER_STATUS, st))

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
        price = ceil_cents(self.base_cost + self.cost_per_second * (eta + route_time)) / 100.0
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
        self.assignment = Assignment(hub, match, offer)
        self.activity = "to_pickup"
        self.log(f"committed to {pool}, fare {match.final_fare:.2f}")
        await beh.send(make_message(hub, Perf.CONFIRM, Protocol.MATCH_AUCTION, match, pool))
        self.add_behaviour(TripRunner(self.assignment),
                           make_template(Protocol.TRIP_EXECUTION, Perf.INFORM, thread=pool))

    async def handle_cancel(self, hub: str, pool: str, now: float) -> None:
        a = self.assignment
        if a and a.match.pool_id == pool and a.hub == hub:
            if self.activity == "on_trip":
                return                                                         # too late to void
            a.cancelled = True
        if self.lock.abort(pool, hub, now, "cancelled"):
            self.offers.pop(pool, None)
            self.log(f"{pool} cancelled by {hub}: lock released")

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
                self.location = nxt
            if a.cancelled:
                raise _TripCancelled()

    async def run_trip(self, beh, a: Assignment) -> None:
        pool, cfp = a.match.pool_id, a.offer.cfp
        riders = list(zip(cfp.rider_jids, cfp.rider_time_windows.keys(), cfp.rider_destinations))
        await self.announce(beh)
        await self.drive(cfp.pickup_node, a)
        # arrived: tell the riders and wait for them (T_no_show, FE-05)
        eta = 0.0
        for jid, req, _ in riders:
            await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        DriverArriving(req, pool, self.me, eta), pool))
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
        a.no_shows = [jid for jid, _, _ in riders if jid not in ready]
        for jid in a.no_shows:                                     # FE-05: report, do not wait longer
            await beh.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        TripFailed(pool, FailureReason.RIDER_CANCELLED, jid), pool))
        boarding = [r for r in riders if r[0] in ready]
        if not boarding:
            self.lock.abort(pool, a.hub, clock.now(), "no_show")
            self.log(f"{pool}: nobody showed up, back to idle")
            return
        await clock.sim_sleep(self.dwell_pickup)
        for jid, req, _ in boarding:
            a.boarded.append(jid)
            await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        RiderBoarded(req, pool, self.me, clock.now()), pool))
        self.passengers = len(boarding)
        self.activity = "on_trip"
        await self.announce(beh)
        # drive the planned order, dropping off as we go
        for stop in a.offer.route[1:]:
            await self.drive(stop, a)
            await clock.sim_sleep(self.dwell_dropoff)
            for jid, req, dest in boarding:
                if dest == stop and jid not in a.dropped:
                    a.dropped.append(jid)
                    self.passengers -= 1
                    await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                                RiderDroppedOff(req, pool, self.me, clock.now()), pool))
        for jid, req, dest in boarding:                            # drop-off at the pickup itself
            if jid not in a.dropped:
                a.dropped.append(jid)
                self.passengers -= 1
                await beh.send(make_message(jid, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                            RiderDroppedOff(req, pool, self.me, clock.now()), pool))
        done = TripCompleted(trip_id=f"trip-{pool}", pool_id=pool, driver_id=self.me,
                             rider_ids=list(a.dropped), completed_at=clock.now(),
                             rider_fares={j: a.match.rider_fares[j] for j in a.dropped
                                          if j in a.match.rider_fares})
        await beh.send(make_message(a.hub, Perf.INFORM, Protocol.TRIP_EXECUTION, done, pool))
        self.lock.finish(pool, clock.now())
        self.offers.pop(pool, None)
        self.completed.append(pool)
        self.finished_trips[pool] = a
        self.log(f"{pool} completed, idle at {self.location}")

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
        elif self.activity != "idle":
            reason = "busy"
        else:
            sp = self.network.staging_points.get(req.staging_point_id) if self.network else None
            target = sp.location if sp else (req.staging_point_id if self.network and
                                             self.network.has_node(req.staging_point_id) else "")
            secs = self.network.travel_time(self.location, target, now) if target else math.inf
            if not target or math.isinf(secs):
                reason = UNREACHABLE
            elif req.incentive + 1e-9 < self.cost_per_second * secs:
                reason = "incentive_too_low"
        if reason:
            await beh.send(make_message(hub, Perf.REFUSE, Protocol.REPOSITION, reason, thread))
            return
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

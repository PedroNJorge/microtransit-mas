"""
src/agents/rider.py   RiderAgent (Role: Rider, Sub-org: Demand). Milestone 7.

One trip at a time, driven by a small state machine (Agent Model 8.3):

    IDLE -> REQUESTING -> AWAITING_POOL -> DECIDING -> AWAITING_MATCH -> DRIVER_EN_ROUTE -> IN_TRIP -> RATING -> IDLE
                |              |               |             |                |               |
                +--------------+---------------+-------------+----------------+---------------+--> IDLE (ends unmatched/failed)

  DRIVER_EN_ROUTE starts at the MatchResult and ends when the driver reports RiderBoarded;
  IN_TRIP is only the ride itself (boarding -> drop-off), so its duration is the ride time.

  TripRequestProtocol     request(TripRequest) -> hub; agree(Pool) / refuse(reason); inform(request_queued)
  PoolFormationProtocol   responder: propose(PoolProposal) -> accept-proposal | reject-proposal(reason);
                          inform(pool_confirmed) -> the rider now escrows its fare; inform(pool_dissolved)
  EscrowProtocol          request(EscrowRequest) -> Payment, ONLY after the pool is confirmed
  MatchAuctionProtocol    inform(MatchResult) = matched; inform(AuctionOutcome awarded=False) = unmatched
  TripExecutionProtocol   DriverArriving -> RiderReady; RiderBoarded; RiderDroppedOff
  SettlementProtocol      inform(Settlement) from Payment = what was actually charged
  ReputationProtocol      rater: inform(Rating) -> Reputation Agent after the drop-off

The legal moves of the state machine are in TRANSITIONS and are checked on every change, so a
message that arrives in the wrong state is logged and ignored instead of corrupting the trip.
Every trip is kept in `trips` (a TripRecord) after it ends, so tests and metrics can read them.

The rating policy is a pluggable function TripRecord -> 1..5. The default is 5, minus one star
for a pickup that took more than `lateness_tolerance` seconds longer than promised, minus one
for waiting beyond the rider's own window.
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_inform, make_message, make_request, make_template
from common.ontology import (
    AuctionOutcome, DriverArriving, Escrow, Refund, EscrowRequest, FailureReason, MatchResult, Perf, Pool,
    PoolProposal, Protocol, Rating, RiderBoarded, RiderDroppedOff, RiderReady, Settlement, TripRequest,
    parse_payload,
)
from common.settings import READY_DELAY, T_POOL_RESPONSE, T_RIDER_PATIENCE
from protocols.payment_common import bare

logger = logging.getLogger(__name__)

IDLE, REQUESTING, AWAITING_POOL, DECIDING = "IDLE", "REQUESTING", "AWAITING_POOL", "DECIDING"
AWAITING_MATCH, DRIVER_EN_ROUTE, IN_TRIP, RATING = "AWAITING_MATCH", "DRIVER_EN_ROUTE", "IN_TRIP", "RATING"

TRANSITIONS = {
    IDLE: {REQUESTING},
    REQUESTING: {AWAITING_POOL, IDLE},
    AWAITING_POOL: {DECIDING, IDLE},
    DECIDING: {AWAITING_MATCH, AWAITING_POOL, IDLE},      # AWAITING_POOL again = re-pooled (a co-rider declined)
    AWAITING_MATCH: {DRIVER_EN_ROUTE, IDLE},
    DRIVER_EN_ROUTE: {IN_TRIP, IDLE},
    IN_TRIP: {RATING, IDLE},
    RATING: {IDLE},
}

# how a trip ended
COMPLETED, REFUSED, DECLINED, DISSOLVED = "completed", "refused", "declined", "dissolved"
UNMATCHED, PAYMENT_FAILED, TIMED_OUT, FAILED = "unmatched", "payment_failed", "timed_out", "failed"


@dataclass
class TripRecord:
    request: TripRequest
    hub_jid: str
    pool_id: str = ""
    proposal: Optional[PoolProposal] = None
    match: Optional[MatchResult] = None
    escrow: Optional[Escrow] = None
    settlement: Optional[Settlement] = None
    refund: Optional[Refund] = None
    outcome: str = ""                              # one of the constants above, "" while running
    reason: str = ""
    requested_at: float = 0.0
    matched_at: float = 0.0
    arrived_at: float = 0.0
    boarded_at: float = 0.0
    dropped_at: float = 0.0
    rating: Optional[Rating] = None
    log: List[str] = field(default_factory=list)

    @property
    def driver(self) -> str:
        return self.match.driver_id if self.match else ""

    @property
    def fare(self) -> float:
        return self.match.rider_fares.get(self.request.rider_id, 0.0) if self.match else 0.0

    @property
    def pickup_delay(self) -> float:
        """Seconds between the match and the boarding, beyond what the driver promised."""
        if not (self.match and self.boarded_at):
            return 0.0
        return (self.boarded_at - self.matched_at) - (self.match.pickup_eta - self.matched_at)


def default_rating(trip: TripRecord, lateness_tolerance: float = 120.0) -> int:
    score = 5
    if trip.pickup_delay > lateness_tolerance:
        score -= 1
    if trip.boarded_at and trip.boarded_at > trip.request.time_window_end:
        score -= 1
    return max(1, score)


class RiderListener(CyclicBehaviour):
    """All incoming protocol traffic; the state machine decides what is legal."""

    async def run(self):
        msg = await self.receive(timeout=0.5)
        r = self.agent
        if msg is not None:
            try:
                await r.on_message(self, msg)
            except Exception:
                logger.exception("%s: error handling %s", r.jid, msg.get_metadata("protocol"))
        await r.check_deadlines(self)


class RiderAgent(BaseAgent):
    role = "rider"

    def __init__(self, jid: str, password: str, hub_jids: Optional[Dict[str, str]] = None,
                 network=None, payment_jid: Optional[str] = None, reputation_jid: Optional[str] = None,
                 rate_with: Optional[Callable[[TripRecord], int]] = None,
                 t_pool_response: float = T_POOL_RESPONSE, patience: float = T_RIDER_PATIENCE,
                 ready_delay: float = READY_DELAY, no_show: bool = False, auto_escrow: bool = True,
                 **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hubs: Dict[str, str] = {h: bare(j) for h, j in (hub_jids or {}).items()}   # hub node id -> JID
        self.network = network
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.reputation_jid = bare(reputation_jid) if reputation_jid else None
        self.rate_with = rate_with or default_rating
        self.t_pool_response, self.patience, self.ready_delay = t_pool_response, patience, ready_delay
        self.no_show, self.auto_escrow = no_show, auto_escrow
        self.state = IDLE
        self.trip: Optional[TripRecord] = None
        self.trips: List[TripRecord] = []
        self.history: List[tuple] = []                 # (sim time, from, to)
        self.ended = asyncio.Event()                   # set each time a trip returns to IDLE
        self._deadline = float("inf")
        self._outbox: List = []

    async def setup(self):
        await super().setup()
        for proto in (Protocol.TRIP_REQUEST, Protocol.POOL_FORMATION, Protocol.ESCROW,
                      Protocol.MATCH_AUCTION, Protocol.TRIP_EXECUTION, Protocol.SETTLEMENT, Protocol.REFUND):
            self.add_behaviour(RiderListener(), make_template(proto))

    # ---- state machine ---------------------------------------------------------------------
    @property
    def me(self) -> str:
        return str(self.jid.bare)

    def go(self, new: str, note: str = "") -> bool:
        if new not in TRANSITIONS[self.state]:
            logger.warning("%s: illegal transition %s -> %s ignored", self.jid, self.state, new)
            return False
        self.history.append((clock.now() if clock.is_started() else 0.0, self.state, new))
        if self.trip and note:
            self.trip.log.append(f"{self.state}->{new}: {note}")
        self.state = new
        return True

    def end_trip(self, outcome: str, reason: str = "") -> None:
        t = self.trip
        if t is None:
            return
        t.outcome, t.reason = outcome, reason
        self.trips.append(t)
        self.trip = None
        self._deadline = float("inf")
        if self.state != IDLE:
            self.go(IDLE, f"{outcome} {reason}".strip())
        logger.info("[%s] trip %s ended: %s %s", self.jid.user, t.request.request_id, outcome, reason)
        self.ended.set()

    def choose_hub(self, destination: str) -> Optional[str]:
        """Nearest hub (by coverage) that covers the destination, among the hubs this rider knows."""
        if self.network is None:
            return next(iter(self.hubs), None)
        for hid in self.network.hubs_covering(destination, clock.now()):
            if hid in self.hubs:
                return hid
        return None

    # ---- TripRequestProtocol (initiator) ----------------------------------------------------
    def request_trip(self, destination: str, max_fare: float, window: float = 3600.0,
                     detour_tolerance: float = 600.0, hub_id: Optional[str] = None,
                     pooling_willingness: bool = False, subsidy_eligible: bool = False,
                     window_start: Optional[float] = None, request_id: Optional[str] = None) -> Optional[TripRequest]:
        """Start a trip. Returns the request, or None if the rider is busy / no hub covers it."""
        if self.state != IDLE:
            return None
        hub_id = hub_id or self.choose_hub(destination)
        if hub_id is None or hub_id not in self.hubs:
            logger.info("[%s] no known hub covers %s", self.jid.user, destination)
            return None
        now = clock.now()
        start = now if window_start is None else window_start
        req = TripRequest(request_id or f"req-{uuid.uuid4().hex[:8]}", self.me, hub_id, destination,
                          start, start + window, detour_tolerance, max_fare, pooling_willingness,
                          subsidy_eligible, now)
        self.trip = TripRecord(req, self.hubs[hub_id], requested_at=now)
        self.ended = asyncio.Event()
        self.go(REQUESTING, "request sent")
        self._deadline = now + self.patience
        self.add_behaviour(_Sender(make_message(self.hubs[hub_id], Perf.REQUEST, Protocol.TRIP_REQUEST,
                                                req, req.request_id)))
        return req

    # ---- incoming ---------------------------------------------------------------------------
    async def on_message(self, beh, msg) -> None:
        t = self.trip
        proto, perf, sender = msg.get_metadata("protocol"), msg.get_metadata("performative"), bare(msg.sender)
        if proto == Protocol.REFUND:                                       # likewise: after the trip has ended
            try:
                back = parse_payload(msg, strict=True)
            except (ValueError, TypeError):
                return
            owner = next((x for x in [t, *reversed(self.trips)] if x is not None and x.pool_id == msg.thread), None)
            if owner and sender == self.payment_jid and isinstance(back, Refund):
                owner.refund = back
            return
        if proto == Protocol.SETTLEMENT:                                   # arrives after the trip has ended
            try:
                settled = parse_payload(msg, strict=True)
            except (ValueError, TypeError):
                return
            owner = next((x for x in [t, *reversed(self.trips)] if x is not None and x.pool_id == msg.thread), None)
            if owner and sender == self.payment_jid and isinstance(settled, Settlement):
                owner.settlement = settled
            return
        if t is None:
            return                                                         # nothing in flight: ignore
        try:
            payload = parse_payload(msg, strict=False)
        except (ValueError, TypeError):
            payload = None
        if proto == Protocol.TRIP_REQUEST:
            await self.on_trip_request(beh, t, sender, perf, payload, msg)
        elif proto == Protocol.POOL_FORMATION:
            await self.on_pool(beh, t, sender, perf, payload)
        elif proto == Protocol.ESCROW:
            self.on_escrow(t, sender, perf, payload, msg)
        elif proto == Protocol.MATCH_AUCTION:
            self.on_auction(t, sender, perf, payload)
        elif proto == Protocol.TRIP_EXECUTION:
            await self.on_execution(beh, t, sender, payload)

    async def on_trip_request(self, beh, t, sender, perf, payload, msg):
        if sender != t.hub_jid:
            return
        if perf == Perf.AGREE and self.state == REQUESTING:
            if isinstance(payload, Pool):                      # immediate (solo) pool
                t.pool_id = payload.pool_id
            self.go(AWAITING_POOL, "agreed" if t.pool_id else "queued for pooling")   # else: pool id comes with the proposal
        elif perf == Perf.REFUSE and self.state == REQUESTING:
            self.end_trip(REFUSED, msg.body or "")
        # inform(request_queued ...) is informational

    async def on_pool(self, beh, t, sender, perf, payload):
        if sender != t.hub_jid:
            return
        if perf == Perf.PROPOSE and isinstance(payload, PoolProposal) and self.state == AWAITING_POOL \
                and payload.request_id == t.request.request_id and t.pool_id in ("", payload.pool_id):
            self.go(DECIDING, "proposal received")
            t.pool_id, t.proposal = payload.pool_id, payload
            reason = self.evaluate_pool(t, payload)
            if reason:
                await beh.send(make_message(t.hub_jid, Perf.REJECT, Protocol.POOL_FORMATION, reason, t.pool_id))
                self.end_trip(DECLINED, reason)
            else:
                await beh.send(make_message(t.hub_jid, Perf.ACCEPT, Protocol.POOL_FORMATION, None, t.pool_id))
                self._deadline = clock.now() + self.patience
        elif perf == Perf.INFORM and isinstance(payload, Pool) and payload.pool_id == t.pool_id \
                and payload.status.value not in ("DISSOLVED", "FORMING") and self.state == DECIDING:   # pool_confirmed
            self.go(AWAITING_MATCH, "pool confirmed")
            if self.payment_jid and self.auto_escrow:
                esc = EscrowRequest(t.request.request_id, self.me, t.pool_id, t.request.max_fare,
                                    t.request.subsidy_eligibility, t.hub_jid)
                await beh.send(make_request(self.payment_jid, Protocol.ESCROW, esc, t.request.request_id))
        elif perf == Perf.INFORM and isinstance(payload, Pool) and payload.status.value == "FORMING" \
                and payload.pool_id == t.pool_id and self.state == DECIDING:       # a co-rider left: back to the queue
            t.pool_id, t.proposal = "", None
            self.go(AWAITING_POOL, "re-pooling")
            self._deadline = clock.now() + self.patience
        elif perf == Perf.INFORM and msg_is_dissolved(payload) and self.state in (AWAITING_POOL, DECIDING):
            self.end_trip(DISSOLVED, "pool dissolved")

    def evaluate_pool(self, t: TripRecord, p: PoolProposal) -> str:
        """'' to accept, else the reason to reject (fare, wait and detour against the request)."""
        r = t.request
        if p.split_fare > r.max_fare + 1e-9:
            return "fare_too_high"
        if clock.now() + p.estimated_wait > r.time_window_end:
            return "wait_too_long"
        if p.added_detour > r.detour_tolerance + 1e-9:
            return "detour_too_large"
        return ""

    def on_escrow(self, t, sender, perf, payload, msg):
        if sender != self.payment_jid:
            return
        if perf == Perf.INFORM and isinstance(payload, Escrow):
            t.escrow = payload
        elif perf == Perf.REFUSE:
            t.log.append(f"escrow refused: {msg.body}")
            if self.state == AWAITING_MATCH:
                self.end_trip(PAYMENT_FAILED, msg.body or "")

    def on_auction(self, t, sender, perf, payload):
        if sender != t.hub_jid or perf != Perf.INFORM:
            return
        if isinstance(payload, MatchResult) and self.state == AWAITING_MATCH and payload.pool_id == t.pool_id:
            t.match, t.matched_at = payload, clock.now()
            self.go(DRIVER_EN_ROUTE, f"matched with {payload.driver_id}")
            self._deadline = float("inf")
        elif isinstance(payload, MatchResult) and t.match is None and payload.pool_id == t.pool_id \
                and self.state in (DRIVER_EN_ROUTE, IN_TRIP):              # the driver was faster than the hub's message
            t.match, t.matched_at = payload, clock.now()
        elif isinstance(payload, AuctionOutcome) and not payload.awarded:
            reason = payload.reason.value if payload.reason else ""
            if self.state == AWAITING_MATCH:
                self.end_trip(UNMATCHED, reason)
            elif self.state in (DRIVER_EN_ROUTE, IN_TRIP):                  # the driver failed after the match
                self.end_trip(FAILED, reason)

    async def on_execution(self, beh, t, sender, payload):
        # the driver can announce its arrival a moment before the hub's MatchResult reaches us
        if t.match is not None and sender != bare(t.match.driver_id):
            return
        if isinstance(payload, DriverArriving) and self.state in (AWAITING_MATCH, DRIVER_EN_ROUTE):
            t.arrived_at = clock.now()
            if self.no_show:
                return
            await asyncio.sleep(clock.to_real(self.ready_delay))
            await beh.send(make_message(sender, Perf.INFORM, Protocol.TRIP_EXECUTION,
                                        RiderReady(payload.request_id, payload.pool_id, clock.now()), payload.pool_id))
        elif isinstance(payload, RiderBoarded) and self.state in (AWAITING_MATCH, DRIVER_EN_ROUTE):
            t.boarded_at = clock.now()
            if self.state == AWAITING_MATCH:                      # boarding beat the MatchResult message
                self.go(DRIVER_EN_ROUTE, "driver already here")
            self.go(IN_TRIP, "boarded")
        elif isinstance(payload, RiderDroppedOff) and self.state == IN_TRIP:
            t.dropped_at = clock.now()
            self.go(RATING, "dropped off")
            await self.rate(beh, t)

    # ---- ReputationProtocol (rater) -----------------------------------------------------------
    async def rate(self, beh, t: TripRecord) -> None:
        score = int(min(5, max(1, self.rate_with(t))))
        if self.reputation_jid:
            t.rating = Rating(f"trip-{t.pool_id}", self.me, bare(t.driver), score)
            await beh.send(make_inform(self.reputation_jid, Protocol.REPUTATION, t.rating, t.rating.trip_id))
        self.end_trip(COMPLETED)

    # ---- patience --------------------------------------------------------------------------------
    async def check_deadlines(self, beh) -> None:
        if self.trip is not None and self.state != IDLE and clock.now() > self._deadline:
            if self.state in (REQUESTING, AWAITING_POOL, DECIDING, AWAITING_MATCH):
                self.end_trip(TIMED_OUT, f"no progress in {self.state}")


def msg_is_dissolved(payload) -> bool:
    return isinstance(payload, Pool) and payload.status.value == "DISSOLVED"


class _Sender(OneShotBehaviour):
    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    async def run(self):
        await self.send(self.msg)

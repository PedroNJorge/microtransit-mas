"""
src/agents/microhub.py   MicrohubAgent (Role: Microhub, Sub-org: Coordination). Milestone 5.

What the hub does in this milestone
  1. TripRequestReceiver   rider -> request(TripRequest) -> hub answers agree(Pool) (the rider needs
                           the pool_id for its escrow) or refuse(reason). One request = one pool
                           (real pooling arrives with the pool-formation milestone).
  2. EscrowWatcher         Payment tells the hub `escrow_confirmed` (inform Escrow HELD, ID-02).
                           When every rider of the pool is escrowed, the auction starts.
                           No escrow within T_escrow (ID-09) -> the pool is dissolved, rider told.
                           If `payment_jid` is None the escrow gate is skipped (stand-alone demos).
  3. MatchAuctionFSM       protocols/match_auction.py. A failed attempt is retried (wider detour)
                           up to `max_attempts`; the final failure refunds the escrows (FE-01).
  4. LateBidHandler        bids that arrive for a pool whose auction is closed are rejected (GC-09).

Roles of the data it keeps: `pools` (live and finished), `matches` (pool_id -> MatchResult).
Drivers: for M5 the hub is given the JIDs of the drivers to call (`driver_jids`); DriverStatus-
based selection of nearby free drivers arrives with the Driver milestone.
Reputation: `reputations` is the hub's own record (jid -> 1..5); the Reputation agent will feed it.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from spade.behaviour import CyclicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.auction import AuctionRules
from common.messages import (
    make_auction_failed, make_message, make_reject_proposal, make_reply, make_request, make_template,
)
from common.ontology import (
    AuctionOutcome, Escrow, EscrowStatus, FailureReason, MatchResult, Perf, Pool, PoolStatus, Protocol,
    TripFailed, TripRequest, parse_payload,
)
from common.settings import (
    AUCTION_PRICING, MAX_AUCTION_ATTEMPTS, T_AUCTION, T_AWARD, T_ESCROW, VALUE_OF_DETOUR,
    VALUE_OF_REPUTATION, VALUE_OF_TIME, WIDEN_DETOUR,
)
from protocols.match_auction import AuctionContext, MATCHED, MatchAuctionFSM, RETRY
from protocols.payment_common import bare

logger = logging.getLogger(__name__)


@dataclass
class PoolRecord:
    pool: Pool
    rider_jids: Dict[str, str]                           # request_id -> rider JID
    escrows: Dict[str, str] = field(default_factory=dict)    # request_id -> escrow_id
    escrow_deadline: float = 0.0
    state: str = "AWAITING_ESCROW"        # AWAITING_ESCROW | AUCTIONING | ASSIGNED | UNMATCHED | DISSOLVED
    contexts: List[AuctionContext] = field(default_factory=list)


class TripRequestReceiver(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        try:
            req = parse_payload(msg, strict=True)
            if not isinstance(req, TripRequest):
                raise ValueError(f"expected TripRequest, got {type(req).__name__}")
        except (ValueError, TypeError) as e:
            await self.send(make_reply(msg, Perf.REFUSE, f"malformed_request:{e}", Protocol.TRIP_REQUEST))
            return
        sender = bare(msg.sender)
        problem = hub.check_request(req, sender)
        if problem:
            hub.log(f"request {req.request_id} refused: {problem}")
            await self.send(make_reply(msg, Perf.REFUSE, problem, Protocol.TRIP_REQUEST))
            return
        rec = hub.open_pool(req, sender)
        await self.send(make_message(sender, Perf.AGREE, Protocol.TRIP_REQUEST, rec.pool, req.request_id))
        if hub.payment_jid is None:
            hub.start_auction(rec)


class EscrowWatcher(CyclicBehaviour):
    """escrow_confirmed / escrow_failed from Payment, plus the T_escrow timer (ID-09)."""

    async def run(self):
        hub = self.agent
        msg = await self.receive(timeout=1)
        if msg is not None:
            await self.handle(msg)
        for rec in list(hub.pools.values()):
            if rec.state == "AWAITING_ESCROW" and clock.now() >= rec.escrow_deadline:
                hub.log(f"pool {rec.pool.pool_id}: no escrow within T_escrow; dissolved")
                await self.dissolve(rec, FailureReason.TIMEOUT)

    async def handle(self, msg):
        hub = self.agent
        if bare(msg.sender) != hub.payment_jid:          # only Payment may confirm money
            hub.log(f"ignored escrow message from {bare(msg.sender)}")
            return
        rec = hub.pool_of_request(msg.thread)
        if rec is None:
            return
        if msg.get_metadata("performative") == Perf.REFUSE:
            hub.log(f"pool {rec.pool.pool_id}: escrow_failed ({msg.body})")
            if rec.state == "AWAITING_ESCROW":
                await self.dissolve(rec, FailureReason.NO_MATCH)
            return
        try:
            esc = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if not isinstance(esc, Escrow) or esc.status != EscrowStatus.HELD or esc.pool_id != rec.pool.pool_id:
            return
        rec.escrows[msg.thread] = esc.escrow_id
        hub.log(f"pool {rec.pool.pool_id}: escrow_confirmed {esc.escrow_id} "
                f"({len(rec.escrows)}/{len(rec.rider_jids)})")
        if rec.state == "AWAITING_ESCROW" and len(rec.escrows) == len(rec.rider_jids):
            hub.start_auction(rec)

    async def dissolve(self, rec: PoolRecord, reason: FailureReason):
        hub = self.agent
        rec.state = "DISSOLVED"
        rec.pool.status = PoolStatus.DISSOLVED
        for rider in rec.rider_jids.values():
            await self.send(make_auction_failed(rider, rec.pool.pool_id, reason))
        if rec.escrows:                                  # someone did pay: give it back
            await self.send(make_request(
                hub.payment_jid, Protocol.REFUND,
                TripFailed(rec.pool.pool_id, reason, str(hub.jid.bare), list(rec.escrows.values())),
                thread=rec.pool.pool_id))


class LateBidHandler(CyclicBehaviour):
    """A bid for an auction that is no longer running gets an answer too (GC-09).
    Silent while the auction is live: the FSM itself handles those."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        rec = hub.pools.get(msg.thread or "")
        if rec is None or rec.state == "AUCTIONING":
            return
        sender = bare(msg.sender)
        hub.log(f"late bid from {sender} for closed pool {msg.thread}")
        await self.send(make_reject_proposal(
            sender, AuctionOutcome(msg.thread, False, None, FailureReason.TIMEOUT)))


class MicrohubAgent(BaseAgent):
    role = "microhub"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, hub_id: str = "", driver_jids: Optional[List[str]] = None,
                 payment_jid: Optional[str] = None, network=None, rules: Optional[AuctionRules] = None,
                 t_auction: float = T_AUCTION, t_award: float = T_AWARD, t_escrow: float = T_ESCROW,
                 max_attempts: int = MAX_AUCTION_ATTEMPTS, widen_detour: float = WIDEN_DETOUR,
                 fare_cap: Optional[float] = None, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_id = hub_id                  # node id in the road network (== MicrohubNode.id)
        self.driver_jids: List[str] = [bare(j) for j in (driver_jids or [])]
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.network = network
        self.rules = rules or AuctionRules(
            pricing=AUCTION_PRICING, value_of_time=VALUE_OF_TIME,
            value_of_detour=VALUE_OF_DETOUR, value_of_reputation=VALUE_OF_REPUTATION)
        self.t_auction, self.t_award, self.t_escrow = t_auction, t_award, t_escrow
        self.max_attempts, self.widen_detour, self.fare_cap = max_attempts, widen_detour, fare_cap
        self.reputations: Dict[str, float] = {}
        self.pools: Dict[str, PoolRecord] = {}
        self.matches: Dict[str, MatchResult] = {}
        self.finished: List[AuctionContext] = []
        self._seq = 0

    # ---- helpers used by behaviours -----------------------------------------------------
    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.hub_id or self.jid, text)

    def account(self, ident: str) -> str:
        ident = bare(ident)
        return ident if "@" in ident else f"{ident}@{self.jid.domain}"

    def reputation_of(self, driver_jid: str) -> Optional[float]:
        return self.reputations.get(bare(driver_jid))

    def pool_of_request(self, request_id: str) -> Optional[PoolRecord]:
        for rec in self.pools.values():
            if request_id in rec.rider_jids:
                return rec
        return None

    def check_request(self, req: TripRequest, sender: str) -> str:
        if self.account(req.rider_id) != sender:
            return "not_authorised"
        if self.pool_of_request(req.request_id) is not None:
            return "duplicate_request"
        if self.network is not None:
            if not self.network.has_node(req.destination_home):
                return "unknown_destination"
            if self.hub_id and self.hub_id not in self.network.hubs_covering(req.destination_home, 0.0):
                return "outside_coverage"
        return ""

    def open_pool(self, req: TripRequest, sender: str) -> PoolRecord:
        self._seq += 1
        pool = Pool(f"{self.hub_id}-pool-{self._seq:04d}", self.hub_id, [req],
                    PoolStatus.LOCKED, clock.now())
        rec = PoolRecord(pool, {req.request_id: sender},
                         escrow_deadline=clock.now() + self.t_escrow)
        self.pools[pool.pool_id] = rec
        self.log(f"request {req.request_id} from {sender} -> {pool.pool_id}")
        return rec

    def start_auction(self, rec: PoolRecord, attempt: int = 0) -> None:
        rec.state = "AUCTIONING"
        rec.pool.status = PoolStatus.AUCTIONING
        ctx = AuctionContext(rec.pool, dict(rec.rider_jids), list(rec.escrows.values()), attempt)
        rec.contexts.append(ctx)
        self.add_behaviour(MatchAuctionFSM(ctx),
                           make_template(Protocol.MATCH_AUCTION, thread=rec.pool.pool_id))

    def on_auction_finished(self, ctx: AuctionContext) -> None:
        """Called by the FSM's END state."""
        rec = self.pools[ctx.pool_id]
        self.finished.append(ctx)
        if ctx.outcome == MATCHED:
            rec.state, rec.pool.status = "ASSIGNED", PoolStatus.ASSIGNED
            self.matches[ctx.pool_id] = ctx.match
        elif ctx.outcome == RETRY:
            self.start_auction(rec, ctx.attempt + 1)
        else:
            rec.state, rec.pool.status = "UNMATCHED", PoolStatus.DISSOLVED

    async def setup(self):
        await super().setup()
        self.add_behaviour(TripRequestReceiver(),
                           make_template(Protocol.TRIP_REQUEST, Perf.REQUEST))
        if self.payment_jid:
            self.add_behaviour(EscrowWatcher(), make_template(Protocol.ESCROW))
        self.add_behaviour(LateBidHandler(), make_template(Protocol.MATCH_AUCTION, Perf.PROPOSE))

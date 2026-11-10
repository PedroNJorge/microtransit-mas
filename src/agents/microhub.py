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
    make_auction_failed, make_inform, make_message, make_pool_proposal, make_reject_proposal, make_reply,
    make_request, make_template,
)
from common.ontology import (
    AuctionOutcome, DriverState, DriverStatus, Escrow, EscrowStatus, FailureReason, MatchResult, Perf,
    Pool, PoolProposal, PoolStatus, Protocol, ReputationScore, SettlementComplete, TripCompleted, TripFailed,
    TripRequest, parse_payload,
)
from common.settings import (
    AUCTION_PRICING, MAX_AUCTION_ATTEMPTS, T_AUCTION, T_AWARD, T_ESCROW, VALUE_OF_DETOUR,
    T_POOL_RESPONSE, T_STATUS, VALUE_OF_REPUTATION, VALUE_OF_TIME, WIDEN_DETOUR,
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
    state: str = "PROPOSED"               # PROPOSED | AWAITING_ESCROW | AUCTIONING | ASSIGNED | UNMATCHED | DISSOLVED
                                          # | COMPLETED | DRIVER_FAILED | CANCELLED
    contexts: List[AuctionContext] = field(default_factory=list)
    no_shows: List[str] = field(default_factory=list)       # rider JIDs reported by the driver
    proposal_deadline: float = 0.0


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
        await self.send(make_message(sender, Perf.INFORM, Protocol.TRIP_REQUEST,
                                     f"request_queued:{hub.batch_estimate():.0f}", req.request_id))
        if hub.propose_pools:                                           # PoolFormationProtocol
            await self.send(make_pool_proposal(sender, hub.proposal_for(rec, req)))
        else:
            hub.confirm_pool(rec)
            if hub.payment_jid is None:
                hub.start_auction(rec)


class PoolResponses(CyclicBehaviour):
    """PoolFormationProtocol, hub side: accept / reject / silence (= reject after T_pool_response)."""

    async def run(self):
        hub = self.agent
        msg = await self.receive(timeout=1)
        if msg is not None:
            rec = hub.pools.get(msg.thread or "")
            sender, perf = bare(msg.sender), msg.get_metadata("performative")
            if rec is not None and rec.state == "PROPOSED" and sender in rec.rider_jids.values():
                if perf == Perf.ACCEPT:
                    hub.confirm_pool(rec)
                    await self.send(make_inform(sender, Protocol.POOL_FORMATION, rec.pool, rec.pool.pool_id))
                    if hub.payment_jid is None:
                        hub.start_auction(rec)
                elif perf == Perf.REJECT:
                    hub.log(f"pool {rec.pool.pool_id} rejected by the rider ({msg.body})")
                    await self.dissolve(rec, sender)
        for rec in list(hub.pools.values()):
            if rec.state == "PROPOSED" and clock.now() >= rec.proposal_deadline:
                hub.log(f"pool {rec.pool.pool_id}: no answer to the proposal; dissolved")
                await self.dissolve(rec, next(iter(rec.rider_jids.values())))

    async def dissolve(self, rec: PoolRecord, rider: str):
        rec.state, rec.pool.status = "DISSOLVED", PoolStatus.DISSOLVED
        await self.send(make_inform(rider, Protocol.POOL_FORMATION, rec.pool, rec.pool.pool_id))


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


class DriverStatusReceiver(CyclicBehaviour):
    """DriverStatusProtocol: keep the live driver registry (CC-07/CC-08, GC-14)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        try:
            st = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(st, DriverStatus) and hub.account(st.driver_id) == bare(msg.sender):
            hub.registry[bare(msg.sender)] = (st, clock.now())


class TripReports(CyclicBehaviour):
    """TripExecutionProtocol, hub side: TripCompleted -> settle; TripFailed -> refund (FE-*)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub, sender = self.agent, bare(msg.sender)
        try:
            rep = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        rec = hub.pools.get(getattr(rep, "pool_id", "") or "")
        match = hub.matches.get(rec.pool.pool_id) if rec else None
        if rec is None or match is None or sender != bare(match.driver_id):
            hub.log(f"ignored trip report from {sender}")                # only the assigned driver
            return
        try:                                  # an unhandled error would kill this behaviour for good
            if isinstance(rep, TripFailed):
                await self.failed(rec, rep, sender)
            elif isinstance(rep, TripCompleted) and rec.state == "ASSIGNED":
                await self.completed(rec, match, rep)
        except Exception:
            logger.exception("%s: error handling a trip report for %s", hub.jid, rec.pool.pool_id)

    async def completed(self, rec: PoolRecord, match: MatchResult, rep: TripCompleted):
        hub = self.agent
        riders = [r for r in rep.rider_ids if r in match.rider_fares]           # never trust the driver's numbers
        rec.state, rec.pool.status = "COMPLETED", PoolStatus.COMPLETED
        hub.log(f"pool {rec.pool.pool_id} completed by {match.driver_id}; settling {len(riders)} rider(s)")
        if hub.reputation_jid:                                  # trip registry: who may rate whom
            await self.send(make_inform(hub.reputation_jid, Protocol.REPUTATION,
                                        TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders,
                                                      rep.completed_at), rec.pool.pool_id))
        if hub.payment_jid:
            await self.send(make_inform(
                hub.payment_jid, Protocol.SETTLEMENT,
                TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders, rep.completed_at,
                              rep.distance_km, {r: match.rider_fares[r] for r in riders}),
                rec.pool.pool_id))
            await self.refund(rec, [j for j in rec.no_shows if j not in riders], FailureReason.RIDER_CANCELLED)

    async def failed(self, rec: PoolRecord, rep: TripFailed, sender: str):
        hub = self.agent
        if rep.reason == FailureReason.RIDER_CANCELLED:                    # FE-05: driver reports a no-show
            jid = bare(rep.failed_agent_id or "")
            if jid in rec.rider_jids.values() and jid not in rec.no_shows:
                rec.no_shows.append(jid)
                hub.log(f"pool {rec.pool.pool_id}: rider {jid} did not show up")
            if len(rec.no_shows) == len(rec.rider_jids) and rec.state == "ASSIGNED":
                rec.state, rec.pool.status = "CANCELLED", PoolStatus.DISSOLVED
                await self.refund(rec, rec.no_shows, FailureReason.RIDER_CANCELLED)
        elif rep.reason == FailureReason.DRIVER_FAILED and rec.state == "ASSIGNED":
            rec.state, rec.pool.status = "DRIVER_FAILED", PoolStatus.DISSOLVED
            hub.log(f"pool {rec.pool.pool_id}: driver {sender} failed; refunding")
            for rider in rec.rider_jids.values():
                await self.send(make_auction_failed(rider, rec.pool.pool_id, FailureReason.DRIVER_FAILED))
            await self.refund(rec, list(rec.rider_jids.values()), FailureReason.DRIVER_FAILED)

    async def refund(self, rec: PoolRecord, rider_jids, reason: FailureReason):
        hub = self.agent
        by_jid = {j: r for r, j in rec.rider_jids.items()}
        ids = [rec.escrows[by_jid[j]] for j in rider_jids if j in by_jid and by_jid[j] in rec.escrows]
        if ids and hub.payment_jid:
            await self.send(make_request(hub.payment_jid, Protocol.REFUND,
                                         TripFailed(rec.pool.pool_id, reason, str(hub.jid.bare), ids),
                                         thread=rec.pool.pool_id))


class SettlementReceiver(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        if bare(msg.sender) != hub.payment_jid:
            return
        try:
            done = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(done, SettlementComplete):
            hub.settlements[done.pool_id] = done
            hub.log(f"pool {done.pool_id} settled: driver paid {done.total_paid:.2f}")


class ReputationReceiver(CyclicBehaviour):
    """reputation_update from the Reputation Agent -> the score the auction uses (never the driver's claim)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        if bare(msg.sender) != hub.reputation_jid:
            return
        try:
            score = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(score, ReputationScore):
            hub.reputations[bare(score.agent_id)] = score.score
            hub.log(f"reputation of {score.agent_id}: {score.score:.2f} ({score.num_ratings} ratings)")


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
                 fare_cap: Optional[float] = None, t_status: float = T_STATUS,
                 reputation_jid: Optional[str] = None, propose_pools: bool = True,
                 t_pool_response: float = T_POOL_RESPONSE, **kwargs):
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
        self.settlements: Dict[str, SettlementComplete] = {}
        self.registry: Dict[str, tuple] = {}            # driver JID -> (DriverStatus, received_at)
        self.t_status = t_status
        self.reputation_jid = bare(reputation_jid) if reputation_jid else None
        self.propose_pools, self.t_pool_response = propose_pools, t_pool_response
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

    def eligible_drivers(self, now: float) -> List[str]:
        """CC-08: do not call drivers known to hold a lock or be busy. A driver with no (fresh)
        status is still called; it refuses `locked` itself if it must (CC-09)."""
        out = []
        for jid in self.driver_jids:
            entry = self.registry.get(jid)
            if entry and now - entry[1] <= 3 * self.t_status and entry[0].state != DriverState.IDLE:
                continue
            out.append(jid)
        return out

    def hub_eta(self, driver_jid: str, pickup: str, now: float) -> Optional[float]:
        """CC-10: the hub's own ETA from the driver's last reported position; None if unknown."""
        entry = self.registry.get(bare(driver_jid))
        if self.network is None or entry is None:
            return None
        try:
            eta = self.network.travel_time(entry[0].location, pickup, now)
        except KeyError:
            return None
        return None if eta == float("inf") else eta

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
                         proposal_deadline=clock.now() + self.t_pool_response)
        self.pools[pool.pool_id] = rec
        self.log(f"request {req.request_id} from {sender} -> {pool.pool_id}")
        return rec

    def batch_estimate(self) -> float:
        """Seconds until the pool is proposed (no batching window yet: pools are single trips)."""
        return 0.0

    def proposal_for(self, rec: PoolRecord, req: TripRequest) -> PoolProposal:
        """What the rider is told before committing: the route, the fare CEILING (the reserve of
        the auction, so the final fare can only be lower) and the expected wait."""
        route, route_time = [self.hub_id, req.destination_home], 0.0
        if self.network is not None:
            planned = self.network.best_stop_order(self.hub_id, [req.destination_home], clock.now())
            if planned:
                route, route_time = planned
        etas = [e for e in (self.hub_eta(j, self.hub_id, clock.now()) for j in self.eligible_drivers(clock.now()))
                if e is not None]
        wait = min(etas) if etas else 0.0
        ceiling = req.max_fare if self.fare_cap is None else min(req.max_fare, self.fare_cap)
        return PoolProposal(rec.pool.pool_id, req.request_id, route, ceiling, wait, 0.0)

    def confirm_pool(self, rec: PoolRecord) -> None:
        rec.state = "AWAITING_ESCROW"
        rec.escrow_deadline = clock.now() + self.t_escrow
        rec.pool.status = PoolStatus.LOCKED

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
        self.add_behaviour(PoolResponses(), make_template(Protocol.POOL_FORMATION))
        if self.reputation_jid:
            self.add_behaviour(ReputationReceiver(), make_template(Protocol.REPUTATION, Perf.INFORM))
        self.add_behaviour(DriverStatusReceiver(), make_template(Protocol.DRIVER_STATUS, Perf.INFORM))
        self.add_behaviour(TripReports(), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))
        if self.payment_jid:
            self.add_behaviour(EscrowWatcher(), make_template(Protocol.ESCROW))
            self.add_behaviour(SettlementReceiver(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
        self.add_behaviour(LateBidHandler(), make_template(Protocol.MATCH_AUCTION, Perf.PROPOSE))

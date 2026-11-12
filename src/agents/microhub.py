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

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.auction import AuctionRules, ceil_cents
from common.policy import PolicyBook
from common.messages import (
    make_auction_failed, make_inform, make_message, make_pool_proposal, make_reject_proposal, make_reply,
    make_request, make_template,
)
from common.ontology import (
    AuctionOutcome, AuditData, AuditReport, AuditRequest, AuditTrip, DriverState, DriverStatus, Escrow, EscrowStatus, FailureReason, MatchResult, Perf,
    Penalty, Policy, Pool, Refund, PoolProposal, PoolStatus, Protocol, ReputationScore, SettlementComplete,
    TripCompleted, TripFailed, TripRequest, parse_payload,
)
from common.settings import (
    AUCTION_PRICING, MAX_AUCTION_ATTEMPTS, T_AUCTION, T_AWARD, T_ESCROW, VALUE_OF_DETOUR,
    T_POOL_RESPONSE, T_STATUS, VALUE_OF_REPUTATION, VALUE_OF_TIME, WIDEN_DETOUR,
)
from common.pooling import GroupPlan, fare_ceiling, reference_solo_fare, split_fare_weighted
from common.settings import (
    DETOUR_WEIGHT, DRIVER_BASE_COST, DRIVER_COST_PER_SECOND, MAX_BATCH, MAX_POOL_SIZE, PICKUP_ALLOWANCE,
    POOL_SOLVER, T_POOL,
)
from protocols.match_auction import AuctionContext, MATCHED, MatchAuctionFSM, RETRY
from protocols.pool_formation import BatchWindow, QueuedRequest, handle_decline, window_closes_at
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
    plan: Optional[GroupPlan] = None                         # the group's exact plan (pooling, M9)
    accepted: set = field(default_factory=set)               # request ids that accepted the proposal


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
        if hub.batching and req.pooling_willingness:                    # wait for the batch window
            hub.queue.append(QueuedRequest(req, sender, clock.now()))
            hub.log(f"request {req.request_id} queued for pooling ({len(hub.queue)} waiting)")
            await self.send(make_message(sender, Perf.AGREE, Protocol.TRIP_REQUEST, "queued", req.request_id))
            await self.send(make_message(sender, Perf.INFORM, Protocol.TRIP_REQUEST,
                                         f"request_queued:{hub.batch_estimate():.0f}", req.request_id))
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
    """PoolFormationProtocol, hub side: accept / reject / silence (= reject after T_pool_response).
    A pool is confirmed only when EVERY rider accepted; otherwise see protocols.pool_formation."""

    async def run(self):
        hub = self.agent
        msg = await self.receive(timeout=1)
        try:
            if msg is not None:
                rec = hub.pools.get(msg.thread or "")
                sender, perf = bare(msg.sender), msg.get_metadata("performative")
                rid = next((r for r, j in rec.rider_jids.items() if j == sender), None) if rec else None
                if rec is not None and rec.state == "PROPOSED" and rid is not None:
                    if perf == Perf.ACCEPT:
                        rec.accepted.add(rid)
                        if rec.accepted == set(rec.rider_jids):
                            hub.confirm_pool(rec)
                            for jid in rec.rider_jids.values():
                                await self.send(make_inform(jid, Protocol.POOL_FORMATION, rec.pool, rec.pool.pool_id))
                            if hub.payment_jid is None:
                                hub.start_auction(rec)
                    elif perf == Perf.REJECT:
                        hub.log(f"pool {rec.pool.pool_id} rejected by {sender} ({msg.body})")
                        await handle_decline(self, hub, rec, [rid])
            for rec in list(hub.pools.values()):
                if rec.state == "PROPOSED" and clock.now() >= rec.proposal_deadline:
                    silent = [r for r in rec.rider_jids if r not in rec.accepted]
                    hub.log(f"pool {rec.pool.pool_id}: no answer from {silent}; they are dropped")
                    await handle_decline(self, hub, rec, silent)
        except Exception:
            logger.exception("%s: error handling a pool response", hub.jid)


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
                                                      clock.now()), rec.pool.pool_id))
        await self.send(make_inform(match.driver_id, Protocol.TRIP_EXECUTION,       # ID-08: now it may rate
                                    TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders,
                                                  clock.now()), rec.pool.pool_id))
        if hub.payment_jid:
            await self.send(make_inform(
                hub.payment_jid, Protocol.SETTLEMENT,
                TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders, rep.completed_at,
                              rep.distance_km, {r: match.rider_fares[r] for r in riders},
                              hub.solo_fares(rec, riders)),
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
            hub.reputation_flags[bare(score.agent_id)] = score.pending_settlement
            hub.log(f"reputation of {score.agent_id}: {score.score:.2f} ({score.num_ratings} ratings)")


class RefundReceiver(CyclicBehaviour):
    """Payment's confirmation that an escrow went back to the rider."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None or bare(msg.sender) != self.agent.payment_jid:
            return
        try:
            back = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(back, Refund):
            self.agent.refunds.append(back)


class PolicyReceiver(CyclicBehaviour):
    """PolicyProtocol (RI-04): acknowledge with agree, or refuse(reason). Only the Regulator's word counts."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        try:
            policy = parse_payload(msg, strict=True)
            if not isinstance(policy, Policy):
                raise ValueError("not a Policy")
            if not hub.regulator_jid or bare(msg.sender) != hub.regulator_jid:
                reason = "unknown_regulator"
            else:
                ack = hub.policies.offer(policy)
                reason = "" if ack.ok else ack.reason
        except (ValueError, TypeError):
            reason = "malformed"
        if not reason:
            hub.log(f"policy v{policy.version} acknowledged (cap {policy.fare_caps.get('max')}, "
                    f"from t={policy.effective_from:.0f})")
        await self.send(make_message(str(msg.sender), Perf.REFUSE if reason else Perf.AGREE,
                                     Protocol.POLICY, reason or None, msg.thread))


class AuditResponder(CyclicBehaviour):
    """AuditProtocol (RI-05): answer the Regulator's audit request, and keep its verdicts."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        if not hub.regulator_jid or bare(msg.sender) != hub.regulator_jid:
            return
        try:
            body = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(body, AuditRequest) and msg.get_metadata("performative") == Perf.REQUEST:
            if not hub.audit_logs_available:
                await self.send(make_message(str(msg.sender), Perf.REFUSE, Protocol.AUDIT, "logs_unavailable",
                                             body.audit_id))
            else:
                await self.send(make_inform(str(msg.sender), Protocol.AUDIT, hub.audit_data(body), body.audit_id))
        elif isinstance(body, AuditReport):
            hub.audit_reports.append(body)
            hub.log(f"audit {msg.thread}: {'passed' if body.compliant else 'NOT compliant'}")
        elif isinstance(body, Penalty):
            hub.penalties.append(body)
            hub.log(f"penalty imposed: {body.reason} ({body.amount:.2f})")


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


class _Wake(OneShotBehaviour):
    """Re-checks the auction queue once a delayed retry is due."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    async def run(self):
        await clock.sim_sleep(self.delay + 0.1)
        self.agent._waking = False
        self.agent._next_auction()


class MicrohubAgent(BaseAgent):
    role = "microhub"
    subscribes_to_traffic = True

    def __init__(self, jid: str, password: str, hub_id: str = "", driver_jids: Optional[List[str]] = None,
                 payment_jid: Optional[str] = None, network=None, rules: Optional[AuctionRules] = None,
                 t_auction: float = T_AUCTION, t_award: float = T_AWARD, t_escrow: float = T_ESCROW,
                 max_attempts: int = MAX_AUCTION_ATTEMPTS, widen_detour: float = WIDEN_DETOUR,
                 fare_cap: Optional[float] = None, t_status: float = T_STATUS,
                 reputation_jid: Optional[str] = None, propose_pools: bool = True,
                 t_pool_response: float = T_POOL_RESPONSE, regulator_jid: Optional[str] = None,
                 audit_logs_available: bool = True, t_pool: float = 0.0, max_pool_size: int = MAX_POOL_SIZE,
                 max_batch: int = MAX_BATCH, solver: str = POOL_SOLVER, detour_weight: float = DETOUR_WEIGHT,
                 solo_fallback: bool = True, reserve_margin: Optional[float] = None,
                 pickup_allowance: float = PICKUP_ALLOWANCE, max_pickup_eta: Optional[float] = None,
                 retry_delay: float = 0.0, window_slack: float = 0.0, **kwargs):
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
        self.regulator_jid = bare(regulator_jid) if regulator_jid else None
        self.audit_logs_available = audit_logs_available
        self.policies = PolicyBook()
        self.audit_reports: List[AuditReport] = []
        self.refunds: List[Refund] = []
        self.penalties: List[Penalty] = []
        self.reputation_flags: Dict[str, bool] = {}
        self.finished: List[AuctionContext] = []
        self._seq = 0
        # pool formation (M9): t_pool = 0 disables batching (every request is an immediate solo pool)
        self.t_pool, self.max_pool_size, self.max_batch = t_pool, max_pool_size, max_batch
        self.solver, self.detour_weight, self.solo_fallback = solver, detour_weight, solo_fallback
        self.queue: List[QueuedRequest] = []
        self.reserve_margin, self.pickup_allowance = reserve_margin, pickup_allowance
        self.max_pickup_eta, self.retry_delay, self.window_slack = max_pickup_eta, retry_delay, window_slack
        self._waking = False
        self.auction_queue: List[tuple] = []
        self.auction_running = False

    # ---- helpers used by behaviours -----------------------------------------------------
    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.hub_id or self.jid, text)

    def account(self, ident: str) -> str:
        ident = bare(ident)
        return ident if "@" in ident else f"{ident}@{self.jid.domain}"

    def reputation_of(self, driver_jid: str) -> Optional[float]:
        return self.reputations.get(bare(driver_jid))

    def eligible_drivers(self, now: float, attempt: int = 0) -> List[str]:
        """CC-08: do not call drivers known to hold a lock or be busy, nor (if `max_pickup_eta` is
        set) drivers too far from the hub to be useful; each retry widens that radius. A driver with
        no (fresh) status is still called; it refuses `locked` itself if it must (CC-09)."""
        out = []
        for jid in self.driver_jids:
            entry = self.registry.get(jid)
            if entry and now - entry[1] <= 3 * self.t_status:
                if entry[0].state != DriverState.IDLE:
                    continue
                if self.max_pickup_eta is not None:
                    eta = self.hub_eta(jid, self.hub_id, now)
                    if eta is not None and eta > self.max_pickup_eta + attempt * 300.0:
                        continue
            out.append(jid)
        return out

    def retry_wait(self, ctx: AuctionContext) -> float:
        """How long to wait before re-running a failed auction. Normally `retry_delay`; but if the
        drivers refused because they were locked by ANOTHER hub's open auction (or none was free to
        call), the lock lasts up to t_auction + t_award, so waiting less is pointless."""
        if not self.retry_delay:
            return 0.0
        locked = sum(1 for r in ctx.refusals.values() if r == "locked")
        if not ctx.recipients or locked >= max(1, len(ctx.recipients) // 2):
            return max(self.retry_delay, self.t_auction + self.t_award)
        return self.retry_delay

    def tariff_reserve(self, reqs, stop_order, attempt: int) -> Optional[float]:
        """Reserve from the posted tariff of the planned route instead of the riders' max fare, so a
        lone bidder cannot be paid the riders' whole budget: margin x (base + rate x (route + pickup
        allowance)). None when disabled. Each retry allows a longer pickup leg."""
        if self.reserve_margin is None or self.network is None:
            return None
        try:
            if stop_order:
                routed = self.network.route_through([self.hub_id, *stop_order], clock.now())
                route = routed[1] if routed else None
            else:
                planned = self.network.best_stop_order(self.hub_id, [r.destination_home for r in reqs], clock.now())
                route = planned[1] if planned else None
        except KeyError:
            return None
        if route is None:
            return None
        cost = DRIVER_BASE_COST + DRIVER_COST_PER_SECOND * (route + self.pickup_allowance * (1 + attempt))
        return ceil_cents(self.reserve_margin * cost) / 100

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
        for rec in reversed(list(self.pools.values())):          # newest first: a re-pooled request has several
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

    def current_fare_cap(self, now: Optional[float] = None) -> Optional[float]:
        """RI-01: the tightest of the hub's own cap and the Regulator's policy in force now."""
        caps = [c for c in (self.fare_cap, self.policies.fare_cap(clock.now() if now is None else now))
                if c is not None]
        return min(caps) if caps else None

    def audit_data(self, req: AuditRequest) -> AuditData:
        trips = []
        for ctx in self.finished:
            if not (req.period_start <= ctx.finished_at <= req.period_end) or ctx.outcome == "retry":
                continue
            dest = ctx.pool.requests[0].destination_home if ctx.pool.requests else ""
            try:
                zone = self.network.zone_of(dest) if self.network is not None else ""
            except KeyError:
                zone = ""
            matched = ctx.outcome == "matched" and ctx.match is not None
            trips.append(AuditTrip(ctx.pool_id, ctx.finished_at, ctx.match.final_fare if matched else 0.0,
                                   matched, zone, ctx.match.driver_id if matched else ""))
        return AuditData(req.audit_id, self.hub_id, req.period_start, req.period_end, trips)

    @property
    def batching(self) -> bool:
        return self.t_pool > 0 and self.propose_pools

    def batch_estimate(self) -> float:
        """Seconds until the batch window closes and pools are proposed."""
        if not self.queue:
            return 0.0
        return max(0.0, window_closes_at(self.queue, self.t_pool) - clock.now())

    def open_group(self, members: List[QueuedRequest], plan: GroupPlan) -> PoolRecord:
        """A pool of one or more requests built by the solver."""
        self._seq += 1
        reqs = [q.req for q in sorted(members, key=lambda q: q.req.request_id)]
        pool = Pool(f"{self.hub_id}-pool-{self._seq:04d}", self.hub_id, reqs, PoolStatus.LOCKED, clock.now())
        rec = PoolRecord(pool, {q.req.request_id: q.sender for q in members},
                         proposal_deadline=clock.now() + self.t_pool_response, plan=plan)
        self.pools[pool.pool_id] = rec
        self.log(f"{pool.pool_id}: {len(reqs)} rider(s), route {list(plan.stops)}, "
                 f"detours {{{', '.join(f'{k}: {v:.0f}s' for k, v in sorted(plan.detour.items()))}}}")
        return rec

    def fare_weights(self, rec: PoolRecord) -> Dict[str, float]:
        """Proportional split: each rider pays in proportion to its direct hub->home time."""
        if rec.plan is None or len(rec.pool.requests) < 2:
            return {}
        return {rid: max(1.0, d) for rid, d in rec.plan.direct.items()}

    def proposal_for(self, rec: PoolRecord, req: TripRequest, plan: Optional[GroupPlan] = None) -> PoolProposal:
        """What the rider is told before committing: the shared route, the fare CEILING (the reserve
        of the auction, so the final fare can only be lower), its detour and the expected wait."""
        route, route_time, detour = [self.hub_id, req.destination_home], 0.0, 0.0
        if plan is not None:
            route, detour = list(plan.stops), plan.detour.get(req.request_id, 0.0)
        elif self.network is not None:
            planned = self.network.best_stop_order(self.hub_id, [req.destination_home], clock.now())
            if planned:
                route, route_time = planned
        etas = [e for e in (self.hub_eta(j, self.hub_id, clock.now()) for j in self.eligible_drivers(clock.now()))
                if e is not None]
        wait = min(etas) if etas else 0.0
        if plan is not None:
            wait += max(0.0, plan.departure - clock.now())
        cap = self.current_fare_cap()
        weights = self.fare_weights(rec)
        if weights:
            total = fare_ceiling({r.request_id: r.max_fare for r in rec.pool.requests}, weights)
            if cap is not None:
                total = min(total, cap)
            ceiling = split_fare_weighted(total, weights)[req.request_id]
        else:
            ceiling = req.max_fare if cap is None else min(req.max_fare, cap)
        return PoolProposal(rec.pool.pool_id, req.request_id, route, ceiling, wait, detour)

    def solo_fares(self, rec: PoolRecord, rider_jids: List[str]) -> Dict[str, float]:
        """Reference solo fare of each rider of a POOLED trip (so Payment can show the pooling
        discount). Empty for solo trips: there is nothing to compare with."""
        if rec.plan is None or len(rec.pool.requests) < 2:
            return {}
        by_req = {r.request_id: r for r in rec.pool.requests}
        return {rec.rider_jids[rid]: reference_solo_fare(rec.plan.direct[rid], DRIVER_BASE_COST,
                                                         DRIVER_COST_PER_SECOND, by_req[rid].max_fare)
                for rid in rec.rider_jids if rec.rider_jids[rid] in rider_jids}

    def confirm_pool(self, rec: PoolRecord) -> None:
        rec.state = "AWAITING_ESCROW"
        rec.escrow_deadline = clock.now() + self.t_escrow
        rec.pool.status = PoolStatus.LOCKED

    def start_auction(self, rec: PoolRecord, attempt: int = 0, delay: Optional[float] = None) -> None:
        """Auctions run ONE AT A TIME per hub: two simultaneous auctions would call the same idle
        drivers, and a driver holding a provisional lock for the first must refuse the second (CC-09),
        starving it of bids. A retry goes to the front of the line, after `retry_delay` (so the locks
        of the failed attempt have expired)."""
        rec.state = "AUCTION_QUEUED"
        wait = (self.retry_delay if delay is None else delay) if attempt else 0.0
        entry = (rec, attempt, clock.now() + wait)
        if attempt:
            self.auction_queue.insert(0, entry)
        else:
            self.auction_queue.append(entry)
        self._next_auction()

    def _next_auction(self) -> None:
        if self.auction_running or not self.auction_queue:
            return
        now = clock.now()
        ready = next((e for e in self.auction_queue if e[2] <= now), None)
        if ready is None:                                   # only delayed retries: wake up when the first is due
            if not self._waking:
                self._waking = True
                self.add_behaviour(_Wake(min(e[2] for e in self.auction_queue) - now))
            return
        self.auction_queue.remove(ready)
        rec, attempt, _ = ready
        self.auction_running = True
        rec.state = "AUCTIONING"
        rec.pool.status = PoolStatus.AUCTIONING
        ctx = AuctionContext(rec.pool, dict(rec.rider_jids), list(rec.escrows.values()), attempt,
                             weights=self.fare_weights(rec))
        if rec.plan is not None:
            ctx.stop_order, ctx.plan_detour = list(rec.plan.stops[1:]), max(rec.plan.detour.values(), default=0.0)
        rec.contexts.append(ctx)
        self.add_behaviour(MatchAuctionFSM(ctx),
                           make_template(Protocol.MATCH_AUCTION, thread=rec.pool.pool_id))

    def on_auction_finished(self, ctx: AuctionContext) -> None:
        """Called by the FSM's END state."""
        rec = self.pools[ctx.pool_id]
        self.finished.append(ctx)
        self.auction_running = False
        if ctx.outcome == MATCHED:
            rec.state, rec.pool.status = "ASSIGNED", PoolStatus.ASSIGNED
            self.matches[ctx.pool_id] = ctx.match
        elif ctx.outcome == RETRY:
            self.start_auction(rec, ctx.attempt + 1, self.retry_wait(ctx))
        else:
            rec.state, rec.pool.status = "UNMATCHED", PoolStatus.DISSOLVED
        self._next_auction()

    async def setup(self):
        await super().setup()
        self.add_behaviour(TripRequestReceiver(),
                           make_template(Protocol.TRIP_REQUEST, Perf.REQUEST))
        self.add_behaviour(PoolResponses(), make_template(Protocol.POOL_FORMATION))
        if self.batching:
            self.add_behaviour(BatchWindow())
        self.add_behaviour(PolicyReceiver(), make_template(Protocol.POLICY, Perf.REQUEST))
        self.add_behaviour(AuditResponder(), make_template(Protocol.AUDIT))
        if self.reputation_jid:
            self.add_behaviour(ReputationReceiver(), make_template(Protocol.REPUTATION, Perf.INFORM))
        self.add_behaviour(DriverStatusReceiver(), make_template(Protocol.DRIVER_STATUS, Perf.INFORM))
        self.add_behaviour(TripReports(), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))
        if self.payment_jid:
            self.add_behaviour(EscrowWatcher(), make_template(Protocol.ESCROW))
            self.add_behaviour(SettlementReceiver(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
            self.add_behaviour(RefundReceiver(), make_template(Protocol.REFUND, Perf.INFORM))
        self.add_behaviour(LateBidHandler(), make_template(Protocol.MATCH_AUCTION, Perf.PROPOSE))

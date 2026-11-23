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
  5. Repositioning         OPT-IN (pass `reposition=RepositionRules()`). Every `t_rebalance` the hub
                           compares the predicted demand at the staging points (DemandForecast
                           messages, or `demand_fn`) with where its idle drivers are, and asks the
                           best ones to move: protocols/reposition.py (RepositionProtocol).

Roles of the data it keeps: `pools` (live and finished), `matches` (pool_id -> MatchResult).
Drivers: for M5 the hub is given the JIDs of the drivers to call (`driver_jids`); DriverStatus-
based selection of nearby free drivers arrives with the Driver milestone.
Reputation: `reputations` is the hub's own record (jid -> 1..5); the Reputation agent will feed it.
"""
import logging
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

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
    AuctionOutcome, AuditData, AuditReport, Handover, LateJoinDecision, LateJoinRequest, PoolAmended, OverflowRequest, AuditRequest, AuditTrip, DemandForecast, HubLoad, DriverState, DriverStatus, Escrow, EscrowStatus, FailureReason, MatchResult, Perf,
    Penalty, Policy, Pool, Refund, PoolProposal, PoolStatus, Protocol, ReputationScore, SettlementComplete,
    StagingPoint, TripCompleted, TripFailed, TripRequest, parse_payload,
)
from common.settings import (
    AUCTION_PRICING, MAX_AUCTION_ATTEMPTS, T_AUCTION, T_AWARD, T_ESCROW, VALUE_OF_DETOUR,
    T_LOAD, T_POOL_RESPONSE, T_REBALANCE, T_REPOSITION, T_STATUS, VALUE_OF_REPUTATION, VALUE_OF_TIME, WIDEN_DETOUR,
)
from common.cells import nearest_hub
from common.costs import driver_cost, dwell_seconds, rate_per_second
from common.pooling import late_join_terms, GroupPlan, fare_ceiling, footprint, reference_solo_fare, split_fare_weighted
from common.settings import (
    DETOUR_WEIGHT, DRIVER_BASE_COST, DRIVER_COST_PER_KM, DRIVER_COST_PER_SECOND, MAX_BATCH, MAX_POOL_SIZE, PICKUP_ALLOWANCE,
    EMISSION_G_PER_KM, NOMINAL_SPEED_KMH, HUB_NEIGHBOURS, LATE_HOLD, LATE_JOIN_MARKUP, LATE_MIN_BIDS, T_LATE_POLL, T_LATE_REPLY, OVERFLOW_AFTER, POOL_SOLVER, T_FAILOVER, T_HEARTBEAT, T_OVERFLOW_REPLY, T_POOL,
)
from protocols.match_auction import AuctionContext, MATCHED, MatchAuctionFSM, RETRY
from protocols.pool_formation import BatchWindow, QueuedRequest, handle_decline, window_closes_at
from protocols.inter_hub import (
    HANDED_OVER, OVERFLOW_PENDING, HeartbeatReceiver, InterHubWatcher, OverflowAccepted, OverflowReceiver, OverflowReplies, OverflowWait,
    PeerView, _Send, neighbours, overflow_target, refund_request,
)
from protocols.payment_common import bare
from protocols.reposition import (
    ARRIVED, RepositionContext, RepositionFSM, RepositionPlan, RepositionPlanner, RepositionRules,
    plan_repositions,
)

logger = logging.getLogger(__name__)


@dataclass
class PoolMetric:
    """What one matched pool drove, against what the same riders would have driven in separate vehicles."""
    pool_id: str
    riders: int
    shared_m: float                      # loaded leg of the shared route, metres
    alone_m: float                       # sum of the riders' own hub -> home routes, metres

    @property
    def saved_m(self) -> float:
        return self.alone_m - self.shared_m


@dataclass
class LateRider:
    """A rider that joined an already-assigned pool: its own pool record, and what it pays."""
    rec: "PoolRecord"
    jid: str
    request_id: str
    fare: float


@dataclass
class LateJoinWait:
    """A late-join request sent to the driver, waiting for its answer."""
    req: TripRequest
    sender: str
    base: "PoolRecord"
    pool_id: str                          # the new rider's pool id (reserved)
    terms: object                         # LateJoinTerms
    driver: str
    deadline: float


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
    times: Dict[str, float] = field(default_factory=dict)    # sim-time milestones: confirmed, queued, auction_start, matched
    late_of: str = ""                                        # this is a late joiner's pool: the pool it joined
    late_terms: object = None                                # ... and the terms it was offered (LateJoinTerms)
    late: List["LateRider"] = field(default_factory=list)    # riders that joined this (assigned) pool afterwards
    late_old: Dict[str, float] = field(default_factory=dict)  # fares before the last join (to restore on an undo), by JID


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
        await admit_request(self, hub, req, sender)


async def admit_request(beh, hub, req: TripRequest, sender: str, allow_late: bool = True) -> None:
    """A validated request enters the hub: offered to a pool whose driver is still on the way (late join), queued for
    pooling, or an immediate pool. Used for requests from riders and for requests adopted from another hub
    (protocols/inter_hub.py)."""
    send = beh.send
    hub.arrivals.append(clock.now())
    if allow_late and hub.late_join and req.pooling_willingness and await hub.offer_late_join(beh, req, sender):
        return                                                      # the driver's answer decides what happens next
    if hub.batching and req.pooling_willingness:                    # wait for the batch window
        hub.queue.append(QueuedRequest(req, sender, clock.now()))
        hub.log(f"request {req.request_id} queued for pooling ({len(hub.queue)} waiting)")
        await send(make_message(sender, Perf.AGREE, Protocol.TRIP_REQUEST, "queued", req.request_id))
        await send(make_message(sender, Perf.INFORM, Protocol.TRIP_REQUEST,
                                     f"request_queued:{hub.batch_estimate():.0f}", req.request_id))
        return
    rec = hub.open_pool(req, sender)
    await send(make_message(sender, Perf.AGREE, Protocol.TRIP_REQUEST, rec.pool, req.request_id))
    await send(make_message(sender, Perf.INFORM, Protocol.TRIP_REQUEST,
                                 f"request_queued:{hub.batch_estimate():.0f}", req.request_id))
    if hub.propose_pools:                                           # PoolFormationProtocol
        await send(make_pool_proposal(sender, hub.proposal_for(rec, req)))
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
                        hub.cancel_late_join(rec, "declined")
                        await handle_decline(self, hub, rec, [rid])
            for rec in list(hub.pools.values()):
                if rec.state == "PROPOSED" and clock.now() >= rec.proposal_deadline:
                    silent = [r for r in rec.rider_jids if r not in rec.accepted]
                    hub.log(f"pool {rec.pool.pool_id}: no answer from {silent}; they are dropped")
                    hub.cancel_late_join(rec, "no_answer")
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
        hub.cancel_late_join(rec, "escrow_failed")
        for rider in rec.rider_jids.values():
            await self.send(make_auction_failed(rider, rec.pool.pool_id, reason))
        if rec.escrows:                                  # someone did pay: give it back
            await self.send(make_request(
                hub.payment_jid, Protocol.REFUND,
                TripFailed(rec.pool.pool_id, reason, str(hub.jid.bare), list(rec.escrows.values())),
                thread=rec.pool.pool_id))


class LateJoinReplies(CyclicBehaviour):
    """LateJoinProtocol, hub side: the driver's agree / refuse to a late-join request (or silence), and the driver
    telling us it had to undo a joiner."""

    async def run(self):
        hub = self.agent
        msg = await self.receive(timeout=1)
        try:
            if msg is not None:
                await self.handle(msg)
            now = clock.now()
            for pid, w in list(hub.late_waits.items()):
                if now >= w.deadline:
                    hub.late_waits.pop(pid, None)
                    hub.log(f"late join {pid}: the driver did not answer; normal pooling instead")
                    await admit_request(self, hub, w.req, w.sender, allow_late=False)
        except Exception:
            logger.exception("%s: late-join round failed", hub.jid)

    async def handle(self, msg):
        hub, sender, perf = self.agent, bare(msg.sender), msg.get_metadata("performative")
        pid = msg.thread or ""
        w = hub.late_waits.get(pid)
        if w is not None and sender == bare(w.driver) and perf in (Perf.AGREE, Perf.REFUSE):
            hub.late_waits.pop(pid, None)
            if perf == Perf.REFUSE:
                hub.log(f"late join {pid} refused by the driver ({msg.body}); normal pooling instead")
                await admit_request(self, hub, w.req, w.sender, allow_late=False)
            else:
                await self.start_proposal(w)
            return
        if perf == Perf.INFORM:                                   # driver: "not confirmed in time, I undid it"
            try:
                d = parse_payload(msg, strict=True)
            except (ValueError, TypeError):
                return
            rec = hub.pools.get(getattr(d, "join_pool_id", ""))
            if isinstance(d, LateJoinDecision) and not d.confirmed and rec is not None and rec.late_of:
                await hub.abandon_late_join(self, rec, d.reason or "driver_undid")

    async def start_proposal(self, w: LateJoinWait):
        """The driver would take the rider: now the rider is told (as for any pool) and asked."""
        hub, req, sender = self.agent, w.req, w.sender
        now = clock.now()
        pool = Pool(w.pool_id, hub.hub_id, [req], PoolStatus.LOCKED, now)
        rec = PoolRecord(pool, {req.request_id: sender}, proposal_deadline=now + hub.t_pool_response,
                         late_of=w.base.pool.pool_id, late_terms=w.terms)
        hub.pools[w.pool_id] = rec
        hub.log(f"request {req.request_id} from {sender} -> {w.pool_id}, late joiner of {rec.late_of}")
        await self.send(make_message(sender, Perf.AGREE, Protocol.TRIP_REQUEST, rec.pool, req.request_id))
        await self.send(make_message(sender, Perf.INFORM, Protocol.TRIP_REQUEST,
                                     f"request_queued:{hub.batch_estimate():.0f}", req.request_id))
        if hub.propose_pools:
            eta = hub.hub_eta(w.driver, hub.hub_id, now) or 0.0
            await self.send(make_pool_proposal(sender, PoolProposal(
                w.pool_id, req.request_id, list(w.terms.plan.stops), w.terms.fares[req.request_id], eta,
                w.terms.plan.detour.get(req.request_id, 0.0))))
        else:
            hub.confirm_pool(rec)
            if hub.payment_jid is None:
                hub.start_auction(rec)


class _AbandonLate(OneShotBehaviour):
    def __init__(self, rec: PoolRecord, reason: str):
        super().__init__()
        self.rec, self.reason = rec, reason

    async def run(self):
        await self.agent.abandon_late_join(self, self.rec, self.reason)


class LoadBroadcaster(CyclicBehaviour):
    """Publishes the hub's HubLoad to every driver, once per `t_load` sim seconds (input of the drivers' brains)."""

    async def run(self):
        hub = self.agent
        load = hub.load_snapshot(clock.now())
        for jid in hub.driver_jids:
            if jid in hub.registry:                                   # only drivers that are up (they announced themselves)
                await self.send(make_inform(jid, Protocol.DRIVER_BRAIN, load, f"load-{hub.hub_id}"))
        await clock.sim_sleep(hub.t_load)


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
        late_done = [lr.jid for lr in rec.late if lr.jid in rep.rider_ids]
        pay = hub.pool_incentives(rec, riders + late_done)                       # one pot for everybody in the vehicle
        if hub.reputation_jid:                                  # trip registry: who may rate whom
            await self.send(make_inform(hub.reputation_jid, Protocol.REPUTATION,
                                        TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id,
                                                      riders + late_done, clock.now()), rec.pool.pool_id))
        await self.send(make_inform(match.driver_id, Protocol.TRIP_EXECUTION,       # ID-08: now it may rate
                                    TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders,
                                                  clock.now()), rec.pool.pool_id))
        if hub.payment_jid:
            if riders:
                await self.send(make_inform(
                    hub.payment_jid, Protocol.SETTLEMENT,
                    TripCompleted(rep.trip_id, rec.pool.pool_id, match.driver_id, riders, rep.completed_at,
                                  rep.distance_km, {r: match.rider_fares[r] for r in riders},
                                  hub.solo_fares(rec, riders), {j: pay[j] for j in riders if j in pay}),
                    rec.pool.pool_id))
            await self.refund(rec, [j for j in rec.no_shows if j not in riders], FailureReason.RIDER_CANCELLED)
        await self.complete_late_riders(rec, match, rep, pay)

    async def complete_late_riders(self, rec: PoolRecord, match: MatchResult, rep: TripCompleted, pay: Dict[str, float]):
        """Riders that joined late have their own pool (and escrow): settle each under it with the same driver."""
        hub = self.agent
        if not rec.late:
            return
        done = [lr for lr in rec.late if lr.jid in rep.rider_ids]
        for lr in rec.late:
            lrec = lr.rec
            if lr not in done:                                              # the driver did not carry him
                lrec.state, lrec.pool.status = "CANCELLED", PoolStatus.DISSOLVED
                await self.refund(lrec, [lr.jid], FailureReason.NO_MATCH)
                # He was told "matched" and must not wait for ever for a ride that is not coming: he starts over at this
                # hub (as when a late join is abandoned), through the normal pooling and auction.
                req = lrec.pool.requests[0]
                hub.log(f"late joiner {lrec.pool.pool_id} was not driven by {match.driver_id}: back to the queue")
                await self.send(make_message(lr.jid, Perf.INFORM, Protocol.INTER_HUB,
                                             Handover(req.request_id, hub.hub_id, hub.hub_id, str(hub.jid.bare),
                                                      FailureReason.NO_MATCH), req.request_id))
                await admit_request(self, hub, req, lr.jid, allow_late=False)
                continue
            lrec.state, lrec.pool.status = "COMPLETED", PoolStatus.COMPLETED
            hub.log(f"pool {lrec.pool.pool_id} (late joiner of {rec.pool.pool_id}) completed; settling {lr.jid}")
            tc = TripCompleted(f"trip-{lrec.pool.pool_id}", lrec.pool.pool_id, match.driver_id, [lr.jid], rep.completed_at,
                               rep.distance_km, {lr.jid: lr.fare}, {}, {lr.jid: pay[lr.jid]} if lr.jid in pay else {})
            if hub.reputation_jid:
                await self.send(make_inform(hub.reputation_jid, Protocol.REPUTATION,
                                            TripCompleted(tc.trip_id, lrec.pool.pool_id, match.driver_id, [lr.jid],
                                                          clock.now()), lrec.pool.pool_id))
            if hub.payment_jid:
                await self.send(make_inform(hub.payment_jid, Protocol.SETTLEMENT, tc, lrec.pool.pool_id))

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
            for lr in rec.late:                                          # the late joiners have their own escrows
                if lr.rec.state == "ASSIGNED":
                    lr.rec.state, lr.rec.pool.status = "DRIVER_FAILED", PoolStatus.DISSOLVED
                    await self.send(make_auction_failed(lr.jid, lr.rec.pool.pool_id, FailureReason.DRIVER_FAILED))
                    await self.refund(lr.rec, [lr.jid], FailureReason.DRIVER_FAILED)

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


class DemandForecastReceiver(CyclicBehaviour):
    """DemandForecastProtocol: inform(DemandForecast) -> the numbers the rebalancing heuristic reads.
    If the hub was given `forecast_jids`, only those senders count (otherwise anyone may skew it)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        if hub.forecast_jids is not None and bare(msg.sender) not in hub.forecast_jids:
            hub.log(f"ignored demand forecast from {bare(msg.sender)}")
            return
        try:
            fc = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        if isinstance(fc, DemandForecast) and hub.record_forecast(fc):
            hub.log(f"forecast for {fc.hub_id}: {fc.expected_requests:.1f} requests "
                    f"until t={fc.horizon_end:.0f}")


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
                 reposition: Optional[RepositionRules] = None, t_rebalance: float = T_REBALANCE,
                 t_reposition: float = T_REPOSITION,
                 demand_fn: Optional[Callable[[StagingPoint, float], float]] = None,
                 forecast_jids: Optional[List[str]] = None,
                 audit_logs_available: bool = True, t_pool: float = 0.0, max_pool_size: int = MAX_POOL_SIZE,
                 max_batch: int = MAX_BATCH, solver: str = POOL_SOLVER, detour_weight: float = DETOUR_WEIGHT,
                 solo_fallback: bool = True, reserve_margin: Optional[float] = None,
                 pickup_allowance: float = PICKUP_ALLOWANCE, max_pickup_eta: Optional[float] = None,
                 retry_delay: float = 0.0, window_slack: float = 0.0, broadcast_demand: bool = False, relax_expired_windows: bool = False,
                 peer_jids: Optional[Dict[str, str]] = None, t_heartbeat: float = T_HEARTBEAT,
                 t_failover: float = T_FAILOVER, overflow_after: int = OVERFLOW_AFTER,
                 k_neighbours: int = HUB_NEIGHBOURS, t_overflow_reply: float = T_OVERFLOW_REPLY,
                 emission_g_per_km: float = EMISSION_G_PER_KM, pool_incentive_per_kg: float = 0.0,
                 late_join: bool = False, late_join_markup: float = LATE_JOIN_MARKUP, t_late_reply: float = T_LATE_REPLY,
                 late_invite: bool = True, late_hold: float = LATE_HOLD, late_min_bids: int = LATE_MIN_BIDS,
                 t_late_poll: float = T_LATE_POLL, **kwargs):
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
        # repositioning (None = this hub never moves drivers)
        self.reposition_rules = reposition
        self.t_rebalance, self.t_reposition = t_rebalance, t_reposition
        self.demand_fn = demand_fn                      # (staging point, now) -> predicted requests
        self.forecast_jids = [bare(j) for j in forecast_jids] if forecast_jids is not None else None
        self.demand_forecasts: Dict[str, DemandForecast] = {}      # hub id -> newest forecast
        self.repositions: Dict[str, RepositionContext] = {}        # thread -> requests in flight
        self.reposition_log: List[RepositionContext] = []          # finished ones
        self.reposition_cooldown: Dict[str, float] = {}            # driver JID -> not before (sim t)
        self._reposition_seq = 0
        # pool formation (M9): t_pool = 0 disables batching (every request is an immediate solo pool)
        self.t_pool, self.max_pool_size, self.max_batch = t_pool, max_pool_size, max_batch
        self.solver, self.detour_weight, self.solo_fallback = solver, detour_weight, solo_fallback
        self.queue: List[QueuedRequest] = []
        self.reserve_margin, self.pickup_allowance = reserve_margin, pickup_allowance
        self.max_pickup_eta, self.retry_delay, self.window_slack = max_pickup_eta, retry_delay, window_slack
        self.relax_expired_windows = relax_expired_windows   # patient runs: a window that has run out is restarted for the drivers
        self._waking = False
        self.broadcast_demand = broadcast_demand      # tell drivers that were not called about the request (BDI input)
        self.arrivals: deque = deque()                # sim times of the requests that entered this hub (last 10 minutes kept)
        self.t_load = T_LOAD                          # HubLoad broadcast period (only with broadcast_demand)
        self._cell_cache: Dict[str, str] = {}
        self.auction_queue: List[tuple] = []
        self.auction_running = False
        # inter-hub coordination (M11): only with `peer_jids` (hub id -> JID of the OTHER hubs)
        self.t_heartbeat, self.t_failover, self.overflow_after = t_heartbeat, t_failover, overflow_after
        self.k_neighbours, self.t_overflow_reply = k_neighbours, t_overflow_reply
        self.peer_jids = {h: bare(j) for h, j in (peer_jids or {}).items() if h != hub_id}
        near = set(neighbours(network, hub_id, k_neighbours)) if network is not None and hub_id in network.hubs \
            else set(self.peer_jids)
        self.peers: Dict[str, PeerView] = {h: PeerView(h, j) for h, j in self.peer_jids.items() if h in near}
        self.overflow_waits: Dict[str, OverflowWait] = {}
        self.overflow_offers: Dict[str, tuple] = {}     # pool id -> (requests, from_hub, reason, from jid, keep until): agreed, not yet accepted
        self.overflow_tried: set = set()                 # pool ids already offered to a neighbour
        self.adopted: List[str] = []                     # request ids taken over from other hubs
        self.emission_g_per_km = emission_g_per_km
        self.pool_incentive_per_kg = pool_incentive_per_kg
        self.late_join = late_join                       # offer riders a seat in pools whose driver is still on the way
        self.late_join_markup = late_join_markup
        self.t_late_reply = t_late_reply
        self.late_invite, self.late_hold, self.late_min_bids, self.t_late_poll = late_invite, late_hold, late_min_bids, t_late_poll
        self.late_waits: Dict[str, LateJoinWait] = {}    # late-join requests waiting for the driver's answer, by new pool id
        self.late_joined: List[str] = []                 # pool ids of riders that joined late (the report counts them)
        self.pool_metrics: List[PoolMetric] = []         # one per matched pool (footprint of sharing)

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
                planned_stops = planned[0][1:] if planned else []
        except KeyError:
            return None
        if route is None:
            return None
        allowance = self.pickup_allowance * (1 + attempt)
        try:
            stops = stop_order or planned_stops
            metres = self.network.route_length([self.hub_id, *stops], clock.now()) or 0.0
        except KeyError:
            metres = 0.0
        speed = metres / route if route > 0 and metres > 0 else NOMINAL_SPEED_KMH / 3.6          # m/s
        dwell = dwell_seconds(len(set(stops)))
        cost = driver_cost((metres + speed * allowance) / 1000.0, route + allowance, dwell)
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

    def record_pool_metrics(self, rec: PoolRecord) -> None:
        """Footprint of a matched pool: vehicle metres on the shared route vs the riders' separate routes."""
        if self.network is None:
            return
        dests = [r.destination_home for r in rec.pool.requests]
        stops = list(rec.plan.stops[1:]) if rec.plan is not None else sorted(set(dests))
        try:
            fp = footprint(self.network, self.hub_id, stops, dests, clock.now())
        except KeyError:
            return
        if fp is not None:
            self.pool_metrics.append(PoolMetric(rec.pool.pool_id, len(dests), fp[0], fp[1]))

    # ---- inter-hub coordination (protocols/inter_hub.py) ------------------------------------------
    def idle_driver_count(self, now: float) -> int:
        """Drivers whose fresh status says IDLE (the spare capacity a neighbour may count on)."""
        return sum(1 for e in self.registry.values()
                   if now - e[1] <= 3 * self.t_status and e[0].state == DriverState.IDLE)

    def load_snapshot(self, now: float) -> HubLoad:
        """What this hub tells the drivers: requests without a driver, the recent arrival rate, and the free drivers that
        count as supply of its cell (idle ones standing in it, cruisers heading to it)."""
        while self.arrivals and now - self.arrivals[0] > 600.0:
            self.arrivals.popleft()
        supply = 0
        for st, at in self.registry.values():
            if now - at > 3 * self.t_status or st.state != DriverState.IDLE:
                continue
            cell = st.target or nearest_hub(self.network, st.location, self._cell_cache)
            supply += 1 if cell == self.hub_id else 0
        return HubLoad(self.hub_id, float(len(self.pending_requests())), len(self.arrivals) / 10.0, float(supply), now)

    def pending_requests(self) -> List[TripRequest]:
        """Requests this hub owes an answer and has not matched: what a neighbour must take over if it dies."""
        out = [q.req for q in self.queue] + [w.req for w in self.late_waits.values()]
        for rec in self.pools.values():
            if rec.state in ("PROPOSED", "AWAITING_ESCROW", "AUCTION_QUEUED", "AUCTIONING", OVERFLOW_PENDING):
                out += list(rec.pool.requests)
        return out

    def maybe_overflow(self, rec: PoolRecord, next_attempt: int) -> bool:
        """After a failed auction: offer the pool to a neighbour instead of retrying (True = the offer is out)."""
        pid = rec.pool.pool_id
        if not self.peers or self.network is None or not self.overflow_after or pid in self.overflow_tried \
                or next_attempt < self.overflow_after:
            return False
        now = clock.now()
        target = overflow_target(self.network, list(rec.pool.requests), self.peers, self.hub_id, now)
        if target is None:
            return False
        self.overflow_tried.add(pid)
        rec.state = OVERFLOW_PENDING
        self.overflow_waits[pid] = OverflowWait(rec, next_attempt, now + self.t_overflow_reply, target)
        self.log(f"pool {pid}: {next_attempt} failed attempts, offered to {target}")
        self.add_behaviour(_Send(make_message(self.peers[target].jid, Perf.REQUEST, Protocol.INTER_HUB,
                                              OverflowRequest(self.hub_id, FailureReason.NO_BIDS, list(rec.pool.requests)),
                                              pid)))
        return True

    def resume_auction(self, w: OverflowWait) -> None:
        if w.rec.state == OVERFLOW_PENDING:
            self.start_auction(w.rec, w.attempt, 0.0)

    def finish_overflow(self, w: OverflowWait) -> None:
        """The neighbour took the pool: give it up and have the riders' escrows returned."""
        rec = w.rec
        rec.state, rec.pool.status = HANDED_OVER, PoolStatus.DISSOLVED
        self.log(f"pool {rec.pool.pool_id} handed over to {w.target}")
        msg = refund_request(self, rec)
        if msg is not None:
            self.add_behaviour(_Send(msg))

    def check_request(self, req: TripRequest, sender: str) -> str:
        if self.account(req.rider_id) != sender:
            return "not_authorised"
        if self.pool_of_request(req.request_id) is not None:
            return "duplicate_request"
        if self.network is not None:
            if not self.network.has_node(req.destination_home):
                return "unknown_destination"
            if self.hub_id and self.hub_id not in self.network.hubs_covering(
                    req.destination_home, clock.now()):
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
        if rec.plan is None or len(rec.pool.requests) + len(rec.late) < 2:
            return {}
        by_req = {r.request_id: r for r in rec.pool.requests}
        return {rec.rider_jids[rid]: reference_solo_fare(rec.plan.direct[rid], DRIVER_BASE_COST,
                                                         rate_per_second(), by_req[rid].max_fare)
                for rid in rec.rider_jids if rec.rider_jids[rid] in rider_jids}

    def pool_incentives(self, rec: PoolRecord, rider_jids: List[str]) -> Dict[str, float]:
        """CO2 incentive of a shared trip: rate x kg saved, split among the riders by their experienced delay
        (detour seconds + 1, so a rider who gave up more time gets more). Empty if off or nothing was saved."""
        late = getattr(rec, "late", [])
        if self.pool_incentive_per_kg <= 0 or rec.plan is None or len(rec.pool.requests) + len(late) < 2:
            return {}
        metric = next((m for m in self.pool_metrics if m.pool_id == rec.pool.pool_id), None)
        if metric is None or metric.saved_m <= 0:
            return {}
        total = self.pool_incentive_per_kg * metric.saved_m * self.emission_g_per_km / 1e6
        jid_of = {**rec.rider_jids, **{lr.request_id: lr.jid for lr in late}}
        weights = {jid_of[rid]: rec.plan.detour.get(rid, 0.0) + 1.0 for rid in jid_of if jid_of[rid] in rider_jids}
        scale = sum(weights.values())
        return {jid: round(total * w / scale, 2) for jid, w in weights.items()} if scale else {}

    # ---- late join (LateJoinProtocol) -----------------------------------------------------------
    def _late_busy(self, base_id: str) -> bool:
        """One joiner at a time per pool: its terms depend on who is in the pool."""
        return any(w.base.pool.pool_id == base_id for w in self.late_waits.values()) or any(
            r.late_of == base_id and r.state in ("PROPOSED", "AWAITING_ESCROW") for r in self.pools.values())

    async def offer_late_join(self, beh, req: TripRequest, sender: str) -> bool:
        """Is there a pool whose driver is still on the way to the hub that `req` could join? If so ask the driver
        (True = the request is now in the driver's hands) - otherwise False and the request goes the normal way."""
        if self.network is None:
            return False
        now = clock.now()
        best = None
        for base in self.pools.values():
            match = self.matches.get(base.pool.pool_id)
            if base.state != "ASSIGNED" or base.late_of or match is None or self._late_busy(base.pool.pool_id):
                continue
            eta = self.hub_eta(match.driver_id, self.hub_id, now)
            if eta is None or eta < 15.0:                        # too close: no time to ask, propose, escrow
                continue
            existing = list(base.pool.requests) + [lr.rec.pool.requests[0] for lr in base.late]
            if not all(r.pooling_willingness for r in existing):     # someone who refused pooling is not given a stranger
                continue
            old = {r.request_id: (match.rider_fares[base.rider_jids[r.request_id]] if r.request_id in base.rider_jids
                                  else next(lr.fare for lr in base.late if lr.request_id == r.request_id))
                   for r in existing}
            terms = late_join_terms(self.network, self.hub_id, existing, req, old, now + eta, rate_per_second(),
                                    self.late_join_markup, self.max_pool_size)
            if terms is not None and (best is None or terms.extra_seconds < best[0].extra_seconds):
                best = (terms, base, match)
        if best is None:
            return False
        terms, base, match = best
        self._seq += 1
        pid = f"{self.hub_id}-pool-{self._seq:04d}"
        self.late_waits[pid] = LateJoinWait(req, sender, base, pid, terms, match.driver_id, now + self.t_late_reply)
        self.log(f"request {req.request_id}: asks the driver of {base.pool.pool_id} (+{terms.extra_seconds:.0f}s, "
                 f"+{terms.extra_pay:.2f})")
        await beh.send(make_message(match.driver_id, Perf.REQUEST, Protocol.LATE_JOIN,
                                    LateJoinRequest(base.pool.pool_id, pid, req.request_id, sender, req.destination_home,
                                                    list(terms.plan.stops), terms.extra_seconds, terms.extra_pay), pid))
        return True

    def cancel_late_join(self, rec: PoolRecord, reason: str) -> None:
        """The rider of a late-join pool said no / did not pay: tell the driver to take it back out."""
        if not rec.late_of or rec.state == "ASSIGNED":
            return
        match = self.matches.get(rec.late_of)
        if match is not None:
            self.add_behaviour(_Send(make_message(match.driver_id, Perf.INFORM, Protocol.LATE_JOIN,
                                                  LateJoinDecision(rec.late_of, rec.pool.pool_id, False, reason),
                                                  rec.pool.pool_id)))

    async def abandon_late_join(self, beh, rec: PoolRecord, reason: str) -> None:
        """The driver could not keep the joiner (it was not confirmed in time). Undo it here too: refund, tell the rider, and
        give back the fares of the others if the join had already been committed."""
        base = self.pools.get(rec.late_of)
        was_committed = rec.state == "ASSIGNED"
        rec.state, rec.pool.status = "DISSOLVED", PoolStatus.DISSOLVED
        self.log(f"late join {rec.pool.pool_id} into {rec.late_of} abandoned ({reason})")
        if base is not None:
            lr = next((x for x in base.late if x.rec is rec), None)
            if lr is not None:
                base.late.remove(lr)
                match = self.matches.get(base.pool.pool_id)
                if match is not None and base.late_old:
                    match.rider_fares.update(base.late_old)
                    match.final_fare = round(sum(match.rider_fares.values()), 2)
        if rec.escrows and self.payment_jid:                      # the rider's money comes back; it escrows again later
            await beh.send(make_request(self.payment_jid, Protocol.REFUND,
                                        TripFailed(rec.pool.pool_id, FailureReason.NO_MATCH, str(self.jid.bare),
                                                   list(rec.escrows.values())), thread=rec.pool.pool_id))
        # The rider must not lose its trip because the join fell through: it goes the normal way (batch / own pool).
        req = rec.pool.requests[0]
        sender = rec.rider_jids[req.request_id]
        await beh.send(make_message(sender, Perf.INFORM, Protocol.INTER_HUB,
                                    Handover(req.request_id, self.hub_id, self.hub_id, str(self.jid.bare),
                                             FailureReason.NO_MATCH), req.request_id))
        await admit_request(beh, self, req, sender, allow_late=False)

    def commit_late_join(self, rec: PoolRecord) -> None:
        """The rider accepted and escrowed: the join is final. The driver keeps it, the first riders' fares drop to their
        new shares (never up), and everybody is told."""
        base, terms = self.pools.get(rec.late_of), rec.late_terms
        match = self.matches.get(rec.late_of)
        if base is None or match is None or base.state != "ASSIGNED" or terms is None:
            self.add_behaviour(_AbandonLate(rec, "pool_gone"))
            return
        req = rec.pool.requests[0]
        jid = rec.rider_jids[req.request_id]
        base.late_old = dict(match.rider_fares)
        for rid, jd in base.rider_jids.items():
            old = match.rider_fares.get(jd)
            if old is not None:
                match.rider_fares[jd] = terms.fares[rid]
        match.final_fare = round(sum(match.rider_fares.values()), 2)
        for lr in base.late:
            lr.fare = terms.fares[lr.request_id]
        fare = terms.fares[req.request_id]
        base.late.append(LateRider(rec, jid, req.request_id, fare))
        base.plan = terms.plan
        rec.state, rec.pool.status = "ASSIGNED", PoolStatus.ASSIGNED
        rec.times["matched"] = clock.now()
        mine = MatchResult(rec.pool.pool_id, match.driver_id, fare, match.pickup_eta, {jid: fare})
        self.matches[rec.pool.pool_id] = mine
        self.late_joined.append(rec.pool.pool_id)
        self.refresh_pool_metric(base)
        self.log(f"late joiner {rec.pool.pool_id} confirmed in {base.pool.pool_id}: fare {fare:.2f}, "
                 f"others pay {{{', '.join(f'{k}: {v:.2f}' for k, v in sorted(match.rider_fares.items()))}}}")
        self.add_behaviour(_Send(make_message(match.driver_id, Perf.INFORM, Protocol.LATE_JOIN,
                                              LateJoinDecision(base.pool.pool_id, rec.pool.pool_id, True), rec.pool.pool_id)))
        self.add_behaviour(_Send(make_message(jid, Perf.INFORM, Protocol.MATCH_AUCTION, mine, rec.pool.pool_id)))
        n = len(base.rider_jids) + len(base.late)
        for rid, jd in base.rider_jids.items():
            self.add_behaviour(_Send(make_message(
                jd, Perf.INFORM, Protocol.LATE_JOIN,
                PoolAmended(base.pool.pool_id, jd, list(terms.plan.stops), base.late_old.get(jd, 0.0),
                            match.rider_fares.get(jd, 0.0), n), base.pool.pool_id)))

    def refresh_pool_metric(self, base: PoolRecord) -> None:
        """Re-measure a pool's footprint now that riders joined it."""
        if self.network is None or base.plan is None:
            return
        reqs = list(base.pool.requests) + [lr.rec.pool.requests[0] for lr in base.late]
        dests = [r.destination_home for r in reqs]
        try:
            fp = footprint(self.network, self.hub_id, list(base.plan.stops[1:]), dests, clock.now())
        except KeyError:
            return
        if fp is None:
            return
        self.pool_metrics = [m for m in self.pool_metrics if m.pool_id != base.pool.pool_id]
        self.pool_metrics.append(PoolMetric(base.pool.pool_id, len(dests), fp[0], fp[1]))

    def confirm_pool(self, rec: PoolRecord) -> None:
        rec.state = "AWAITING_ESCROW"
        rec.times.setdefault("confirmed", clock.now())
        rec.escrow_deadline = clock.now() + self.t_escrow
        rec.pool.status = PoolStatus.LOCKED

    def start_auction(self, rec: PoolRecord, attempt: int = 0, delay: Optional[float] = None) -> None:
        """Auctions run ONE AT A TIME per hub: two simultaneous auctions would call the same idle
        drivers, and a driver holding a provisional lock for the first must refuse the second (CC-09),
        starving it of bids. A retry goes to the front of the line, after `retry_delay` (so the locks
        of the failed attempt have expired)."""
        if rec.late_of:                                      # a late joiner has no auction: it rides with a driver already assigned
            self.commit_late_join(rec)
            return
        rec.state = "AUCTION_QUEUED"
        rec.times.setdefault("queued", clock.now())          # first time only: retries are part of the auction phase
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
        rec.times.setdefault("auction_start", now)
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
            rec.times["matched"] = clock.now()
            self.matches[ctx.pool_id] = ctx.match
            self.record_pool_metrics(rec)
        elif ctx.outcome == RETRY:
            if not self.maybe_overflow(rec, ctx.attempt + 1):
                self.start_auction(rec, ctx.attempt + 1, self.retry_wait(ctx))
        else:
            rec.state, rec.pool.status = "UNMATCHED", PoolStatus.DISSOLVED
        self._next_auction()

    # ---- repositioning (protocols/reposition.py) ------------------------------------------
    def record_forecast(self, fc: DemandForecast) -> bool:
        """Keep the newest forecast per hub. False if ignored (negative, or older than the one held)."""
        old = self.demand_forecasts.get(fc.hub_id)
        if fc.expected_requests < 0 or (old is not None and fc.horizon_start < old.horizon_start):
            return False
        self.demand_forecasts[fc.hub_id] = fc
        return True

    def predicted_demand(self, sp: StagingPoint, now: float) -> float:
        """Expected requests at a staging point: `demand_fn` if the hub has one, otherwise the sum of the
        unexpired forecasts of the hubs the point serves (sp.covered_hubs)."""
        if self.demand_fn is not None:
            return self.demand_fn(sp, now)
        total = 0.0
        for hub_id in sp.covered_hubs:
            fc = self.demand_forecasts.get(hub_id)
            if fc is not None and fc.horizon_end > now:
                total += fc.expected_requests
        return total

    def _fresh_status(self, jid: str, now: float) -> Optional[DriverStatus]:
        entry = self.registry.get(jid)
        return entry[0] if entry and now - entry[1] <= 3 * self.t_status else None

    def idle_drivers(self, now: float) -> Dict[str, str]:
        """driver JID -> node, for the drivers this hub may move right now: its own, idle, empty, with a
        fresh status, not already being moved, not on cooldown, and with enough shift left."""
        rules = self.reposition_rules or RepositionRules()
        moving = {c.plan.driver_jid for c in self.repositions.values()}
        out: Dict[str, str] = {}
        for jid in self.driver_jids:
            st = self._fresh_status(jid, now)
            if (st is None or st.state != DriverState.IDLE or st.current_passengers
                    or jid in moving or self.reposition_cooldown.get(jid, -math.inf) > now
                    or st.shift_end - now < rules.min_shift_left):
                continue
            out[jid] = st.location
        return out

    def reposition_supply(self, now: float) -> Dict[str, int]:
        """staging point id -> idle drivers standing there (any hub's) + requests in flight towards it."""
        here = Counter(st.location for st, at in self.registry.values()
                       if now - at <= 3 * self.t_status and st.state == DriverState.IDLE)
        supply = {sp.id: here[sp.location] for sp in self.network.staging_points.values()}
        for ctx in self.repositions.values():
            sp_id = ctx.plan.staging_point_id
            supply[sp_id] = supply.get(sp_id, 0) + 1
        return supply

    def plan_repositions(self, now: float) -> List[RepositionPlan]:
        """The heuristic, fed with this hub's view. Does not send anything."""
        if self.network is None or self.reposition_rules is None:
            return []
        idle = self.idle_drivers(now)
        if not idle:
            return []

        def seconds(a: str, b: str) -> float:
            try:
                return self.network.travel_time(a, b, now)
            except KeyError:                       # a node the graph does not know
                return math.inf

        return plan_repositions(idle, list(self.network.staging_points.values()),
                                lambda sp: self.predicted_demand(sp, now), seconds,
                                self.reposition_supply(now), self.reposition_rules)

    def start_reposition(self, plan: RepositionPlan) -> RepositionContext:
        """Start one RepositionFSM (also usable directly, without the heuristic)."""
        self._reposition_seq += 1
        ctx = RepositionContext(plan, f"{self.hub_id}-repo-{self._reposition_seq:04d}",
                                self.hub_id, self.t_reposition)
        self.repositions[ctx.thread] = ctx
        self.add_behaviour(RepositionFSM(ctx), make_template(Protocol.REPOSITION, thread=ctx.thread))
        return ctx

    def rebalance(self, now: float) -> List[RepositionContext]:
        """One rebalancing round: plan, then start one FSM per move."""
        started = [self.start_reposition(p) for p in self.plan_repositions(now)]
        for ctx in started:
            self.log(f"rebalance: {ctx.plan.driver_jid} {ctx.plan.from_node} -> {ctx.plan.staging_point_id} "
                     f"({ctx.plan.travel_time:.0f}s, incentive {ctx.plan.incentive:.2f})")
        return started

    def on_reposition_finished(self, ctx: RepositionContext) -> None:
        """Called by the FSM's END state. Anything but an arrival puts the driver on cooldown."""
        self.repositions.pop(ctx.thread, None)
        self.reposition_log.append(ctx)
        if ctx.outcome != ARRIVED:
            cooldown = (self.reposition_rules or RepositionRules()).cooldown
            self.reposition_cooldown[ctx.plan.driver_jid] = clock.now() + cooldown
        self.log(f"reposition {ctx.thread}: {ctx.outcome}" + (f" ({ctx.reason})" if ctx.reason else ""))

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
        if self.broadcast_demand and self.network is not None:
            self.add_behaviour(LoadBroadcaster())
        if self.late_join:
            self.add_behaviour(LateJoinReplies(), make_template(Protocol.LATE_JOIN))
        self.add_behaviour(TripReports(), make_template(Protocol.TRIP_EXECUTION, Perf.INFORM))
        if self.payment_jid:
            self.add_behaviour(EscrowWatcher(), make_template(Protocol.ESCROW))
            self.add_behaviour(SettlementReceiver(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
            self.add_behaviour(RefundReceiver(), make_template(Protocol.REFUND, Perf.INFORM))
        self.add_behaviour(LateBidHandler(), make_template(Protocol.MATCH_AUCTION, Perf.PROPOSE))
        if self.peers:                                                   # InterHubCoordinationProtocol (M11)
            self.add_behaviour(HeartbeatReceiver(), make_template(Protocol.INTER_HUB, Perf.INFORM))
            self.add_behaviour(OverflowReceiver(), make_template(Protocol.INTER_HUB, Perf.REQUEST))
            self.add_behaviour(OverflowReplies(), make_template(Protocol.INTER_HUB, Perf.AGREE))
            self.add_behaviour(OverflowReplies(), make_template(Protocol.INTER_HUB, Perf.REFUSE))
            self.add_behaviour(OverflowAccepted(), make_template(Protocol.INTER_HUB, Perf.ACCEPT))
            self.add_behaviour(InterHubWatcher())
        if self.reposition_rules is not None:
            self.add_behaviour(DemandForecastReceiver(), make_template(Protocol.DEMAND_FORECAST, Perf.INFORM))
            self.add_behaviour(RepositionPlanner())

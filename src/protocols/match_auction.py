"""
src/protocols/match_auction.py   MatchAuctionProtocol (Hub -> Drivers; Rider is only informed)

One FSMBehaviour per auction attempt, run by the MicrohubAgent:

    START -> CFP_SENT -> COLLECTING_BIDS -> EVALUATING -+-> AWARDING -> INFORMING -> END
                                ^                       |       |
                                |                       |       +--(winner defaults)--> EVALUATING
                                |                       +-> FAILURE -> END
                            (retry = a NEW FSM with attempt + 1, started by the hub)

  START            build the CFP (pool, reserve = riders' max fare, deadline = now + T_auction)
  CFP_SENT         cfp -> every known driver
  COLLECTING_BIDS  propose / refuse until everyone answered or the deadline; late bids are
                   not accepted (the hub-level handler rejects them: GC-09)
  EVALUATING       common.auction.evaluate_bids (pure): validity, multi-attribute ranking, price
  AWARDING         accept-proposal -> winner; wait T_award for its `confirm` (ID-05, CC-03).
                   Silent or refusing winner: cancel/skip, price the rest again without it.
  INFORMING        reject-proposal -> every other bidder (GC-09), then inform(MatchResult) -> riders.
                   The rider hears about the match only AFTER the driver confirmed (ID-05).
  FAILURE          no valid bid / nobody confirmed: reject-proposal -> every bidder; if attempts
                   are left the hub retries with widened detour, otherwise riders are told
                   (AuctionOutcome awarded=False) and Payment is asked to refund (FE-01).
  END              hand the finished context back to the hub.

Messages (all with thread = pool_id)
  Hub -> Driver   cfp(CFP) · accept-proposal(MatchResult) · reject-proposal(AuctionOutcome)
                  · cancel(text, voids an accept that was not confirmed in time)
  Driver -> Hub   propose(Bid) · refuse(reason) · confirm(MatchResult) · refuse(reason) to an accept
  Hub -> Rider    inform(MatchResult) · inform(AuctionOutcome awarded=False)
  Hub -> Payment  request(TripFailed) on the RefundProtocol, only on final failure

Robustness: an exception in any state is logged, recorded in the context and routed to FAILURE,
because SPADE kills a whole FSM on an unhandled error and the escrowed riders would never be
told. Every step is appended to `ctx.trace` and logged as one line, so the log shows the
full protocol.
"""
import logging
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from spade.behaviour import FSMBehaviour, State

from common import clock
from common.auction import AuctionResult, BidReject, evaluate_bids, split_fare
from common.pooling import fare_ceiling, split_fare_weighted
from common.messages import (
    make_accept_proposal, make_auction_failed, make_cfp, make_inform, make_message, make_reject_proposal,
    make_request,
)
from common.ontology import (
    AuctionOutcome, Bid, CFP, FailureReason, MatchResult, Perf, Pool, Protocol, TripFailed,
)
from protocols.payment_common import bare, expect

logger = logging.getLogger(__name__)

START, CFP_SENT, COLLECTING_BIDS = "START", "CFP_SENT", "COLLECTING_BIDS"
EVALUATING, AWARDING, INFORMING = "EVALUATING", "AWARDING", "INFORMING"
FAILURE, END = "FAILURE", "END"

MATCHED, RETRY, FAILED = "matched", "retry", "failed"      # AuctionContext.outcome


@dataclass
class AuctionContext:
    """Everything one auction attempt knows. Created by the hub, filled in by the states."""
    pool: Pool
    rider_jids: Dict[str, str]                 # request_id -> rider JID
    escrow_ids: List[str] = field(default_factory=list)
    attempt: int = 0
    # Pool formation (M9): fare weights (request_id -> direct ride seconds; empty = equal split),
    # the planned drop-off order and the largest detour that plan imposes.
    weights: Dict[str, float] = field(default_factory=dict)
    stop_order: List[str] = field(default_factory=list)
    plan_detour: Optional[float] = None
    # ---- filled while running ----
    cfp: Optional[CFP] = None
    recipients: List[str] = field(default_factory=list)
    bids: List[Bid] = field(default_factory=list)       # driver_id = sender JID, reputation = hub's
    refusals: Dict[str, str] = field(default_factory=dict)
    responded: set = field(default_factory=set)
    excluded: set = field(default_factory=set)          # winners that failed to confirm
    result: Optional[AuctionResult] = None
    match: Optional[MatchResult] = None
    outcome: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    trace: List[dict] = field(default_factory=list)

    @property
    def pool_id(self) -> str:
        return self.pool.pool_id


def note(ctx: AuctionContext, state: str, event: str, **details) -> None:
    ctx.trace.append({"t": clock.now(), "state": state, "event": event, **details})
    extra = " ".join(f"{k}={v}" for k, v in details.items())
    logger.info("[%s #%d] %-15s %s %s", ctx.pool_id, ctx.attempt, state, event, extra)


class _AuctionState(State):
    name = ""

    def __init__(self, ctx: AuctionContext):
        super().__init__()
        self.ctx = ctx

    async def run(self):
        try:
            await self.step()
        except Exception as e:                       # see module docstring
            logger.exception("%s: auction %s failed in %s", self.agent.jid, self.ctx.pool_id, self.name)
            self.ctx.error = f"{type(e).__name__}: {e}"
            note(self.ctx, self.name, "error", error=self.ctx.error)
            self.set_next_state(END if self.name in (FAILURE, END) else FAILURE)

    async def step(self):
        raise NotImplementedError

    async def reject(self, jid: str, awarded: bool, winner: Optional[str], reason: FailureReason):
        await self.send(make_reject_proposal(
            jid, AuctionOutcome(self.ctx.pool_id, awarded, winner, reason)))


class _Start(_AuctionState):
    name = START

    async def step(self):
        ctx, hub = self.ctx, self.agent
        now = clock.now()
        ctx.started_at = now
        reqs = ctx.pool.requests
        if ctx.weights and len(reqs) > 1:           # proportional split: nobody's share may exceed their max_fare
            reserve = fare_ceiling({r.request_id: r.max_fare for r in reqs}, ctx.weights)
        else:
            reserve = min(r.max_fare for r in reqs) * len(reqs)     # an equal split never exceeds any max_fare
        tariff = hub.tariff_reserve(reqs, ctx.stop_order, ctx.attempt)
        if tariff is not None:                                   # bound what a lone bidder can win
            reserve = min(reserve, tariff)
        cap = hub.current_fare_cap(now)                          # RI-01: a bid above the cap cannot win
        if cap is not None:
            reserve = min(reserve, cap)
        ctx.cfp = CFP(
            pool_id=ctx.pool_id, hub_id=hub.hub_id, pickup_node=hub.hub_id,
            rider_destinations=[r.destination_home for r in reqs],
            rider_time_windows={r.request_id: [r.time_window_start, r.time_window_end] for r in reqs},
            passenger_count=len(reqs), auction_deadline=now + hub.t_auction, max_total_fare=reserve,
            max_detour=(min(r.detour_tolerance for r in reqs) if ctx.plan_detour is None
                        else ctx.plan_detour + 1.0) + ctx.attempt * hub.widen_detour,
            attempt=ctx.attempt, rider_jids=[ctx.rider_jids[r.request_id] for r in reqs],
            stop_order=list(ctx.stop_order))
        note(ctx, START, "auction opens", riders=len(reqs), reserve=reserve,
             max_detour=ctx.cfp.max_detour, deadline=round(ctx.cfp.auction_deadline, 1),
             pricing=hub.rules.pricing)
        self.set_next_state(CFP_SENT)


class _CfpSent(_AuctionState):
    name = CFP_SENT

    async def step(self):
        ctx, hub = self.ctx, self.agent
        ctx.recipients = hub.eligible_drivers(clock.now(), ctx.attempt)          # CC-08
        for jid in ctx.recipients:
            await self.send(make_cfp(jid, ctx.cfp))
        note(ctx, CFP_SENT, "cfp sent", drivers=len(ctx.recipients))
        self.set_next_state(COLLECTING_BIDS)


class _Collecting(_AuctionState):
    name = COLLECTING_BIDS

    async def step(self):
        ctx, hub = self.ctx, self.agent
        while len(ctx.responded) < len(ctx.recipients):
            remaining = ctx.cfp.auction_deadline - clock.now()
            if remaining <= 0:
                break
            msg = await self.receive(timeout=max(clock.to_real(remaining), 0.001))
            if msg is None:
                break
            sender, perf = bare(msg.sender), msg.get_metadata("performative")
            if sender not in ctx.recipients:
                note(ctx, COLLECTING_BIDS, "ignored: not a bidder", sender=sender)
                continue
            if perf == Perf.REFUSE:
                if sender not in ctx.responded:
                    ctx.responded.add(sender)
                    ctx.refusals[sender] = msg.body or ""
                    note(ctx, COLLECTING_BIDS, "refuse", driver=sender, reason=msg.body or "-")
            elif perf == Perf.PROPOSE:
                await self._bid(msg, sender)
        late = [d for d in ctx.recipients if d not in ctx.responded]
        note(ctx, COLLECTING_BIDS, "bidding closed", bids=len(ctx.bids), refusals=len(ctx.refusals),
             silent=len(late))
        self.set_next_state(EVALUATING)

    async def _bid(self, msg, sender):
        ctx, hub = self.ctx, self.agent
        try:
            bid = expect(msg, Bid)
        except ValueError as e:
            ctx.responded.add(sender)
            ctx.refusals[sender] = "malformed_bid"
            note(ctx, COLLECTING_BIDS, "malformed bid", driver=sender, error=e)
            return
        if hub.account(bid.driver_id) != sender:
            note(ctx, COLLECTING_BIDS, "ignored: bid names another driver", sender=sender,
                 claims=bid.driver_id)
            return
        if bid.attempt != ctx.attempt:               # a straggler from an earlier attempt
            note(ctx, COLLECTING_BIDS, "stale bid rejected", driver=sender, attempt=bid.attempt)
            await self.reject(sender, False, None, FailureReason.TIMEOUT)
            return
        if sender in ctx.responded:
            note(ctx, COLLECTING_BIDS, "ignored: second answer", driver=sender)
            return
        ctx.responded.add(sender)
        # price is the driver's; the reputation is the hub's own view, never the driver's claim
        bid = replace(bid, driver_id=sender, reputation=hub.reputation_of(sender))
        hub_eta = hub.hub_eta(sender, ctx.cfp.pickup_node, clock.now())
        if hub_eta is not None and abs(hub_eta - bid.eta) > 1e-6:           # CC-10: the hub's ETA counts
            note(ctx, COLLECTING_BIDS, "eta recomputed", driver=sender, claimed=bid.eta, hub=round(hub_eta, 1))
            bid = replace(bid, eta=hub_eta)
        ctx.bids.append(bid)
        note(ctx, COLLECTING_BIDS, "bid", driver=sender, price=bid.price, eta=bid.eta,
             detour=bid.max_detour, rep=bid.reputation)


class _Evaluating(_AuctionState):
    name = EVALUATING

    async def step(self):
        ctx, hub = self.ctx, self.agent
        ctx.result = evaluate_bids(ctx.cfp, ctx.bids, hub.rules, now=clock.now(),
                                   excluded=ctx.excluded)
        res = ctx.result
        for bid, reason in res.rejected:
            note(ctx, EVALUATING, "bid invalid", driver=bid.driver_id, reason=reason)
        for rank, s in enumerate(res.ranking, 1):
            note(ctx, EVALUATING, "ranked", rank=rank, driver=s.bid.driver_id,
                 price=s.bid.price, penalty=round(s.penalty, 3), cost=round(s.cost, 3))
        if res.winner is None:
            note(ctx, EVALUATING, "no valid bid")
            self.set_next_state(FAILURE)
            return
        note(ctx, EVALUATING, "winner", driver=res.winner.bid.driver_id, bid=res.winner.bid.price,
             pays=res.price, rule=res.rules.pricing)
        self.set_next_state(AWARDING)


class _Awarding(_AuctionState):
    name = AWARDING

    async def step(self):
        ctx, hub = self.ctx, self.agent
        res = ctx.result
        winner = res.winner.bid
        if ctx.weights:
            fares = split_fare_weighted(res.price, {ctx.rider_jids[k]: w for k, w in ctx.weights.items()})
        else:
            fares = split_fare(res.price, list(ctx.rider_jids.values()))
        ctx.match = MatchResult(ctx.pool_id, winner.driver_id, res.price,
                                clock.now() + winner.eta, fares)
        await self.send(make_accept_proposal(winner.driver_id, ctx.match))
        note(ctx, AWARDING, "accept-proposal sent", driver=winner.driver_id, fare=res.price)

        deadline = clock.now() + hub.t_award
        verdict = ""
        while not verdict:
            remaining = deadline - clock.now()
            if remaining <= 0:
                verdict = "timeout"
                break
            msg = await self.receive(timeout=max(clock.to_real(remaining), 0.001))
            if msg is None:
                verdict = "timeout"
                break
            sender, perf = bare(msg.sender), msg.get_metadata("performative")
            if sender == winner.driver_id and perf == Perf.CONFIRM:
                verdict = "confirmed"
            elif sender == winner.driver_id and perf == Perf.REFUSE:
                verdict = f"refused ({msg.body or '-'})"
            elif perf == Perf.PROPOSE and sender in ctx.recipients:      # too late to take part
                note(ctx, AWARDING, "late bid rejected", driver=sender)
                await self.reject(sender, False, None, FailureReason.TIMEOUT)
        if verdict == "confirmed":
            note(ctx, AWARDING, "driver confirmed", driver=winner.driver_id)
            self.set_next_state(INFORMING)
            return
        note(ctx, AWARDING, f"award failed: {verdict}", driver=winner.driver_id)
        if verdict == "timeout":
            await self.send(make_message(winner.driver_id, Perf.CANCEL, Protocol.MATCH_AUCTION,
                                         "award_timeout", ctx.pool_id))
        ctx.excluded.add(winner.driver_id)
        ctx.match = None
        self.set_next_state(EVALUATING)                 # price the remaining bids again


class _Informing(_AuctionState):
    name = INFORMING

    async def step(self):
        ctx = self.ctx
        win = ctx.match.driver_id
        ranked = {s.bid.driver_id for s in ctx.result.ranking}
        for bid in ctx.bids:
            if bid.driver_id == win or bid.driver_id in ctx.excluded:
                continue
            reason = FailureReason.OUTBID if bid.driver_id in ranked else FailureReason.INVALID_BID
            await self.reject(bid.driver_id, True, win, reason)
            note(ctx, INFORMING, "reject-proposal", driver=bid.driver_id, reason=reason.value)
        for rider in ctx.rider_jids.values():
            await self.send(make_inform(rider, Protocol.MATCH_AUCTION, ctx.match, ctx.pool_id))
            note(ctx, INFORMING, "rider informed", rider=rider, driver=win, fare=ctx.match.rider_fares.get(rider))
        ctx.outcome = MATCHED
        self.set_next_state(END)


class _Failure(_AuctionState):
    name = FAILURE

    async def step(self):
        ctx, hub = self.ctx, self.agent
        reason = FailureReason.NO_BIDS if not ctx.bids else FailureReason.NO_MATCH
        for bid in ctx.bids:                             # every bidder gets an answer (GC-09)
            if bid.driver_id not in ctx.excluded:
                await self.reject(bid.driver_id, False, None, reason)
        final = ctx.attempt + 1 >= hub.max_attempts
        if not final:
            ctx.outcome = RETRY
            note(ctx, FAILURE, "no match; will retry widened", reason=reason.value,
                 next_attempt=ctx.attempt + 1)
        else:
            ctx.outcome = FAILED
            for rider in ctx.rider_jids.values():
                await self.send(make_auction_failed(rider, ctx.pool_id, reason))
            if hub.payment_jid and ctx.escrow_ids:
                await self.send(make_request(
                    hub.payment_jid, Protocol.REFUND,
                    TripFailed(ctx.pool_id, FailureReason.NO_MATCH, str(hub.jid.bare), list(ctx.escrow_ids)),
                    thread=ctx.pool_id))
            note(ctx, FAILURE, "pool unmatched: riders informed, refund requested",
                 reason=reason.value, escrows=len(ctx.escrow_ids))
        self.set_next_state(END)


class _End(_AuctionState):
    name = END

    async def step(self):
        ctx = self.ctx
        ctx.finished_at = clock.now()
        if not ctx.outcome:                              # error path: treat as a failed attempt
            ctx.outcome = FAILED
        note(ctx, END, "auction closed", outcome=ctx.outcome,
             seconds=round(ctx.finished_at - ctx.started_at, 2))
        self.agent.on_auction_finished(ctx)


class MatchAuctionFSM(FSMBehaviour):
    """One auction attempt. Register with make_template(Protocol.MATCH_AUCTION, thread=pool_id)."""

    def __init__(self, ctx: AuctionContext):
        self.ctx = ctx
        super().__init__()

    def setup(self):
        c = self.ctx
        self.add_state(START, _Start(c), initial=True)
        self.add_state(CFP_SENT, _CfpSent(c))
        self.add_state(COLLECTING_BIDS, _Collecting(c))
        self.add_state(EVALUATING, _Evaluating(c))
        self.add_state(AWARDING, _Awarding(c))
        self.add_state(INFORMING, _Informing(c))
        self.add_state(FAILURE, _Failure(c))
        self.add_state(END, _End(c))
        for a, b in [(START, CFP_SENT), (CFP_SENT, COLLECTING_BIDS), (COLLECTING_BIDS, EVALUATING),
                     (EVALUATING, AWARDING), (EVALUATING, FAILURE), (AWARDING, INFORMING),
                     (AWARDING, EVALUATING), (INFORMING, END), (FAILURE, END),
                     (START, FAILURE), (CFP_SENT, FAILURE), (COLLECTING_BIDS, FAILURE),
                     (AWARDING, FAILURE), (INFORMING, FAILURE)]:
            self.add_transition(a, b)

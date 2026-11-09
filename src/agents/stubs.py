"""
src/agents/stubs.py   Test doubles for Milestone 5 (not registered as scenario roles).

StubDriver   answers every CFP with a bid. `mode` scripts misbehaviour:
               bid (default) · refuse · silent · late (bids after the deadline)
               · no_confirm (ignores the award) · refuse_award · wrong_attempt
             Price = `price` (fixed) or `price_fn(cfp)`; ETA/detour fixed (self-reported: M6 has the
             hub compute them from DriverStatus + the graph).
ScriptedRider  sends a TripRequest to a hub, escrows the fare at Payment, and records everything
             it is told in `events` (kind, payload). `done` is set on MatchResult/AuctionOutcome.
"""
import asyncio
import logging
import uuid
from typing import Callable, List, Optional, Tuple

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_message, make_propose, make_refuse, make_request, make_template
from common.ontology import (
    AuctionOutcome, Bid, CFP, DriverArriving, Escrow, EscrowRequest, MatchResult, Perf, Pool, Protocol,
    RiderBoarded, RiderDroppedOff, RiderReady, Settlement, TripRequest, parse_payload,
)
from protocols.payment_common import bare

logger = logging.getLogger(__name__)


class _DriverBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        d, perf = self.agent, msg.get_metadata("performative")
        payload = None
        try:
            payload = parse_payload(msg, strict=False)
        except (ValueError, TypeError):
            pass
        d.inbox.append((perf, payload if payload is not None else msg.body))
        hub = bare(msg.sender)
        if perf == Perf.CFP and isinstance(payload, CFP):
            await d.on_cfp(self, hub, payload)
        elif perf == Perf.ACCEPT and isinstance(payload, MatchResult):
            if d.mode == "no_confirm":
                return
            if d.mode == "refuse_award":
                await self.send(make_refuse(hub, payload.pool_id, "unavailable"))
                return
            d.awards.append(payload)
            await self.send(make_message(hub, Perf.CONFIRM, Protocol.MATCH_AUCTION, payload, payload.pool_id))


class StubDriver(BaseAgent):
    role = "driver"

    def __init__(self, jid: str, password: str, mode: str = "bid", price: float = 8.0,
                 price_fn: Optional[Callable[[CFP], float]] = None, eta: float = 120.0,
                 capacity: int = 4, late_by: float = 5.0, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.mode, self.price, self.price_fn, self.eta = mode, price, price_fn, eta
        self.capacity, self.late_by = capacity, late_by
        self.inbox: List[Tuple[str, object]] = []
        self.awards: List[MatchResult] = []

    async def on_cfp(self, beh, hub: str, cfp: CFP):
        if self.mode == "silent":
            return
        if self.mode == "refuse":
            await beh.send(make_refuse(hub, cfp.pool_id, "busy"))
            return
        if self.mode == "late":
            await asyncio.sleep(clock.to_real(max(cfp.auction_deadline - clock.now(), 0) + self.late_by))
        price = self.price_fn(cfp) if self.price_fn else self.price
        attempt = cfp.attempt + 7 if self.mode == "wrong_attempt" else cfp.attempt
        bid = Bid(str(uuid.uuid4()), str(self.jid.bare), cfp.pool_id, price, self.eta,
                  cfp.max_detour, self.capacity, attempt)
        await beh.send(make_propose(hub, bid))

    async def setup(self):
        await super().setup()
        self.add_behaviour(_DriverBehaviour(), make_template(Protocol.MATCH_AUCTION))


class _RiderRequest(OneShotBehaviour):
    async def run(self):
        r = self.agent
        r.events.append(("request", r.request))
        await self.send(make_request(r.hub_jid, Protocol.TRIP_REQUEST, r.request, r.request.request_id))


class _RiderListener(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        r, perf, proto = self.agent, msg.get_metadata("performative"), msg.get_metadata("protocol")
        try:
            payload = parse_payload(msg, strict=False)
        except (ValueError, TypeError):
            payload = None
        if proto == Protocol.TRIP_REQUEST and perf == Perf.AGREE and isinstance(payload, Pool):
            r.events.append(("agree", payload))
            if r.payment_jid:
                esc = EscrowRequest(r.request.request_id, str(r.jid.bare), payload.pool_id,
                                    r.request.max_fare, r.request.subsidy_eligibility, bare(r.hub_jid))
                await self.send(make_request(r.payment_jid, Protocol.ESCROW, esc, r.request.request_id))
        elif proto == Protocol.TRIP_REQUEST:
            r.events.append(("refused", msg.body)); r.done.set()
        elif proto == Protocol.ESCROW:
            r.events.append(("escrow", payload if payload is not None else msg.body))
        elif proto == Protocol.TRIP_EXECUTION:
            if isinstance(payload, DriverArriving):
                r.events.append(("arriving", payload))
                if not r.no_show:
                    await asyncio.sleep(clock.to_real(r.ready_after))
                    await self.send(make_message(
                        bare(msg.sender), Perf.INFORM, Protocol.TRIP_EXECUTION,
                        RiderReady(payload.request_id, payload.pool_id, clock.now()), payload.pool_id))
            elif isinstance(payload, RiderBoarded):
                r.events.append(("boarded", payload))
            elif isinstance(payload, RiderDroppedOff):
                r.events.append(("dropped", payload)); r.trip_done.set()
        elif proto == Protocol.SETTLEMENT and isinstance(payload, Settlement):
            r.events.append(("settled", payload))
        elif proto == Protocol.MATCH_AUCTION and isinstance(payload, MatchResult):
            r.events.append(("matched", payload)); r.done.set()
        elif proto == Protocol.MATCH_AUCTION and isinstance(payload, AuctionOutcome):
            r.events.append(("unmatched", payload)); r.done.set()


class ScriptedRider(BaseAgent):
    role = "rider"

    def __init__(self, jid: str, password: str, hub_jid: str = "", payment_jid: Optional[str] = None,
                 request: Optional[TripRequest] = None, auto_request: bool = True,
                 no_show: bool = False, ready_after: float = 5.0, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_jid, self.payment_jid, self.request = hub_jid, payment_jid, request
        self.auto_request = auto_request
        self.events: List[Tuple[str, object]] = []
        self.done = asyncio.Event()
        self.trip_done = asyncio.Event()
        self.no_show, self.ready_after = no_show, ready_after

    def kinds(self) -> List[str]:
        return [k for k, _ in self.events]

    def last(self, kind: str):
        return next((p for k, p in reversed(self.events) if k == kind), None)

    async def setup(self):
        await super().setup()
        self.add_behaviour(_RiderListener(), make_template(Protocol.TRIP_REQUEST))
        self.add_behaviour(_RiderListener(), make_template(Protocol.ESCROW))
        self.add_behaviour(_RiderListener(), make_template(Protocol.MATCH_AUCTION))
        self.add_behaviour(_RiderListener(), make_template(Protocol.TRIP_EXECUTION))
        self.add_behaviour(_RiderListener(), make_template(Protocol.SETTLEMENT))
        if self.auto_request and self.request:
            self.send_request()

    def send_request(self):
        self.add_behaviour(_RiderRequest())

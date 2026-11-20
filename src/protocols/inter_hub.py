"""
src/protocols/inter_hub.py   Inter-hub coordination (Milestone 11): hubs absorb overflow and recover from failure.

InterHubCoordinationProtocol (all messages between hubs, plus one notice to a rider)

    Hub --inform(HubHeartbeat)------> each neighbour hub    every T_heartbeat: "I am alive, I see N idle drivers,
                                                            and these are the requests I have not matched yet"
    Hub --request(OverflowRequest)--> one neighbour         "I cannot find a driver for this pool, please take it"
        <--agree | refuse(reason)---                        (same thread = pool id)
    Hub --accept-proposal-----------> that neighbour        only if the agree came in time: "yes, it is yours"; the
                                                            neighbour adopts the requests only on this message
    Hub --inform(Handover)----------> Rider                 "your request is now handled by me, talk to me"

Neighbours
    The k nearest hubs of a hub (and every hub that counts it among its k nearest). The relation is computed from
    the graph, so all hubs agree on it without exchanging anything.

Failure recovery (done when: a killed hub's requests are absorbed by its neighbours within T_failover)
    A hub is a copy-holder: every heartbeat carries the requests it has not matched yet (queued for a pool, pool
    proposed, waiting for escrow, in auction). A neighbour that has heard nothing for `t_failover` declares the
    hub failed. Each request of the failed hub is then given to ONE neighbour: the nearest live neighbour of the
    failed hub that covers the destination (ties by id). Every neighbour computes the same answer from the same
    data, so they do not need to talk to each other; the chosen one adopts the request (as if the rider had sent
    it to it) and sends the rider a Handover. Requests that were already matched are not touched: their driver
    finishes the trip.

Overflow (capacity)
    When a pool has failed `overflow_after` auctions (no driver wanted it), the hub offers it to the neighbour
    that covers all its destinations and reports the most idle drivers. The neighbour agrees if it has an idle
    driver and can serve the requests; the first hub, if the agree arrives within `t_overflow_reply`, gives the pool
    up, has the riders' escrows returned and sends accept-proposal; ONLY THEN does the neighbour adopt the requests and
    inform the riders (the riders escrow again with the new hub). On refuse or silence the hub resumes its own
    auction. (An earlier version adopted on agreeing; a reply that came after the first hub had stopped waiting
    left the rider served by both hubs.)

Known limits
    * A hub that is only silent (not dead) is treated as failed; if it comes back its requests may be served twice.
    * A rider's escrow for a pool of the dead hub stays held until Payment returns it (T_ESCROW_HOLD_MAX); the rider
      escrows again with the new hub.
    * Requests whose destination no live neighbour covers are not taken over; the rider times out as before.
"""
import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from common import clock
from common.messages import make_message, make_request
from common.ontology import (
    FailureReason, Handover, HubHeartbeat, OverflowRequest, Perf, Protocol, TripFailed, TripRequest, parse_payload,
)
from protocols.payment_common import bare

logger = logging.getLogger(__name__)

HANDED_OVER = "HANDED_OVER"
OVERFLOW_PENDING = "OVERFLOW_PENDING"


# ---- pure rules (no agents, easy to test) ---------------------------------------------------------------------------
def neighbours(network, hub_id: str, k: int) -> List[str]:
    """The k nearest hubs of `hub_id` plus every hub that has `hub_id` among its k nearest (symmetric)."""
    ids = sorted(network.hubs)

    def nearest(h: str) -> set:
        ranked = sorted((network.travel_time(h, o), o) for o in ids if o != h)
        return {o for t, o in ranked[:k] if t != math.inf}

    return sorted(nearest(hub_id) | {o for o in ids if o != hub_id and hub_id in nearest(o)})


def takeover_hub(network, req: TripRequest, failed: str, candidates: Iterable[str], now: float) -> Optional[str]:
    """The hub that adopts `req` of the failed hub: the nearest candidate covering its destination (ties by id)."""
    live = set(candidates) - {failed}
    for hub in network.hubs_covering(req.destination_home, now):        # nearest first, ties by id
        if hub in live:
            return hub
    return None


def overflow_target(network, requests: List[TripRequest], peers: Dict[str, "PeerView"], me: str, now: float,
                    exclude: Iterable[str] = ()) -> Optional[str]:
    """The live neighbour with the most idle drivers that covers every destination of the pool."""
    skip = set(exclude) | {me}
    best = []
    for hid, p in peers.items():
        if p.failed or hid in skip or p.idle_drivers <= 0:
            continue
        if all(hid in network.hubs_covering(r.destination_home, now) for r in requests):
            best.append((-p.idle_drivers, hid))
    return min(best)[1] if best else None


@dataclass
class PeerView:
    hub_id: str
    jid: str
    last_seen: float = -math.inf
    idle_drivers: int = 0
    queued: int = 0
    pending: List[TripRequest] = field(default_factory=list)
    failed: bool = False


OFFER_KEEP = 600.0               # sim seconds a neighbour remembers an offer it agreed to, waiting for the accept


@dataclass
class OverflowWait:
    rec: object                      # the PoolRecord offered to a neighbour
    attempt: int                     # the auction attempt to resume if the neighbour says no
    deadline: float
    target: str                      # the neighbour hub that was asked


# ---- behaviours -------------------------------------------------------------------------------------------------------
class _Send(OneShotBehaviour):
    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    async def run(self):
        await self.send(self.msg)


class _Adopt(OneShotBehaviour):
    """Take one request over from another hub: tell the rider, then handle it like a fresh request."""

    def __init__(self, req: TripRequest, from_hub: str, reason: FailureReason):
        super().__init__()
        self.req, self.from_hub, self.reason = req, from_hub, reason

    async def run(self):
        from agents.microhub import admit_request          # late import: microhub imports this module
        hub, req = self.agent, self.req
        sender = hub.account(req.rider_id)
        problem = hub.check_request(req, sender)
        if problem:
            hub.log(f"cannot adopt {req.request_id} from {self.from_hub}: {problem}")
            return
        hub.adopted.append(req.request_id)
        hub.log(f"adopts {req.request_id} from {self.from_hub} ({self.reason.value})")
        await self.send(make_message(sender, Perf.INFORM, Protocol.INTER_HUB,
                                     Handover(req.request_id, self.from_hub, hub.hub_id, str(hub.jid.bare), self.reason),
                                     req.request_id))
        await admit_request(self, hub, req, sender)


class InterHubWatcher(CyclicBehaviour):
    """Heartbeats out, failure detection, takeover, overflow time-outs."""

    def __init__(self):
        super().__init__()
        self._next_beat: Optional[float] = None

    async def run(self):
        await asyncio.sleep(0.05)                    # short slices keep the behaviour killable
        hub, now = self.agent, clock.now()
        try:
            if self._next_beat is None:
                self._next_beat = now + hub.t_heartbeat       # not at once: the other hubs may still be starting
                for p in hub.peers.values():         # grace: nobody is late before the first round
                    p.last_seen = max(p.last_seen, now)
            if now >= self._next_beat:
                self._next_beat = now + hub.t_heartbeat
                await self.beat(now)
            for p in list(hub.peers.values()):
                if not p.failed and now - p.last_seen > hub.t_failover:
                    p.failed = True
                    hub.log(f"{p.hub_id} silent for {now - p.last_seen:.0f}s: declared failed")
                    self.take_over(p, now)
            for pid, offer in list(hub.overflow_offers.items()):
                if now >= offer[4]:
                    hub.overflow_offers.pop(pid, None)
            for pool_id, w in list(hub.overflow_waits.items()):
                if now >= w.deadline:
                    hub.overflow_waits.pop(pool_id, None)
                    hub.log(f"overflow of {pool_id}: no answer, carrying on alone")
                    hub.resume_auction(w)
        except Exception:                            # a bad round must not switch coordination off
            logger.exception("%s: inter-hub round failed", hub.jid)

    async def beat(self, now: float) -> None:
        hub = self.agent
        hb = HubHeartbeat(hub.hub_id, now, hub.idle_driver_count(now), len(hub.queue), hub.pending_requests())
        for p in hub.peers.values():
            await self.send(make_message(p.jid, Perf.INFORM, Protocol.INTER_HUB, hb, f"hb-{hub.hub_id}"))

    def take_over(self, failed: PeerView, now: float) -> None:
        hub = self.agent
        if hub.network is None or not failed.pending:
            return
        around = set(neighbours(hub.network, failed.hub_id, hub.k_neighbours))
        candidates = {h for h in around if h == hub.hub_id or not (h in hub.peers and hub.peers[h].failed)}
        mine = [r for r in failed.pending
                if takeover_hub(hub.network, r, failed.hub_id, candidates, now) == hub.hub_id]
        hub.log(f"takeover from {failed.hub_id}: {len(mine)} of {len(failed.pending)} pending requests are mine")
        failed.pending = []
        for req in mine:
            hub.add_behaviour(_Adopt(req, failed.hub_id, FailureReason.HUB_FAILED))


class HeartbeatReceiver(CyclicBehaviour):
    """inform(HubHeartbeat) from a neighbour: remember its load and its pending requests."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        try:
            hb = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        peer = hub.peers.get(getattr(hb, "hub_id", ""))
        if not isinstance(hb, HubHeartbeat) or peer is None or bare(msg.sender) != peer.jid:
            return
        if peer.failed:
            hub.log(f"{peer.hub_id} is back")
        peer.failed, peer.last_seen = False, clock.now()
        peer.idle_drivers, peer.queued, peer.pending = hb.idle_drivers, hb.queued, list(hb.pending)


class OverflowReceiver(CyclicBehaviour):
    """request(OverflowRequest) from a neighbour: agree (and adopt) or refuse(reason)."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub, sender = self.agent, bare(msg.sender)
        try:
            ov = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            ov = None
        peer = hub.peers.get(getattr(ov, "from_hub", ""))
        if not isinstance(ov, OverflowRequest) or peer is None or peer.jid != sender:
            await self.send(make_message(sender, Perf.REFUSE, Protocol.INTER_HUB, "not_a_neighbour", msg.thread))
            return
        reason = ""
        if hub.idle_driver_count(clock.now()) <= 0:
            reason = "no_idle_driver"
        else:
            for r in ov.pending_requests:
                reason = hub.check_request(r, hub.account(r.rider_id))
                if reason:
                    break
        if reason:
            hub.log(f"overflow {msg.thread} from {ov.from_hub} refused: {reason}")
            await self.send(make_message(sender, Perf.REFUSE, Protocol.INTER_HUB, reason, msg.thread))
            return
        hub.overflow_offers[msg.thread or ""] = (list(ov.pending_requests), ov.from_hub, ov.reason, sender,
                                                 clock.now() + OFFER_KEEP)
        await self.send(make_message(sender, Perf.AGREE, Protocol.INTER_HUB, "accepted", msg.thread))      # adopt only on accept


class OverflowReplies(CyclicBehaviour):
    """agree / refuse from the neighbour an overflow request was sent to."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        w = hub.overflow_waits.get(msg.thread or "")
        if w is None:
            return
        peer = next((p for p in hub.peers.values() if p.jid == bare(msg.sender)), None)
        if peer is None or peer.hub_id != w.target:
            return
        hub.overflow_waits.pop(msg.thread, None)
        if msg.get_metadata("performative") == Perf.AGREE:
            hub.finish_overflow(w)
            await self.send(make_message(bare(msg.sender), Perf.ACCEPT, Protocol.INTER_HUB, "yours", msg.thread))
        else:
            hub.log(f"overflow of {msg.thread} refused by {peer.hub_id} ({msg.body}); carrying on alone")
            hub.resume_auction(w)


class OverflowAccepted(CyclicBehaviour):
    """accept-proposal from the hub that offered a pool: it has given the pool up, the requests are ours now."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        hub = self.agent
        offer = hub.overflow_offers.pop(msg.thread or "", None)
        if offer is None or bare(msg.sender) != offer[3]:
            return
        requests, from_hub, reason, _, _ = offer
        for r in requests:
            hub.add_behaviour(_Adopt(r, from_hub, reason))


def refund_request(hub, rec) -> Optional[object]:
    """Message asking Payment to return the escrows of a pool the hub gives up (None: nothing is held)."""
    if not rec.escrows or not hub.payment_jid:
        return None
    return make_request(hub.payment_jid, Protocol.REFUND,
                        TripFailed(rec.pool.pool_id, FailureReason.NO_BIDS, str(hub.jid.bare), list(rec.escrows.values())),
                        thread=rec.pool.pool_id)

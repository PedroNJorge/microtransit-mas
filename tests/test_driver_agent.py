"""
Milestone 6, protocol level: a real DriverAgent against scripted hubs (no MicrohubAgent), to
pin down the message-level behaviour of the lock: bid, refuse(locked), expiry, reject, accept,
cancel, plus DriverStatus broadcasts (CC-07) and the RepositionProtocol.
"""
import asyncio
import uuid

import pytest
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.driver import DriverAgent
from common import clock
from common.graph import build_grid_network
from common.lock import LockState
from common.messages import make_cfp, make_message, make_reject_proposal, make_accept_proposal
from common.ontology import (
    AuctionOutcome, Bid, CFP, DriverState, DriverStatus, FailureReason, MatchResult, Perf, Protocol,
    RepositionRequest, parse_payload,
)

from conftest import XMPP_PORT

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SPEED = 30
HUB_NODE = "n1_1"


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


class Probe(Agent):
    """Plays a hub: records everything, sends whatever the test says."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.inbox = []

    class Rx(CyclicBehaviour):
        async def run(self):
            m = await self.receive(timeout=0.2)
            if m is not None:
                self.agent.inbox.append(m)

    async def setup(self):
        self.add_behaviour(self.Rx())

    async def tell(self, msg):
        sent = asyncio.Event()

        class Tx(OneShotBehaviour):
            async def run(self):
                await self.send(msg)
                sent.set()

        self.add_behaviour(Tx())
        await asyncio.wait_for(sent.wait(), 5)

    def got(self, protocol=None, performative=None, thread=None):
        return [m for m in self.inbox
                if (protocol is None or m.get_metadata("protocol") == protocol)
                and (performative is None or m.get_metadata("performative") == performative)
                and (thread is None or m.thread == thread)]


async def until(cond, timeout=6.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class Rig:
    def __init__(self, net, driver_kw=None):
        n = uuid.uuid4().hex[:6]
        self.h1 = Probe(f"h1_{n}@localhost", "pw", port=XMPP_PORT)
        self.h2 = Probe(f"h2_{n}@localhost", "pw", port=XMPP_PORT)
        kw = dict(start_hub=HUB_NODE, network=net, t_provisional=60.0, dwell_pickup=1, dwell_dropoff=1,
                  hub_jids=[f"h1_{n}@localhost", f"h2_{n}@localhost"])
        kw.update(driver_kw or {})
        self.drv = DriverAgent(f"drv_{n}@localhost", "pw", port=XMPP_PORT, **kw)
        self.n = n

    async def __aenter__(self):
        for a in (self.h1, self.h2, self.drv):
            await asyncio.wait_for(a.start(auto_register=True), 20)
        return self

    async def __aexit__(self, *exc):
        for a in (self.drv, self.h1, self.h2):
            await a.stop()

    def cfp(self, hub, pool="pool-1", dest="n2_2", attempt=0, fare=20.0, riders=("rider@localhost",),
            detour=600.0, window_end=7200.0):
        return CFP(pool, str(hub.jid.bare), HUB_NODE, [dest] * len(riders),
                   {f"req-{i}": [0.0, window_end] for i, _ in enumerate(riders)}, len(riders),
                   clock.now() + 300, fare, detour, attempt, rider_jids=list(riders))

    def match(self, pool, fare=5.0):
        return MatchResult(pool, str(self.drv.jid.bare), fare, 0.0, {"rider@localhost": fare})

    async def cfp_and_wait(self, hub, **kw):
        c = self.cfp(hub, **kw)
        before = len(hub.got(Protocol.MATCH_AUCTION))
        await hub.tell(make_cfp(str(self.drv.jid.bare), c))
        assert await until(lambda: len(hub.got(Protocol.MATCH_AUCTION)) > before)
        return hub.got(Protocol.MATCH_AUCTION)[-1]


# ---- bidding -------------------------------------------------------------------------------

async def test_idle_driver_bids_its_true_cost_and_takes_the_lock(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        reply = await r.cfp_and_wait(r.h1)
        assert reply.get_metadata("performative") == Perf.PROPOSE
        bid = parse_payload(reply)
        # eta 0 (at the hub), route n1_1 -> n2_2 = 120 s: cost = 2.00 + 0.004 * 120
        assert bid.price == pytest.approx(2.48) and bid.eta == 0 and bid.max_detour == 0
        assert bid.driver_id == str(r.drv.jid.bare) and bid.attempt == 0
        assert r.drv.lock.state == LockState.PROVISIONAL and r.drv.state == DriverState.BIDDING


async def test_second_hub_gets_refuse_locked(xmpp_server, sim_clock, net):                  # CC-09
    async with Rig(net) as r:
        assert (await r.cfp_and_wait(r.h1)).get_metadata("performative") == Perf.PROPOSE
        reply = await r.cfp_and_wait(r.h2, pool="pool-B")
        assert reply.get_metadata("performative") == Perf.REFUSE and reply.body == "locked"
        assert r.drv.lock.holder.pool_id == "pool-1"                                      # first lock untouched


async def test_duplicate_cfp_gets_the_same_bid_again(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        first = parse_payload(await r.cfp_and_wait(r.h1))
        second = parse_payload(await r.cfp_and_wait(r.h1))
        assert first.bid_id == second.bid_id


async def test_reject_proposal_releases_the_lock_and_driver_bids_elsewhere(xmpp_server, sim_clock, net):   # CC-04
    async with Rig(net) as r:
        await r.cfp_and_wait(r.h1)
        await r.h1.tell(make_reject_proposal(str(r.drv.jid.bare),
                                             AuctionOutcome("pool-1", True, "other", FailureReason.OUTBID)))
        assert await until(lambda: r.drv.lock.state == LockState.FREE)
        assert (await r.cfp_and_wait(r.h2, pool="pool-B")).get_metadata("performative") == Perf.PROPOSE


async def test_reject_from_the_wrong_hub_is_ignored(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.cfp_and_wait(r.h1)
        await r.h2.tell(make_reject_proposal(str(r.drv.jid.bare),
                                             AuctionOutcome("pool-1", False, None, FailureReason.TIMEOUT)))
        await asyncio.sleep(0.5)
        assert r.drv.lock.state == LockState.PROVISIONAL


async def test_provisional_lock_expires_and_the_late_accept_is_refused(xmpp_server, sim_clock, net):  # CC-02
    async with Rig(net, dict(t_provisional=20.0)) as r:                  # 20 sim s = 0.67 s real
        await r.cfp_and_wait(r.h1)
        assert await until(lambda: r.drv.lock.state == LockState.FREE, 4)
        await r.h1.tell(make_accept_proposal(str(r.drv.jid.bare), r.match("pool-1")))
        assert await until(lambda: any(m.get_metadata("performative") == Perf.REFUSE and m.body == "lock_expired"
                                       for m in r.h1.got(Protocol.MATCH_AUCTION)))
        assert r.drv.assignment is None and r.drv.state == DriverState.IDLE


async def test_expired_lock_frees_the_driver_for_another_hub(xmpp_server, sim_clock, net):
    async with Rig(net, dict(t_provisional=20.0)) as r:
        await r.cfp_and_wait(r.h1)
        await asyncio.sleep(clock.to_real(25))
        assert (await r.cfp_and_wait(r.h2, pool="pool-B")).get_metadata("performative") == Perf.PROPOSE


async def test_accept_without_a_bid_is_refused(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.h1.tell(make_accept_proposal(str(r.drv.jid.bare), r.match("ghost")))
        assert await until(lambda: r.h1.got(Protocol.MATCH_AUCTION, Perf.REFUSE))
        assert r.h1.got(Protocol.MATCH_AUCTION, Perf.REFUSE)[0].body == "no_lock"


async def test_accept_from_another_hub_is_refused_and_lock_kept(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.cfp_and_wait(r.h1)
        await r.h2.tell(make_accept_proposal(str(r.drv.jid.bare), r.match("pool-1")))
        assert await until(lambda: r.h2.got(Protocol.MATCH_AUCTION, Perf.REFUSE))
        assert r.h2.got(Protocol.MATCH_AUCTION, Perf.REFUSE)[0].body == "not_holder"
        assert r.drv.lock.state == LockState.PROVISIONAL


async def test_unknown_hub_is_refused(xmpp_server, sim_clock, net):
    async with Rig(net, dict(hub_jids=["someone_else@localhost"])) as r:
        reply = await r.cfp_and_wait(r.h1)
        assert reply.get_metadata("performative") == Perf.REFUSE and reply.body == "unknown_hub"


# ---- refusals that do not take the lock ------------------------------------------------------

@pytest.mark.parametrize("kw,cfp_kw,reason", [
    ({"capacity": 1}, {"riders": ("a@localhost", "b@localhost")}, "no_capacity"),
    ({"shift_end": 100.0}, {}, "shift_ending"),
    ({}, {"fare": 1.0}, "over_reserve"),
    ({"start_hub": "n5_5"}, {"window_end": 100.0}, "too_late"),
    ({}, {"dest": "nowhere"}, "unreachable"),
])
async def test_driver_refuses_with_a_reason_and_keeps_the_lock_free(xmpp_server, sim_clock, net, kw, cfp_kw, reason):
    async with Rig(net, kw) as r:
        reply = await r.cfp_and_wait(r.h1, **cfp_kw)
        assert reply.get_metadata("performative") == Perf.REFUSE and reply.body == reason
        assert r.drv.lock.state == LockState.FREE


async def test_detour_beyond_the_riders_tolerance_is_refused(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        c = r.cfp(r.h1, riders=("a@localhost", "b@localhost"), detour=0.0)
        c.rider_destinations = ["n1_3", "n3_1"]                     # opposite directions: a detour for someone
        await r.h1.tell(make_cfp(str(r.drv.jid.bare), c))
        assert await until(lambda: r.h1.got(Protocol.MATCH_AUCTION))
        m = r.h1.got(Protocol.MATCH_AUCTION)[0]
        assert m.get_metadata("performative") == Perf.REFUSE and m.body == "detour_too_large"


# ---- DriverStatusProtocol (CC-07) ------------------------------------------------------------

def states(hub):
    return [parse_payload(m).state for m in hub.got(Protocol.DRIVER_STATUS)]


async def test_status_is_broadcast_on_start_and_on_every_lock_change(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        assert await until(lambda: states(r.h1) == [DriverState.IDLE])
        await r.cfp_and_wait(r.h1)
        assert await until(lambda: states(r.h1)[-1] == DriverState.BIDDING)
        await r.h1.tell(make_reject_proposal(str(r.drv.jid.bare),
                                             AuctionOutcome("pool-1", False, None, FailureReason.NO_BIDS)))
        assert await until(lambda: states(r.h1)[-1] == DriverState.IDLE)
        assert states(r.h1) == [DriverState.IDLE, DriverState.BIDDING, DriverState.IDLE]
        assert states(r.h2) == states(r.h1)                       # GC-14: every hub sees the same


async def test_status_reports_expiry(xmpp_server, sim_clock, net):
    async with Rig(net, dict(t_provisional=20.0)) as r:
        await r.cfp_and_wait(r.h1)
        assert await until(lambda: states(r.h1)[-2:] == [DriverState.BIDDING, DriverState.IDLE], 5)


async def test_heartbeat_repeats_the_status(xmpp_server, sim_clock, net):
    async with Rig(net, dict(t_status=15.0)) as r:                # every 0.5 s real
        assert await until(lambda: len(r.h1.got(Protocol.DRIVER_STATUS)) >= 3, 5)
        assert set(states(r.h1)) == {DriverState.IDLE}


# ---- RepositionProtocol ----------------------------------------------------------------------

def reposition(r, sp="sp-n4_1", incentive=2.0, thread="rp-1"):
    return make_message(str(r.drv.jid.bare), Perf.REQUEST, Protocol.REPOSITION,
                        RepositionRequest(str(r.h1.jid.bare), str(r.drv.jid.bare), sp, incentive), thread)


async def test_idle_driver_repositions_and_reports_arrival(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.h1.tell(reposition(r))
        assert await until(lambda: r.h1.got(Protocol.REPOSITION, Perf.AGREE))
        assert await until(lambda: states(r.h1)[-1] in (DriverState.REPOSITIONING, DriverState.IDLE))
        assert await until(lambda: r.h1.got(Protocol.REPOSITION, Perf.INFORM), 8)
        assert r.drv.location == "n4_1" and r.drv.state == DriverState.IDLE
        assert DriverState.REPOSITIONING in states(r.h1)


async def test_reposition_is_refused_while_locked(xmpp_server, sim_clock, net):             # GC-13
    async with Rig(net) as r:
        await r.cfp_and_wait(r.h1)
        await r.h1.tell(reposition(r))
        assert await until(lambda: r.h1.got(Protocol.REPOSITION, Perf.REFUSE))
        assert r.h1.got(Protocol.REPOSITION, Perf.REFUSE)[0].body == "locked"
        assert r.drv.location == HUB_NODE


async def test_reposition_with_too_small_an_incentive_is_refused(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.h1.tell(reposition(r, incentive=0.0))
        assert await until(lambda: r.h1.got(Protocol.REPOSITION, Perf.REFUSE))
        assert r.h1.got(Protocol.REPOSITION, Perf.REFUSE)[0].body == "incentive_too_low"


async def test_cfp_during_repositioning_is_refused(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await r.h1.tell(reposition(r, sp="sp-n4_1"))
        assert await until(lambda: r.drv.activity == "repositioning")
        reply = await r.cfp_and_wait(r.h2, pool="pool-B")
        assert reply.get_metadata("performative") == Perf.REFUSE and reply.body == "repositioning"


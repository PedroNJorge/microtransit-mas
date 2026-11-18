"""
Milestone 11 done-when: an integration test kills a hub and observes neighbouring hubs absorbing requests.

  hub A queues a rider's request (long batch window) and tells its neighbour B in every heartbeat -> A is killed
  -> B hears nothing for t_failover, declares A failed, adopts the request, the rider gets a Handover and the trip
  is completed through B.
  Overflow: hub A has no driver to call, so after one failed auction it offers the pool to B, which has one.
"""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import COMPLETED, RiderAgent
from common import clock
from common.graph import build_grid_network

from conftest import XMPP_PORT
from test_driver_agent import until

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SPEED = 30
A, B = "n1_1", "n1_4"
DEST = "n1_2"                                   # covered by both A and B
T_HEARTBEAT, T_FAILOVER = 30.0, 90.0            # sim seconds (1 and 3 real seconds at SPEED 30)


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


class World:
    def __init__(self, net, a_drivers=False, a_pool=600.0, **a_kw):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0)
        self.rep = ReputationAgent(jid("rep"), "pw", port=XMPP_PORT)
        self.a_jid, self.b_jid = jid("huba"), jid("hubb")
        common = dict(network=net, payment_jid=self.pay.jid, reputation_jid=self.rep.jid, t_auction=60.0,
                      t_award=40.0, t_escrow=90.0, t_heartbeat=T_HEARTBEAT, t_failover=T_FAILOVER, k_neighbours=1)
        self.a = MicrohubAgent(self.a_jid, "pw", port=XMPP_PORT, hub_id=A, t_pool=a_pool,
                               peer_jids={B: self.b_jid}, **common, **a_kw)
        self.b = MicrohubAgent(self.b_jid, "pw", port=XMPP_PORT, hub_id=B, t_pool=20.0,
                               peer_jids={A: self.a_jid}, **common)
        self.rep.hub_jids = [str(self.a.jid.bare), str(self.b.jid.bare)]
        self.driver = DriverAgent(jid("d0"), "pw", port=XMPP_PORT, start_hub=B, network=net,
                                  hub_jids=[self.b.jid], payment_jid=self.pay.jid, t_provisional=300.0,
                                  dwell_pickup=1, dwell_dropoff=1)
        self.b.driver_jids = [str(self.driver.jid.bare)]
        self.a.driver_jids = [str(self.driver.jid.bare)] if a_drivers else []
        self.rider = RiderAgent(jid("rider"), "pw", port=XMPP_PORT, hub_jids={A: self.a.jid, B: self.b.jid},
                                network=net, payment_jid=self.pay.jid, reputation_jid=self.rep.jid, ready_delay=2.0)
        self.agents = [self.pay, self.rep, self.a, self.b, self.driver, self.rider]

    async def __aenter__(self):
        for x in self.agents:
            await asyncio.wait_for(x.start(auto_register=True), 20)
        assert await until(lambda: len(self.b.registry) == 1)
        return self

    async def __aexit__(self, *exc):
        for x in reversed(self.agents):
            try:
                await x.stop()
            except Exception:
                pass


async def test_a_killed_hubs_pending_request_is_absorbed_by_its_neighbour(xmpp_server, sim_clock, net):
    async with World(net) as w:
        req = w.rider.request_trip(DEST, 20.0, hub_id=A, pooling_willingness=True)
        assert req is not None
        # A keeps the request in its batch queue and copies it to B with every heartbeat
        assert await until(lambda: any(r.request_id == req.request_id for r in w.b.peers[A].pending), timeout=15)
        await w.a.stop()
        killed_at = clock.now()
        assert await until(lambda: req.request_id in w.b.adopted, timeout=30)
        assert w.b.peers[A].failed
        assert clock.now() - killed_at <= T_FAILOVER + 2 * T_HEARTBEAT, "took too long"
        await asyncio.wait_for(w.rider.ended.wait(), 90)
        trip = w.rider.trips[-1]
        assert trip.outcome == COMPLETED, (trip.outcome, trip.reason, trip.log)
        assert trip.hub_jid == str(w.b.jid.bare)


async def test_a_hub_with_no_driver_passes_the_pool_to_a_neighbour_that_has_one(xmpp_server, sim_clock, net):
    async with World(net, a_pool=0.0, overflow_after=1, retry_delay=20.0, max_attempts=4) as w:
        assert await until(lambda: w.a.peers[B].idle_drivers == 1, timeout=15)       # A learned B has a driver
        req = w.rider.request_trip(DEST, 20.0, hub_id=A, pooling_willingness=False)
        assert req is not None
        await asyncio.wait_for(w.rider.ended.wait(), 90)
        trip = w.rider.trips[-1]
        assert trip.outcome == COMPLETED, (trip.outcome, trip.reason, trip.log)
        assert req.request_id in w.b.adopted
        assert any(rec.state == "HANDED_OVER" for rec in w.a.pools.values())
        assert trip.hub_jid == str(w.b.jid.bare)

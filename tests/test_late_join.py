"""
Late join (plan item 4): a rider joins a pool whose driver is still on the way to the hub.

  rider 1 is matched (driver far from the hub) -> rider 2 asks for a trip in the same direction -> the hub finds that
  rider 2 fits (everyone's detour within their own tolerance) and asks the driver -> the driver agrees (provisionally) ->
  rider 2 is proposed the new fare, accepts and escrows -> the join is final -> one vehicle takes both; rider 1's fare
  did not go up; the driver is paid the extra driving; both riders are settled and the ledger balances.
  The driver can refuse (late_join off, or an extra pay below its cost): rider 2 is then pooled the normal way.
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
HUB, FAR = "n1_1", "n1_5"


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
    def __init__(self, net, driver_kw=None, hub_kw=None):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0)
        self.rep = ReputationAgent(jid("rep"), "pw", port=XMPP_PORT)
        hk = dict(t_auction=90.0, t_award=60.0, t_escrow=120.0, t_pool=0.0, late_join=True)
        hk.update(hub_kw or {})
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=self.pay.jid, reputation_jid=self.rep.jid, **hk)
        self.rep.hub_jids = [str(self.hub.jid.bare)]
        self.driver = DriverAgent(jid("d0"), "pw", port=XMPP_PORT, start_hub=FAR, network=net,
                                  hub_jids=[self.hub.jid], payment_jid=self.pay.jid, t_provisional=300.0,
                                  dwell_pickup=1, dwell_dropoff=1)
        for k, v in (driver_kw or {}).items():
            setattr(self.driver, k, v)
        self.hub.driver_jids = [str(self.driver.jid.bare)]
        self.riders = [RiderAgent(jid(f"rider{i}"), "pw", port=XMPP_PORT, hub_jids={HUB: self.hub.jid}, network=net,
                                  payment_jid=self.pay.jid, reputation_jid=self.rep.jid, ready_delay=2.0)
                       for i in range(2)]
        self.agents = [self.pay, self.rep, self.hub, self.driver, *self.riders]

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        assert await until(lambda: len(self.hub.registry) == 1)
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            try:
                await a.stop()
            except Exception:
                pass

    async def finish(self, *riders, timeout=150):
        """Wait for the riders to end; on a time-out fail with everything needed to see where it stuck."""
        try:
            for r in riders:
                await asyncio.wait_for(r.ended.wait(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError("stuck: " + repr([(r.state, r.trip.log if r.trip else r.trips[-1].log) for r in self.riders])
                                 + " pools=" + repr({k: v.state for k, v in self.hub.pools.items()})
                                 + " late_waits=" + repr(list(self.hub.late_waits)) + " driver=" + repr(
                                     (self.driver.activity, self.driver.location,
                                      self.driver.assignment and (self.driver.assignment.frozen,
                                                                  self.driver.assignment.riders,
                                                                  list(self.driver.assignment.pending)))))

    async def first_rider_assigned(self):
        r1 = self.riders[0].request_trip("n1_2", 20.0, hub_id=HUB, pooling_willingness=False)
        assert r1 is not None
        assert await until(lambda: any(r.state == "ASSIGNED" for r in self.hub.pools.values()), timeout=40)
        return r1


async def normal_way(w):
    """The driver said no: nobody joined late, rider 2 was handled as any request (this world has a single driver, who is
    busy, so it may well end unmatched), rider 1 was not disturbed and the money balances."""
    assert w.hub.late_joined == [] and all(not r.late_of or r.state != "ASSIGNED" for r in w.hub.pools.values())
    assert w.riders[0].trips[-1].outcome == COMPLETED, w.riders[0].trips[-1].log
    assert not w.riders[0].trips[-1].amended
    assert w.pay.ledger.check_conservation()


async def test_a_rider_joins_a_pool_whose_driver_is_still_on_the_way(xmpp_server, sim_clock, net):
    async with World(net, hub_kw={"pool_incentive_per_kg": 20000.0}) as w:   # grid edges are 1 m: a huge rate makes the cents visible
        await w.first_rider_assigned()
        base = next(r for r in w.hub.pools.values() if r.state == "ASSIGNED")
        fare_before = w.hub.matches[base.pool.pool_id].rider_fares[w.riders[0].jid.bare.__str__()]
        r2 = w.riders[1].request_trip("n1_3", 20.0, hub_id=HUB, pooling_willingness=True)
        assert r2 is not None
        await w.finish(w.riders[0], w.riders[1])
        t1, t2 = w.riders[0].trips[-1], w.riders[1].trips[-1]
        assert (t1.outcome, t2.outcome) == (COMPLETED, COMPLETED), (t1.log, t2.log)
        assert len(w.hub.late_joined) == 1 and len(base.late) == 1
        assert t1.boarded_at == t2.boarded_at or abs(t1.boarded_at - t2.boarded_at) < 5, "one vehicle, one pickup"
        assert t1.amended and t1.amended[0].new_fare <= fare_before + 1e-9           # nobody's fare rose
        assert await until(lambda: t1.settlement is not None and t2.settlement is not None, timeout=30)
        ledger = w.pay.ledger
        driver = str(w.driver.jid.bare)
        paid = t1.settlement.driver_payout + t2.settlement.driver_payout
        assert abs(ledger.balance(driver) - paid) < 0.011 and paid > fare_before        # the driver got the extra driving
        got = t1.settlement.pooling_incentive + t2.settlement.pooling_incentive           # CO2 incentive, from its own fund
        assert got > 0 and abs(ledger.pooling_spent - got) < 0.011
        assert ledger.check_conservation()


async def test_a_driver_that_refuses_leaves_the_rider_to_normal_pooling(xmpp_server, sim_clock, net):
    async with World(net, driver_kw={"late_join": False}) as w:
        await w.first_rider_assigned()
        r2 = w.riders[1].request_trip("n1_3", 20.0, hub_id=HUB, pooling_willingness=True)
        assert r2 is not None
        await w.finish(w.riders[0], w.riders[1])
        await normal_way(w)


async def test_a_driver_who_wants_more_than_the_hub_offers_refuses(xmpp_server, sim_clock, net):
    async with World(net, driver_kw={"bid_markup": 5.0}) as w:
        await w.first_rider_assigned()
        r2 = w.riders[1].request_trip("n1_3", 20.0, hub_id=HUB, pooling_willingness=True)
        assert r2 is not None
        await w.finish(w.riders[0], w.riders[1])
        await normal_way(w)

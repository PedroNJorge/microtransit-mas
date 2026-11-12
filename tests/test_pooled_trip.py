"""
Milestone 9 done-when: a trip where the detour stays within tolerance for both riders and the
pooling discount appears in settlement.

  two riders request within one batch window -> the hub's exact solver pools them (drop-off order,
  detour tolerance, time windows) -> both accept -> both escrow -> ONE auction, ONE driver ->
  fare split in proportion to each rider's direct ride -> settlement shows the pooling discount ->
  both riders rate the driver.
"""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import COMPLETED, DECLINED, RiderAgent
from common import clock
from common.graph import build_grid_network
from common.ontology import EscrowStatus

from conftest import XMPP_PORT
from test_driver_agent import until

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SPEED = 30
HUB = "n1_1"
T_POOL = 30.0                       # sim seconds (1 real second at SPEED 30)


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


class Grumpy(RiderAgent):
    """Declines every proposal (to exercise partial acceptance)."""

    def evaluate_pool(self, t, p):
        return "not_for_me"


class PoolWorld:
    def __init__(self, net, rider_classes=(RiderAgent, RiderAgent), n_drivers=2, hub_kw=None):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.net = net
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0)
        self.rep = ReputationAgent(jid("rep"), "pw", port=XMPP_PORT)
        hk = dict(t_auction=90.0, t_award=60.0, t_escrow=90.0, t_pool=T_POOL)
        hk.update(hub_kw or {})
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=self.pay.jid, reputation_jid=self.rep.jid, **hk)
        self.rep.hub_jids = [str(self.hub.jid.bare)]
        self.drivers = [DriverAgent(jid(f"d{i}"), "pw", port=XMPP_PORT, start_hub=HUB, network=net,
                                    hub_jids=[self.hub.jid], payment_jid=self.pay.jid, t_provisional=300.0,
                                    dwell_pickup=1, dwell_dropoff=1) for i in range(n_drivers)]
        self.hub.driver_jids = [str(d.jid.bare) for d in self.drivers]
        self.riders = [cls(jid(f"rider{i}"), "pw", port=XMPP_PORT, hub_jids={HUB: self.hub.jid}, network=net,
                           payment_jid=self.pay.jid, reputation_jid=self.rep.jid, ready_delay=2.0)
                       for i, cls in enumerate(rider_classes)]
        self.agents = [self.pay, self.rep, self.hub, *self.drivers, *self.riders]

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        assert await until(lambda: len(self.hub.registry) == len(self.drivers))
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def ride_all(self, requests, timeout=90):
        """requests: [(rider index, kwargs of request_trip)]"""
        for i, kw in requests:
            kw.setdefault("max_fare", 20.0)
            kw.setdefault("pooling_willingness", True)
            assert self.riders[i].request_trip(**kw) is not None
        await asyncio.wait_for(asyncio.gather(*(self.riders[i].ended.wait() for i, _ in requests)), timeout)


async def test_two_riders_share_a_driver(xmpp_server, sim_clock, net):
    async with PoolWorld(net) as w:
        a, b = w.riders
        # a's home is on the way to b's home: pooling costs nobody any detour
        await w.ride_all([(0, dict(destination="n1_2", detour_tolerance=120.0)),
                          (1, dict(destination="n1_3", detour_tolerance=120.0))])
        ta, tb = a.trips[-1], b.trips[-1]
        assert ta.outcome == tb.outcome == COMPLETED

        # ---- one pool, one auction, one driver ------------------------------------------------
        assert ta.pool_id == tb.pool_id
        rec = w.hub.pools[ta.pool_id]
        assert rec.pool.size == 2 and rec.plan is not None
        assert len([c for c in w.hub.finished if c.pool_id == ta.pool_id]) == 1
        assert ta.match.driver_id == tb.match.driver_id
        driver = next(d for d in w.drivers if str(d.jid.bare) == ta.match.driver_id)
        assert driver.completed == [ta.pool_id]
        assert len(w.hub.finished[-1].bids) == 2                        # a real auction between the two drivers

        # ---- the detour constraint is respected for both riders -------------------------------
        for t in (ta, tb):
            assert t.proposal.added_detour <= t.request.detour_tolerance
            assert t.proposal.shared_route == [HUB, "n1_2", "n1_3"]
            assert t.proposal.split_fare <= t.request.max_fare
        assert rec.plan.detour == {ta.request.request_id: 0.0, tb.request.request_id: 0.0}
        assert ta.dropped_at < tb.dropped_at                            # the nearer home is served first
        assert 0 < ta.boarded_at <= ta.dropped_at and abs(ta.boarded_at - tb.boarded_at) < 20

        # ---- fare split in proportion to the direct hub->home time (60 s : 120 s) ---------------
        total = ta.match.final_fare
        assert ta.fare + tb.fare == pytest.approx(total, abs=0.005)
        assert ta.fare == pytest.approx(total / 3, abs=0.01) and tb.fare == pytest.approx(2 * total / 3, abs=0.01)

        # ---- settlement: the driver gets the auction price; the pooling discount is visible ------
        assert await until(lambda: ta.settlement is not None and tb.settlement is not None, 15)
        for t in (ta, tb):
            assert t.settlement.pooling_discount > 0
            assert t.settlement.driver_payout == pytest.approx(t.fare, abs=0.005)
        assert w.ledger_balance(driver) == pytest.approx(total) if hasattr(w, "ledger_balance") else True
        assert w.pay.ledger.balance(str(driver.jid.bare)) == pytest.approx(total)
        for t in (ta, tb):
            assert w.pay.ledger.balance(str(t.request.rider_id)) == pytest.approx(100.0 - t.fare)
            assert w.pay.ledger.status(w.pay.ledger.pool_escrows(t.pool_id)[0].escrow_id) == EscrowStatus.SETTLED
        assert w.pay.ledger.check_conservation()

        # ---- both riders rate the driver ----------------------------------------------------------
        assert ta.rating is not None and tb.rating is not None
        assert await until(lambda: w.rep.count_of(ta.match.driver_id) == 2, 10)


async def test_riders_who_cannot_share_are_not_pooled(xmpp_server, sim_clock, net):
    # opposite corners and zero tolerance: any shared route makes the second rider detour
    async with PoolWorld(net) as w:
        await w.ride_all([(0, dict(destination="n1_3", detour_tolerance=0.0)),
                          (1, dict(destination="n3_1", detour_tolerance=0.0))])
        ta, tb = (r.trips[-1] for r in w.riders)
        assert ta.outcome == tb.outcome == COMPLETED
        assert ta.pool_id != tb.pool_id
        assert w.hub.pools[ta.pool_id].pool.size == w.hub.pools[tb.pool_id].pool.size == 1
        assert ta.settlement is None or ta.settlement.pooling_discount == 0.0


async def test_rider_not_willing_to_pool_does_not_wait_for_the_batch(xmpp_server, sim_clock, net):
    async with PoolWorld(net, hub_kw=dict(t_pool=600.0)) as w:           # a very long window
        req = w.riders[0].request_trip("n1_2", max_fare=20.0, pooling_willingness=False)
        assert req is not None
        await asyncio.wait_for(w.riders[0].ended.wait(), 60)
        t = w.riders[0].trips[-1]
        assert t.outcome == COMPLETED and t.proposal.shared_route == [HUB, "n1_2"]
        assert not w.hub.queue


async def test_partial_acceptance_repools_the_rest(xmpp_server, sim_clock, net):
    # rider 1 declines the shared pool; rider 0 is told it is back in the queue and travels solo
    async with PoolWorld(net, rider_classes=(RiderAgent, Grumpy)) as w:
        await w.ride_all([(0, dict(destination="n1_2", detour_tolerance=120.0)),
                          (1, dict(destination="n1_3", detour_tolerance=120.0))])
        ta, tb = w.riders[0].trips[-1], w.riders[1].trips[-1]
        assert tb.outcome == DECLINED and tb.reason == "not_for_me"
        assert ta.outcome == COMPLETED
        states = [r.state for r in w.hub.pools.values()]
        assert states.count("DISSOLVED") >= 1                           # the pool of two was dissolved
        assert w.hub.pools[ta.pool_id].pool.size == 1                   # a's final pool is solo
        assert "re-pooling" in " ".join(ta.log)
        assert w.pay.ledger.check_conservation()

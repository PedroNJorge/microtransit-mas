"""
Milestone 7 done-when: the full single-trip pipeline, end to end, with real agents --
Rider, Microhub, Driver (and the Payment and Reputation agents around them):

  rider requests -> hub proposes the pool -> rider accepts -> escrow -> auction -> driver executes
  -> settlement -> rating -> reputation update (which the hub then uses in the next auction).
"""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import (
    AWAITING_MATCH, AWAITING_POOL, COMPLETED, DECIDING, DECLINED, DRIVER_EN_ROUTE, IDLE, IN_TRIP, PAYMENT_FAILED, RATING,
    REFUSED, REQUESTING, UNMATCHED, DISSOLVED, RiderAgent,
)
from agents.stubs import ScriptedRider
from common import clock
from common.graph import build_grid_network
from common.messages import make_inform
from common.ontology import DriverState, EscrowStatus, Protocol, Rating, TripRequest

from conftest import XMPP_PORT
from test_driver_agent import Probe, until

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SPEED = 30
HUB = "n1_1"


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
    """Payment + Reputation + Hub + driver(s) + one rider, unique names per test."""

    def __init__(self, net, drivers=(("n1_1", {}),), rider_kw=None, balance=100.0, hub_kw=None,
                 scripted=False, scripted_kw=None):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.net = net
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=balance)
        self.rep = ReputationAgent(jid("rep"), "pw", port=XMPP_PORT)
        hk = dict(t_auction=90.0, t_award=60.0, t_escrow=90.0)
        hk.update(hub_kw or {})
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=self.pay.jid, reputation_jid=self.rep.jid, **hk)
        self.rep.hub_jids = [str(self.hub.jid.bare)]
        self.drivers = [DriverAgent(jid(f"d{i}"), "pw", port=XMPP_PORT, start_hub=start, network=net,
                                    hub_jids=[self.hub.jid], payment_jid=self.pay.jid, t_provisional=300.0,
                                    dwell_pickup=1, dwell_dropoff=1, **kw)
                        for i, (start, kw) in enumerate(drivers)]
        self.hub.driver_jids = [str(d.jid.bare) for d in self.drivers]
        if scripted:
            rj = jid("rider")
            req = TripRequest(f"req-{n}", rj, HUB, "n2_2", 0.0, 7200.0, 600.0, 20.0, False, False)
            self.rider = ScriptedRider(rj, "pw", port=XMPP_PORT, hub_jid=self.hub.jid, payment_jid=self.pay.jid,
                                       request=req, auto_request=False, **(scripted_kw or {}))
        else:
            self.rider = RiderAgent(
                jid("rider"), "pw", port=XMPP_PORT, hub_jids={HUB: self.hub.jid}, network=net,
                payment_jid=self.pay.jid, reputation_jid=self.rep.jid, ready_delay=2.0, **(rider_kw or {}))
        self.agents = [self.pay, self.rep, self.hub, *self.drivers, self.rider]

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        assert await until(lambda: len(self.hub.registry) == len(self.drivers))      # statuses reached the hub
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def ride(self, dest="n2_2", timeout=60, **kw):
        kw.setdefault("max_fare", 20.0)
        req = self.rider.request_trip(dest, **kw)
        assert req is not None
        await asyncio.wait_for(self.rider.ended.wait(), timeout)
        return req

    @property
    def ledger(self):
        return self.pay.ledger


def path(history):
    return [history[0][1]] + [h[2] for h in history]


# ---------------------------------------------------------------------------------------------

async def test_full_single_trip_pipeline(xmpp_server, sim_clock, net):
    async with World(net) as w:
        drv, rider = w.drivers[0], w.rider
        req = await w.ride()
        trip = rider.trips[-1]

        # ---- the rider's state machine went through every state of Agent Model 8.3 ---------------
        assert path(rider.history) == [IDLE, REQUESTING, AWAITING_POOL, DECIDING, AWAITING_MATCH,
                                       DRIVER_EN_ROUTE, IN_TRIP, RATING, IDLE]
        assert trip.outcome == COMPLETED and rider.state == IDLE and rider.trip is None
        assert trip.proposal.split_fare == 20.0 and trip.proposal.shared_route == [HUB, "n2_2"]

        # ---- matched, driven, dropped off ----------------------------------------------------------
        assert trip.match.driver_id == str(drv.jid.bare)
        assert 0 < trip.boarded_at < trip.dropped_at
        # IN_TRIP is the ride only: it starts at boarding and ends at the drop-off
        entered = {b: when for when, a, b in rider.history}
        assert entered[DRIVER_EN_ROUTE] == pytest.approx(trip.matched_at, abs=1.0)
        assert entered[IN_TRIP] == pytest.approx(trip.boarded_at, abs=1.0) and entered[IN_TRIP] > entered[DRIVER_EN_ROUTE]
        assert entered[RATING] == pytest.approx(trip.dropped_at, abs=1.0)
        assert drv.state == DriverState.IDLE and drv.location == "n2_2" and drv.completed == [trip.pool_id]

        # ---- settlement: escrowed, then the driver is paid exactly the auction price --------------
        assert await until(lambda: trip.settlement is not None, 10)
        rec = w.ledger.pool_escrows(trip.pool_id)[0]
        assert w.ledger.status(rec.escrow_id) == EscrowStatus.SETTLED
        assert w.ledger.balance(str(drv.jid.bare)) == pytest.approx(trip.match.final_fare)
        assert w.ledger.balance(str(rider.jid.bare)) == pytest.approx(100.0 - trip.match.final_fare)
        assert w.ledger.check_conservation()

        # ---- rating -> reputation update -> the hub knows it -----------------------------------------
        assert trip.rating is not None and trip.rating.score == 5 and trip.rating.ratee_id == str(drv.jid.bare)
        assert await until(lambda: w.rep.count_of(str(drv.jid.bare)) == 1, 10)
        expected = (2 * 3.0 + 5) / 3                                    # prior 3.0 x weight 2, one 5-star
        assert w.rep.published(str(drv.jid.bare)).score == pytest.approx(expected, abs=1e-3)
        assert await until(lambda: str(drv.jid.bare) in w.hub.reputations, 10)
        assert w.hub.reputations[str(drv.jid.bare)] == pytest.approx(expected, abs=1e-3)


async def test_next_auction_uses_the_updated_reputation(xmpp_server, sim_clock, net):
    async with World(net) as w:
        d = str(w.drivers[0].jid.bare)
        await w.ride()
        assert await until(lambda: d in w.hub.reputations, 10)
        await w.ride()                                                   # same rider, same driver
        assert [t.outcome for t in w.rider.trips] == [COMPLETED, COMPLETED]
        bids = w.hub.finished[-1].bids
        assert bids[0].reputation == pytest.approx(w.hub.reputations[d])
        assert await until(lambda: w.rep.count_of(d) == 2, 10)
        assert w.rep.published(d).num_ratings == 2


async def test_low_rating_lowers_the_score(xmpp_server, sim_clock, net):
    async with World(net, rider_kw={"rate_with": lambda t: 1}) as w:
        d = str(w.drivers[0].jid.bare)
        await w.ride()
        assert await until(lambda: w.rep.count_of(d) == 1, 10)
        assert w.rep.published(d).score == pytest.approx((6 + 1) / 3, abs=1e-3)
        assert w.rep.published(d).score < 3.0


async def test_rider_declines_a_proposal_it_cannot_use(xmpp_server, sim_clock, net):
    # the only driver is 3 hops (180 s) from the hub but the rider can only wait ~1 minute
    async with World(net, drivers=(("n4_1", {}),)) as w:
        await w.ride(window=60.0)
        trip = w.rider.trips[-1]
        assert trip.outcome == DECLINED and trip.reason == "wait_too_long"
        assert path(w.rider.history) == [IDLE, REQUESTING, AWAITING_POOL, DECIDING, IDLE]
        assert await until(lambda: w.hub.pools[trip.pool_id].state == "DISSOLVED", 5)
        assert w.hub.finished == [] and w.ledger.pool_escrows(trip.pool_id) == []     # nothing was held


async def test_rider_that_stays_silent_gets_its_pool_dissolved(xmpp_server, sim_clock, net):
    async with World(net, scripted=True, scripted_kw={"silent_on_proposal": True},
                     hub_kw={"t_pool_response": 20.0}) as w:
        w.rider.send_request()
        await asyncio.wait_for(w.rider.done.wait(), 30)
        assert w.rider.last("dissolved") is not None and w.hub.finished == []


async def test_insufficient_funds_ends_the_trip_without_an_auction(xmpp_server, sim_clock, net):
    async with World(net, balance=5.0, hub_kw={"t_escrow": 6.0}) as w:
        await w.ride()
        assert w.rider.trips[-1].outcome == PAYMENT_FAILED
        assert w.hub.finished == [] and w.drivers[0].completed == []


async def test_destination_outside_every_hubs_coverage(xmpp_server, sim_clock, net):
    async with World(net) as w:
        assert w.rider.request_trip("n5_5", 20.0) is None              # the rider knows no hub covers it
        assert w.rider.state == IDLE
        await w.ride("n5_5", hub_id=HUB)                               # forced: the hub refuses
        assert w.rider.trips[-1].outcome == REFUSED and w.rider.trips[-1].reason == "outside_coverage"


async def test_no_driver_answers_so_the_rider_is_unmatched_and_refunded(xmpp_server, sim_clock, net):
    async with World(net, drivers=(("n1_1", {"shift_end": 1.0}),), hub_kw={"t_auction": 30.0}) as w:
        await w.ride(timeout=90)
        trip = w.rider.trips[-1]
        assert trip.outcome == UNMATCHED and trip.reason == "NO_BIDS"
        rec = w.ledger.pool_escrows(trip.pool_id)[0]
        assert await until(lambda: w.ledger.status(rec.escrow_id) == EscrowStatus.REFUNDED, 10)
        assert w.ledger.balance(str(w.rider.jid.bare)) == pytest.approx(100.0)


async def test_a_busy_rider_cannot_start_a_second_trip(xmpp_server, sim_clock, net):
    async with World(net) as w:
        assert w.rider.request_trip("n2_2", 20.0) is not None
        assert w.rider.request_trip("n2_2", 20.0) is None
        await asyncio.wait_for(w.rider.ended.wait(), 60)


# ---- ReputationProtocol integrity ----------------------------------------------------------------

async def test_forged_and_stranger_ratings_do_not_count(xmpp_server, sim_clock, net):
    async with World(net) as w:
        d = str(w.drivers[0].jid.bare)
        await w.ride()
        assert await until(lambda: w.rep.count_of(d) == 1, 10)
        trip = w.rider.trips[-1]
        stranger = Probe(f"stranger_{uuid.uuid4().hex[:6]}@localhost", "pw", port=XMPP_PORT)
        await stranger.start(auto_register=True)
        try:
            tid = trip.rating.trip_id
            me = str(stranger.jid.bare)
            # 1. a stranger rates the driver for a trip he was not on
            await stranger.tell(make_inform(str(w.rep.jid.bare), Protocol.REPUTATION, Rating(tid, me, d, 1), tid))
            # 2. a stranger rates in the real rider's name
            await stranger.tell(make_inform(str(w.rep.jid.bare), Protocol.REPUTATION,
                                            Rating(tid, str(w.rider.jid.bare), d, 1), tid))
            await asyncio.sleep(1.0)
        finally:
            await stranger.stop()
        assert w.rep.count_of(d) == 1
        assert {why for _, why in w.rep.rejected} == {"not_a_participant", "not_authorised"}

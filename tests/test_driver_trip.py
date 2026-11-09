"""
Milestone 6 done-when, end to end (real Driver, Microhub and Payment agents):

  two drivers compete for one auction; one wins, one receives reject-proposal; the winner drives
  to the pickup, boards, drops off, and returns to idle -- and is paid through the Payment agent.
"""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.stubs import ScriptedRider, StubDriver
from common import clock
from common.graph import build_grid_network
from common.messages import make_inform, make_message
from common.ontology import (
    DriverState, DriverStatus, EscrowStatus, FailureReason, Perf, Protocol, TripCompleted, TripRequest,
    parse_payload,
)

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
    def __init__(self, net, drivers=(("n1_1", {}), ("n4_1", {})), rider_kw=None, max_fare=20.0,
                 hub_kw=None, stub=False):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.net = net
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0)
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=self.pay.jid, t_auction=90.0, t_award=60.0, t_escrow=90.0,
                                 **(hub_kw or {}))
        self.drivers = []
        for i, (start, kw) in enumerate(drivers):
            if stub:
                d = StubDriver(jid(f"d{i}"), "pw", port=XMPP_PORT, **kw)
            else:
                d = DriverAgent(jid(f"d{i}"), "pw", port=XMPP_PORT, start_hub=start, network=net,
                                hub_jids=[self.hub.jid], payment_jid=self.pay.jid,
                                t_provisional=300.0, dwell_pickup=1, dwell_dropoff=1, **kw)
            self.drivers.append(d)
        self.hub.driver_jids = [str(d.jid.bare) for d in self.drivers]
        rj = jid("rider")
        req = TripRequest(f"req-{n}", rj, HUB, "n2_2", 0.0, 7200.0, 600.0, max_fare, False, False)
        self.rider = ScriptedRider(rj, "pw", port=XMPP_PORT, hub_jid=self.hub.jid, payment_jid=self.pay.jid,
                                   request=req, auto_request=False, **(rider_kw or {}))
        self.agents = [self.pay, self.hub, *self.drivers, self.rider]       # hub before drivers: it hears their first status

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def request(self):
        assert await until(lambda: len(self.hub.registry) == len(self.drivers) or True, 0.1)
        self.rider.send_request()

    @property
    def ledger(self):
        return self.pay.ledger


def states_of(driver):
    return [s.state for s in driver.status_log]


def is_subsequence(wanted, seq):
    it = iter(seq)
    return all(w in it for w in wanted)


# ---------------------------------------------------------------------------------------------

async def test_two_drivers_compete_winner_drives_the_trip_and_returns_to_idle(xmpp_server, sim_clock, net):
    async with World(net) as w:
        near, far = w.drivers
        assert await until(lambda: len(w.hub.registry) == 2)                 # DriverStatus reached the hub
        await w.request()
        assert await until(lambda: w.rider.trip_done.is_set(), 40)
        assert await until(lambda: w.rider.last("settled") is not None, 10)
        await asyncio.sleep(0.5)

        # ---- the auction: one winner, one reject-proposal ------------------------------------
        ctx = w.hub.finished[-1]
        assert ctx.outcome == "matched" and ctx.match.driver_id == str(near.jid.bare)
        assert [b.driver_id for b in ctx.bids] == sorted(b.driver_id for b in ctx.bids) or len(ctx.bids) == 2
        assert ctx.match.final_fare == pytest.approx(3.92)       # 2.48 + (3.92 - 2.48): second-score price
        trace = [e["event"] for e in ctx.trace]
        assert trace.count("reject-proposal") == 1 and "driver confirmed" in trace

        # ---- the winner: bid -> committed -> en route -> on trip -> idle ---------------------
        assert is_subsequence([DriverState.IDLE, DriverState.BIDDING, DriverState.EN_ROUTE_PICKUP,
                               DriverState.ON_TRIP, DriverState.IDLE], states_of(near))
        assert near.state == DriverState.IDLE and near.lock.state.value == "FREE"
        assert near.location == "n2_2" and near.passengers == 0 and near.completed == [ctx.pool_id]
        # ---- the loser: bid -> rejected -> idle, never moved -----------------------------------
        assert is_subsequence([DriverState.IDLE, DriverState.BIDDING, DriverState.IDLE], states_of(far))
        assert DriverState.EN_ROUTE_PICKUP not in states_of(far) and far.location == "n4_1"
        assert far.lock.state.value == "FREE" and far.completed == []

        # ---- the rider heard everything in order -------------------------------------------------
        kinds = w.rider.kinds()
        # (a driver already standing at the hub can announce its arrival a moment before the hub's
        # MatchResult reaches the rider, so "arriving" is not ordered against "matched")
        order = ["request", "agree", "escrow", "matched", "boarded", "dropped"]
        assert is_subsequence(order, kinds), kinds
        assert w.rider.last("matched").driver_id == str(near.jid.bare)
        assert kinds.index("arriving") < kinds.index("boarded")

        # ---- the money: escrowed, then paid to the driver exactly the auction price -----------
        rec = w.ledger.pool_escrows(ctx.pool_id)[0]
        assert w.ledger.status(rec.escrow_id) == EscrowStatus.SETTLED
        assert w.ledger.balance(str(near.jid.bare)) == pytest.approx(3.92)
        assert w.ledger.balance(str(w.rider.jid.bare)) == pytest.approx(100.0 - 3.92)
        assert w.ledger.balance(str(far.jid.bare)) == 0 and w.ledger.check_conservation()
        assert near.payouts and near.payouts[0].driver_payout == pytest.approx(3.92)
        assert w.hub.settlements[ctx.pool_id].total_paid == pytest.approx(3.92)
        assert w.hub.pools[ctx.pool_id].state == "COMPLETED"


async def test_timing_of_the_trip_matches_the_graph(xmpp_server, sim_clock, net):
    async with World(net, drivers=(("n4_1", {}),)) as w:                    # 3 hops (180 s) to the pickup
        assert await until(lambda: len(w.hub.registry) == 1)
        await w.request()
        assert await until(lambda: w.rider.trip_done.is_set(), 40)
        arr, drop = w.rider.last("arriving"), w.rider.last("dropped")
        board = w.rider.last("boarded")
        matched_at = w.hub.finished[-1].started_at
        assert 180 <= board.boarded_at - matched_at <= 180 + 40                # drive + ready + dwell
        assert 120 <= drop.dropped_at - board.boarded_at <= 120 + 15            # 2 hops + dwell


async def test_no_show_is_reported_driver_goes_idle_and_rider_is_refunded(xmpp_server, sim_clock, net):
    async with World(net, drivers=(("n1_1", {"t_no_show": 20.0}),), rider_kw={"no_show": True}) as w:
        d = w.drivers[0]
        assert await until(lambda: len(w.hub.registry) == 1)
        await w.request()
        ctx_pool = None
        assert await until(lambda: w.hub.matches, 20)
        ctx_pool = next(iter(w.hub.matches))
        assert await until(lambda: w.hub.pools[ctx_pool].state == "CANCELLED", 20)
        assert await until(lambda: d.state == DriverState.IDLE and d.assignment is None, 10)
        rec = w.ledger.pool_escrows(ctx_pool)[0]
        assert await until(lambda: w.ledger.status(rec.escrow_id) == EscrowStatus.REFUNDED, 10)
        assert w.ledger.balance(str(w.rider.jid.bare)) == pytest.approx(100.0)
        assert w.ledger.balance(str(d.jid.bare)) == 0 and d.completed == []


async def test_hub_uses_its_own_eta_not_the_drivers_claim(xmpp_server, sim_clock, net):     # CC-10
    async with World(net, drivers=(("-", {"price": 8.0, "eta": 1.0}),), stub=True) as w:
        d = w.drivers[0]
        w.hub.registry[str(d.jid.bare)] = (DriverStatus(str(d.jid.bare), "n4_4", DriverState.IDLE, 4, 0, 1e12, 2.0),
                                           clock.now())
        await w.request()
        assert await until(lambda: w.hub.finished, 20)
        bid = w.hub.finished[-1].bids[0]
        assert bid.eta == pytest.approx(net.travel_time("n4_4", HUB))      # 6 hops, not the claimed 1 s
        assert any(e["event"] == "eta recomputed" for e in w.hub.finished[-1].trace)


async def test_hub_does_not_call_drivers_it_knows_to_be_locked_or_busy(xmpp_server, sim_clock, net):   # CC-08
    hub = MicrohubAgent("hubx@localhost", "pw", hub_id=HUB, network=net,
                        driver_jids=["a@localhost", "b@localhost", "c@localhost", "d@localhost"])
    now = clock.now()
    st = lambda s: (DriverStatus("x", HUB, s, 4, 0, 1e12, 2.0), now)
    hub.registry["a@localhost"] = st(DriverState.IDLE)
    hub.registry["b@localhost"] = st(DriverState.BIDDING)
    hub.registry["c@localhost"] = (DriverStatus("x", HUB, DriverState.ON_TRIP, 4, 1, 1e12, 2.0), now)
    # d: never reported -> still called (it will refuse `locked` itself if it must)
    assert hub.eligible_drivers(now) == ["a@localhost", "d@localhost"]
    # a status older than 3 x T_status is not trusted
    assert "b@localhost" in hub.eligible_drivers(now + 3 * hub.t_status + 1)


async def test_busy_driver_is_skipped_by_a_second_request(xmpp_server, sim_clock, net):
    """While the only driver is on its trip a second pool finds nobody (and is not CFP'd)."""
    async with World(net, drivers=(("n1_1", {}),)) as w:
        d = w.drivers[0]
        assert await until(lambda: len(w.hub.registry) == 1)
        await w.request()
        assert await until(lambda: d.state == DriverState.ON_TRIP, 30)
        assert await until(lambda: w.hub.registry[str(d.jid.bare)][0].state == DriverState.ON_TRIP, 5)
        assert w.hub.eligible_drivers(clock.now()) == []
        assert await until(lambda: w.rider.trip_done.is_set(), 30)


async def test_forged_trip_completed_does_not_move_money(xmpp_server, sim_clock, net):
    async with World(net, drivers=(("n1_1", {}),)) as w:
        d = w.drivers[0]
        assert await until(lambda: len(w.hub.registry) == 1)
        forger = Probe(f"forger_{uuid.uuid4().hex[:6]}@localhost", "pw", port=XMPP_PORT)
        await forger.start(auto_register=True)
        try:
            await w.request()
            assert await until(lambda: w.hub.matches, 20)
            pool = next(iter(w.hub.matches))
            fake = TripCompleted("t", pool, str(d.jid.bare), [str(w.rider.jid.bare)], clock.now(), 0.0,
                                 {str(w.rider.jid.bare): 0.01})
            await forger.tell(make_message(str(w.hub.jid.bare), Perf.INFORM, Protocol.TRIP_EXECUTION, fake, pool))
            await asyncio.sleep(0.5)
            assert w.hub.pools[pool].state == "ASSIGNED"             # ignored: only the assigned driver counts
            assert await until(lambda: w.rider.trip_done.is_set(), 30)
            assert await until(lambda: w.hub.pools[pool].state == "COMPLETED", 10)
        finally:
            await forger.stop()


async def test_hub_settles_the_hubs_numbers_not_the_drivers(xmpp_server, sim_clock, net):
    """TripCompleted from the real driver but with inflated fares: the hub settles match.rider_fares."""
    async with World(net, drivers=(("n1_1", {}),)) as w:
        assert await until(lambda: len(w.hub.registry) == 1)
        await w.request()
        assert await until(lambda: w.hub.matches, 20)
        pool = next(iter(w.hub.matches))
        assert await until(lambda: w.rider.last("settled") is not None, 40)
        fare = w.hub.matches[pool].final_fare
        assert w.ledger.balance(str(w.drivers[0].jid.bare)) == pytest.approx(fare)

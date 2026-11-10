"""
Milestone 8 done-when: a reputation score does not become visible to microhubs before settlement
(ID-07), and becomes visible after -- for the driver (rated by the rider) and for the rider
(rated by the driver, ID-08).

Payment's settlement queue (FE-08) is held shut with `settlement_open` so the trip is completed
but not settled, then opened.
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
from common.messages import make_query_if
from common.ontology import Perf, Protocol, ReputationScore, parse_payload

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
    def __init__(self, net, t_settlement_max=600.0):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0,
                                reputation_jid=jid("rep"))
        self.pay.settlement_open = asyncio.Event()                   # shut: settlements queue up
        self.rep = ReputationAgent(jid("rep"), "pw", port=XMPP_PORT, payment_jid=jid("pay"),
                                   t_settlement_max=t_settlement_max, sweep_every=5.0)
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=jid("pay"), reputation_jid=jid("rep"),
                                 t_auction=90.0, t_award=60.0, t_escrow=90.0)
        self.rep.hub_jids = [jid("hub")]
        self.drv = DriverAgent(jid("d"), "pw", port=XMPP_PORT, start_hub=HUB, network=net,
                               hub_jids=[jid("hub")], payment_jid=jid("pay"), reputation_jid=jid("rep"),
                               t_provisional=300.0, dwell_pickup=1, dwell_dropoff=1)
        self.hub.driver_jids = [jid("d")]
        self.rider = RiderAgent(jid("rider"), "pw", port=XMPP_PORT, hub_jids={HUB: jid("hub")}, network=net,
                                payment_jid=jid("pay"), reputation_jid=jid("rep"), ready_delay=2.0)
        self.probe = Probe(jid("probe"), "pw", port=XMPP_PORT)
        self.agents = [self.pay, self.rep, self.hub, self.drv, self.rider, self.probe]
        self.d, self.r = jid("d"), jid("rider")

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        assert await until(lambda: len(self.hub.registry) == 1)
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def drive_trip(self):
        assert self.rider.request_trip("n2_2", 20.0) is not None
        await asyncio.wait_for(self.rider.ended.wait(), 60)
        assert self.rider.trips[-1].outcome == COMPLETED
        # both ratings have reached Reputation (and are accepted)
        assert await until(lambda: len(self.rep.entries) == 2, 15)

    async def query(self, who):
        before = len(self.probe.got(Protocol.REPUTATION))
        q = make_query_if(str(self.rep.jid.bare), Protocol.REPUTATION)
        q.body = who
        await self.probe.tell(q)
        assert await until(lambda: len(self.probe.got(Protocol.REPUTATION)) > before)
        return self.probe.got(Protocol.REPUTATION)[-1]


async def test_scores_are_invisible_before_settlement_and_visible_after(xmpp_server, sim_clock, net):
    async with World(net) as w:
        await w.drive_trip()
        await asyncio.sleep(1.5)                                    # give any (wrong) publication time to arrive

        # ---- trip completed, ratings accepted, NOT settled -> nothing is visible --------------------
        rec = w.pay.ledger.pool_escrows(w.rider.trips[-1].pool_id)[0]
        assert w.pay.ledger.status(rec.escrow_id).value == "HELD"          # completed, not settled
        assert w.rep.held_count(w.d) == 1 and w.rep.held_count(w.r) == 1
        assert w.rep.published(w.d) is None and w.rep.published(w.r) is None
        assert w.d not in w.hub.reputations and w.r not in w.hub.reputations
        for who in (w.d, w.r):
            reply = await w.query(who)
            assert reply.get_metadata("performative") == Perf.REFUSE and reply.body == "unpublished"

        # ---- Payment settles -> both become visible, unflagged ---------------------------------------
        w.pay.settlement_open.set()
        assert await until(lambda: w.d in w.hub.reputations and w.r in w.hub.reputations, 15)
        assert w.hub.reputations[w.d] == pytest.approx((6 + 5) / 3, abs=1e-3)
        assert w.hub.reputations[w.r] == pytest.approx((6 + 5) / 3, abs=1e-3)
        assert not w.hub.reputation_flags[w.d] and not w.hub.reputation_flags[w.r]
        reply = await w.query(w.d)
        score = parse_payload(reply)
        assert isinstance(score, ReputationScore) and score.num_ratings == 1 and not score.pending_settlement


async def test_unsettled_rating_is_published_flagged_after_t_settlement_max_then_unflagged(xmpp_server, sim_clock, net):
    async with World(net, t_settlement_max=60.0) as w:                 # 60 sim s = 2 s real
        await w.drive_trip()
        # the deadline passes with Payment still queued -> published with pending_settlement
        assert await until(lambda: w.d in w.hub.reputations, 15)
        assert w.hub.reputation_flags[w.d] is True and w.hub.reputation_flags[w.r] is True
        assert w.rep.published(w.d).pending_settlement
        assert (await w.query(w.d)) is not None
        # settlement finally arrives -> same ratings, flag removed
        w.pay.settlement_open.set()
        assert await until(lambda: w.hub.reputation_flags.get(w.d) is False
                           and w.hub.reputation_flags.get(w.r) is False, 15)
        assert w.rep.published(w.d).num_ratings == 1                    # still one rating, not two


async def test_the_next_auction_sees_the_score_only_after_settlement(xmpp_server, sim_clock, net):
    async with World(net) as w:
        await w.drive_trip()
        assert w.d not in w.hub.reputations                              # a second trip now would be scored with the default
        w.pay.settlement_open.set()
        assert await until(lambda: w.d in w.hub.reputations, 15)
        assert await until(lambda: w.drv.state.value == "IDLE", 10)
        assert w.rider.request_trip("n2_2", 20.0) is not None
        await asyncio.wait_for(w.rider.ended.wait(), 60)
        assert w.hub.finished[-1].bids[0].reputation == pytest.approx(w.hub.reputations[w.d])

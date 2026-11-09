"""
Milestone 5 integration tests: MicrohubAgent + MatchAuctionFSM + Payment + stub drivers.

Done when: a single rider request at a microhub triggers a CFP, gets a stub bid, the auction
awards, and the rider is informed; the log shows the full protocol.
"""
import asyncio
import logging
import uuid

import pytest

from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.stubs import ScriptedRider, StubDriver
from common import clock
from common.ontology import AuctionOutcome, EscrowStatus, FailureReason, MatchResult, Perf, TripRequest

from conftest import XMPP_PORT

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SPEED = 20


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


def request(rider_jid, max_fare=20.0, rid=None):
    return TripRequest(rid or f"req-{uuid.uuid4().hex[:6]}", rider_jid, "hub", "n00003", 0.0, 7200.0,
                       600.0, max_fare, False, False)


class World:
    def __init__(self, drivers=({"price": 8.0},), payment=True, rider_escrows=True, rider_balance=100.0,
                 hub_kwargs=None, max_fare=20.0):
        n = uuid.uuid4().hex[:6]
        self.n = n
        jid = lambda k: f"{k}_{n}@localhost"
        self.pay = (PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=rider_balance)
                    if payment else None)
        self.drivers = [StubDriver(jid(f"d{i}"), "pw", port=XMPP_PORT, **kw) for i, kw in enumerate(drivers)]
        hk = dict(t_auction=40.0, t_award=30.0, t_escrow=40.0)
        hk.update(hub_kwargs or {})
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id="hub",
                                 driver_jids=[d.jid for d in self.drivers],
                                 payment_jid=self.pay.jid if payment else None, **hk)
        rj = jid("rider")
        self.rider = ScriptedRider(rj, "pw", port=XMPP_PORT, hub_jid=self.hub.jid,
                                   payment_jid=self.pay.jid if (payment and rider_escrows) else None,
                                   request=request(rj, max_fare), auto_request=False)
        self.agents = [a for a in [self.pay, *self.drivers, self.hub, self.rider] if a]

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def run(self, timeout=20):
        self.rider.send_request()
        await asyncio.wait_for(self.rider.done.wait(), timeout)
        await asyncio.sleep(0.5)                    # let trailing rejects / refunds land

    @property
    def ctx(self):
        return self.hub.finished[-1]

    def trace(self, ctx=None):
        return [(e["state"], e["event"]) for e in (ctx or self.ctx).trace]


async def test_single_rider_single_stub_driver_full_protocol(xmpp_server, sim_clock, caplog):
    caplog.set_level(logging.INFO, logger="protocols.match_auction")
    async with World() as w:
        await w.run()
        assert w.rider.kinds()[:2] == ["request", "agree"]
        match = w.rider.last("matched")
        assert isinstance(match, MatchResult) and match.driver_id == str(w.drivers[0].jid.bare)
        assert 8.0 <= match.final_fare <= 20.0               # IR for the driver, reserve for the rider
        assert sum(match.rider_fares.values()) == pytest.approx(match.final_fare)
        # the stub saw: cfp then accept; and confirmed
        assert [p for p, _ in w.drivers[0].inbox] == [Perf.CFP, Perf.ACCEPT]
        states = [s for s, _ in w.trace()]
        order = ["START", "CFP_SENT", "COLLECTING_BIDS", "EVALUATING", "AWARDING", "INFORMING", "END"]
        assert [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]] == order
        assert w.ctx.outcome == "matched" and w.hub.pools[w.ctx.pool_id].state == "ASSIGNED"
    log = caplog.text
    for needle in ("auction opens", "cfp sent", "bid", "winner", "accept-proposal sent",
                   "driver confirmed", "rider informed", "auction closed"):
        assert needle in log, needle


async def test_escrow_is_held_before_the_auction_starts(xmpp_server, sim_clock):
    async with World() as w:
        await w.run()
        recs = w.pay.ledger.pool_escrows(w.ctx.pool_id)
        assert len(recs) == 1 and w.pay.ledger.status(recs[0].escrow_id) == EscrowStatus.HELD
        assert w.ctx.escrow_ids == [recs[0].escrow_id]
        assert w.ctx.started_at >= 0


async def test_two_drivers_threshold_price_and_loser_is_answered(xmpp_server, sim_clock):
    async with World(drivers=({"price": 8.0, "eta": 120.0}, {"price": 9.0, "eta": 60.0})) as w:
        await w.run()
        match = w.rider.last("matched")
        assert match.driver_id == str(w.drivers[0].jid.bare)
        assert match.final_fare == pytest.approx(8.76)
        perfs = [p for p, _ in w.drivers[1].inbox]
        assert perfs == [Perf.CFP, Perf.REJECT]               # GC-09
        outcome = w.drivers[1].inbox[1][1]
        assert isinstance(outcome, AuctionOutcome) and outcome.winner_id == str(w.drivers[0].jid.bare)


async def test_first_price_rule_pays_own_bid(xmpp_server, sim_clock):
    from common.auction import AuctionRules
    rules = AuctionRules(pricing="first_price")
    async with World(drivers=({"price": 8.0}, {"price": 9.0}), hub_kwargs={"rules": rules}) as w:
        await w.run()
        assert w.rider.last("matched").final_fare == pytest.approx(8.0)


async def test_rider_is_informed_only_after_driver_confirmed(xmpp_server, sim_clock):
    async with World() as w:
        await w.run()
        t_confirm = next(e["t"] for e in w.ctx.trace if e["event"] == "driver confirmed")
        t_inform = next(e["t"] for e in w.ctx.trace if e["event"] == "rider informed")
        assert t_confirm <= t_inform                         # ID-05


async def test_unconfirmed_winner_is_skipped_and_cancelled(xmpp_server, sim_clock):
    async with World(drivers=({"price": 8.0, "mode": "no_confirm"}, {"price": 9.0}),
                     hub_kwargs={"t_award": 3.0}) as w:
        await w.run()
        match = w.rider.last("matched")
        assert match.driver_id == str(w.drivers[1].jid.bare)
        assert Perf.CANCEL in [p for p, _ in w.drivers[0].inbox]
        assert w.drivers[1].awards and not w.drivers[0].awards
        assert ("AWARDING", "award failed: timeout") in w.trace()
        assert match.final_fare == pytest.approx(20.0)          # lone remaining bidder pays the reserve


async def test_refusing_award_moves_to_next_bidder(xmpp_server, sim_clock):
    async with World(drivers=({"price": 8.0, "mode": "refuse_award"}, {"price": 9.0})) as w:
        await w.run()
        assert w.rider.last("matched").driver_id == str(w.drivers[1].jid.bare)


async def test_no_bids_retries_then_informs_rider_and_refunds(xmpp_server, sim_clock):
    async with World(drivers=({"mode": "silent"}, {"mode": "refuse"}),
                     hub_kwargs={"t_auction": 4.0}) as w:
        await w.run(timeout=30)
        out = w.rider.last("unmatched")
        assert isinstance(out, AuctionOutcome) and not out.awarded and out.reason == FailureReason.NO_BIDS
        ctxs = w.hub.pools[out.pool_id].contexts
        assert [c.attempt for c in ctxs] == [0, 1] and [c.outcome for c in ctxs] == ["retry", "failed"]
        assert ctxs[1].cfp.max_detour == ctxs[0].cfp.max_detour + w.hub.widen_detour
        recs = w.pay.ledger.pool_escrows(out.pool_id)
        assert recs and all(w.pay.ledger.status(r.escrow_id) == EscrowStatus.REFUNDED for r in recs)
        assert w.pay.ledger.balance(str(w.rider.jid.bare)) == pytest.approx(100.0)
        assert w.pay.ledger.check_conservation()


async def test_late_bid_is_rejected_not_ignored(xmpp_server, sim_clock):
    async with World(drivers=({"price": 8.0}, {"price": 7.0, "mode": "late", "late_by": 5.0}),
                     hub_kwargs={"t_auction": 6.0}) as w:
        await w.run()
        await asyncio.sleep(2.5)
        assert w.rider.last("matched").driver_id == str(w.drivers[0].jid.bare)
        assert Perf.REJECT in [p for p, _ in w.drivers[1].inbox]
        assert w.drivers[1].awards == []


async def test_stale_attempt_bid_is_rejected(xmpp_server, sim_clock):
    async with World(drivers=({"mode": "wrong_attempt"},), hub_kwargs={"t_auction": 5.0}) as w:
        await w.run(timeout=30)
        assert w.rider.last("unmatched") is not None
        assert Perf.REJECT in [p for p, _ in w.drivers[0].inbox]


async def test_no_escrow_within_timeout_dissolves_the_pool(xmpp_server, sim_clock):
    async with World(rider_escrows=False, hub_kwargs={"t_escrow": 4.0}) as w:     # ID-09
        await w.run()
        out = w.rider.last("unmatched")
        assert out.reason == FailureReason.TIMEOUT
        assert w.drivers[0].inbox == []                       # no CFP before money is held (ID-02)


async def test_insufficient_funds_never_reaches_the_drivers(xmpp_server, sim_clock):
    async with World(rider_balance=5.0, hub_kwargs={"t_escrow": 6.0}) as w:
        await w.run()
        assert w.rider.last("unmatched") is not None
        assert w.drivers[0].inbox == []


async def test_without_payment_the_auction_runs_directly(xmpp_server, sim_clock):
    async with World(payment=False) as w:
        await w.run()
        assert w.rider.last("matched") is not None


async def test_spoofed_rider_request_is_refused(xmpp_server, sim_clock):
    async with World(payment=False) as w:
        w.rider.request = request("someone_else@localhost")
        await w.run()
        assert w.rider.kinds()[-1] == "refused" and w.rider.last("refused") == "not_authorised"
        assert w.hub.pools == {}


async def test_hub_still_answers_pings(xmpp_server, sim_clock):
    from harness.ping import ping_agents
    async with World(payment=False) as w:
        assert w.hub.role == "microhub" and w.hub.subscribes_to_traffic

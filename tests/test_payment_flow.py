"""
Milestone 4 done-when: an integration test exercises hold -> settle -> disbursement in both the
success and the failure (refund) paths, over real XMPP with stub rider / hub / driver agents.
"""
import asyncio
import uuid

import pytest
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.payment import PaymentAgent
from common import clock
from common.ledger import Reason
from common.messages import make_message, make_request
from common.ontology import (
    Escrow, EscrowRequest, EscrowStatus, FailureReason, Perf, Policy, Protocol, Refund,
    Settlement, SettlementComplete, TripCompleted, TripFailed, parse_payload,
)

pytestmark = pytest.mark.integration

SPEED = 100


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


# ---- test doubles ------------------------------------------------------------------------

class Stub(Agent):
    """Records every message it receives and can send any message."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.inbox = []

    class Rx(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=0.2)
            if msg is not None:
                self.agent.inbox.append(msg)

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

    def got(self, protocol=None, performative=None, ontology=None):
        return [m for m in self.inbox
                if (protocol is None or m.get_metadata("protocol") == protocol)
                and (performative is None or m.get_metadata("performative") == performative)
                and (ontology is None or m.get_metadata("ontology") == ontology)]

    def payloads(self, cls):
        return [parse_payload(m) for m in self.got(ontology=cls.__name__)]


async def until(cond, timeout=6.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class World:
    """A Payment agent plus two riders, a hub and a driver, with unique names per test."""

    def __init__(self, port, **payment_kwargs):
        self.port = port
        n = uuid.uuid4().hex[:6]
        self.n = n
        payment_kwargs.setdefault("default_rider_balance", 100.0)
        payment_kwargs.setdefault("subsidy_discount", 0.5)
        self.pay = PaymentAgent(f"pay_{n}@localhost", "pw", port=port, **payment_kwargs)
        self.names = {k: f"{k}_{n}" for k in ("r1", "r2", "hub", "drv")}
        self.r1, self.r2, self.hub, self.drv = (
            Stub(f"{self.names[k]}@localhost", "pw", port=port) for k in ("r1", "r2", "hub", "drv"))

    def jid(self, key):
        return f"{self.names[key]}@localhost"

    @property
    def ledger(self):
        return self.pay.ledger

    async def __aenter__(self):
        for a in (self.pay, self.r1, self.r2, self.hub, self.drv):
            try:                       # a hang here is a server problem: say which agent
                await asyncio.wait_for(a.start(auto_register=True), 20)
            except asyncio.TimeoutError:
                raise RuntimeError(f"agent {a.jid} could not connect within 20 s") from None
        return self

    async def __aexit__(self, *exc):
        for a in (self.r1, self.r2, self.hub, self.drv, self.pay):
            await a.stop()

    # ---- protocol helpers (what the real agents will do) ----------------------------------
    def escrow_request(self, rider_key, amount, pool="pool-1", eligible=False, req=None,
                       hub_id=None):
        return EscrowRequest(req or f"req-{rider_key}-{self.n}", self.jid(rider_key), pool, amount,
                             eligible, self.jid("hub") if hub_id is None else hub_id)

    async def hold(self, rider_key, amount, **kw):
        stub = getattr(self, rider_key)
        await stub.tell(make_request(self.to_pay(), Protocol.ESCROW,
                                     self.escrow_request(rider_key, amount, **kw),
                                     thread=kw.get("req") or f"req-{rider_key}-{self.n}"))

    def to_pay(self):
        return self.pay.jid.bare

    async def hub_says_completed(self, fares, riders=None, sender=None, pool="pool-1"):
        tc = TripCompleted(f"trip-{self.n}", pool, self.jid("drv"),
                           riders if riders is not None else [self.jid("r1"), self.jid("r2")],
                           clock.now(), 4.2, fares)
        await (sender or self.hub).tell(make_message(
            self.to_pay(), Perf.INFORM, Protocol.SETTLEMENT, tc, thread=pool))

    async def hub_says_failed(self, escrow_ids=(), reason=FailureReason.NO_MATCH, sender=None,
                              pool="pool-1"):
        tf = TripFailed(pool, reason, self.jid("hub"), list(escrow_ids))
        await (sender or self.hub).tell(make_request(self.to_pay(), Protocol.REFUND, tf,
                                                     thread=pool))


async def two_holds(w, **kw):
    """r1 (subsidy-eligible) holds 20.00, r2 holds 15.00; waits until both are confirmed."""
    await w.hold("r1", 20.0, eligible=True)
    await w.hold("r2", 15.0)
    assert await until(lambda: len(w.hub.got(Protocol.ESCROW, Perf.INFORM)) == 2)


# ---- success path: hold -> settle -> disbursement -----------------------------------------

@pytest.mark.asyncio
async def test_success_path_hold_settle_disburse(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        # 1. HOLD: riders get the escrow, the hub gets escrow_confirmed (ID-02)
        await two_holds(w)
        for stub, amount in ((w.r1, 20.0), (w.r2, 15.0)):
            e = stub.payloads(Escrow)[0]
            assert e.status == EscrowStatus.HELD and e.amount == amount and e.pool_id == "pool-1"
        assert {e.escrow_id for e in w.hub.payloads(Escrow)} == {
            e.escrow_id for s in (w.r1, w.r2) for e in s.payloads(Escrow)}
        assert w.ledger.balance(w.jid("r1")) == 80.0 and w.ledger.balance(w.jid("r2")) == 85.0

        # 2. SETTLE (bare ids on purpose): fares 12.00 and 9.00 against holds of 20.00 and 15.00
        await w.hub_says_completed({w.names["r1"]: 12.0, w.names["r2"]: 9.0},
                                   riders=[w.names["r1"], w.names["r2"]])
        assert await until(lambda: w.hub.payloads(SettlementComplete))

        # hub: one completion message with the totals
        done = w.hub.payloads(SettlementComplete)[0]
        assert (done.pool_id, done.total_paid, done.total_subsidy, done.total_returned) == \
            ("pool-1", 21.0, 6.0, 14.0)
        # driver: paid exactly the fares, one payout per escrow, no subsidy deducted
        assert await until(lambda: len(w.drv.payloads(Settlement)) == 2)
        assert sorted(s.driver_payout for s in w.drv.payloads(Settlement)) == [9.0, 12.0]
        assert w.ledger.balance(w.jid("drv")) == 21.0
        # riders: charge + excess + subsidy disbursement (only the eligible one gets it)
        assert await until(lambda: w.r1.payloads(Settlement) and w.r2.payloads(Settlement))
        s1, s2 = w.r1.payloads(Settlement)[0], w.r2.payloads(Settlement)[0]
        assert (s1.driver_payout, s1.excess_returned, s1.subsidy_applied) == (12.0, 8.0, 6.0)
        assert (s2.driver_payout, s2.excess_returned, s2.subsidy_applied) == (9.0, 6.0, 0.0)
        # money
        assert w.ledger.balance(w.jid("r1")) == 94.0 and w.ledger.balance(w.jid("r2")) == 91.0
        assert w.ledger.subsidy_spent == 6.0 and w.ledger.check_conservation()
        assert all(w.ledger.status(e.escrow_id) == EscrowStatus.SETTLED
                   for e in w.ledger.pool_escrows("pool-1"))
        # everything about this pool shares one thread
        assert {m.thread for m in w.drv.got(Protocol.SETTLEMENT)} == {"pool-1"}


@pytest.mark.asyncio
async def test_settlement_without_fares_charges_the_whole_hold(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_completed({})
        assert await until(lambda: w.hub.payloads(SettlementComplete))
        assert w.hub.payloads(SettlementComplete)[0].total_paid == 35.0
        assert w.ledger.balance(w.jid("drv")) == 35.0


@pytest.mark.asyncio
async def test_repeated_trip_completed_pays_once(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        fares = {w.jid("r1"): 12.0, w.jid("r2"): 9.0}
        await w.hub_says_completed(fares)
        assert await until(lambda: len(w.drv.payloads(Settlement)) == 2)
        await w.hub_says_completed(fares)                                 # hub retries
        assert await until(lambda: len(w.hub.payloads(SettlementComplete)) == 2)
        await asyncio.sleep(0.3)
        assert w.hub.payloads(SettlementComplete)[0] == w.hub.payloads(SettlementComplete)[1]
        assert len(w.drv.payloads(Settlement)) == 2                       # no second payout
        assert w.ledger.balance(w.jid("drv")) == 21.0 and w.ledger.check_conservation()
        assert w.pay.stats["settlements"] == 1 and w.pay.stats["settlement_replayed"] == 1


# ---- failure path: hold -> refund ------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_path_hold_then_refund(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_failed(reason=FailureReason.NO_MATCH)           # all held escrows of the pool
        assert await until(lambda: len(w.hub.payloads(Refund)) == 2)
        assert await until(lambda: w.r1.payloads(Refund) and w.r2.payloads(Refund))

        r1, r2 = w.r1.payloads(Refund)[0], w.r2.payloads(Refund)[0]
        assert (r1.amount, r1.reason) == (20.0, FailureReason.NO_MATCH)
        assert (r2.amount, r2.reason) == (15.0, FailureReason.NO_MATCH)
        assert w.ledger.balance(w.jid("r1")) == w.ledger.balance(w.jid("r2")) == 100.0
        assert w.ledger.balance(w.jid("drv")) == 0.0 and w.ledger.subsidy_spent == 0.0
        assert all(w.ledger.status(e.escrow_id) == EscrowStatus.REFUNDED
                   for e in w.ledger.pool_escrows("pool-1"))
        assert w.ledger.check_conservation()


@pytest.mark.asyncio
async def test_refund_of_a_single_escrow(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        first = w.r1.payloads(Escrow)[0]
        await w.hub_says_failed([first.escrow_id], FailureReason.RIDER_CANCELLED)
        assert await until(lambda: w.r1.payloads(Refund))
        assert w.ledger.balance(w.jid("r1")) == 100.0 and w.ledger.balance(w.jid("r2")) == 85.0
        # the other rider still settles normally afterwards
        await w.hub_says_completed({w.jid("r2"): 10.0}, riders=[w.jid("r2")])
        assert await until(lambda: w.hub.payloads(SettlementComplete))
        assert w.ledger.balance(w.jid("drv")) == 10.0
        assert w.ledger.balance(w.jid("r2")) == 85.0 + 5.0               # hold 15, fare 10, 5 back
        assert w.ledger.check_conservation()


@pytest.mark.asyncio
async def test_repeated_refund_returns_money_once(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_failed()
        assert await until(lambda: len(w.hub.payloads(Refund)) == 2)
        await w.hub_says_failed()
        assert await until(lambda: len(w.hub.payloads(Refund)) == 4)      # hub is told again...
        await asyncio.sleep(0.3)
        assert len(w.r1.payloads(Refund)) == 1                             # ...riders are not
        assert w.ledger.balance(w.jid("r1")) == 100.0 and w.ledger.check_conservation()


@pytest.mark.asyncio
async def test_refund_after_settlement_is_refused(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_completed({})
        assert await until(lambda: w.hub.payloads(SettlementComplete))
        await w.hub_says_failed()
        assert await until(lambda: len(w.hub.got(Protocol.REFUND, Perf.REFUSE)) == 2)
        bodies = [m.body for m in w.hub.got(Protocol.REFUND, Perf.REFUSE)]
        assert all(b.startswith(Reason.ALREADY_SETTLED + ":esc-") for b in bodies)
        assert w.ledger.balance(w.jid("drv")) == 35.0 and w.ledger.balance(w.jid("r1")) == 90.0


# ---- refused escrow --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insufficient_funds_refuses_rider_and_notifies_hub(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        w.ledger.open_wallet(w.jid("r1"), 5.0)                             # poor rider
        await w.hold("r1", 20.0)
        assert await until(lambda: w.r1.got(Protocol.ESCROW, Perf.REFUSE)
                           and w.hub.got(Protocol.ESCROW, Perf.REFUSE))
        assert w.r1.got(Protocol.ESCROW, Perf.REFUSE)[0].body == Reason.INSUFFICIENT_FUNDS
        assert w.hub.got(Protocol.ESCROW, Perf.REFUSE)[0].body == Reason.INSUFFICIENT_FUNDS   # escrow_failed
        assert not w.hub.got(Protocol.ESCROW, Perf.INFORM)
        assert w.ledger.balance(w.jid("r1")) == 5.0 and not w.ledger.held()


@pytest.mark.asyncio
async def test_unknown_rider_when_no_endowment(xmpp_server, sim_clock):
    async with World(xmpp_server, default_rider_balance=None) as w:
        await w.hold("r1", 5.0)
        assert await until(lambda: w.r1.got(Protocol.ESCROW, Perf.REFUSE))
        assert w.r1.got(Protocol.ESCROW, Perf.REFUSE)[0].body == Reason.UNKNOWN_ACCOUNT


@pytest.mark.asyncio
async def test_retried_escrow_request_charges_once(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await w.hold("r1", 20.0, req="same")
        await w.hold("r1", 20.0, req="same")
        assert await until(lambda: len(w.r1.payloads(Escrow)) == 2)
        a, b = w.r1.payloads(Escrow)
        assert a.escrow_id == b.escrow_id
        assert w.ledger.balance(w.jid("r1")) == 80.0 and len(w.ledger.held()) == 1


# ---- abuse ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nobody_can_spend_another_riders_money(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        req = w.escrow_request("r1", 20.0)                                  # in r1's name...
        await w.r2.tell(make_request(w.to_pay(), Protocol.ESCROW, req, thread=req.request_id))  # ...sent by r2
        assert await until(lambda: w.r2.got(Protocol.ESCROW, Perf.REFUSE))
        assert w.r2.got(Protocol.ESCROW, Perf.REFUSE)[0].body.startswith(Reason.NOT_AUTHORISED)
        assert not w.hub.inbox and not w.ledger.held()
        assert not w.ledger.has_wallet(w.jid("r1"))


@pytest.mark.asyncio
async def test_driver_cannot_trigger_settlement(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_completed({}, sender=w.drv)                       # GC-03: only the hub
        assert await until(lambda: w.drv.got(Protocol.SETTLEMENT, Perf.REFUSE))
        assert w.drv.got(Protocol.SETTLEMENT, Perf.REFUSE)[0].body.startswith(Reason.NOT_AUTHORISED)
        assert w.ledger.balance(w.jid("drv")) == 0.0 and len(w.ledger.held()) == 2


@pytest.mark.asyncio
async def test_rider_cannot_pull_money_back_mid_trip(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_failed(sender=w.r1)                                # rider tries a refund
        assert await until(lambda: len(w.r1.got(Protocol.REFUND, Perf.REFUSE)) == 2)
        assert all(m.body.startswith(Reason.NOT_AUTHORISED) for m in w.r1.got(Protocol.REFUND, Perf.REFUSE))
        assert w.ledger.balance(w.jid("r1")) == 80.0 and len(w.ledger.held()) == 2


@pytest.mark.asyncio
async def test_another_hub_cannot_settle_or_refund(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        other = Stub(f"otherhub_{w.n}@localhost", "pw", port=xmpp_server)
        await other.start(auto_register=True)
        try:
            await w.hub_says_completed({}, sender=other)
            await w.hub_says_failed(sender=other)
            assert await until(lambda: other.got(Protocol.SETTLEMENT, Perf.REFUSE)
                               and len(other.got(Protocol.REFUND, Perf.REFUSE)) == 2)
        finally:
            await other.stop()
        assert len(w.ledger.held()) == 2 and w.ledger.balance(w.jid("drv")) == 0.0


@pytest.mark.asyncio
async def test_fare_above_the_hold_is_refused_and_nothing_moves(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        await two_holds(w)
        await w.hub_says_completed({w.jid("r1"): 12.0, w.jid("r2"): 99.0})
        assert await until(lambda: w.hub.got(Protocol.SETTLEMENT, Perf.REFUSE))
        assert w.hub.got(Protocol.SETTLEMENT, Perf.REFUSE)[0].body.startswith(Reason.FARE_EXCEEDS_ESCROW)
        assert w.ledger.balance(w.jid("drv")) == 0.0 and len(w.ledger.held()) == 2


@pytest.mark.asyncio
async def test_garbage_does_not_kill_the_protocols(xmpp_server, sim_clock):
    async with World(xmpp_server) as w:
        pay = w.to_pay()
        # unparsable body, wrong payload type, no payload: each gets a protocol-level failure
        await w.r1.tell(make_message(pay, Perf.REQUEST, Protocol.ESCROW, "{not json", thread="g1"))
        await w.r1.tell(make_message(pay, Perf.REQUEST, Protocol.ESCROW,
                                     TripFailed("p", FailureReason.NO_MATCH), thread="g2"))
        await w.hub.tell(make_message(pay, Perf.INFORM, Protocol.SETTLEMENT, "???", thread="g3"))
        await w.hub.tell(make_message(pay, Perf.REQUEST, Protocol.REFUND, None, thread="g4"))
        assert await until(lambda: len(w.r1.got(performative=Perf.FAILURE)) == 2
                           and w.hub.got(Protocol.SETTLEMENT, Perf.FAILURE)
                           and w.hub.got(Protocol.REFUND, Perf.FAILURE))
        # all three FSMs are still alive and serve normal traffic afterwards
        await two_holds(w)
        await w.hub_says_completed({})
        assert await until(lambda: w.hub.payloads(SettlementComplete))
        assert w.ledger.balance(w.jid("drv")) == 35.0


# ---- liveness net -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_abandoned_escrow_is_returned_after_hold_max(xmpp_server, sim_clock):
    # hold_max 20 sim s = 0.2 real s at speed 100; sweep every 5 sim s
    async with World(xmpp_server, hold_max=20.0, sweep_every=5.0) as w:
        await w.hold("r1", 20.0)
        assert await until(lambda: w.hub.payloads(Escrow))
        assert w.ledger.balance(w.jid("r1")) == 80.0
        assert await until(lambda: w.r1.payloads(Refund) and w.hub.payloads(Refund), timeout=8)
        refund = w.r1.payloads(Refund)[0]
        assert (refund.amount, refund.reason) == (20.0, FailureReason.TIMEOUT)
        assert w.ledger.balance(w.jid("r1")) == 100.0 and not w.ledger.held()
        assert w.pay.stats["refund_expired"] == 1 and w.ledger.check_conservation()


@pytest.mark.asyncio
async def test_settled_escrow_is_not_swept(xmpp_server, sim_clock):
    async with World(xmpp_server, hold_max=20.0, sweep_every=5.0) as w:
        await w.hold("r1", 20.0)
        assert await until(lambda: w.hub.payloads(Escrow))
        await w.hub_says_completed({w.jid("r1"): 20.0}, riders=[w.jid("r1")])
        assert await until(lambda: w.hub.payloads(SettlementComplete))
        await asyncio.sleep(0.6)                                            # > 3x hold_max
        assert not w.r1.payloads(Refund) and w.ledger.balance(w.jid("drv")) == 20.0


# ---- misc -------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_agent_still_answers_pings(xmpp_server, sim_clock):
    from harness.ping import ping_agents
    async with World(xmpp_server) as w:
        res = await ping_agents([w.to_pay()], port=xmpp_server, timeout=5)
        assert res[0].ok and res[0].role == "payment"


def test_policy_changes_subsidy_rate_and_ignores_stale_versions():
    pay = PaymentAgent("pay@localhost", "pw", subsidy_discount=0.5)
    def policy(version, rate):
        return Policy("pol", version, 0.0, {}, [], {"eligible_discount": rate})
    assert pay.set_policy(policy(1, 0.8)) and pay.ledger.subsidy_discount == 0.8
    assert not pay.set_policy(policy(1, 0.1)) and pay.ledger.subsidy_discount == 0.8
    assert pay.set_policy(policy(2, 0.2)) and pay.ledger.subsidy_discount == 0.2
    assert pay.account("rider_01") == "rider_01@localhost"
    assert pay.account("rider_01@other") == "rider_01@other"

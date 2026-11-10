"""
Milestone 8: the Regulator imposes fare caps (PolicyProtocol, RI-01/RI-04) and audits hubs
(AuditProtocol, RI-02/RI-05), with real Hub, Driver, Payment and Rider agents.
"""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.regulator import RegulatorAgent
from agents.rider import COMPLETED, RiderAgent, UNMATCHED
from common import clock
from common.graph import build_grid_network
from common.messages import make_request
from common.ontology import Policy, Protocol

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
    def __init__(self, net, hub_kw=None, reg_kw=None, drivers=(HUB,)):
        n = uuid.uuid4().hex[:6]
        jid = lambda k: f"{k}_{n}@localhost"
        self.pay = PaymentAgent(jid("pay"), "pw", port=XMPP_PORT, default_rider_balance=100.0,
                                regulator_jid=jid("reg"))
        hk = dict(t_auction=90.0, t_award=60.0, t_escrow=90.0)
        hk.update(hub_kw or {})
        self.hub = MicrohubAgent(jid("hub"), "pw", port=XMPP_PORT, hub_id=HUB, network=net,
                                 payment_jid=jid("pay"), regulator_jid=jid("reg"), **hk)
        self.drivers = [DriverAgent(jid(f"d{i}"), "pw", port=XMPP_PORT, start_hub=start, network=net,
                                    hub_jids=[jid("hub")], payment_jid=jid("pay"), regulator_jid=jid("reg"),
                                    t_provisional=300.0, dwell_pickup=1, dwell_dropoff=1)
                        for i, start in enumerate(drivers)]
        self.hub.driver_jids = [str(d.jid.bare) for d in self.drivers]
        rk = dict(hub_jids=[jid("hub")], driver_jids=[str(d.jid.bare) for d in self.drivers],
                  payment_jid=jid("pay"), auto_audit=False, t_policy_ack=15.0, t_audit_response=30.0)
        rk.update(reg_kw or {})
        self.reg = RegulatorAgent(jid("reg"), "pw", port=XMPP_PORT, **rk)
        self.rider = RiderAgent(jid("rider"), "pw", port=XMPP_PORT, hub_jids={HUB: jid("hub")}, network=net,
                                payment_jid=jid("pay"), ready_delay=2.0)
        self.probe = Probe(jid("probe"), "pw", port=XMPP_PORT)
        self.agents = [self.pay, self.reg, self.hub, *self.drivers, self.rider, self.probe]
        self.jid = jid

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        assert await until(lambda: len(self.hub.registry) == len(self.drivers))
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def ride(self):
        assert self.rider.request_trip("n2_2", 20.0) is not None
        await asyncio.wait_for(self.rider.ended.wait(), 60)
        return self.rider.trips[-1]

    async def acked(self, version=1, n=None):
        n = n or (1 + len(self.drivers) + 1)                      # hub + drivers + payment
        return await until(lambda: len(self.reg.acks.get(version, {})) == n, 10)


# ---- PolicyProtocol -------------------------------------------------------------------------

async def test_policy_reaches_hub_driver_and_payment_and_all_agree(xmpp_server, sim_clock, net):
    async with World(net) as w:
        pol = w.reg.publish_policy(fare_caps={"max": 12.0}, subsidy_rules={"eligible_discount": 0.3})
        assert await w.acked()
        assert set(w.reg.acks[pol.version].values()) == {"agree"} and w.reg.unacknowledged() == []
        assert w.hub.policies.latest == pol and w.drivers[0].policies.latest == pol
        assert w.hub.current_fare_cap() == 12.0
        # Payment adopts the subsidy rate (lazily, when it next settles)
        w.pay.refresh_policy()
        assert w.pay.ledger.subsidy_discount == pytest.approx(0.3)


async def test_new_version_keeps_what_it_does_not_change(xmpp_server, sim_clock, net):
    async with World(net) as w:
        w.reg.publish_policy(fare_caps={"max": 12.0}, equity_zones=["Z1"])
        p2 = w.reg.publish_policy(subsidy_rules={"eligible_discount": 0.2})
        assert p2.version == 2 and p2.fare_caps == {"max": 12.0} and p2.equity_zones == ["Z1"]
        assert await w.acked(2)


async def test_a_cap_below_the_sustainable_driver_level_is_not_published(xmpp_server, sim_clock, net):
    async with World(net) as w:
        with pytest.raises(ValueError):
            w.reg.publish_policy(fare_caps={"max": 1.0})
        assert w.reg.book.latest is None


async def test_policy_from_anyone_but_the_regulator_is_refused(xmpp_server, sim_clock, net):
    async with World(net) as w:
        fake = Policy("pol-x", 99, 0.0, {"max": 0.5}, [], {})
        await w.probe.tell(make_request(str(w.hub.jid.bare), Protocol.POLICY, fake, thread="99"))
        assert await until(lambda: w.probe.got(Protocol.POLICY))
        assert w.probe.got(Protocol.POLICY)[0].body == "unknown_regulator"
        assert w.hub.policies.latest is None and w.hub.current_fare_cap() is None


async def test_silent_recipient_gets_one_resend_and_is_listed_unacknowledged(xmpp_server, sim_clock, net):
    async with World(net) as w:
        ghost = "ghost@localhost"
        w.reg.publish_policy(fare_caps={"max": 12.0},
                             recipients=[str(w.hub.jid.bare), ghost])
        assert await until(lambda: w.reg.acks[1].get(str(w.hub.jid.bare)) == "agree", 10)
        assert await until(lambda: (1, ghost) in w.reg._resent, 10)
        assert w.reg.unacknowledged() == [ghost]


async def test_a_policy_can_take_effect_later(xmpp_server, sim_clock, net):
    async with World(net) as w:
        later = clock.now() + 300
        w.reg.publish_policy(fare_caps={"max": 12.0}, effective_from=later)
        assert await w.acked()
        assert w.hub.current_fare_cap(later - 1) is None and w.hub.current_fare_cap(later) == 12.0


# ---- RI-01: the cap binds the auction -------------------------------------------------------------

async def test_fare_never_exceeds_the_cap(xmpp_server, sim_clock, net):
    async with World(net, drivers=(HUB, "n4_1")) as w:
        w.reg.publish_policy(fare_caps={"max": 3.0})
        assert await w.acked(n=1 + 2 + 1)
        trip = await w.ride()
        assert trip.outcome == COMPLETED and trip.match.final_fare <= 3.0 + 1e-9
        assert trip.proposal.split_fare == 3.0                           # the rider is told the ceiling up front
        # the 3-hop driver's cost (3.20) is above the cap: it refused, only one bid reached the hub
        assert len(w.hub.finished[-1].bids) == 1


async def test_a_cap_below_every_drivers_cost_leaves_the_rider_unmatched_and_refunded(xmpp_server, sim_clock, net):
    async with World(net, hub_kw={"t_auction": 40.0}) as w:
        w.reg.publish_policy(fare_caps={"max": 2.2})                      # above the floor (2.0), below the cost (2.48)
        assert await w.acked()
        trip = await w.ride()
        assert trip.outcome == UNMATCHED
        assert await until(lambda: trip.refund is not None, 10)             # Payment told the rider
        assert w.pay.ledger.balance(str(w.rider.jid.bare)) == pytest.approx(100.0)
        assert await until(lambda: w.hub.refunds, 10)


# ---- AuditProtocol ----------------------------------------------------------------------------------

async def test_audit_of_a_compliant_hub_passes(xmpp_server, sim_clock, net):
    async with World(net, drivers=(HUB, "n4_1")) as w:
        w.reg.publish_policy(fare_caps={"max": 3.0})
        assert await w.acked(n=4)
        await w.ride()
        aid = w.reg.audit(str(w.hub.jid.bare), period_start=0.0)
        assert await until(lambda: w.reg.outcomes, 10)
        out = w.reg.outcomes[-1]
        assert out.audit_id == aid and out.cooperated and out.report.compliant and out.penalty is None
        assert out.report.fare_violations == 0 and list(out.report.zone_service_share.values()) == [1.0]
        assert await until(lambda: w.hub.audit_reports, 10) and w.hub.audit_reports[0].compliant
        assert w.hub.penalties == []


async def test_audit_catches_a_hub_that_never_applied_the_cap(xmpp_server, sim_clock, net):
    async with World(net) as w:
        # the hub is NOT among the recipients: it never hears about the cap and charges the uncapped reserve
        w.reg.publish_policy(fare_caps={"max": 3.0}, recipients=[str(d.jid.bare) for d in w.drivers])
        assert await until(lambda: len(w.reg.acks.get(1, {})) == 1, 10)
        trip = await w.ride()
        assert trip.match.final_fare > 3.0
        w.reg.audit(str(w.hub.jid.bare), period_start=0.0)
        assert await until(lambda: w.reg.outcomes, 10)
        out = w.reg.outcomes[-1]
        assert not out.report.compliant and out.violations == [trip.pool_id] and out.penalty.amount > 0
        assert await until(lambda: w.hub.penalties, 10) and "fare_cap_violation" in w.hub.penalties[0].reason
        assert not w.hub.audit_reports[0].compliant


async def test_the_regulator_judges_by_its_own_policy_record_not_the_hubs_claim(xmpp_server, sim_clock, net):
    """A fare above a cap that was NOT yet in force when it was awarded is not a violation."""
    async with World(net) as w:
        trip = await w.ride()                                              # awarded with no policy at all
        assert trip.match.final_fare == 20.0
        w.reg.publish_policy(fare_caps={"max": 3.0})                      # published after the trip
        w.reg.audit(str(w.hub.jid.bare), period_start=0.0)
        assert await until(lambda: w.reg.outcomes, 10)
        assert w.reg.outcomes[-1].report.compliant


async def test_hub_that_refuses_the_logs_is_penalised_for_non_cooperation(xmpp_server, sim_clock, net):
    async with World(net, hub_kw={"audit_logs_available": False}) as w:
        w.reg.audit(str(w.hub.jid.bare))
        assert await until(lambda: w.reg.outcomes, 10)
        out = w.reg.outcomes[-1]
        assert not out.cooperated and out.penalty.reason == "non_cooperation"
        assert await until(lambda: w.hub.penalties, 10)


async def test_silence_is_non_cooperation_too(xmpp_server, sim_clock, net):
    async with World(net, reg_kw={"t_audit_response": 20.0}) as w:
        w.reg.audit("ghost@localhost")
        assert await until(lambda: w.reg.outcomes, 10)
        assert w.reg.outcomes[-1].penalty.reason == "non_cooperation" and w.reg.pending == {}


async def test_audit_data_from_the_wrong_sender_is_ignored(xmpp_server, sim_clock, net):
    async with World(net, reg_kw={"t_audit_response": 600.0}) as w:
        aid = w.reg.audit(str(w.hub.jid.bare))
        from common.messages import make_inform
        from common.ontology import AuditData
        await w.probe.tell(make_inform(str(w.reg.jid.bare), Protocol.AUDIT, AuditData(aid, HUB, 0.0, 1.0), aid))
        assert await until(lambda: w.reg.outcomes, 10)                     # the real hub's answer is the one judged
        assert w.reg.outcomes[0].hub == str(w.hub.jid.bare) and len(w.reg.outcomes) == 1


async def test_hubs_are_audited_every_t_audit(xmpp_server, sim_clock, net):                   # RI-02
    async with World(net, reg_kw={"auto_audit": True, "t_audit": 60.0}) as w:
        assert await until(lambda: len(w.reg.outcomes) >= 2, 15)
        assert all(o.hub == str(w.hub.jid.bare) and o.report.compliant for o in w.reg.outcomes)
        a, b = w.reg.outcomes[0].report, w.reg.outcomes[1].report
        assert b.period_start == pytest.approx(a.period_end)               # consecutive, no gaps

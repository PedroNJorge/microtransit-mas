"""Reactive side of the BDI layer: percepts out, cruise commands in, a cruising driver is still available."""
import asyncio
import uuid

import pytest

from agents.driver import DriverAgent
from common import clock
from common.messages import make_accept_proposal, make_cfp, make_message
from common.ontology import (
    BrainPercept, CruiseCommand, DriverState, MatchResult, Perf, Protocol, parse_payload,
)

from conftest import XMPP_PORT
from test_driver_agent import Probe, until, sim_clock, net, Rig, HUB_NODE  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class BrainRig(Rig):
    """Rig + a probe that plays the brain."""

    def __init__(self, net, driver_kw=None):
        n = uuid.uuid4().hex[:6]
        self.brain = Probe(f"brain_{n}@localhost", "pw", port=XMPP_PORT)
        kw = dict(brain_jid=f"brain_{n}@localhost", t_percept=30.0)
        kw.update(driver_kw or {})
        super().__init__(net, kw)

    async def __aenter__(self):
        await asyncio.wait_for(self.brain.start(auto_register=True), 20)
        return await super().__aenter__()

    async def __aexit__(self, *exc):
        await super().__aexit__(*exc)
        await self.brain.stop()

    def percepts(self):
        return [parse_payload(m, strict=True) for m in self.brain.got(Protocol.DRIVER_BRAIN, Perf.INFORM)]

    async def command(self, hub_id, sender=None):
        cmd = CruiseCommand(str(self.drv.jid.bare), hub_id)
        await (sender or self.brain).tell(make_message(self.drv.jid, Perf.REQUEST, Protocol.DRIVER_BRAIN, cmd, "c1"))


async def test_driver_sends_percepts_to_its_brain(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        assert await until(lambda: r.percepts())
        p = r.percepts()[0]
        assert isinstance(p, BrainPercept)
        assert (p.state, p.location, p.at_hub, p.home) == ("idle", HUB_NODE, HUB_NODE, HUB_NODE)
        assert HUB_NODE in p.hub_ids and set(p.hub_ids) == set(net.hubs)        # the brain sees every hub
    assert p.etas[p.hub_ids.index(HUB_NODE)] == 0.0


async def test_every_cfp_is_a_sighting_of_demand(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        for i in range(3):
            c = r.cfp(r.h1, pool=f"p{i}", fare=0.01)                      # reserve too low: refused, no lock
            c.hub_id = "n4_1"
            c.constraints["callees"] = 4.0
            await r.h1.tell(make_cfp(r.drv.jid, c))
        assert await until(lambda: len(r.drv.obs) == 3)
        press = r.drv.pressures(clock.now())
        assert press == {"n4_1": pytest.approx(0.75)}                    # 3 requests / 4 drivers called
        r.drv._last_percept = (None, -1e9)                               # force the next percept now
        assert await until(lambda: any(p.pressures and "n4_1" in p.hub_ids for p in r.percepts()), 8)
        p = [p for p in r.percepts() if "n4_1" in p.hub_ids][-1]
        assert p.pressures[p.hub_ids.index("n4_1")] == pytest.approx(0.75)
        assert p.etas[p.hub_ids.index("n4_1")] == net.travel_time(HUB_NODE, "n4_1")


async def test_a_repeated_cfp_counts_once(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        c = r.cfp(r.h1, fare=0.01)
        await r.h1.tell(make_cfp(r.drv.jid, c))
        await r.h1.tell(make_cfp(r.drv.jid, c))
        assert await until(lambda: len(r.h1.got(Protocol.MATCH_AUCTION, Perf.REFUSE)) == 2)
        assert len(r.drv.obs) == 1


async def test_brain_command_makes_an_idle_driver_cruise_and_it_stays_available(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        await r.command("n1_3")
        assert await until(lambda: r.brain.got(Protocol.DRIVER_BRAIN, Perf.AGREE))
        assert await until(lambda: r.drv.activity == "cruising")
        assert r.drv.state == DriverState.IDLE                           # available: the hubs still see it as idle
        assert await until(lambda: r.drv.location != HUB_NODE, 8)
        # a call while cruising is answered with a bid, not a refusal
        await r.h1.tell(make_cfp(r.drv.jid, r.cfp(r.h1, pool="mid", dest="n1_2")))
        assert await until(lambda: r.h1.got(Protocol.MATCH_AUCTION, Perf.PROPOSE, "mid"), 8)
        assert not r.h1.got(Protocol.MATCH_AUCTION, Perf.REFUSE, "mid")


async def test_cruise_reaches_the_hub_and_returns_to_idle(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        await r.command("n1_2")
        assert await until(lambda: r.drv.location == "n1_2" and r.drv.activity == "idle", 12)
        assert r.drv.cruises == ["n1_2"] and r.drv.cruise is None


async def test_commands_from_anyone_but_the_brain_are_ignored(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        await r.command("n1_3", sender=r.h1)
        await asyncio.sleep(0.5)
        assert r.drv.cruises == [] and not r.h1.got(Protocol.DRIVER_BRAIN)


async def test_a_driver_holding_a_lock_refuses_to_cruise(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        await r.h1.tell(make_cfp(r.drv.jid, r.cfp(r.h1, pool="held")))   # a real bid -> provisional lock
        assert await until(lambda: r.h1.got(Protocol.MATCH_AUCTION, Perf.PROPOSE))
        await r.command("n1_3")
        assert await until(lambda: r.brain.got(Protocol.DRIVER_BRAIN, Perf.REFUSE))
        assert r.brain.got(Protocol.DRIVER_BRAIN, Perf.REFUSE)[0].body == "locked"
        assert r.drv.cruises == []


async def test_winning_a_job_stops_the_cruise_and_starts_the_trip(xmpp_server, sim_clock, net):
    async with BrainRig(net) as r:
        await r.command("n4_4")                                          # a long cruise
        assert await until(lambda: r.drv.activity == "cruising")
        await r.h1.tell(make_cfp(r.drv.jid, r.cfp(r.h1, pool="job", dest="n1_2")))
        assert await until(lambda: r.h1.got(Protocol.MATCH_AUCTION, Perf.PROPOSE, "job"), 8)
        bid = parse_payload(r.h1.got(Protocol.MATCH_AUCTION, Perf.PROPOSE, "job")[0], strict=True)
        match = MatchResult("job", str(r.drv.jid.bare), bid.price, clock.now() + 600, {"rider@localhost": bid.price})
        await r.h1.tell(make_accept_proposal(r.drv.jid, match))
        assert await until(lambda: r.drv.assignment is not None and r.drv.cruise is None, 8)
        assert r.drv.activity in ("to_pickup", "on_trip")
        assert await until(lambda: not r.drv._cruise_busy, 8)            # the cruise ended at its next junction
        assert r.drv.location != "n4_4"                                  # and the driver never finished it


async def test_a_driver_without_a_brain_sends_no_percepts_and_ignores_commands(xmpp_server, sim_clock, net):
    async with Rig(net) as r:
        await asyncio.sleep(0.5)
        assert r.drv.brain_jid is None and r.drv.cruises == []


async def test_demand_signals_from_hubs_count_even_when_the_driver_was_not_called(xmpp_server, sim_clock, net):
    from common.messages import make_inform
    from common.ontology import DemandSignal
    async with BrainRig(net) as r:
        sig = DemandSignal("n4_1", "pX", 0.0, clock.now())
        await r.h1.tell(make_inform(r.drv.jid, Protocol.DRIVER_BRAIN, sig, "pX"))
        await r.h1.tell(make_inform(r.drv.jid, Protocol.DRIVER_BRAIN, sig, "pX"))     # same pool: once
        await r.brain.tell(make_inform(r.drv.jid, Protocol.DRIVER_BRAIN, DemandSignal("n4_1", "pY", 0.0, 0.0), "pY"))
        assert await until(lambda: len(r.drv.obs) == 1)
        await asyncio.sleep(0.3)
        assert len(r.drv.obs) == 1                       # the brain is not a hub: its signal is ignored
        assert r.drv.pressures(clock.now()) == {"n4_1": pytest.approx(1.0)}   # nobody was called: 1 request / max(1, 0)

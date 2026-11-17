"""BDI layer end to end: a real DriverBrain (SPADE-BDI, AgentSpeak) and a real DriverAgent.

A scripted hub keeps calling for proposals at a far hub (the driver can't win them: reserve too low),
which is a sighting of demand. The brain believes pressure(n4_1, ...) is high, forms the intention to
be there, and commands the driver to cruise. A job then overrides the intention."""
import asyncio
import uuid

import pytest

pytest.importorskip("spade_bdi")

from agents.brain import DriverBrain
from agents.driver import DriverAgent
from common import clock
from common.messages import make_cfp
from common.ontology import CFP, Protocol, Perf

from conftest import XMPP_PORT
from test_driver_agent import Probe, until, sim_clock, net  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
HOME, FAR = "n1_1", "n4_1"


class World:
    def __init__(self, net):
        n = uuid.uuid4().hex[:6]
        self.hub = Probe(f"hub_{n}@localhost", "pw", port=XMPP_PORT)
        self.drv = DriverAgent(f"drv_{n}@localhost", "pw", port=XMPP_PORT, start_hub=HOME, network=net,
                               hub_jids=[f"hub_{n}@localhost"], brain_jid=f"brain_{n}@localhost",
                               t_percept=10.0, t_provisional=60.0, dwell_pickup=1, dwell_dropoff=1)
        self.brain = DriverBrain(f"brain_{n}@localhost", "pw", driver_jid=f"drv_{n}@localhost", port=XMPP_PORT)
        self.agents = [self.hub, self.drv, self.brain]

    async def __aenter__(self):
        for a in self.agents:
            await asyncio.wait_for(a.start(auto_register=True), 20)
        return self

    async def __aexit__(self, *exc):
        for a in reversed(self.agents):
            await a.stop()

    async def sighting(self, k, callees=2.0):
        for i in range(k):
            c = CFP(f"p{uuid.uuid4().hex[:6]}", FAR, FAR, ["n4_2"], {"r": [0.0, 7200.0]}, 1, clock.now() + 300,
                    0.01, 600.0, 0, {"callees": callees}, ["rider@localhost"])
            await self.hub.tell(make_cfp(self.drv.jid, c))


async def test_brain_sends_an_idle_driver_to_where_the_requests_are(xmpp_server, sim_clock, net):
    async with World(net) as w:
        assert await until(lambda: w.brain.percepts_received >= 1, 10)       # beliefs flow: home hub known
        assert w.brain.commands == []                                          # no demand seen yet: stay home
        await w.sighting(4)                                                    # 4 requests / 2 drivers -> pressure 2.0
        assert await until(lambda: w.brain.commands, 15)
        assert w.brain.commands[0] == FAR
        assert await until(lambda: w.drv.cruises == [FAR], 8)
        assert w.brain.agreed >= 1
        assert await until(lambda: w.drv.location == FAR and w.drv.activity == "idle", 15)
        # arrived and standing on the hub: the brain keeps it there (no further command)
        n = len(w.brain.commands)
        await asyncio.sleep(2.0)
        assert len(w.brain.commands) == n


async def test_brain_does_not_move_a_driver_for_weak_demand(xmpp_server, sim_clock, net):
    async with World(net) as w:
        assert await until(lambda: w.brain.percepts_received >= 1, 10)
        await w.sighting(1, callees=8.0)                                       # pressure 0.125 < min_pressure
        await asyncio.sleep(3.0)
        assert w.brain.commands == [] and w.drv.cruises == []

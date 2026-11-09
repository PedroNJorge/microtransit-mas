"""Milestone 3: skeleton agents answer pings; hubs/drivers track traffic."""
import asyncio

import pytest

from agents.base_agent import BaseAgent
from agents.registry import AGENT_CLASSES
from common import clock
from common.messages import make_message
from common.ontology import Perf, Protocol, TrafficUpdate
from harness.ping import ping_agents

pytestmark = pytest.mark.integration


def test_registry_covers_every_role():
    assert set(AGENT_CLASSES) == {"rider", "driver", "microhub", "payment", "regulator",
                                  "reputation", "environment"}
    assert all(issubclass(c, BaseAgent) for c in AGENT_CLASSES.values())
    assert all(c.role == name for name, c in AGENT_CLASSES.items())


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=50)
    yield
    clock.reset()


async def _start(cls, port, name, **kw):
    a = cls(f"{name}@localhost", "pw", port=port, **kw)
    await a.start(auto_register=True)
    return a


@pytest.mark.asyncio
async def test_every_skeleton_answers_with_its_own_role(xmpp_server, sim_clock):
    agents = [await _start(AGENT_CLASSES[role], xmpp_server, f"t_{role}")
              for role in ("rider", "driver", "microhub", "payment", "regulator", "reputation")]
    try:
        results = await ping_agents([str(a.jid) for a in agents], port=xmpp_server, timeout=5)
    finally:
        for a in agents:
            await a.stop()
    assert all(r.ok for r in results), results
    assert [r.role for r in results] == ["rider", "driver", "microhub", "payment",
                                         "regulator", "reputation"]
    assert all(a.pings_answered == 1 for a in agents)


@pytest.mark.asyncio
async def test_ping_to_absent_agent_times_out_without_hurting_others(xmpp_server, sim_clock):
    a = await _start(AGENT_CLASSES["payment"], xmpp_server, "t_pay2")
    try:
        res = await ping_agents(["t_pay2@localhost", "ghost@localhost"], port=xmpp_server,
                                timeout=1.0)
    finally:
        await a.stop()
    assert res[0].ok and not res[1].ok and "no answer" in res[1].error


@pytest.mark.asyncio
async def test_malformed_ping_gets_failure_reply(xmpp_server, sim_clock):
    from spade.agent import Agent
    from spade.behaviour import OneShotBehaviour

    target = await _start(AGENT_CLASSES["regulator"], xmpp_server, "t_reg")
    got = []

    class Rx(OneShotBehaviour):
        async def run(self):
            msg = make_message("t_reg@localhost", Perf.REQUEST, Protocol.HEARTBEAT, "not a ping",
                               thread="x1")
            await self.send(msg)
            got.append(await self.receive(timeout=3))

    probe = Agent("t_probe@localhost", "pw", port=xmpp_server)
    await probe.start(auto_register=True)
    b = Rx()
    probe.add_behaviour(b)
    await b.join()
    await probe.stop()
    await target.stop()
    assert got[0] is not None and got[0].get_metadata("performative") == Perf.FAILURE
    assert got[0].thread == "x1"


@pytest.mark.asyncio
async def test_hub_applies_full_then_delta_traffic(xmpp_server, sim_clock):
    from spade.agent import Agent
    from spade.behaviour import OneShotBehaviour
    from common.messages import make_inform

    hub = await _start(AGENT_CLASSES["microhub"], xmpp_server, "t_hub", hub_id="hub_x")
    rider = await _start(AGENT_CLASSES["rider"], xmpp_server, "t_rider2")   # not a subscriber
    sender = Agent("t_env@localhost", "pw", port=xmpp_server)
    await sender.start(auto_register=True)

    class Send(OneShotBehaviour):
        async def run(self):
            for upd in (TrafficUpdate("tu-1", 10.0, {"a|b": 5.0, "b|a": 6.0}, True),
                        TrafficUpdate("tu-2", 20.0, {"a|b": 9.0}, False),
                        TrafficUpdate("tu-0", 5.0, {"a|b": 1.0}, False)):     # stale: ignored
                await self.send(make_inform("t_hub@localhost", Protocol.TRAFFIC_UPDATE, upd,
                                            thread=upd.update_id))
    b = Send()
    sender.add_behaviour(b)
    await b.join()
    await asyncio.sleep(1.5)
    snapshot = (dict(hub.edge_weights), hub.traffic_update_id, hub.traffic_updates_received)
    await sender.stop(); await hub.stop(); await rider.stop()
    assert snapshot == ({"a|b": 9.0, "b|a": 6.0}, "tu-2", 2)

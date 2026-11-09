"""
Milestone 2 done-when: a test driver agent running alone receives a traffic update every
T_traffic simulated seconds. Needs the in-memory XMPP server from conftest.py (started
automatically on a non-default port).
"""
import asyncio

import pytest
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour

from agents.environment import EnvironmentAgent
from common import clock
from common.messages import make_template
from common.ontology import Protocol, TrafficUpdate, parse_payload

SPEED = 100            # 100 sim s per real s
T_TRAFFIC = 20.0       # sim s  -> 0.2 real s
pytestmark = pytest.mark.integration      # needs a local XMPP server

W = {"a|b": 90.0, "b|a": 90.0, "b|c": 40.0}


class StubNetwork:
    def __init__(self, weights):
        self.weights = dict(weights)

    def snapshot(self, update_id, timestamp):
        return TrafficUpdate(update_id, timestamp, dict(self.weights))

    def apply_traffic_update(self, upd):
        self.weights.update(upd.edge_weights)


class DriverStub(Agent):
    """Test driver: records every TrafficUpdate it receives (message + payload)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.updates = []

    class Rx(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=0.5)
            if msg:
                self.agent.updates.append((msg, parse_payload(msg, strict=True)))

    async def setup(self):
        self.add_behaviour(self.Rx(), make_template(protocol=Protocol.TRAFFIC_UPDATE))


@pytest.fixture
def sim_clock():
    clock.reset()
    clock.start(speed=SPEED)
    yield
    clock.reset()


async def _start(port, name, subscribers=(), **kw):
    driver = DriverStub(f"{name}@localhost", "pw", port=port)
    await driver.start(auto_register=True)
    env = EnvironmentAgent(f"env_{name}@localhost", "pw", StubNetwork(W),
                           subscribers=list(subscribers), t_traffic=T_TRAFFIC, port=port, **kw)
    return driver, env


@pytest.mark.asyncio
async def test_driver_receives_update_every_t_traffic(xmpp_server, sim_clock):
    driver, env = await _start(xmpp_server, "drv1", ["drv1@localhost"], full_every=10)
    await env.start(auto_register=True)
    await asyncio.sleep(1.5)
    await env.stop(); await driver.stop()

    updates = [u for _, u in driver.updates]
    assert len(updates) >= 4
    gaps = [b.timestamp - a.timestamp for a, b in zip(updates, updates[1:])]
    assert all(abs(g - T_TRAFFIC) <= 4.0 for g in gaps), gaps          # 4 sim s = 40 ms real
    assert [u.update_id for u in updates] == [f"tu-{i}" for i in range(1, len(updates) + 1)]


@pytest.mark.asyncio
async def test_first_is_full_snapshot_then_empty_deltas(xmpp_server, sim_clock):
    driver, env = await _start(xmpp_server, "drv2", ["drv2@localhost"], full_every=100)
    await env.start(auto_register=True)
    await asyncio.sleep(1.0)
    await env.stop(); await driver.stop()

    ups = [u for _, u in driver.updates]
    assert ups[0].full_snapshot and ups[0].edge_weights == W
    assert all(not u.full_snapshot and u.edge_weights == {} for u in ups[1:])


@pytest.mark.asyncio
async def test_messages_carry_expected_metadata(xmpp_server, sim_clock):
    driver, env = await _start(xmpp_server, "drv3", ["drv3@localhost"])
    await env.start(auto_register=True)
    await asyncio.sleep(0.6)
    await env.stop(); await driver.stop()

    msg, upd = driver.updates[0]
    assert msg.get_metadata("performative") == "inform"
    assert msg.get_metadata("protocol") == Protocol.TRAFFIC_UPDATE
    assert msg.get_metadata("ontology") == "TrafficUpdate"
    assert msg.thread == upd.update_id


@pytest.mark.asyncio
async def test_late_subscriber_gets_full_snapshot_immediately(xmpp_server, sim_clock):
    early, env = await _start(xmpp_server, "early", ["early@localhost"], full_every=100)
    late = DriverStub("late@localhost", "pw", port=xmpp_server)
    await late.start(auto_register=True)
    await env.start(auto_register=True)
    await asyncio.sleep(0.5)                           # a couple of deltas already sent
    env.network.weights["b|c"] = 77.0                  # state changed meanwhile
    env.add_subscriber("late@localhost")
    await asyncio.sleep(0.15)                          # < one tick: must come from the catch-up
    await env.stop(); await early.stop(); await late.stop()

    assert late.updates, "late subscriber got nothing"
    first = late.updates[0][1]
    assert first.full_snapshot and first.edge_weights["b|c"] == 77.0
    assert set(first.edge_weights) == set(W)


@pytest.mark.asyncio
async def test_no_subscribers_means_no_messages_and_no_crash(xmpp_server, sim_clock):
    driver, env = await _start(xmpp_server, "drv5")
    await env.start(auto_register=True)
    await asyncio.sleep(0.6)
    await env.stop(); await driver.stop()
    assert driver.updates == []

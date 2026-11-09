"""
src/agents/base_agent.py
BaseAgent: what every agent in the MAS shares. Milestone 3 scope: answer a Ping with a Pong.

Discovery (replaces the Directory Facilitator, which SPADE 4 does not have)
- Addresses are SCENARIO-DRIVEN: the scenario config fixes the JID of every fixed agent
  (hub_trindade@localhost, payment@localhost, ...), so agents never search for each other.
  The launcher derives who needs whom from that roster (e.g. the Environment's subscribers).
- Drivers will announce their state to nearby hubs with a DriverStatus message (Milestone 6).

Liveness
- HeartbeatProtocol: request(Ping) -> inform(Pong), same thread. The Pong carries the agent's
  JID and role, so a successful ping proves the agent is up and is the right kind of agent.
- A ping that cannot be parsed gets a protocol-level `failure` reply (Perf.FAILURE), never a
  business-level one.

Subclasses: set `role` and call `await super().setup()` first thing in their own setup().
"""
import logging
import time

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour

from common import clock
from common.messages import make_message, make_pong, make_reply, make_template
from common.ontology import Perf, Ping, Pong, Protocol, TrafficUpdate, parse_payload

logger = logging.getLogger(__name__)


class PingBehaviour(CyclicBehaviour):
    """Answer every incoming Ping with a Pong."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        agent = self.agent
        try:
            ping = parse_payload(msg)
            if not isinstance(ping, Ping):
                raise ValueError(f"expected a Ping, got {type(ping).__name__}")
        except (ValueError, TypeError) as e:
            logger.warning("%s: malformed ping from %s: %s", agent.jid, msg.sender, e)
            await self.send(make_reply(msg, Perf.FAILURE, f"malformed ping: {e}",
                                       Protocol.HEARTBEAT))
            return
        pong = Pong(ping_id=ping.ping_id, agent_id=str(agent.jid), role=agent.role,
                    ping_sent_at=ping.sent_at, replied_at=clock.now())
        await self.send(make_pong(msg, pong))
        agent.pings_answered += 1


class TrafficReceiver(CyclicBehaviour):
    """Keep `agent.edge_weights` current from the Environment's TrafficUpdates.
    A full snapshot replaces the table; a delta only overwrites the edges it lists.
    Updates older than the last applied one are ignored."""

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        agent = self.agent
        try:
            upd = parse_payload(msg, strict=True)
            if not isinstance(upd, TrafficUpdate):
                raise ValueError(f"expected a TrafficUpdate, got {type(upd).__name__}")
        except (ValueError, TypeError) as e:
            logger.warning("%s: malformed traffic update: %s", agent.jid, e)
            return
        if upd.timestamp < agent.traffic_timestamp:
            return
        if upd.full_snapshot:
            agent.edge_weights = dict(upd.edge_weights)
        else:
            agent.edge_weights.update(upd.edge_weights)
        agent.traffic_timestamp = upd.timestamp
        agent.traffic_update_id = upd.update_id
        agent.traffic_updates_received += 1


class BaseAgent(Agent):
    role: str = "base"
    subscribes_to_traffic: bool = False       # hubs and drivers set this to True

    def __init__(self, jid: str, password: str, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.pings_answered = 0
        self.created_at = time.monotonic()
        self.edge_weights: dict = {}               # latest known base travel times (seconds)
        self.traffic_timestamp: float = float("-inf")
        self.traffic_update_id: str = ""
        self.traffic_updates_received = 0

    async def setup(self):
        self.add_behaviour(
            PingBehaviour(),
            make_template(protocol=Protocol.HEARTBEAT, performative=Perf.REQUEST))
        if self.subscribes_to_traffic:
            self.add_behaviour(TrafficReceiver(), make_template(protocol=Protocol.TRAFFIC_UPDATE))

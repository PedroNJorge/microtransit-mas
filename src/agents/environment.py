"""
src/agents/environment.py
EnvironmentAgent (Role: Environment, Sub-org: Environmental Simulation).

Milestone 2 scope: broadcast TrafficUpdate messages every T_traffic SIM-seconds.
Travel times are static for now; the seam for variation is `traffic_model` (below).
No auction, pooling or routing logic lives here.

Role Model responsibilities
- Liveness: publishes a traffic update at a defined frequency.
- Safety:   never publishes contradictory states. Every update is built from ONE network
            state, carries a strictly increasing id (tu-1, tu-2, ...) and is sent to all
            subscribers in the same tick.

Wire format (see TrafficUpdate in ontology.py)
- The first update, every `full_every`-th update after it, and the catch-up sent to a late
  subscriber are FULL snapshots. All other updates are DELTAS (only changed edges, often
  empty). With the Porto graph (~8.5k edges) this turns ~190 KB per message into a few
  bytes while nothing changes.
- Limitation: a delta cannot say "this edge disappeared". Edges are never removed in this
  system (closures are Disruptions, a separate overlay).
- XMPP delivers in order, so a subscriber that started with a full snapshot stays consistent;
  the periodic full snapshot is the self-healing net for anything that slips.

The network
- Any object with the two methods of `TrafficNetwork` below. Your TransitNetwork
  (graph.py) already has both. The agent never reads the graph structure directly, so it
  works unchanged when graph.py moves to the directed Porto graph.

Traffic variation (not used yet)
- `traffic_model(current_weights, sim_time) -> {edge_key: new_weight}` is called once per
  tick BEFORE the update is built. Return only the edges that change. Returned weights
  must be > 0 and finite and the edges must exist. The agent applies them through
  `network.apply_traffic_update`.

Discovery
- SPADE 4 has no Directory Facilitator, so recipients are the `subscribers` list given by
  the harness (add more with add_subscriber()). A subscribe-by-message mechanism is a
  Milestone 3 decision.
"""
import logging
import math
from typing import Callable, Dict, Iterable, List, Optional, Protocol as TypingProtocol

from spade.behaviour import OneShotBehaviour, PeriodicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_inform
from common.ontology import Protocol, TrafficUpdate
from common.settings import FULL_SNAPSHOT_EVERY, T_TRAFFIC

logger = logging.getLogger(__name__)

TrafficModel = Callable[[Dict[str, float], float], Dict[str, float]]


class TrafficNetwork(TypingProtocol):
    """What the Environment needs from the road network."""

    def snapshot(self, update_id: str, timestamp: float) -> TrafficUpdate:
        """Full TrafficUpdate with the current base weight of every edge."""

    def apply_traffic_update(self, upd: TrafficUpdate) -> None:
        """Overwrite the listed edges' base weights."""


class TrafficUpdateBehaviour(PeriodicBehaviour):
    """Every T_traffic sim-seconds (period converted to real seconds): build one update and
    send it to every subscriber. Runs once immediately at start."""

    async def run(self):
        agent = self.agent
        update = agent.next_traffic_update()
        subscribers = list(agent.subscribers)
        for jid in subscribers:
            await self.send(make_inform(jid, Protocol.TRAFFIC_UPDATE, update,
                                        thread=update.update_id))
        logger.debug("Environment sent %s (%s, %d edges) to %d subscribers",
                     update.update_id, "full" if update.full_snapshot else "delta",
                     len(update.edge_weights), len(subscribers))


class SendFullSnapshot(OneShotBehaviour):
    """Catch-up for a subscriber that joined after the first update."""

    def __init__(self, jid: str):
        super().__init__()
        self.jid = jid

    async def run(self):
        update = self.agent.current_full_update()
        await self.send(make_inform(self.jid, Protocol.TRAFFIC_UPDATE, update,
                                    thread=update.update_id))


class EnvironmentAgent(BaseAgent):
    role = "environment"

    def __init__(self, jid: str, password: str, network: TrafficNetwork,
                 subscribers: Optional[Iterable[str]] = None,
                 t_traffic: float = T_TRAFFIC,
                 full_every: int = FULL_SNAPSHOT_EVERY,
                 traffic_model: Optional[TrafficModel] = None, **kwargs):
        super().__init__(jid, password, **kwargs)
        if t_traffic <= 0:
            raise ValueError("t_traffic must be > 0")
        if full_every < 1:
            raise ValueError("full_every must be >= 1")
        self.network = network
        self.subscribers: List[str] = []
        for s in subscribers or []:
            if s not in self.subscribers:
                self.subscribers.append(s)
        self.t_traffic = t_traffic
        self.full_every = full_every
        self.traffic_model = traffic_model
        self._seq = 0                                   # id of the last update built
        self._since_full = 0                            # updates since the last full snapshot
        self._last_weights: Optional[Dict[str, float]] = None

    # ---- subscribers ---------------------------------------------------------
    def add_subscriber(self, jid: str) -> None:
        """Add a recipient. If updates have already started, it immediately receives a full
        snapshot so it never has to guess the state from deltas."""
        if jid in self.subscribers:
            return
        self.subscribers.append(jid)
        if self._seq > 0 and self.is_alive():
            self.add_behaviour(SendFullSnapshot(jid))

    def remove_subscriber(self, jid: str) -> None:
        if jid in self.subscribers:
            self.subscribers.remove(jid)

    # ---- service: update_traffic() (Agent Model 4.5) -----------------------------
    def apply_traffic_model(self) -> Dict[str, float]:
        """Run the optional traffic model and apply its changes. Returns what changed."""
        if self.traffic_model is None:
            return {}
        now = clock.now()
        current = self.network.snapshot("model-input", now).edge_weights
        changes = self.traffic_model(dict(current), now)
        for key, w in changes.items():
            if key not in current:
                raise KeyError(f"traffic_model returned unknown edge {key!r}")
            if not (isinstance(w, (int, float)) and math.isfinite(w) and w > 0):
                raise ValueError(f"traffic_model returned invalid weight {w!r} for {key!r}")
        if changes:
            self.network.apply_traffic_update(
                TrafficUpdate(f"model-{self._seq + 1}", now,
                              {k: float(v) for k, v in changes.items()}, full_snapshot=False))
        return changes

    def next_traffic_update(self) -> TrafficUpdate:
        """Advance one tick: apply the traffic model, then build the update to broadcast
        (full or delta). Call once per tick."""
        self.apply_traffic_model()
        self._seq += 1
        snap = self.network.snapshot(f"tu-{self._seq}", clock.now())
        weights = dict(snap.edge_weights)
        full = self._last_weights is None or self._since_full + 1 >= self.full_every
        if full:
            update = TrafficUpdate(snap.update_id, snap.timestamp, weights, full_snapshot=True)
            self._since_full = 0
        else:
            changed = {k: w for k, w in weights.items() if self._last_weights.get(k) != w}
            update = TrafficUpdate(snap.update_id, snap.timestamp, changed, full_snapshot=False)
            self._since_full += 1
        self._last_weights = weights
        return update

    def current_full_update(self) -> TrafficUpdate:
        """Full snapshot of the CURRENT state, labelled with the latest update id (state
        only changes inside a tick, so it equals what other subscribers already hold)."""
        snap = self.network.snapshot(f"tu-{self._seq}", clock.now())
        return TrafficUpdate(snap.update_id, snap.timestamp, dict(snap.edge_weights),
                             full_snapshot=True)

    # ---- lifecycle -----------------------------------------------------------------
    async def setup(self):
        await super().setup()                 # answers pings
        self.add_behaviour(TrafficUpdateBehaviour(period=clock.to_real(self.t_traffic)))

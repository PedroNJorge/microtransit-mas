"""
src/common/graph.py
Road network for the Microtransit MAS: a directed, weighted networkx graph.

Typical use
    net = TransitNetwork.from_file("config/porto_graph.json")   # built by tools/build_graph.py
    net.travel_time("hub_trindade", "n01234")                   # seconds, inf if unreachable
    net.hubs_covering("n01234")                                 # hubs whose catchment holds it

Conventions (all times in SIM SECONDS, positions in projected metres)
- DIRECTED: one-way streets are real. The wire id of an edge is "a|b" (ordered); "b|a" is a
  different edge. `both_directions(a, b)` gives both ids for a two-way closure.
- Homes are ordinary graph nodes (type "residential"); hubs are nodes of type "hub".
- MicrohubNode.coverage_radius is in TRAVEL SECONDS from the hub to the node (not metres).
- `base_time` of an edge is changed only by TrafficUpdate (apply_traffic_update). Disruptions
  are an OVERLAY: effective_time = base_time * severity, or the edge is hidden when closed,
  while start_time <= t < end_time.
- A path query evaluates disruptions at ONE time t (the departure time). `route_through`
  advances t between legs. This is an approximation, not a time-dependent shortest path.

Performance (3.7k nodes / 8.5k edges, Porto; measured): load 0.2 s, ~6 MB per copy, one Dijkstra tree ~7 ms
(+0.5 MB per cached tree). Trees are cached
per (source, active disruptions) in an LRU and the whole cache is dropped whenever weights,
disruptions or structure change. Call `warm_hubs()` after loading to prime the 8 hub trees.
Never precompute all pairs (3.7k^2 = 14M entries).

Determinism: ties between equal-cost paths always resolve to the smallest node id, and
best_stop_order breaks ties by (time, node id), so runs are reproducible.

Each agent can hold its own TransitNetwork and keep it current from TrafficUpdate / Disruption
messages (that is the decentralised design); the Environment owns the authoritative copy.
"""
import json
import logging
import math
from collections import OrderedDict
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple, Union

import networkx as nx

from common.ontology import Disruption, MicrohubNode, StagingPoint, TrafficUpdate

logger = logging.getLogger(__name__)

EDGE_SEP = "|"


def edge_key(a: str, b: str) -> str:
    """Wire id of the DIRECTED edge a -> b."""
    return f"{a}{EDGE_SEP}{b}"


def parse_edge_key(key: str) -> Tuple[str, str]:
    a, sep, b = key.partition(EDGE_SEP)
    if not sep or not a or not b:
        raise ValueError(f"Bad edge key: {key!r}")
    return a, b


def both_directions(a: str, b: str) -> List[str]:
    """Edge ids of a two-way road (for Disruption.affected_edges)."""
    return [edge_key(a, b), edge_key(b, a)]


def _positive_finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


class TransitNetwork:
    def __init__(self, cache_size: int = 512):
        self.g = nx.DiGraph()
        self.hubs: Dict[str, MicrohubNode] = {}
        self.hub_info: Dict[str, Dict[str, str]] = {}      # hub id -> {"name", "role"}
        self.staging_points: Dict[str, StagingPoint] = {}
        self.disruptions: Dict[str, Disruption] = {}
        self.meta: dict = {}
        self.last_update_id: Optional[str] = None
        self.last_update_ts: float = -math.inf
        self._dis_edges: Dict[str, FrozenSet[Tuple[str, str]]] = {}
        self._cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._cache_size = cache_size
        self.cache_hits = 0
        self.cache_misses = 0

    def _touch(self) -> None:
        """Weights, disruptions or structure changed: cached trees are invalid."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def add_node(self, node_id: str, x: float, y: float, type: str = "junction",
                 zone: str = "unknown", **attrs) -> None:
        if node_id in self.g:
            raise ValueError(f"Duplicate node {node_id!r}")
        self.g.add_node(node_id, pos=(float(x), float(y)), type=type, zone=zone, **attrs)
        self._touch()

    def add_road(self, a: str, b: str, base_time: Optional[float] = None,
                 length: Optional[float] = None, bidirectional: bool = True) -> None:
        """Add a road a -> b (and b -> a when bidirectional). base_time defaults to the
        euclidean distance, which is only meaningful if positions and times share units."""
        for n in (a, b):
            if n not in self.g:
                raise KeyError(f"Road references unknown node {n!r}")
        if a == b:
            raise ValueError(f"Self-loop on {a!r}")
        if base_time is None:
            base_time = self.distance(a, b)
        if not _positive_finite(base_time):
            raise ValueError(f"Edge {a}->{b} needs base_time > 0 (got {base_time!r})")
        length = self.distance(a, b) if length is None else float(length)
        pairs = [(a, b), (b, a)] if bidirectional else [(a, b)]
        for u, v in pairs:
            if self.g.has_edge(u, v):
                raise ValueError(f"Duplicate edge {u}->{v}")
        for u, v in pairs:
            self.g.add_edge(u, v, base_time=float(base_time), length=length)
        self._touch()

    def add_hub(self, hub: MicrohubNode, name: str = "", role: str = "") -> None:
        if hub.id not in self.g:
            raise KeyError(f"Hub node {hub.id} not in graph")
        if not _positive_finite(hub.coverage_radius) or hub.capacity <= 0:
            raise ValueError(f"Hub {hub.id}: coverage_radius (s) and capacity must be > 0")
        self.hubs[hub.id] = hub
        self.hub_info[hub.id] = {"name": name, "role": role}
        self.g.nodes[hub.id]["type"] = "hub"

    def add_staging_point(self, sp: StagingPoint) -> None:
        if sp.location not in self.g:
            raise KeyError(f"Staging location {sp.location} not in graph")
        self.staging_points[sp.id] = sp
        self.g.nodes[sp.location].setdefault("staging", []).append(sp.id)

    @classmethod
    def from_dict(cls, data: dict, cache_size: int = 512) -> "TransitNetwork":
        """Build from the config/porto_graph.json structure (nodes, edges, hubs, meta)."""
        net = cls(cache_size=cache_size)
        net.meta = dict(data.get("meta", {}))
        for n in data["nodes"]:
            net.add_node(n["id"], n["x"], n["y"], type=n.get("type", "junction"),
                         zone=n.get("zone", "unknown"), lat=n.get("lat"), lon=n.get("lon"))
        for e in data["edges"]:
            net.add_road(e["from"], e["to"], e["travel_time"], e.get("length"),
                         bidirectional=False)
        for h in data.get("hubs", []):
            net.add_hub(MicrohubNode(id=h["id"], coverage_radius=float(h["coverage_radius"]),
                                     capacity=int(h["capacity"])),
                        name=h.get("name", ""), role=h.get("role", ""))
        return net

    @classmethod
    def from_file(cls, path: Union[str, Path], cache_size: int = 512) -> "TransitNetwork":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f), cache_size=cache_size)

    # ------------------------------------------------------------------
    # Nodes, zones, geometry
    # ------------------------------------------------------------------
    def has_node(self, n: str) -> bool:
        return n in self.g

    def node_type(self, n: str) -> str:
        return self.g.nodes[n]["type"]

    def zone_of(self, n: str) -> str:
        return self.g.nodes[n]["zone"]

    def nodes_of_type(self, type: str) -> List[str]:
        return sorted(n for n, d in self.g.nodes(data=True) if d["type"] == type)

    @property
    def zones(self) -> List[str]:
        return sorted({d["zone"] for _, d in self.g.nodes(data=True)})

    def pos(self, n: str) -> Tuple[float, float]:
        return self.g.nodes[n]["pos"]

    def distance(self, a: str, b: str) -> float:
        (x1, y1), (x2, y2) = self.pos(a), self.pos(b)
        return math.hypot(x2 - x1, y2 - y1)

    # ------------------------------------------------------------------
    # Environment updates (TrafficUpdate / Disruption messages)
    # ------------------------------------------------------------------
    def apply_traffic_update(self, upd: TrafficUpdate) -> bool:
        """Overwrite the listed edges' base weights (works for full snapshots and deltas).
        Returns False and changes nothing if the update is older than the last applied one
        (out-of-order delivery). Raises KeyError/ValueError, applying nothing, if any edge
        is unknown or any weight is not a positive finite number."""
        if upd.timestamp < self.last_update_ts:
            logger.warning("Ignoring stale TrafficUpdate %s (t=%.1f < %.1f)",
                           upd.update_id, upd.timestamp, self.last_update_ts)
            return False
        parsed = []
        for key, w in upd.edge_weights.items():
            a, b = parse_edge_key(key)
            if not self.g.has_edge(a, b):
                raise KeyError(f"Unknown edge {key!r} in update {upd.update_id}")
            if not _positive_finite(w):
                raise ValueError(f"Invalid weight {w!r} for edge {key!r}")
            parsed.append((a, b, float(w)))
        changed = False
        for a, b, w in parsed:
            if self.g[a][b]["base_time"] != w:
                self.g[a][b]["base_time"] = w
                changed = True
        if changed:
            self._touch()
        self.last_update_id, self.last_update_ts = upd.update_id, upd.timestamp
        return True

    def add_disruption(self, d: Disruption) -> None:
        """Register (or replace, same id) a disruption. Edge ids are directional."""
        if not d.end_time > d.start_time:
            raise ValueError("Disruption end_time must be > start_time")
        if not d.closed and not _positive_finite(d.severity):
            raise ValueError("Disruption severity must be a positive finite number")
        edges = set()
        for key in d.affected_edges:
            a, b = parse_edge_key(key)
            if not self.g.has_edge(a, b):
                raise KeyError(f"Disruption {d.disruption_id}: unknown edge {key!r}")
            edges.add((a, b))
        self.disruptions[d.disruption_id] = d
        self._dis_edges[d.disruption_id] = frozenset(edges)
        self._touch()

    def remove_disruption(self, disruption_id: str) -> bool:
        if disruption_id not in self.disruptions:
            return False
        del self.disruptions[disruption_id]
        del self._dis_edges[disruption_id]
        self._touch()
        return True

    def active_disruptions(self, t: float) -> List[Disruption]:
        return sorted((d for d in self.disruptions.values() if d.start_time <= t < d.end_time),
                      key=lambda d: d.disruption_id)

    def expire_disruptions(self, t: float) -> List[str]:
        gone = sorted(i for i, d in self.disruptions.items() if d.end_time <= t)
        for i in gone:
            self.remove_disruption(i)
        return gone

    def edge_weights(self) -> Dict[str, float]:
        """Base weight of EVERY edge keyed 'a|b' (no disruptions applied)."""
        return {edge_key(a, b): d["base_time"] for a, b, d in self.g.edges(data=True)}

    def snapshot(self, update_id: str, timestamp: float) -> TrafficUpdate:
        """Full TrafficUpdate of the current base weights (what the Environment broadcasts)."""
        return TrafficUpdate(update_id=update_id, timestamp=timestamp,
                             edge_weights=self.edge_weights(), full_snapshot=True)

    # ------------------------------------------------------------------
    # Travel times (disruption-aware, cached)
    # ------------------------------------------------------------------
    def effective_time(self, a: str, b: str, t: float) -> Optional[float]:
        """Travel time of one edge at time t, or None if it is closed or does not exist."""
        if not self.g.has_edge(a, b):
            return None
        w = self.g[a][b]["base_time"]
        for d in self.active_disruptions(t):
            if (a, b) in self._dis_edges[d.disruption_id]:
                if d.closed:
                    return None
                w *= d.severity
        return w

    def _tree(self, src: str, t: float):
        """(pred, dist) of the shortest-path tree from src at time t. Cached."""
        if src not in self.g:
            raise KeyError(f"Unknown node {src!r}")
        active = self.active_disruptions(t)
        key = (src, tuple(d.disruption_id for d in active))
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            return hit
        self.cache_misses += 1
        mods: Dict[Tuple[str, str], Optional[float]] = {}
        for d in active:
            for uv in self._dis_edges[d.disruption_id]:
                if d.closed:
                    mods[uv] = None
                elif mods.get(uv, 1.0) is not None:
                    mods[uv] = mods.get(uv, 1.0) * d.severity
        if mods:
            def weight(u, v, data):
                m = mods.get((u, v), 1.0)
                return None if m is None else data["base_time"] * m
        else:
            weight = "base_time"
        pred, dist = nx.dijkstra_predecessor_and_distance(self.g, src, weight=weight)
        self._cache[key] = (pred, dist)
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return pred, dist

    def warm_hubs(self, t: float = 0.0) -> None:
        """Prime the cache with the shortest-path tree of every hub."""
        for hid in sorted(self.hubs):
            self._tree(hid, t)

    def cache_info(self) -> Dict[str, int]:
        return {"size": len(self._cache), "hits": self.cache_hits, "misses": self.cache_misses}

    def travel_time(self, src: str, dst: str, t: float = 0.0) -> float:
        """Shortest travel time in sim seconds; 0 if src == dst; inf if unreachable at t.
        Raises KeyError for unknown nodes (a typo must not look like 'unreachable')."""
        if dst not in self.g:
            raise KeyError(f"Unknown node {dst!r}")
        _, dist = self._tree(src, t)
        return dist.get(dst, math.inf)

    def shortest_path(self, src: str, dst: str, t: float = 0.0
                      ) -> Optional[Tuple[List[str], float]]:
        """(path, travel_time), or None if unreachable at t. Ties resolve to the smallest id."""
        if dst not in self.g:
            raise KeyError(f"Unknown node {dst!r}")
        pred, dist = self._tree(src, t)
        if dst not in dist:
            return None
        path, node = [dst], dst
        while node != src:
            node = min(pred[node])
            path.append(node)
        return path[::-1], dist[dst]

    def hubs_covering(self, node: str, t: float = 0.0) -> List[str]:
        """Hubs whose coverage radius (travel seconds, hub -> node) contains `node`,
        nearest first (ties by id)."""
        hits = []
        for hid, hub in self.hubs.items():
            tt = self.travel_time(hid, node, t)
            if tt <= hub.coverage_radius:
                hits.append((tt, hid))
        return [h for _, h in sorted(hits)]

    # ------------------------------------------------------------------
    # Pooling support
    # ------------------------------------------------------------------
    def route_through(self, stops: Iterable[str], t: float = 0.0
                      ) -> Optional[Tuple[List[str], float]]:
        """Chain shortest paths through ordered stops; time advances along the way."""
        stops = list(stops)
        if not stops:
            raise ValueError("route_through needs at least one stop")
        full, clock, total = [stops[0]], t, 0.0
        for a, b in zip(stops, stops[1:]):
            res = self.shortest_path(a, b, clock)
            if res is None:
                return None
            path, dt = res
            full.extend(path[1:])
            clock += dt
            total += dt
        return full, total

    def detour_seconds(self, origin: str, dest: str, stops: List[str],
                       t: float = 0.0) -> Optional[float]:
        """Extra ride time in SECONDS for a rider on the shared route `stops` versus going
        direct (compare with TripRequest.detour_tolerance, also seconds). None if either is
        unreachable. Detours are absolute seconds, never ratios: no division, no 0/0."""
        if origin not in stops or dest not in stops:
            raise ValueError("origin and dest must both be in stops")
        i, j = stops.index(origin), stops.index(dest)
        if j < i:
            raise ValueError("dest must come after origin in stops")
        direct = self.travel_time(origin, dest, t)
        shared = self.route_through(stops[i:j + 1], t)
        if shared is None or math.isinf(direct):
            return None
        return shared[1] - direct

    def detour_within(self, origin: str, dest: str, stops: List[str], tolerance: float,
                      t: float = 0.0) -> bool:
        """True if the rider's extra ride time on the shared route is <= tolerance (seconds,
        i.e. TripRequest.detour_tolerance). False if either leg is unreachable at t."""
        extra = self.detour_seconds(origin, dest, stops, t)
        return extra is not None and extra <= tolerance

    def best_stop_order(self, pickup: str, dropoffs: List[str], t: float = 0.0
                        ) -> Optional[Tuple[List[str], float]]:
        """Greedy nearest-neighbour drop-off order from the pickup (a starting heuristic;
        swap for something better later). Duplicates collapse; ties break by (time, id).
        Returns (stops, total_time) or None if some drop-off is unreachable."""
        order, remaining, here, clock = [pickup], set(dropoffs), pickup, t
        while remaining:
            tt, nxt = min((self.travel_time(here, n, clock), n) for n in remaining)
            if math.isinf(tt):
                return None
            clock += tt
            order.append(nxt)
            remaining.discard(nxt)
            here = nxt
        return order, clock - t


# ----------------------------------------------------------------------
# Demo / test fixtures
# ----------------------------------------------------------------------
def build_grid_network(n: int = 6, spacing: float = 1.0,
                       seconds_per_unit: float = 60.0) -> TransitNetwork:
    """n x n two-way grid (1 spacing unit = seconds_per_unit seconds) with 4 hubs and a
    staging point per hub. A small fixture for tests and demos, not the Porto network."""
    net = TransitNetwork()
    name = lambda i, j: f"n{i}_{j}"
    for i in range(n):
        for j in range(n):
            net.add_node(name(i, j), i * spacing, j * spacing, type="residential")
    for i in range(n):
        for j in range(n):
            if i + 1 < n:
                net.add_road(name(i, j), name(i + 1, j), spacing * seconds_per_unit)
            if j + 1 < n:
                net.add_road(name(i, j), name(i, j + 1), spacing * seconds_per_unit)
    for hid in (name(1, 1), name(n - 2, 1), name(1, n - 2), name(n - 2, n - 2)):
        net.add_hub(MicrohubNode(id=hid, coverage_radius=2.5 * spacing * seconds_per_unit,
                                 capacity=6))
        net.add_staging_point(StagingPoint(id=f"sp-{hid}", location=hid, capacity=4,
                                           covered_hubs=[hid]))
    return net


if __name__ == "__main__":
    import sys
    import time

    path = sys.argv[1] if len(sys.argv) > 1 else "config/porto_graph.json"
    t0 = time.perf_counter()
    net = TransitNetwork.from_file(path)
    print(f"loaded {net.g.number_of_nodes()} nodes / {net.g.number_of_edges()} edges / "
          f"{len(net.hubs)} hubs in {time.perf_counter() - t0:.2f}s")
    t0 = time.perf_counter()
    net.warm_hubs()
    print(f"warmed {len(net.hubs)} hub trees in {time.perf_counter() - t0:.2f}s")
    ids = sorted(net.hubs)
    for a in ids:
        print(f"  {a:<26}", " ".join(f"{net.travel_time(a, b):5.0f}" for b in ids))
    home = net.nodes_of_type("residential")[0]
    print("sample home:", home, net.zone_of(home), "covered by", net.hubs_covering(home))

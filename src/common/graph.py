"""
Road network for the Microtransit MAS, built on networkx.

Owned by the Environment agent; other agents query it (or receive its
TrafficUpdate / Disruption messages and keep a local copy via the same class).

Design notes
- Undirected weighted graph. Edge id on the wire is "nodeA|nodeB" (sorted),
  matching TrafficUpdate.edge_weights and Disruption.affected_edges.
- `base_time` on each edge is updated by TrafficUpdate (full snapshot).
- Disruptions are an overlay: effective_time = base_time * severity, or the edge
  is hidden if closed. They apply while start_time <= t < end_time.
- Hubs are graph nodes (MicrohubNode.id == node id); StagingPoints live on nodes.
"""
import math
from typing import Dict, Iterable, List, Optional, Tuple
import networkx as nx
from ontology import (Disruption, MicrohubNode, StagingPoint, TrafficUpdate)


def edge_key(a: str, b: str) -> str:
    """Canonical wire id for an undirected edge."""
    x, y = sorted((a, b))
    return f"{x}|{y}"


class TransitNetwork:
    def __init__(self):
        self.g = nx.Graph()
        self.hubs: Dict[str, MicrohubNode] = {}
        self.staging_points: Dict[str, StagingPoint] = {}
        self.disruptions: Dict[str, Disruption] = {}
        self.last_update_id: Optional[str] = None

    # Construction
    def add_node(self, node_id: str, x: float, y: float) -> None:
        self.g.add_node(node_id, pos=(x, y))

    def add_road(self, a: str, b: str, base_time: Optional[float] = None) -> None:
        """base_time defaults to euclidean distance (1 unit = 1 time unit)."""
        if base_time is None:
            base_time = self.distance(a, b)
        self.g.add_edge(a, b, base_time=base_time, length=self.distance(a, b))

    def add_hub(self, hub: MicrohubNode) -> None:
        if hub.id not in self.g:
            raise KeyError(f"Hub node {hub.id} not in graph")
        self.hubs[hub.id] = hub
        self.g.nodes[hub.id]["hub"] = True

    def add_staging_point(self, sp: StagingPoint) -> None:
        if sp.location not in self.g:
            raise KeyError(f"Staging location {sp.location} not in graph")
        self.staging_points[sp.id] = sp
        self.g.nodes[sp.location].setdefault("staging", []).append(sp.id)

    # Geometry helpers
    def pos(self, n: str) -> Tuple[float, float]:
        return self.g.nodes[n]["pos"]

    def distance(self, a: str, b: str) -> float:
        (x1, y1), (x2, y2) = self.pos(a), self.pos(b)
        return math.hypot(x2 - x1, y2 - y1)

    def hubs_covering(self, node: str) -> List[str]:
        """Hubs whose coverage radius contains `node`, nearest first."""
        hits = [(self.distance(h.id, node), h.id) for h in self.hubs.values()
                if self.distance(h.id, node) <= h.coverage_radius]
        return [h for _, h in sorted(hits)]

    # Environment updates (from TrafficUpdate / Disruption messages)
    def apply_traffic_update(self, upd: TrafficUpdate) -> None:
        """Full snapshot: overwrite base_time of every listed edge."""
        for key, t in upd.edge_weights.items():
            a, b = key.split("|")
            if self.g.has_edge(a, b):
                self.g[a][b]["base_time"] = t
        self.last_update_id = upd.update_id

    def add_disruption(self, d: Disruption) -> None:
        self.disruptions[d.disruption_id] = d

    def expire_disruptions(self, t: float) -> List[str]:
        gone = [i for i, d in self.disruptions.items() if d.end_time <= t]
        for i in gone:
            del self.disruptions[i]
        return gone

    # Travel time (disruption-aware)
    def effective_time(self, a: str, b: str, t: float) -> Optional[float]:
        """Travel time of edge at time t, or None if closed/absent."""
        if not self.g.has_edge(a, b):
            return None
        w = self.g[a][b]["base_time"]
        key = edge_key(a, b)
        for d in self.disruptions.values():
            if d.start_time <= t < d.end_time and key in d.affected_edges:
                if d.closed:
                    return None
                w *= d.severity
        return w

    def _weight_fn(self, t: float):
        return lambda u, v, _data: self.effective_time(u, v, t)

    def shortest_path(self, src: str, dst: str, t: float = 0.0
                      ) -> Optional[Tuple[List[str], float]]:
        """(path, travel_time) or None if unreachable at time t."""
        try:
            length, path = nx.single_source_dijkstra(
                self.g, src, dst, weight=self._weight_fn(t))
            return path, length
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def travel_time(self, src: str, dst: str, t: float = 0.0) -> float:
        res = self.shortest_path(src, dst, t)
        return math.inf if res is None else res[1]

    # Pooling support
    def route_through(self, stops: Iterable[str], t: float = 0.0
                      ) -> Optional[Tuple[List[str], float]]:
        """Chain shortest paths through ordered stops. Time advances along the way."""
        stops = list(stops)
        full: List[str] = [stops[0]]
        clock = t
        total = 0.0
        for a, b in zip(stops, stops[1:]):
            res = self.shortest_path(a, b, clock)
            if res is None:
                return None
            path, dt = res
            full.extend(path[1:])
            clock += dt
            total += dt
        return full, total

    def detour_ratio(self, origin: str, dest: str, stops: List[str],
                     t: float = 0.0) -> float:
        """Extra ride time for a rider on a shared route vs going direct.
        `stops` is the pool's ordered stop list; ride time is measured from the
        rider's origin to dest within it. Compare with TripRequest.detour_tolerance."""
        direct = self.travel_time(origin, dest, t)
        i, j = stops.index(origin), stops.index(dest)
        res = self.route_through(stops[i:j + 1], t)
        if res is None or direct in (0.0, math.inf):
            return math.inf
        return (res[1] - direct) / direct

    def best_stop_order(self, pickup: str, dropoffs: List[str], t: float = 0.0
                        ) -> Optional[Tuple[List[str], float]]:
        """Greedy nearest-neighbour ordering of drop-offs from the pickup node
        (simple pooling heuristic to start from; swap for something better later)."""
        order, remaining, here, clock = [pickup], set(dropoffs), pickup, t
        while remaining:
            nxt = min(remaining, key=lambda n: self.travel_time(here, n, clock))
            dt = self.travel_time(here, nxt, clock)
            if math.isinf(dt):
                return None
            clock += dt
            order.append(nxt)
            remaining.discard(nxt)
            here = nxt
        return order, clock - t

    # Snapshot for TrafficUpdate broadcast
    def snapshot(self, update_id: str, timestamp: float) -> TrafficUpdate:
        return TrafficUpdate(
            update_id=update_id,
            timestamp=timestamp,
            edge_weights={edge_key(a, b): d["base_time"]
                          for a, b, d in self.g.edges(data=True)},
        )


# Demo scenario
def build_grid_network(n: int = 6, spacing: float = 1.0) -> TransitNetwork:
    """n x n grid city with hubs at the corners-ish and a staging point per hub."""
    net = TransitNetwork()
    name = lambda i, j: f"n{i}_{j}"
    for i in range(n):
        for j in range(n):
            net.add_node(name(i, j), i * spacing, j * spacing)
    for i in range(n):
        for j in range(n):
            if i + 1 < n:
                net.add_road(name(i, j), name(i + 1, j))
            if j + 1 < n:
                net.add_road(name(i, j), name(i, j + 1))

    for hid in (name(1, 1), name(n - 2, 1), name(1, n - 2), name(n - 2, n - 2)):
        net.add_hub(MicrohubNode(id=hid, coverage_radius=2.5 * spacing, capacity=6))
        net.add_staging_point(StagingPoint(id=f"sp-{hid}", location=hid, capacity=4,
                                           covered_hubs=[hid]))
    return net


if __name__ == "__main__":
    from ontology import DisruptionType

    net = build_grid_network()
    print("Hubs:", list(net.hubs))
    print("Hubs covering n0_0:", net.hubs_covering("n0_0"))

    path, t = net.shortest_path("n0_0", "n5_5")
    print(f"Baseline n0_0 -> n5_5: {t:.1f}  ({len(path)} nodes)")

    # Close one edge on the path and congest another
    closed = edge_key(path[2], path[3])
    slow = edge_key(path[4], path[5])
    net.add_disruption(Disruption("d1", DisruptionType.ROAD_CLOSURE, [closed],
                                  start_time=0, end_time=30, closed=True))
    net.add_disruption(Disruption("d2", DisruptionType.ACCIDENT, [slow],
                                  start_time=0, end_time=30, severity=3.0))
    path2, t2 = net.shortest_path("n0_0", "n5_5", t=5)
    print(f"With disruptions:     {t2:.1f}")

    stops, total = net.best_stop_order("n1_1", ["n4_4", "n3_5", "n5_2"], t=5)
    print("Pool stop order:", stops, f"total={total:.1f}")
    print("Detour ratio for rider to n3_5:",
          round(net.detour_ratio("n1_1", "n3_5", stops, t=5), 2))
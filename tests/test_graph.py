"""Tests for common/graph.py (directed TransitNetwork). Unit tests use tiny graphs; the last
section checks the real config/porto_graph.json and is skipped if it is missing."""
import math
import time
from pathlib import Path

import networkx as nx
import pytest

from common.graph import (TransitNetwork, both_directions, build_grid_network, edge_key,
                          parse_edge_key)
from common.ontology import Disruption, DisruptionType, MicrohubNode, TrafficUpdate

PORTO = Path(__file__).resolve().parents[1] / "config" / "porto_graph.json"


def make_net():
    """A->B 100 (two-way), B->C 50 (one-way), C->A 30 (one-way), A->C 400 (one-way), Z island."""
    net = TransitNetwork()
    for i, n in enumerate("ABCZ"):
        net.add_node(n, i * 100.0, 0.0)
    net.add_road("A", "B", 100)                       # two-way
    net.add_road("B", "C", 50, bidirectional=False)
    net.add_road("C", "A", 30, bidirectional=False)
    net.add_road("A", "C", 400, bidirectional=False)
    return net


def closure(did, edges, start=0, end=100):
    return Disruption(did, DisruptionType.ROAD_CLOSURE, edges, start, end, closed=True)


def slowdown(did, edges, severity, start=0, end=100):
    return Disruption(did, DisruptionType.ACCIDENT, edges, start, end, severity=severity)


# ---- edge keys ----------------------------------------------------------------

def test_edge_keys_are_directional():
    assert edge_key("a", "b") != edge_key("b", "a")
    assert parse_edge_key(edge_key("hubA", "n1")) == ("hubA", "n1")
    assert both_directions("a", "b") == ["a|b", "b|a"]
    for bad in ("nobar", "|b", "a|"):
        with pytest.raises(ValueError):
            parse_edge_key(bad)


# ---- construction ----------------------------------------------------------------

def test_construction_validation():
    net = make_net()
    with pytest.raises(ValueError):
        net.add_node("A", 0, 0)                               # duplicate node
    with pytest.raises(KeyError):
        net.add_road("A", "nope", 10)
    with pytest.raises(ValueError):
        net.add_road("A", "A", 10)
    for bad in (0, -5, math.inf, math.nan):
        with pytest.raises(ValueError):
            net.add_road("B", "Z", bad)
    with pytest.raises(ValueError):
        net.add_road("A", "B", 10)                            # duplicate edge
    with pytest.raises(KeyError):
        net.add_hub(MicrohubNode("nope", 100, 5))
    with pytest.raises(ValueError):
        net.add_hub(MicrohubNode("A", 0, 5))


def test_from_dict_roundtrips_and_validates():
    data = {"nodes": [{"id": "A", "x": 0, "y": 0, "type": "hub", "zone": "Z1"},
                      {"id": "B", "x": 10, "y": 0, "type": "residential", "zone": "Z2"}],
            "edges": [{"from": "A", "to": "B", "travel_time": 12.5, "length": 10},
                      {"from": "B", "to": "A", "travel_time": 20.0, "length": 10}],
            "hubs": [{"id": "A", "name": "Alpha", "role": "r", "coverage_radius": 60, "capacity": 4}]}
    net = TransitNetwork.from_dict(data)
    assert net.travel_time("A", "B") == 12.5 and net.travel_time("B", "A") == 20.0
    assert net.zone_of("B") == "Z2" and net.node_type("A") == "hub"
    assert net.hubs["A"].coverage_radius == 60 and net.hub_info["A"]["name"] == "Alpha"
    bad = {**data, "edges": [{"from": "A", "to": "X", "travel_time": 1}]}
    with pytest.raises(KeyError):
        TransitNetwork.from_dict(bad)
    bad = {**data, "edges": [{"from": "A", "to": "B", "travel_time": -1}]}
    with pytest.raises(ValueError):
        TransitNetwork.from_dict(bad)


# ---- shortest paths -------------------------------------------------------------------

def test_directed_travel_times_and_paths():
    net = make_net()
    assert net.travel_time("A", "C") == 150                   # via B, not the 400 edge
    assert net.shortest_path("A", "C") == (["A", "B", "C"], 150)
    assert net.travel_time("C", "B") == 130                   # C->A->B, no direct edge back
    assert net.travel_time("A", "A") == 0
    assert net.shortest_path("A", "A") == (["A"], 0)


def test_unreachable_is_inf_and_none_but_unknown_node_raises():
    net = make_net()
    assert net.travel_time("A", "Z") == math.inf
    assert net.shortest_path("A", "Z") is None
    for args in (("A", "nope"), ("nope", "A")):
        with pytest.raises(KeyError):
            net.travel_time(*args)
        with pytest.raises(KeyError):
            net.shortest_path(*args)


def test_equal_cost_ties_resolve_to_smallest_id_regardless_of_insertion_order():
    def build(order):
        net = TransitNetwork()
        for n in "SXYD":
            net.add_node(n, 0, 0)
        for a, b in order:
            net.add_road(a, b, 10, bidirectional=False)
        return net
    e = [("S", "X"), ("S", "Y"), ("X", "D"), ("Y", "D")]
    for order in (e, e[::-1], [e[1], e[3], e[0], e[2]]):
        assert build(order).shortest_path("S", "D")[0] == ["S", "X", "D"]


# ---- cache ---------------------------------------------------------------------------------

def test_cache_hits_and_invalidation():
    net = make_net()
    net.travel_time("A", "C"); net.travel_time("A", "B"); net.travel_time("A", "C")
    assert net.cache_info() == {"size": 1, "hits": 2, "misses": 1}
    assert net.apply_traffic_update(TrafficUpdate("u1", 1.0, {"A|B": 100.0}, False))
    assert net.cache_info()["size"] == 1                      # no-op update keeps the cache
    assert net.apply_traffic_update(TrafficUpdate("u2", 2.0, {"A|B": 500.0}, False))
    assert net.cache_info()["size"] == 0                      # real change drops it
    assert net.travel_time("A", "C") == 400                   # recomputed with the new weight


def test_cache_is_bounded_lru():
    net = TransitNetwork(cache_size=2)
    for n in "ABC":
        net.add_node(n, 0, 0)
    net.add_road("A", "B", 1); net.add_road("B", "C", 1)
    for src in "ABC":
        net.travel_time(src, "C")
    assert net.cache_info()["size"] == 2


# ---- traffic updates -----------------------------------------------------------------------------

def test_snapshot_is_full_directional_and_applies_to_another_copy():
    net, other = make_net(), make_net()
    other.apply_traffic_update(TrafficUpdate("x", 0.0, {"A|B": 999.0, "B|A": 999.0}, False))
    snap = net.snapshot("tu-1", 5.0)
    assert snap.full_snapshot and snap.update_id == "tu-1" and snap.timestamp == 5.0
    assert snap.edge_weights == {"A|B": 100.0, "B|A": 100.0, "B|C": 50.0, "C|A": 30.0, "A|C": 400.0}
    assert other.apply_traffic_update(snap)
    assert other.edge_weights() == net.edge_weights()


def test_delta_overwrites_only_listed_edges():
    net = make_net()
    net.apply_traffic_update(TrafficUpdate("u", 1.0, {"B|C": 70.0}, False))
    assert net.edge_weights()["B|C"] == 70.0 and net.edge_weights()["A|B"] == 100.0
    assert net.last_update_id == "u"


def test_stale_update_is_ignored():
    net = make_net()
    assert net.apply_traffic_update(TrafficUpdate("new", 10.0, {"B|C": 70.0}, False))
    assert not net.apply_traffic_update(TrafficUpdate("old", 5.0, {"B|C": 1.0}, False))
    assert net.edge_weights()["B|C"] == 70.0 and net.last_update_id == "new"
    assert net.apply_traffic_update(TrafficUpdate("same-ts", 10.0, {"B|C": 71.0}, False))


@pytest.mark.parametrize("bad", [0, -1.0, math.inf, math.nan, True, "5"])
def test_invalid_weights_rejected_atomically(bad):
    net = make_net()
    before = net.edge_weights()
    with pytest.raises(ValueError):
        net.apply_traffic_update(TrafficUpdate("u", 1.0, {"B|C": 60.0, "A|B": bad}, False))
    assert net.edge_weights() == before                        # B|C was NOT applied


def test_unknown_or_reversed_edge_rejected_atomically():
    net = make_net()
    before = net.edge_weights()
    for key in ("C|B", "A|Q", "garbage"):                      # C|B does not exist (one-way)
        with pytest.raises((KeyError, ValueError)):
            net.apply_traffic_update(TrafficUpdate("u", 1.0, {"B|C": 60.0, key: 5.0}, False))
    assert net.edge_weights() == before


# ---- disruptions -----------------------------------------------------------------------------------

def test_closure_reroutes_and_can_make_unreachable():
    net = make_net()
    net.add_disruption(closure("d1", ["B|C"]))
    assert net.travel_time("A", "C", t=5) == 400               # forced onto the long edge
    net.add_disruption(closure("d2", ["A|C"]))
    assert net.travel_time("A", "C", t=5) == math.inf
    assert net.shortest_path("A", "C", t=5) is None


def test_slowdown_multiplies_and_stacks():
    net = make_net()
    net.add_disruption(slowdown("d1", ["B|C"], 3.0))
    assert net.effective_time("B", "C", 5) == 150
    net.add_disruption(slowdown("d2", ["B|C"], 2.0))
    assert net.effective_time("B", "C", 5) == 300
    assert net.travel_time("A", "C", t=5) == 400               # 100+300 vs direct 400


def test_disruption_window_is_half_open_and_cache_aware():
    net = make_net()
    net.add_disruption(closure("d1", ["B|C"], start=10, end=20))
    assert net.travel_time("A", "C", t=5) == 150               # before
    assert net.travel_time("A", "C", t=10) == 400              # starts (inclusive)
    assert net.travel_time("A", "C", t=19.9) == 400
    assert net.travel_time("A", "C", t=20) == 150              # ends (exclusive)
    assert net.travel_time("A", "C", t=15) == 400              # cached tree for window reused


def test_disruption_is_directional_and_two_way_helper_works():
    net = make_net()
    net.add_disruption(closure("d1", ["A|B"]))                 # only A->B
    assert net.effective_time("A", "B", 5) is None and net.effective_time("B", "A", 5) == 100
    net.add_disruption(closure("d2", both_directions("A", "B")))
    assert net.effective_time("B", "A", 5) is None


def test_disruption_validation_expire_remove():
    net = make_net()
    with pytest.raises(KeyError):
        net.add_disruption(closure("bad", ["C|B"]))
    with pytest.raises(ValueError):
        net.add_disruption(closure("bad", ["A|B"], start=10, end=10))
    with pytest.raises(ValueError):
        net.add_disruption(slowdown("bad", ["A|B"], 0))
    net.add_disruption(closure("d1", ["B|C"], end=20))
    net.add_disruption(closure("d2", ["A|B"], end=50))
    assert [d.disruption_id for d in net.active_disruptions(10)] == ["d1", "d2"]
    assert net.expire_disruptions(25) == ["d1"]
    assert net.travel_time("A", "C", t=30) == 400              # d2 still closes A->B? A->C direct
    assert net.remove_disruption("d2") and not net.remove_disruption("d2")
    assert net.travel_time("A", "C", t=30) == 150


# ---- pooling helpers ------------------------------------------------------------------------------------

def grid():
    return build_grid_network(6, 1.0, seconds_per_unit=60)


def test_route_through_chains_legs():
    net = grid()
    path, total = net.route_through(["n1_1", "n1_3", "n3_3"])
    assert total == 240 and path[0] == "n1_1" and path[-1] == "n3_3"
    assert net.route_through(["n1_1"]) == (["n1_1"], 0.0)
    with pytest.raises(ValueError):
        net.route_through([])


def test_detour_seconds_and_tolerance_check():
    net = grid()
    stops = ["n1_1", "n1_4", "n4_4", "n4_1"]
    assert net.detour_seconds("n1_1", "n1_4", stops) == 0
    # shared: 3 + 3 + 3 blocks = 540 s; direct n1_1 -> n4_1: 3 blocks = 180 s
    assert net.detour_seconds("n1_1", "n4_1", stops) == 360
    assert net.detour_within("n1_1", "n4_1", stops, tolerance=360)        # boundary is inclusive
    assert not net.detour_within("n1_1", "n4_1", stops, tolerance=359.9)
    assert net.detour_within("n1_1", "n1_4", stops, tolerance=0)
    with pytest.raises(ValueError):
        net.detour_seconds("n4_1", "n1_1", stops)                # wrong order
    with pytest.raises(ValueError):
        net.detour_seconds("n1_1", "n0_0", stops)                # not in stops
    assert not hasattr(net, "detour_ratio")                      # seconds only, by design


def test_zero_direct_time_is_not_a_problem():
    net = grid()
    stops = ["n1_1", "n1_1", "n1_3"]                             # origin == dest: direct = 0
    assert net.detour_seconds("n1_1", "n1_1", stops) == 0
    assert net.detour_within("n1_1", "n1_1", stops, tolerance=0)


def test_detour_unreachable_is_none_and_not_within():
    net = make_net()
    net.add_node("Y", 0, 0)
    net.add_road("C", "Y", 10, bidirectional=False)
    assert net.detour_seconds("A", "Y", ["A", "C", "Y"]) is not None
    net.add_disruption(closure("d", ["C|Y"]))
    assert net.detour_seconds("A", "Y", ["A", "C", "Y"], t=5) is None
    assert not net.detour_within("A", "Y", ["A", "C", "Y"], tolerance=1e9, t=5)


def test_best_stop_order_is_deterministic_and_dedupes():
    net = grid()
    first = net.best_stop_order("n1_1", ["n4_4", "n3_1", "n1_3", "n3_1"])
    assert first == net.best_stop_order("n1_1", ["n1_3", "n3_1", "n4_4"])      # order/dup-insensitive
    assert first[0][0] == "n1_1" and sorted(first[0][1:]) == ["n1_3", "n3_1", "n4_4"]
    assert first[0][1] == "n1_3"                                               # tie with n3_1 -> smaller id


def test_best_stop_order_unreachable_returns_none():
    net = make_net()
    assert net.best_stop_order("A", ["B", "Z"]) is None


# ---- hubs / coverage ----------------------------------------------------------------------------------------

def test_hubs_covering_uses_travel_time_not_distance():
    net = TransitNetwork()
    for n, x in (("H1", 0), ("H2", 1000), ("P", 500)):
        net.add_node(n, x, 0)
    net.add_road("H1", "P", 100, bidirectional=False)         # 500 m in 100 s
    net.add_road("H2", "P", 130, bidirectional=False)         # 500 m in 130 s
    net.add_hub(MicrohubNode("H1", 120, 5))
    net.add_hub(MicrohubNode("H2", 120, 5))
    assert net.hubs_covering("P") == ["H1"]                    # same distance, only H1 in time
    net.apply_traffic_update(TrafficUpdate("u", 1.0, {"H2|P": 110.0}, False))
    assert net.hubs_covering("P") == ["H1", "H2"]              # nearest first
    net.add_disruption(slowdown("d", ["H1|P"], 2.0))
    assert net.hubs_covering("P", t=5) == ["H2"]


def test_grid_fixture_and_node_queries():
    net = grid()
    assert len(net.hubs) == 4 and len(net.staging_points) == 4
    assert net.nodes_of_type("hub") == sorted(net.hubs)
    assert net.hubs_covering("n0_0")[0] == "n1_1"
    assert "unknown" in net.zones


# ---- works as the EnvironmentAgent's network (contract check) -----------------------------------------------------

def test_works_as_environment_network():
    from agents.environment import EnvironmentAgent
    from common import clock
    clock.reset(); clock.start(speed=10)
    net = make_net()
    calls = []

    def model(weights, t):
        calls.append(1)
        return {"B|C": weights["B|C"] * 2} if len(calls) == 2 else {}

    env = EnvironmentAgent("env@localhost", "pw", net, traffic_model=model, full_every=50)
    u1, u2, u3 = (env.next_traffic_update() for _ in range(3))
    assert u1.full_snapshot and set(u1.edge_weights) == {"A|B", "B|A", "B|C", "C|A", "A|C"}
    assert u2.edge_weights == {"B|C": 100.0} and not u2.full_snapshot
    assert u3.edge_weights == {}
    assert net.travel_time("A", "C") == 200                    # model change reached the graph


# ---- the real Porto network ------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def net():
    return TransitNetwork.from_file(PORTO)


@pytest.mark.skipif(not PORTO.exists(), reason="config/porto_graph.json not built yet")
class TestPorto:
    def test_loads_with_expected_shape(self, net):
        c = net.meta["counts"]
        assert net.g.number_of_nodes() == c["nodes"] and net.g.number_of_edges() == c["edges"]
        assert len(net.hubs) == 8 and len(net.nodes_of_type("hub")) == 8
        assert len(net.nodes_of_type("residential")) == c["residential"]
        assert all(net.hubs[h].coverage_radius > 0 for h in net.hubs)
        assert net.hub_info["hub_trindade"]["name"] == "Trindade"

    def test_loads_quickly(self):
        t0 = time.perf_counter()
        TransitNetwork.from_file(PORTO)
        assert time.perf_counter() - t0 < 5.0

    def test_hub_times_match_an_independent_dijkstra(self, net):
        for h in ("hub_trindade", "hub_senhora_da_hora"):
            ref = nx.single_source_dijkstra_path_length(net.g, h, weight="base_time")
            for n in net.nodes_of_type("residential")[::97]:
                assert net.travel_time(h, n) == pytest.approx(ref[n])

    def test_one_way_streets_make_times_asymmetric(self, net):
        pairs = [(a, b) for a in net.hubs for b in net.hubs if a < b]
        assert any(abs(net.travel_time(a, b) - net.travel_time(b, a)) > 1 for a, b in pairs)

    def test_path_is_valid_and_matches_time(self, net):
        home = net.nodes_of_type("residential")[500]
        path, total = net.shortest_path("hub_campanha", home)
        assert path[0] == "hub_campanha" and path[-1] == home
        assert sum(net.g[a][b]["base_time"] for a, b in zip(path, path[1:])) == pytest.approx(total)

    def test_warm_hubs_fills_cache_and_is_fast(self, net):
        net._touch()
        t0 = time.perf_counter()
        net.warm_hubs()
        assert time.perf_counter() - t0 < 3.0
        assert net.cache_info()["size"] == 8

    def test_every_residential_node_is_reachable_from_a_hub(self, net):
        for h in net.hubs:
            assert all(math.isfinite(net.travel_time(h, n))
                       for n in net.nodes_of_type("residential")[::50])

    def test_own_snapshot_is_a_noop_and_has_every_edge(self, net):
        snap = net.snapshot("s", 1.0)
        assert len(snap.edge_weights) == net.g.number_of_edges() and snap.full_snapshot
        net.warm_hubs()
        assert net.apply_traffic_update(snap)
        assert net.cache_info()["size"] == 8                   # unchanged weights keep the cache

    def test_closing_an_edge_on_the_route_changes_it(self, net):
        net.expire_disruptions(1e9)
        home = net.nodes_of_type("residential")[800]
        path, base = net.shortest_path("hub_trindade", home)
        mid = len(path) // 2
        closed = (path[mid], path[mid + 1])
        net.add_disruption(closure("test-closure", [edge_key(*closed)], start=0, end=100))
        try:
            res = net.shortest_path("hub_trindade", home, t=10)
            assert res is None or closed not in list(zip(res[0], res[0][1:]))
            assert net.travel_time("hub_trindade", home, t=10) >= base
            assert net.travel_time("hub_trindade", home, t=200) == pytest.approx(base)
        finally:
            net.remove_disruption("test-closure")

    def test_hubs_covering_a_central_home_includes_a_central_hub(self, net):
        trindade_zone_homes = [n for n in net.nodes_of_type("residential")
                               if net.travel_time("hub_trindade", n) < 120]
        assert trindade_zone_homes
        assert "hub_trindade" in net.hubs_covering(trindade_zone_homes[0])

"""
tests/test_scenarios.py   Dynamic scenario sections (demand / events / forecasts / reposition) and the six shipped scenarios.

    pytest tests/test_scenarios.py -v
    pytest tests/test_scenarios.py -v -k "not xmpp"
"""
import asyncio
import copy
import glob
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from common import clock                                           # noqa: E402
from common.graph import TransitNetwork                            # noqa: E402
from harness.launcher import Harness                               # noqa: E402
from harness.runner import covered_nodes, event_disruptions, plan_demand   # noqa: E402
from harness.scenario import Scenario, ScenarioError               # noqa: E402

GRAPH = "config/porto_graph.json"
CENTRE = ["hub_aliados", "hub_trindade"]           # the two hubs nearest the centroid of the Porto graph



_NET = TransitNetwork.from_file(str(ROOT / GRAPH))
NODE_T = next(n for n in sorted(_NET.g.successors("hub_trindade")) if _NET.g.has_edge(n, "hub_trindade"))   # a two-way neighbour
NODE_C = next(n for n in sorted(_NET.g.successors("hub_campanha")) if _NET.g.has_edge(n, "hub_campanha"))
EDGE_T, EDGE_T_REV, EDGE_C = f"hub_trindade|{NODE_T}", f"{NODE_T}|hub_trindade", f"hub_campanha|{NODE_C}"
T_EDGE = _NET.effective_time("hub_trindade", NODE_T, 0)             # its normal travel time (s)


def base(**extra):
    d = {"name": "t", "graph": GRAPH, "hubs": "all", "drivers": {"count": 2}, "riders": {"count": 2}, "seed": 7}
    d.update(extra)
    return d


@pytest.fixture(scope="module")
def net():
    return TransitNetwork.from_file(str(ROOT / GRAPH))


@pytest.fixture(autouse=True)
def _clock():
    clock.reset(100.0)
    yield
    clock.reset()


# ---- schema ----------------------------------------------------------------------------------------
def test_shipped_scenarios_load_and_have_expected_shape():
    files = sorted(glob.glob(str(ROOT / "config" / "scenarios" / "0*.json")))
    assert len(files) == 6
    for f in files:
        d = json.load(open(f))
        d["graph"] = GRAPH
        sc = Scenario.from_dict(d)
        assert sc.duration and (sc.demand or sc.events)


@pytest.mark.parametrize("sel,expected", [
    ("all", 8), ("hub_trindade", 1), (["hub_trindade", "hub_campanha"], 2), ({"centre": 2}, 2), ({"periphery": 3}, 3),
    ({"role": "rail interface (east)"}, 1), ({"index": [0, 7]}, 2)])
def test_hub_selectors(sel, expected):
    sc = Scenario.from_dict(base(demand=[{"hubs": sel, "start": 0, "end": 10, "count": 1}]))
    assert len(sc.demand[0].hubs) == expected


def test_centre_selector_picks_the_central_hubs():
    sc = Scenario.from_dict(base(demand=[{"hubs": {"centre": 2}, "start": 0, "end": 10, "count": 1}]))
    assert sorted(sc.demand[0].hubs) == sorted(CENTRE)


def test_periphery_is_disjoint_from_centre():
    sc = Scenario.from_dict(base(demand=[{"hubs": {"periphery": 6}, "start": 0, "end": 10, "count": 1}]))
    assert not set(sc.demand[0].hubs) & set(CENTRE)


def test_placement_expands_and_sets_driver_count():
    sc = Scenario.from_dict(base(drivers={"placement": [{"hubs": {"centre": 2}, "count": 5}]}))
    assert sc.n_drivers == 5 and set(sc.driver_start_hubs) == set(CENTRE)
    starts = [s.kwargs["start_hub"] for s in sc.roster() if s.role == "driver"]
    assert set(starts) == set(CENTRE)


@pytest.mark.parametrize("patch,msg", [
    ({"demand": [{"hubs": "nope", "start": 0, "end": 10, "count": 1}]}, "not active hubs"),
    ({"demand": [{"hubs": "all", "start": 10, "end": 10, "count": 1}]}, "start < end"),
    ({"demand": [{"hubs": "all", "start": 0, "end": 10, "count": 1, "pooling": 1.5}]}, "[0, 1]"),
    ({"demand": [{"hubs": "all", "start": 0, "end": 10, "count": 1, "bogus": 1}]}, "unknown keys"),
    ({"demand": [{"hubs": "all", "start": 0, "end": 10}]}, "missing"),
    ({"events": [{"at": 1, "type": "earthquake"}]}, "type must be"),
    ({"events": [{"at": 1, "type": "kill_hub", "hub": "nope"}]}, "not active hubs"),
    ({"events": [{"at": 1, "type": "traffic", "duration": 10, "where": "all"}]}, "exactly one"),
    ({"events": [{"at": 1, "type": "traffic", "duration": 10, "where": "all", "severity": 0.5}]}, ">= 1"),
    ({"events": [{"at": 1, "type": "traffic", "duration": 10, "where": "all",
                  "profile": [[0, 2], [20, 3]]}]}, "below 'duration'"),
    ({"events": [{"at": 1, "type": "block_road", "duration": 10, "where": {"edges": ["nobar"]}}]}, "edge ids"),
    ({"events": [{"at": 1, "type": "block_road", "duration": 10, "where": "all"}]}, "where"),
    ({"events": [{"at": 1, "type": "kill_hub", "hub": "hub_trindade", "duration": 3}]}, "unknown keys"),
    ({"forecasts": [{"at": 0, "hub": "all", "expected": 3, "horizon": 10}]}, "exactly one hub"),
    ({"reposition": {"speed": 3}}, "unknown keys"),
    ({"drivers": {"count": 3, "placement": [{"hubs": "all", "count": 2}]}}, "!= sum"),
    ({"drivers": {"start_hubs": ["hub_trindade"], "placement": [{"hubs": "all", "count": 2}]}}, "not both"),
])
def test_invalid_sections_are_rejected(patch, msg):
    with pytest.raises(ScenarioError, match=msg):
        Scenario.from_dict(base(**patch))


def test_duplicate_event_ids_rejected():
    ev = {"id": "a", "at": 1, "type": "kill_hub", "hub": "hub_trindade"}
    with pytest.raises(ScenarioError, match="duplicate"):
        Scenario.from_dict(base(events=[ev, dict(ev, at=2)]))


# ---- demand plan -------------------------------------------------------------------------------------
def demand_sc(**kw):
    spec = {"id": "d", "hubs": ["hub_trindade", "hub_campanha"], "start": 100, "end": 200, "count": 10}
    spec.update(kw)
    return Scenario.from_dict(base(demand=[spec]))


def test_plan_is_deterministic_and_seeded(net):
    a = plan_demand(demand_sc(pattern="random"), net)
    assert a == plan_demand(demand_sc(pattern="random"), net)
    other = Scenario.from_dict(dict(base(demand=[{"id": "d", "hubs": ["hub_trindade"], "start": 0, "end": 9,
                                                  "count": 5, "pattern": "random"}]), seed=8))
    assert plan_demand(other, net) != plan_demand(Scenario.from_dict(dict(base(demand=[
        {"id": "d", "hubs": ["hub_trindade"], "start": 0, "end": 9, "count": 5, "pattern": "random"}]), seed=9)), net)


@pytest.mark.parametrize("pattern", ["even", "random"])
def test_plan_times_hubs_and_destinations(net, pattern):
    plan = plan_demand(demand_sc(pattern=pattern), net)
    assert len(plan) == 10 and [r.at for r in plan] == sorted(r.at for r in plan)
    assert all(100 <= r.at < 200 for r in plan)
    for r in plan:
        assert r.hub in ("hub_trindade", "hub_campanha")
        assert r.destination in covered_nodes(net, r.hub)            # a trip the hub can actually take


def test_even_pattern_alternates_hubs_and_spaces_evenly(net):
    plan = plan_demand(demand_sc(pattern="even"), net)
    assert [r.hub for r in plan[:4]] == ["hub_trindade", "hub_campanha", "hub_trindade", "hub_campanha"]
    gaps = {round(b.at - a.at, 6) for a, b in zip(plan, plan[1:])}
    assert gaps == {10.0}


def test_pooling_probability_extremes(net):
    assert all(r.pooling for r in plan_demand(demand_sc(pooling=1.0), net))
    assert not any(r.pooling for r in plan_demand(demand_sc(pooling=0.0), net))


# ---- events -> disruptions -----------------------------------------------------------------------------
def ev(sc_events):
    return Scenario.from_dict(base(events=sc_events)).events[0]


def test_block_explicit_edges_both_directions(net):
    e = ev([{"id": "b", "at": 50, "type": "block_road", "duration": 100,
             "where": {"edges": [EDGE_T]}}])
    (d,) = event_disruptions(e, net, 7)
    assert d.closed and sorted(d.affected_edges) == [EDGE_T, EDGE_T_REV]
    assert (d.start_time, d.end_time) == (50, 150)


def test_block_one_way_only(net):
    e = ev([{"id": "b", "at": 0, "type": "block_road", "duration": 10, "both_directions": False,
             "where": {"edges": [EDGE_T]}}])
    assert event_disruptions(e, net, 7)[0].affected_edges == [EDGE_T]


def test_random_block_lies_on_a_hub_to_hub_route(net):
    e = ev([{"id": "r", "at": 0, "type": "block_road", "duration": 10,
             "where": {"random_on_routes": 2, "among": ["hub_trindade", "hub_campanha"]}, "both_directions": False}])
    (d,) = event_disruptions(e, net, 7)
    path = net.shortest_path("hub_trindade", "hub_campanha")[0] + net.shortest_path("hub_campanha", "hub_trindade")[0]
    on_route = {f"{a}|{b}" for a, b in zip(path, path[1:])}
    assert len(d.affected_edges) == 2 and set(d.affected_edges) <= on_route
    assert event_disruptions(e, net, 7)[0].affected_edges == d.affected_edges       # same seed, same roads


def test_applied_block_makes_the_edge_unusable_then_it_reopens(net):
    local = TransitNetwork.from_file(str(ROOT / GRAPH))
    e = ev([{"id": "b", "at": 100, "type": "block_road", "duration": 100, "where": {"edges": [EDGE_T]}}])
    for d in event_disruptions(e, local, 1):
        local.add_disruption(d)
    assert local.effective_time("hub_trindade", NODE_T, 150) is None
    assert local.effective_time("hub_trindade", NODE_T, 50) == T_EDGE == local.effective_time("hub_trindade", NODE_T, 200)


def test_traffic_profile_becomes_consecutive_disruptions(net):
    e = ev([{"id": "t", "at": 1000, "type": "traffic", "duration": 600, "where": "all",
             "profile": [[0, 1.5], [200, 3.0], [400, 1.5]]}])
    ds = event_disruptions(e, net, 1)
    assert [(d.start_time, d.end_time, d.severity) for d in ds] == [
        (1000, 1200, 1.5), (1200, 1400, 3.0), (1400, 1600, 1.5)]
    assert all(not d.closed and len(d.affected_edges) == net.g.number_of_edges() for d in ds)
    local = TransitNetwork.from_file(str(ROOT / GRAPH))
    for d in ds:
        local.add_disruption(d)
    assert local.effective_time("hub_trindade", NODE_T, 1300) == pytest.approx(3.0 * T_EDGE)
    assert local.effective_time("hub_trindade", NODE_T, 1700) == pytest.approx(T_EDGE)


def test_traffic_near_a_hub_is_local(net):
    e = ev([{"id": "t", "at": 0, "type": "traffic", "duration": 10, "severity": 2,
             "where": {"near": "hub_trindade", "radius": 200}}])
    (d,) = event_disruptions(e, net, 1)
    assert 0 < len(d.affected_edges) < net.g.number_of_edges() // 4
    assert EDGE_T in d.affected_edges and EDGE_C not in d.affected_edges


# ---- end to end (real agents) ----------------------------------------------------------------------
def free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def test_xmpp_events_requests_forecast_and_hub_kill():
    """One short run: a request stream, a road closure, a forecast, a hub kill; checks they all happened."""
    d = base(clock={"speed": 100}, duration=1500, drivers={"count": 3, "start_hubs": ["hub_trindade"]},
             riders={"count": 3}, xmpp={"port": free_port(), "server_port": free_port()},
             demand=[{"id": "s", "hubs": ["hub_trindade"], "start": 100, "end": 400, "count": 3}],
             events=[{"id": "blk", "at": 200, "type": "block_road", "duration": 300,
                      "where": {"edges": [EDGE_T]}},
                     {"id": "die", "at": 900, "type": "kill_hub", "hub": "hub_campanha"}],
             forecasts=[{"at": 50, "hub": "hub_trindade", "expected": 5, "horizon": 800, "to": "hub_trindade"}],
             reposition={"t_rebalance": 120})
    d["hubs"] = ["hub_trindade", "hub_campanha"]

    async def go():
        async with Harness(Scenario.from_dict(d)) as h:
            await asyncio.sleep(clock.to_real(1200))
            foz, maia = h.agents["hub_trindade@localhost"], h.agents["hub_campanha@localhost"]
            riders = [a for a in h.agents.values() if a.role == "rider"]
            return (h.runner.summary.issued, h.runner.errors(), [t for _, t in h.runner.summary.events],
                    maia.is_alive(), foz.is_alive(), foz.demand_forecasts.get("hub_trindade"),
                    sum(len(r.trips) for r in riders) + sum(1 for r in riders if r.trip))

    try:
        issued, errors, notes, maia_alive, foz_alive, fc, started = asyncio.run(go())
    except RuntimeError as e:                       # no XMPP server available here
        pytest.skip(str(e))
    assert errors == [] and issued == 3 and started == 3
    assert any("blk" in n for n in notes) and any("hub_campanha killed" in n for n in notes)
    assert not maia_alive and foz_alive
    assert fc is not None and fc.expected_requests == 5
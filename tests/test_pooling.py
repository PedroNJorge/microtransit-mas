"""Milestone 9, pure maths: exact group evaluation, insertion operator, partition DP, fare split."""
import itertools
import math
import random

import pytest

from common.graph import build_grid_network
from common.ontology import TripRequest
from common.pooling import (
    best_partition, evaluate_group, fare_ceiling, greedy_partition, insert_rider, register_solver,
    reference_solo_fare, split_fare_weighted, SOLVERS, common_departure,
)

HUB = "n1_1"


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


def req(i, dest, tol=600.0, start=0.0, end=3600.0, max_fare=20.0):
    return TripRequest(f"r{i}", f"rider{i}", HUB, dest, start, end, tol, max_fare, True, False, 0.0)


# ---- windows ----------------------------------------------------------------------------------

def test_common_departure_overlap_and_disjoint():
    a, b = req(1, "n1_2", start=0, end=100), req(2, "n1_3", start=50, end=200)
    assert common_departure([a, b], now=10) == 50
    assert common_departure([a, b], now=70) == 70
    c = req(3, "n1_3", start=150, end=300)
    assert common_departure([a, c], now=0) is None          # windows do not overlap
    assert common_departure([a, b], now=120) is None        # a's window already closed


# ---- group evaluation ---------------------------------------------------------------------------

def test_on_the_way_destinations_have_zero_detour(net):
    plan = evaluate_group(net, HUB, [req(1, "n1_2"), req(2, "n1_3")], now=0)
    assert plan is not None
    assert plan.stops == (HUB, "n1_2", "n1_3")               # drop the nearer one first
    assert plan.detour == {"r1": 0.0, "r2": 0.0}
    assert plan.route_time == pytest.approx(net.travel_time(HUB, "n1_3"))


def test_detour_tolerance_binds(net):
    # opposite directions: whoever goes second rides far longer than direct
    a, b = req(1, "n1_3", tol=0.0), req(2, "n3_1", tol=0.0)
    assert evaluate_group(net, HUB, [a, b], now=0) is None
    loose = evaluate_group(net, HUB, [req(1, "n1_3", tol=900), req(2, "n3_1", tol=900)], now=0)
    assert loose is not None
    assert all(loose.detour[k] <= 900 for k in loose.detour)
    assert max(loose.detour.values()) > 0


def test_tolerance_is_per_rider_not_pool_wide(net):
    tight = req(1, "n1_3", tol=0.0)
    easy = req(2, "n3_1", tol=900)
    plan = evaluate_group(net, HUB, [tight, easy], now=0)
    assert plan is not None
    assert plan.detour["r1"] == 0.0                          # the tight rider is served first
    assert plan.stops[1] == "n1_3"


def test_windows_make_group_infeasible(net):
    a = req(1, "n1_2", start=0, end=100)
    b = req(2, "n1_3", start=500, end=900)
    assert evaluate_group(net, HUB, [a, b], now=0) is None
    assert evaluate_group(net, HUB, [a, b], now=0, respect_windows=False) is not None


def test_marginal_detour_matches_environment_formula(net):
    plan = evaluate_group(net, HUB, [req(1, "n1_2"), req(2, "n1_3")], now=0)
    t_with = plan.route_time
    t_without = net.travel_time(HUB, "n1_2")                 # route without r2
    assert plan.marginal["r2"] == pytest.approx(t_with - t_without)
    assert plan.marginal["r1"] == pytest.approx(0.0)         # r1 is on the way to r2


def test_group_evaluation_is_optimal_vs_bruteforce(net):
    rng = random.Random(7)
    nodes = [n for n in net.nodes_of_type("home") if net.travel_time(HUB, n) <= 150] or ["n1_2", "n2_1", "n1_3"]
    for _ in range(15):
        k = rng.randint(2, 4)
        dests = [rng.choice(nodes) for _ in range(k)]
        reqs = [req(i, d, tol=1200) for i, d in enumerate(dests)]
        plan = evaluate_group(net, HUB, reqs, now=0)
        best = math.inf
        for perm in itertools.permutations(sorted(set(dests))):
            t, here, ride = 0.0, HUB, {}
            for n in perm:
                t += net.travel_time(here, n)
                ride.setdefault(n, t)
                here = n
            det = sum(max(0.0, ride[d] - net.travel_time(HUB, d)) for d in dests)
            best = min(best, t + det)
        assert plan is not None and plan.cost() == pytest.approx(best)


# ---- insertion operator ---------------------------------------------------------------------------

def test_insert_rider_picks_cheapest_position(net):
    route = ["n1_2", "n1_4"]
    new_route, marginal = insert_rider(net, HUB, route, "n1_3", now=0)
    assert new_route == ["n1_2", "n1_3", "n1_4"]
    assert marginal == pytest.approx(0.0)                    # lies on the way


def test_insert_rider_reports_marginal_detour(net):
    new_route, marginal = insert_rider(net, HUB, ["n1_2"], "n3_1", now=0)
    without = net.travel_time(HUB, "n1_2")
    sim = sum(net.travel_time(a, b) for a, b in zip([HUB, *new_route], new_route))
    assert marginal == pytest.approx(sim - without)


def test_insert_duplicate_destination_is_free(net):
    assert insert_rider(net, HUB, ["n1_2", "n1_3"], "n1_3", now=0) == (["n1_2", "n1_3"], 0.0)


# ---- partition ------------------------------------------------------------------------------------

def test_partition_pools_compatible_riders(net):
    reqs = [req(1, "n1_2"), req(2, "n1_3")]
    part = best_partition(net, HUB, reqs, now=0)
    assert len(part.groups) == 1 and set(part.groups[0].request_ids) == {"r1", "r2"}


def test_partition_splits_incompatible_riders(net):
    reqs = [req(1, "n1_3", tol=0), req(2, "n3_1", tol=0)]
    part = best_partition(net, HUB, reqs, now=0)
    assert len(part.groups) == 2 and not part.unserved


def test_partition_respects_capacity(net):
    reqs = [req(i, "n1_2") for i in range(1, 6)]
    part = best_partition(net, HUB, reqs, now=0, max_size=4)
    assert sorted(len(g.request_ids) for g in part.groups) == [1, 4]


def test_partition_reports_unservable_rider(net):
    gone = req(1, "n1_2", start=0, end=10)
    part = best_partition(net, HUB, [gone, req(2, "n1_3")], now=100)
    assert part.unserved == ("r1",)


def test_partition_covers_every_served_request_once(net):
    rng = random.Random(3)
    nodes = ["n1_2", "n1_3", "n2_1", "n3_1", "n2_2"]
    for _ in range(8):
        reqs = [req(i, rng.choice(nodes), tol=rng.choice([0, 120, 600])) for i in range(rng.randint(2, 6))]
        part = best_partition(net, HUB, reqs, now=0)
        seen = [i for g in part.groups for i in g.request_ids] + list(part.unserved)
        assert sorted(seen) == sorted(r.request_id for r in reqs)
        for g in part.groups:
            assert len(g.request_ids) <= 4
            for r in reqs:
                if r.request_id in g.request_ids:
                    assert g.detour[r.request_id] <= r.detour_tolerance + 1e-9


def test_exact_never_worse_than_greedy(net):
    rng = random.Random(11)
    nodes = ["n1_2", "n1_3", "n2_1", "n3_1", "n2_2", "n1_1"]
    for _ in range(12):
        reqs = [req(i, rng.choice(nodes), tol=rng.choice([0, 150, 600])) for i in range(rng.randint(2, 6))]
        ex = best_partition(net, HUB, reqs, now=0)
        gr = greedy_partition(net, HUB, reqs, now=0)
        assert ex.cost <= gr.cost + 1e-9
        assert len(ex.unserved) <= len(gr.unserved)


def test_partition_too_big_is_rejected(net):
    with pytest.raises(ValueError):
        best_partition(net, HUB, [req(i, "n1_2") for i in range(12)], now=0)


def test_solver_registry(net):
    assert {"exact", "greedy"} <= set(SOLVERS)
    register_solver("noop", lambda *a, **k: best_partition(*a, **k))
    assert SOLVERS["noop"](net, HUB, [req(1, "n1_2")], 0).groups


# ---- fares -----------------------------------------------------------------------------------------

def test_weighted_split_sums_exactly_and_is_proportional():
    out = split_fare_weighted(10.00, {"a": 60.0, "b": 120.0})
    assert out == {"a": 3.33, "b": 6.67}
    assert sum(round(v * 100) for v in out.values()) == 1000


def test_weighted_split_property():
    rng = random.Random(1)
    for _ in range(200):
        n = rng.randint(1, 4)
        w = {f"r{i}": rng.uniform(0, 500) for i in range(n)}
        total = rng.randint(1, 5000) / 100
        out = split_fare_weighted(total, w)
        assert sum(round(v * 100) for v in out.values()) == round(total * 100)
        assert all(v >= 0 for v in out.values())


def test_weighted_split_degenerate_weights_fall_back_to_equal():
    assert split_fare_weighted(1.00, {"a": 0, "b": 0}) == {"a": 0.5, "b": 0.5}


def test_fare_ceiling_never_exceeds_anyones_max():
    rng = random.Random(5)
    for _ in range(200):
        n = rng.randint(1, 4)
        mf = {f"r{i}": rng.randint(300, 2500) / 100 for i in range(n)}
        w = {k: rng.uniform(30, 400) for k in mf}
        ceil = fare_ceiling(mf, w)
        shares = split_fare_weighted(ceil, w)
        assert all(shares[k] <= mf[k] + 1e-9 for k in mf)


def test_reference_solo_fare_is_capped():
    assert reference_solo_fare(100, 2.0, 0.004, 20.0) == pytest.approx(2.40)
    assert reference_solo_fare(100000, 2.0, 0.004, 20.0) == 20.0


# ---- the batch wait counts against a rider's tolerance (sharing only; a rider alone is not batched) -------------

def queued(i, dest, created_at, tol):
    return TripRequest(f"r{i}", f"rider{i}", HUB, dest, 0.0, 3600.0, tol, 20.0, True, False, created_at)


def test_waiting_for_the_batch_uses_up_the_tolerance_of_a_shared_ride(net):
    a, b = queued(1, "n1_2", 100.0, 200.0), queued(2, "n1_3", 100.0, 200.0)      # zero detour on the same road
    assert evaluate_group(net, HUB, [a, b], now=200.0) is not None                 # waited 100 s <= 200 s
    plan = evaluate_group(net, HUB, [a, b], now=200.0)
    assert plan.wait == {"r1": 100.0, "r2": 100.0}
    assert evaluate_group(net, HUB, [a, b], now=400.0) is None                     # waited 300 s > 200 s: may not share


def test_a_rider_alone_is_never_refused_for_having_waited(net):
    a = queued(1, "n1_2", 100.0, 10.0)
    plan = evaluate_group(net, HUB, [a], now=900.0)
    assert plan is not None and plan.wait == {"r1": 0.0}


def test_the_wait_does_not_change_which_partition_is_cheapest(net):
    """Sunk cost: it limits who may share, it is not part of the objective."""
    a, b = queued(1, "n1_2", 100.0, 900.0), queued(2, "n1_3", 100.0, 900.0)
    p1, p2 = best_partition(net, HUB, [a, b], now=150.0), best_partition(net, HUB, [a, b], now=600.0)
    assert [g.request_ids for g in p1.groups] == [g.request_ids for g in p2.groups]


# ---- footprint -------------------------------------------------------------------------------------------------

def test_a_shared_route_drives_fewer_metres_than_separate_ones(net):
    from common.pooling import footprint
    shared, alone = footprint(net, HUB, ["n1_2", "n1_3"], ["n1_2", "n1_3"], 0.0)
    assert shared == pytest.approx(2.0) and alone == pytest.approx(3.0)             # grid lengths: 1 per road
    assert shared < alone


def test_footprint_of_a_solo_ride_saves_nothing(net):
    from common.pooling import footprint
    shared, alone = footprint(net, HUB, ["n1_3"], ["n1_3"], 0.0)
    assert shared == alone


def test_route_length_sums_edge_lengths(net):
    assert net.route_length([HUB, "n1_3"], 0.0) == pytest.approx(2.0)
    assert net.route_length([HUB, "n1_3", "n1_2"], 0.0) == pytest.approx(3.0)


# ---- CO2 pooling incentive (hub side) ----------------------------------------------------------
def test_pool_incentive_split_by_delay_and_zero_when_nothing_saved():
    from types import SimpleNamespace as NS
    from agents.microhub import MicrohubAgent, PoolMetric
    hub = NS(pool_incentive_per_kg=2.0, emission_g_per_km=150.0,
             pool_metrics=[PoolMetric("p1", 2, 3000.0, 9000.0)])            # 6 km saved = 0.9 kg -> 1.80 EUR
    rec = NS(pool=NS(pool_id="p1", requests=[1, 2]), rider_jids={"q1": "a@x", "q2": "b@x"},
             plan=NS(detour={"q1": 0.0, "q2": 299.0}))
    got = MicrohubAgent.pool_incentives(hub, rec, ["a@x", "b@x"])
    assert got == {"a@x": 0.01, "b@x": 1.79} and abs(sum(got.values()) - 1.80) < 0.011
    hub.pool_metrics = [PoolMetric("p1", 2, 9000.0, 9000.0)]
    assert MicrohubAgent.pool_incentives(hub, rec, ["a@x", "b@x"]) == {}
    hub.pool_metrics, hub.pool_incentive_per_kg = [PoolMetric("p1", 2, 3000.0, 9000.0)], 0.0
    assert MicrohubAgent.pool_incentives(hub, rec, ["a@x", "b@x"]) == {}


# ---- late join terms ---------------------------------------------------------------------------
def test_late_join_never_raises_an_existing_fare_and_pays_the_driver_the_extra():
    from common.pooling import late_join_terms
    net = build_grid_network()
    a = req(1, "n1_3")
    b = req(2, "n1_4")                         # same direction, a bit further: a cheap addition
    t = late_join_terms(net, HUB, [a], b, {"r1": 6.0}, 100.0, 0.004, 0.3, 4)
    assert t is not None and t.extra_seconds >= 0
    assert t.fares["r1"] <= 6.0 and abs(sum(t.fares.values()) - t.total) < 0.011
    assert t.total == round(6.0 + t.extra_pay, 2)
    assert t.fares["r2"] >= t.extra_pay - 1e-9


def test_late_join_refused_when_full_when_tolerance_breaks_or_fare_too_high():
    from common.pooling import late_join_terms
    net = build_grid_network()
    a = req(1, "n1_3", tol=0.0)                # nobody may be delayed: any extra driving breaks a's tolerance
    far = req(2, "n3_1", tol=0.0)              # opposite direction, and nobody may be delayed either
    assert late_join_terms(net, HUB, [a], far, {"r1": 6.0}, 100.0, 0.004, 0.3, 4) is None
    ok = req(3, "n1_4")
    assert late_join_terms(net, HUB, [req(1, "n1_3")], ok, {"r1": 6.0}, 100.0, 0.004, 0.3, 1) is None   # capacity
    poor = req(4, "n1_4", max_fare=0.01)
    assert late_join_terms(net, HUB, [req(1, "n1_3")], poor, {"r1": 6.0}, 100.0, 0.004, 0.3, 4) is None

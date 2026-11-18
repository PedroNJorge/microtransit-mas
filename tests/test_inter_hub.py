"""Milestone 11: the pure rules of inter-hub coordination and the scenario option (no network traffic)."""
import pytest

from common.graph import build_grid_network
from common.ontology import TripRequest
from harness.scenario import Scenario, ScenarioError
from protocols.inter_hub import PeerView, neighbours, overflow_target, takeover_hub

NET = build_grid_network()
A, B, C, D = "n1_1", "n1_4", "n4_1", "n4_4"


def req(dest, i=1):
    return TripRequest(f"r{i}", f"rider{i}", A, dest, 0.0, 3600.0, 300.0, 20.0, True, False, 0.0)


def test_neighbours_are_symmetric_and_exclude_self():
    for h in NET.hubs:
        near = neighbours(NET, h, 2)
        assert h not in near and len(near) >= 2
        for o in near:
            assert h in neighbours(NET, o, 2)


def test_neighbours_with_k_one_still_includes_whoever_counts_you():
    assert set(neighbours(NET, A, 1)) >= {B} or set(neighbours(NET, A, 1)) >= {C}


def test_takeover_picks_the_nearest_covering_live_hub():
    r = req("n1_2")                                           # covered by A and B only
    assert takeover_hub(NET, r, A, {A, B, C, D}, 0.0) == B
    assert takeover_hub(NET, r, A, {A, C, D}, 0.0) is None    # B is not live, nobody else covers it
    assert takeover_hub(NET, r, A, {B}, 0.0) == B


def test_takeover_never_gives_a_request_back_to_the_failed_hub():
    assert takeover_hub(NET, req("n1_2"), B, {A, B}, 0.0) == A


def test_every_neighbour_agrees_on_who_takes_a_request():
    r = req("n1_2")
    candidates = set(neighbours(NET, A, 3)) | {A}
    answers = {takeover_hub(NET, r, A, candidates - {A}, 0.0) for _ in candidates}
    assert len(answers) == 1


def test_overflow_target_needs_idle_drivers_and_coverage():
    peers = {B: PeerView(B, "b@x", idle_drivers=2), C: PeerView(C, "c@x", idle_drivers=5)}
    assert overflow_target(NET, [req("n1_2")], peers, A, 0.0) == B        # C does not cover n1_2
    peers[B].idle_drivers = 0
    assert overflow_target(NET, [req("n1_2")], peers, A, 0.0) is None
    peers[B].idle_drivers, peers[B].failed = 2, True
    assert overflow_target(NET, [req("n1_2")], peers, A, 0.0) is None


def test_overflow_prefers_the_hub_with_more_idle_drivers():
    peers = {B: PeerView(B, "b@x", idle_drivers=1), A: PeerView(A, "a@x", idle_drivers=4)}
    assert overflow_target(NET, [req("n1_2")], peers, D, 0.0) == A


def make(**extra):
    return Scenario.from_dict({"name": "t", "graph": "config/porto_graph.json", "hubs": "all",
                               "drivers": {"count": 2}, "riders": {"count": 1}, **extra})


def test_inter_hub_is_on_by_default_and_reaches_the_hubs():
    sc = make()
    assert sc.inter_hub == {}
    hubs = [s for s in sc.roster() if s.role == "microhub"]
    assert hubs and all("peer_jids" not in s.kwargs for s in hubs)      # added by the launcher, not the roster


def test_inter_hub_options_and_validation():
    assert make(inter_hub=False).inter_hub is None
    assert make(inter_hub={"t_failover": 90}).inter_hub == {"t_failover": 90.0}
    with pytest.raises(ScenarioError, match="inter_hub"):
        make(inter_hub={"speed": 1})
    with pytest.raises(ScenarioError, match="inter_hub"):
        make(inter_hub="yes")

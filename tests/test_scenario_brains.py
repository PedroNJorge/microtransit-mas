"""Scenario schema for the driver brains: roster, kwargs and validation (no agents are started)."""
import pytest

pytest.importorskip("spade_bdi")

from agents.registry import AGENT_CLASSES
from harness.scenario import Scenario, ScenarioError

GRAPH = "config/porto_graph.json"


def make(**drivers):
    return Scenario.from_dict({"name": "t", "graph": GRAPH, "hubs": "all",
                               "drivers": {"count": 3, **drivers}, "riders": {"count": 1}})


def roles(sc):
    return [s.role for s in sc.roster()]


def test_default_has_no_brains_and_no_policy():
    sc = make()
    assert "driver_brain" not in roles(sc)
    for s in sc.roster():
        assert "brain_jid" not in s.kwargs and "return_policy" not in s.kwargs and "broadcast_demand" not in s.kwargs


def test_brains_add_one_agent_per_driver_wired_to_it():
    sc = make(brains=True)
    brains = [s for s in sc.roster() if s.role == "driver_brain"]
    drivers = [s for s in sc.roster() if s.role == "driver"]
    assert len(brains) == len(drivers) == 3
    for b, d in zip(brains, drivers):
        assert b.kwargs["driver_jid"] == d.jid and d.kwargs["brain_jid"] == b.jid and b.kwargs["bid_policy"] is False
    assert all(s.kwargs.get("broadcast_demand") for s in sc.roster() if s.role == "microhub")


def test_brains_start_before_their_drivers():
    order = roles(make(brains=True))
    assert max(i for i, r in enumerate(order) if r == "driver_brain") < min(i for i, r in enumerate(order) if r == "driver")


def test_brain_bids_flag_reaches_the_brain():
    sc = make(brains=True, brain_bids=True)
    assert all(s.kwargs["bid_policy"] for s in sc.roster() if s.role == "driver_brain")


def test_return_policy_goes_to_the_drivers():
    sc = make(return_policy="nearest_hub")
    assert all(s.kwargs["return_policy"] == "nearest_hub" for s in sc.roster() if s.role == "driver")


def test_validation():
    with pytest.raises(ScenarioError, match="needs"):
        make(brain_bids=True)
    with pytest.raises(ScenarioError, match="return_policy"):
        make(return_policy="teleport")
    with pytest.raises(ScenarioError, match="true or false"):
        make(brains="yes")


def test_brain_role_is_registered():
    assert "driver_brain" in AGENT_CLASSES

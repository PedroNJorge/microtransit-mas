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


def test_time_value_goes_to_the_drivers_as_cost_per_second_and_defaults_to_the_wage():
    assert all("cost_per_second" not in s.kwargs for s in make().roster() if s.role == "driver")   # default: the wage target
    sc = make(time_value_per_hour=1.8)
    assert all(s.kwargs["cost_per_second"] == pytest.approx(1.8 / 3600) for s in sc.roster() if s.role == "driver")
    assert all(s.kwargs["cost_per_second"] == 0.0 for s in make(time_value_per_hour=0).roster() if s.role == "driver")


def test_a_bad_time_value_is_a_scenario_error():
    for bad in (-1, "x", True):
        with pytest.raises(ScenarioError):
            make(time_value_per_hour=bad)


def test_late_invite_is_a_scenario_switch_on_by_default():
    d = {"name": "t", "graph": GRAPH, "hubs": "all", "drivers": {"count": 1}, "riders": {"count": 1}}
    assert Scenario.from_dict(d).late_invite is True
    assert Scenario.from_dict({**d, "late_invite": False}).late_invite is False


def test_surplus_scale_hours_turns_the_bid_rule_on_in_euros_of_wage():
    assert all("surplus_scale" not in s.kwargs for s in make().roster() if s.role == "driver")        # default: off
    sc = make(surplus_scale_hours=2)
    assert all(s.kwargs["surplus_scale"] == pytest.approx(2 * 4.96) for s in sc.roster() if s.role == "driver")
    assert all(s.kwargs["surplus_scale"] == 0.0 for s in make(surplus_scale_hours=0).roster() if s.role == "driver")
    for bad in (-1, "x", True):
        with pytest.raises(ScenarioError):
            make(surplus_scale_hours=bad)


def test_lone_margin_scenario_key():
    d = {"name": "t", "graph": GRAPH, "hubs": "all", "drivers": {"count": 1}, "riders": {"count": 1}}
    assert Scenario.from_dict(d).lone_margin == pytest.approx(0.10)
    assert Scenario.from_dict({**d, "lone_margin": None}).lone_margin is None
    assert Scenario.from_dict({**d, "lone_margin": 0}).lone_margin == 0.0
    for bad in (-0.1, "x", True):
        with pytest.raises(ScenarioError):
            Scenario.from_dict({**d, "lone_margin": bad})


def test_expected_policy_and_its_margin_reach_the_brain():
    sc = make(brains=True, brain_policy="expected", brain_margin_eur=0.8)
    brains = [s for s in sc.roster() if s.role == "driver_brain"]
    assert brains and all(s.kwargs["policy"] == "expected" and s.kwargs["margin_eur"] == 0.8 for s in brains)
    assert all("margin_eur" not in s.kwargs for s in make(brains=True).roster() if s.role == "driver_brain")    # only for "expected"
    for bad in (-1, "x", True):
        with pytest.raises(ScenarioError):
            make(brains=True, brain_policy="expected", brain_margin_eur=bad)

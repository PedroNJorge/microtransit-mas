import random

import pytest

from harness.experiment import fleet_stats
from harness.labour_market import DriverView, LabourRules, parse_rules, step
from harness.scenario import Scenario, ScenarioError

W = 4.96


def rules(**kw):
    base = dict(p_exit=1.0, consecutive=2, min_fleet=1, reserve=2)
    base.update(kw)
    return LabourRules(**base)


def V(name, net, idle=True, tenure=7200.0):
    return DriverView(name, idle, tenure, net)


def test_exit_needs_two_checks_below_the_line_and_an_idle_driver():
    r, streaks = rules(), {}
    rng = random.Random(1)
    low = 0.65 * W - 0.1                       # below the exit line
    d1 = step([V("a", low), V("b", 9.0)], 0, r, streaks, rng)
    assert d1.exits == [] and streaks["a"] == 1                       # one check is not enough
    d2 = step([V("a", low), V("b", 9.0)], 0, r, streaks, rng)
    assert d2.exits == ["a"]                                          # two in a row: leaves (p_exit = 1)
    d3 = step([V("a", low, idle=False), V("b", 9.0)], 0, r, {"a": 5}, rng)
    assert d3.exits == []                                             # busy drivers never leave
    d4 = step([V("a", 3.3), V("b", 9.0)], 0, r, {"a": 1}, rng)
    assert d4.exits == [] and 3.3 > 0.65 * W                          # above the line the streak resets


def test_young_drivers_and_the_minimum_fleet_are_protected():
    r = rules(min_fleet=2)
    streaks = {"a": 5, "b": 5}
    d = step([V("a", 0.0), V("b", 0.0)], 0, r, streaks, random.Random(1))
    assert d.exits == []                                              # fleet would drop below the minimum of 2
    d = step([V("a", 0.0, tenure=600.0), V("b", 9.0)], 0, rules(), {"a": 5}, random.Random(1))
    assert d.exits == []                                              # under min_tenure: not even evaluated


def test_entry_when_the_average_is_above_the_line_and_a_reserve_is_left():
    r = rules()
    assert step([V("a", 6.0), V("b", 6.0)], 1, r, {}, random.Random(1)).enter == 1          # 6.0 > 1.10 x 4.96 = 5.456
    assert step([V("a", 6.0), V("b", 4.0)], 1, r, {}, random.Random(1)).enter == 0          # average 5.0
    assert step([V("a", 6.0), V("b", 6.0)], 0, r, {}, random.Random(1)).enter == 0          # nobody in reserve
    assert step([], 1, r, {}, random.Random(1)).enter == 0


def test_entry_on_long_waiting_riders_is_opt_in_and_enters_several_drivers():
    views = [V("a", 3.0), V("b", 3.0)]                                       # income far below the entry line
    off = rules()
    assert step(views, 5, off, {}, random.Random(1), long_waiting=10).enter == 0             # signal off by default
    on = rules(entry_wait=600.0, entry_min_waiting=3, max_enter=2)
    assert step(views, 5, on, {}, random.Random(1), long_waiting=2).enter == 0               # too few long waiters
    assert step(views, 5, on, {}, random.Random(1), long_waiting=3).enter == 2
    assert step(views, 1, on, {}, random.Random(1), long_waiting=9).enter == 1               # only one reserve driver left
    assert step([V("a", 6.0), V("b", 6.0)], 5, on, {}, random.Random(1), long_waiting=0).enter == 1   # income signal still works
    assert step([V("a", 3.0, tenure=10.0)], 5, on, {}, random.Random(1), long_waiting=4).enter == 2   # works before anyone is eligible
    assert parse_rules({"entry_wait": 600, "max_enter": 3}).max_enter == 3
    assert parse_rules({"entry_wait": None}).entry_wait is None
    with pytest.raises(ScenarioError):
        parse_rules({"entry_wait": 0})


def test_fleet_stats_time_weighted():
    log = [(1800.0, "exit", "d1", 3), (5400.0, "enter", "d9", 4)]
    s = fleet_stats(log, 4, 7200.0)
    # 4 for 1800 s, 3 for 3600 s, 4 for 1800 s -> (4*1800 + 3*3600 + 4*1800)/7200 = 3.5
    assert s["fleet_mean"] == pytest.approx(3.5) and s["fleet_min"] == 3 and s["fleet_max"] == 4
    assert s["exits"] == 1 and s["entries"] == 1 and s["fleet_end"] == 4
    assert s["turnover_per_hour"] == pytest.approx(1.0)
    assert fleet_stats([], 5, 3600.0)["fleet_mean"] == 5


GRAPH = "config/porto_graph.json"


def make(labour=None, **extra):
    d = {"name": "t", "graph": GRAPH, "hubs": "all", "drivers": {"count": 3}, "riders": {"count": 1}, **extra}
    if labour is not None:
        d["labour_market"] = labour
    return Scenario.from_dict(d)


def test_scenario_adds_offline_reserve_drivers_and_validates_the_rules():
    assert not any("start_offline" in x.kwargs for x in make().roster())
    sc = make({"reserve": 2})
    drivers = [x for x in sc.roster() if x.role == "driver"]
    assert len(drivers) == 5 and [x.kwargs.get("start_offline", False) for x in drivers] == [False] * 3 + [True] * 2
    for bad in ({"nope": 1}, {"p_exit": 2}, {"exit_ratio": 1.2, "entry_ratio": 1.0}, {"consecutive": 0}, 5):
        with pytest.raises(ScenarioError):
            make(bad)
    assert parse_rules({"reserve": 4, "p_exit": 0.2}).reserve == 4


def test_driver_leaves_only_when_idle_and_a_reserve_driver_enters():
    from agents.driver import DriverAgent
    d = DriverAgent("d1@x", "p", start_hub="h1")
    assert d.online_from == 0.0 and d.go_online(10.0, "h2") is False       # already on duty
    d.activity = "on_trip"
    assert d.go_offline(100.0) is False                                    # busy: cannot leave
    d.activity = "idle"
    assert d.go_offline(100.0) is True and d.online_until == 100.0 and d.state.name == "OFFLINE"
    r = DriverAgent("r1@x", "p", start_hub="h1", start_offline=True)
    assert r.online_from is None and r.state.name == "OFFLINE"
    assert r.go_online(900.0, "h3") is True
    assert r.online_from == 900.0 and r.location == "h3" and r.activity == "idle"
    assert r.rolling_net(900.0 + 1800.0)[1] == pytest.approx(0.5)          # half an hour on duty


def test_waiting_riders_counts_only_unboarded_riders_past_the_limit(monkeypatch):
    from types import SimpleNamespace as NS
    from harness import labour_market as lm
    monkeypatch.setattr(lm.clock, "now", lambda: 1000.0)
    def rider(requested, boarded=0.0, trip=True):
        return NS(role="rider", trip=NS(requested_at=requested, boarded_at=boarded) if trip else None)
    h = NS(agents={"a": rider(100.0), "b": rider(800.0), "c": rider(100.0, boarded=300.0), "d": rider(0, trip=False),
                   "e": NS(role="driver"), "f": rider(399.0)})
    assert lm.waiting_riders(h, 600.0) == 2          # a (900 s) and f (601 s); b waited 200 s, c is on board, d has no trip


def test_entry_hubs_rank_by_deficit_and_cycle(monkeypatch):
    from types import SimpleNamespace as NS
    from harness import labour_market as lm
    def hub(hid, waiting, rate, supply):
        return NS(role="microhub", hub_id=hid, load_snapshot=lambda now, w=waiting, r=rate, s=supply: NS(waiting=w, rate=r, supply=s))
    h = NS(agents={"1": hub("h1", 0, 0, 3), "2": hub("h2", 5, 1, 0), "3": hub("h3", 1, 0, 0)})
    assert lm.entry_hubs(h, 0.0, 2) == ["h2", "h3"]
    assert lm.entry_hubs(h, 0.0, 4) == ["h2", "h3", "h1", "h2"]

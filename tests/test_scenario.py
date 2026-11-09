"""Scenario parsing and validation (no XMPP needed)."""
import copy
import json
from pathlib import Path

import pytest

from harness.scenario import DEFAULT_SCENARIO, PROJECT_ROOT, Scenario, ScenarioError

GRAPH = PROJECT_ROOT / "config" / "porto_graph.json"
HUBS = [h["id"] for h in json.loads(GRAPH.read_text())["hubs"]]

BASE = {"graph": "config/porto_graph.json", "hubs": HUBS[:3]}


def make(**over):
    d = copy.deepcopy(BASE)
    d.update(over)
    return Scenario.from_dict(d)


def test_default_scenario_file_loads():
    s = Scenario.load(DEFAULT_SCENARIO)
    assert s.hub_ids and s.n_drivers >= 0 and s.speed > 0
    assert s.graph_path.exists()


def test_all_scenarios_in_config_load():
    files = list((PROJECT_ROOT / "config" / "scenarios").glob("*.json"))
    assert files
    for f in files:
        Scenario.load(f)


def test_full_scenario_uses_every_hub():
    s = Scenario.load(PROJECT_ROOT / "config" / "scenarios" / "full_8hubs.json")
    assert s.hub_ids == HUBS


def test_defaults_and_domain_follows_host():
    s = make()
    assert s.xmpp.domain == s.xmpp.host == "localhost"
    assert (s.n_drivers, s.n_riders, s.seed) == (2, 2, 42)
    assert s.jid("payment") == "payment@localhost"
    assert make(xmpp={"host": "10.0.0.5", "domain": "mas.local"}).jid("x") == "x@mas.local"


def test_hubs_all():
    assert make(hubs="all").hub_ids == HUBS


def test_roster_structure_and_order():
    s = make(drivers={"count": 3}, riders={"count": 2})
    roster = s.roster()
    roles = [r.role for r in roster]
    assert roles == (["payment", "regulator", "reputation"] + ["microhub"] * 3
                     + ["driver"] * 3 + ["rider"] * 2 + ["environment"])
    assert roster[-1].role == "environment"                       # started last
    assert len({r.jid for r in roster}) == len(roster)            # unique JIDs
    assert [r.name for r in roster if r.role == "driver"] == ["driver_01", "driver_02", "driver_03"]
    hub = next(r for r in roster if r.role == "microhub")
    assert hub.name == hub.kwargs["hub_id"] == HUBS[0]


def test_drivers_start_round_robin_over_active_hubs():
    s = make(drivers={"count": 5})
    starts = [r.kwargs["start_hub"] for r in s.roster() if r.role == "driver"]
    assert starts == [HUBS[0], HUBS[1], HUBS[2], HUBS[0], HUBS[1]]


def test_explicit_start_hubs():
    s = make(drivers={"count": 3, "start_hubs": [HUBS[2]]})
    assert {r.kwargs["start_hub"] for r in s.roster() if r.role == "driver"} == {HUBS[2]}


def test_zero_agents_allowed():
    s = make(drivers={"count": 0}, riders={"count": 0})
    assert [r.role for r in s.roster()].count("driver") == 0


def test_padding_grows_with_count():
    s = make(riders={"count": 100})
    assert s.roster()[-2].name == "rider_100" and "rider_001" in [r.name for r in s.roster()]


def test_resolve_by_name_jid_and_role():
    s = make()
    assert [x.name for x in s.resolve("driver_01")] == ["driver_01"]
    assert [x.name for x in s.resolve("driver_01@localhost")] == ["driver_01"]
    assert len(s.resolve("microhub")) == 3
    assert s.resolve("nobody") == []


@pytest.mark.parametrize("patch,msg", [
    ({"extra": 1}, "unknown keys"),
    ({"hubs": ["hub_atlantis"]}, "not in the graph"),
    ({"hubs": []}, "non-empty"),
    ({"hubs": "some"}, "non-empty"),
    ({"hubs": [HUBS[0], HUBS[0]]}, "duplicates"),
    ({"drivers": {"count": -1}}, "drivers.count"),
    ({"drivers": {"count": True}}, "drivers.count"),
    ({"drivers": {"count": 2.5}}, "drivers.count"),
    ({"drivers": {"cuont": 2}}, "unknown keys"),
    ({"drivers": {"count": 1, "start_hubs": [HUBS[5]]}}, "ACTIVE hubs"),
    ({"riders": {"n": 2}}, "unknown keys"),
    ({"clock": {"speed": 0}}, "clock.speed"),
    ({"clock": {"speed": "fast"}}, "clock.speed"),
    ({"xmpp": {"port": "5222"}}, "xmpp.port"),
    ({"xmpp": {"prot": 5222}}, "unknown keys"),
    ({"environment": {"t_traffic": 0}}, "t_traffic"),
    ({"environment": {"full_every": 0}}, "full_every"),
    ({"seed": -1}, "seed"),
    ({"payment": {"rider_balanse": 5}}, "unknown keys"),
    ({"payment": {"subsidy_discount": 1.5}}, "subsidy_discount"),
    ({"payment": {"subsidy_discount": -0.1}}, "subsidy_discount"),
    ({"payment": {"subsidy_budget": -1}}, "subsidy_budget"),
    ({"payment": {"rider_balance": -1}}, "rider_balance"),
    ({"payment": {"hold_max": 0}}, "hold_max"),
    ({"graph": "config/nope.json"}, "Graph file not found"),
])
def test_invalid_scenarios_are_rejected(patch, msg):
    with pytest.raises(ScenarioError, match=msg):
        make(**patch)


def test_missing_required_keys():
    with pytest.raises(ScenarioError, match="graph"):
        Scenario.from_dict({"hubs": "all"})
    with pytest.raises(ScenarioError, match="hubs"):
        Scenario.from_dict({"graph": "config/porto_graph.json"})


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ScenarioError, match="not found"):
        Scenario.load(tmp_path / "x.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ScenarioError, match="invalid JSON"):
        Scenario.load(bad)


def test_absolute_graph_path_and_name_default(tmp_path):
    f = tmp_path / "mine.json"
    f.write_text(json.dumps({"graph": str(GRAPH), "hubs": [HUBS[0]]}))
    s = Scenario.load(f)
    assert s.graph_path == Path(GRAPH) and s.name == "mine"


def test_payment_section_defaults_and_roster_kwargs():
    pay = next(r for r in make().roster() if r.role == "payment")
    assert pay.kwargs == {"default_rider_balance": 100.0, "subsidy_discount": 0.5,
                          "subsidy_budget": None, "hold_max": 7200.0}
    s = make(payment={"rider_balance": 30, "subsidy_discount": 0.25, "subsidy_budget": 500,
                      "hold_max": 600})
    pay = next(r for r in s.roster() if r.role == "payment")
    assert pay.kwargs == {"default_rider_balance": 30.0, "subsidy_discount": 0.25,
                          "subsidy_budget": 500.0, "hold_max": 600.0}
    assert [r.role for r in s.roster()][:3] == ["payment", "regulator", "reputation"]

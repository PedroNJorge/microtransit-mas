"""Scenario-based experiment runner: statistics, arms, metric collection from records, output files (no simulation is run)."""
import csv
import json
import math
from pathlib import Path

import pytest

from harness.experiment import (Arm, hub_metrics, load_plan, mean_ci, metric_names, normalise, paired_difference, parse_arm,
                                parse_value, repeat_noise, report, set_path, significant, summary_rows, wait_metrics,
                                write_runs_csv, write_summary_csv)
from harness.scenario import ScenarioError

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = "config/scenarios/07_migrating_demand.json"


@pytest.fixture(autouse=True)
def _from_the_project_root(monkeypatch):
    """Plans and arms name their files relative to the project root, as when run from there."""
    monkeypatch.chdir(ROOT)


# ---- statistics --------------------------------------------------------------------------------------------------------
def test_mean_ci_known_values():
    m, h = mean_ci([10, 12, 14])
    assert m == 12 and h == pytest.approx(4.303 * 2 / math.sqrt(3))
    assert math.isnan(mean_ci([5])[1]) and math.isnan(mean_ci([])[0])


def test_repeat_noise_is_sd_within_seed():
    runs = [dict(seed=1, x=10.0), dict(seed=1, x=14.0), dict(seed=2, x=100.0), dict(seed=2, x=100.0)]
    assert repeat_noise(runs, "x") == pytest.approx((math.sqrt(8) + 0) / 2)
    assert math.isnan(repeat_noise([dict(seed=1, x=1.0)], "x"))


def test_paired_difference_matches_seed_and_repeat():
    a = [dict(seed=s, repeat=0, x=v) for s, v in [(1, 100.0), (2, 200.0), (3, 300.0)]]
    b = [dict(seed=s, repeat=0, x=v) for s, v in [(1, 90.0), (2, 190.0), (3, 290.0), (4, 5.0)]]
    d, h, n = paired_difference(a, b, "x")
    assert (d, n) == (-10.0, 3) and h == 0.0 and significant(d, h)


def test_paired_difference_skips_missing_values():
    a = [dict(seed=1, repeat=0, x=1.0), dict(seed=2, repeat=0, x=math.nan)]
    b = [dict(seed=1, repeat=0, x=3.0), dict(seed=2, repeat=0, x=5.0)]
    assert paired_difference(a, b, "x")[2] == 1


# ---- arms --------------------------------------------------------------------------------------------------------------
def test_values_are_json_when_they_parse():
    assert parse_value("true") is True and parse_value("60") == 60 and parse_value("[1,2]") == [1, 2]
    assert parse_value("proportional") == "proportional"


def test_set_path_creates_the_dicts_on_the_way():
    d = {"drivers": {"count": 3}}
    set_path(d, "drivers.brain_policy", "proportional")
    set_path(d, "clock.speed", 15)
    set_path(d, "t_pool", 30)
    assert d == {"drivers": {"count": 3, "brain_policy": "proportional"}, "clock": {"speed": 15}, "t_pool": 30}


def test_parse_arm_reads_the_scenario_and_the_overrides():
    a = parse_arm("prop=config/x.json,drivers.brain_policy=proportional,late_join=false,t_pool=60")
    assert (a.name, a.scenario) == ("prop", "config/x.json")
    assert a.overrides == {"drivers.brain_policy": "proportional", "late_join": False, "t_pool": 60}
    assert parse_arm("plain=a.json").overrides == {}
    for bad in ("noscenario", "x=", "=a.json", "x=a.json,oops"):
        with pytest.raises(SystemExit):
            parse_arm(bad)


def test_an_arm_builds_its_scenario_with_speed_overrides_and_seed():
    arm = Arm("prop", "config/scenarios/brains_07_migrating_demand.json", {"drivers.brain_policy": "proportional"})
    data = arm.data_for(seed=7, speed=15)
    assert data["seed"] == 7 and data["clock"]["speed"] == 15 and data["drivers"]["brain_policy"] == "proportional"
    assert data["name"].endswith("/prop/seed7")
    sc = arm.scenario_for(7, 15)
    assert sc.seed == 7 and sc.speed == 15 and sc.brain_policy == "proportional"
    # the arm's own override of the speed wins over the shared one
    assert Arm("fast", SCENARIO, {"clock.speed": 30}).scenario_for(1, 15).speed == 30
    # an arm never changes the file it reads: the same arm twice gives the same answer
    assert arm.data_for(7, 15) == data


def test_a_bad_override_or_file_is_a_scenario_error():
    with pytest.raises(ScenarioError):
        Arm("x", SCENARIO, {"drivers.brain_policy": "nonsense"}).scenario_for(1, 15)
    with pytest.raises(ScenarioError):
        Arm("x", "config/scenarios/does_not_exist.json").scenario_for(1, 15)


def test_a_plan_file_gives_arms_and_options(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"speed": 15, "seeds": [1, 2], "scenario": SCENARIO,
                                "arms": {"a": {}, "b": {"set": {"late_join": False}}, "c": {"scenario": "other.json"}}}))
    arms, opts = load_plan(str(plan))
    assert [x.name for x in arms] == ["a", "b", "c"]
    assert arms[1].overrides == {"late_join": False} and arms[2].scenario == "other.json" and arms[0].scenario == SCENARIO
    assert opts == {"speed": 15, "seeds": [1, 2]}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"arms": {"a": {}}}))
    with pytest.raises(SystemExit):
        load_plan(str(bad))


def test_the_shipped_plan_loads_and_every_arm_is_a_valid_scenario():
    arms, opts = load_plan("config/experiments/07_brains.json")
    assert len(arms) >= 3 and opts["speed"] == 15
    for a in arms:
        a.scenario_for(1, opts["speed"])


# ---- metrics from records -------------------------------------------------------------------------------------------------
def trips():
    rows = [(0, 20, 120, 300), (100, 130, 400, 600), (200, 210, 260, 500), (1300, 1320, 1400, 1700)]   # req, match, board, drop
    return [dict(requested=r, matched=m, boarded=b, dropped=d, window_end=3600.0, rating=5) for r, m, b, d in rows]


def test_wait_metrics_split_the_wait_and_report_it_by_quarter_and_stream():
    out = wait_metrics(trips(), streams=[("early", 0, 1200), ("late", 1200, 2400)])
    assert out["wait_mean"] == pytest.approx((120 + 300 + 60 + 100) / 4)
    assert out["wait_to_match_mean"] == pytest.approx((20 + 30 + 10 + 20) / 4)
    assert out["wait_after_match_mean"] == pytest.approx((100 + 270 + 50 + 80) / 4)
    assert out["wait_max"] == 300 and out["wait_min"] == 60
    assert [out[f"wait_q{i}"] for i in (1, 2, 3, 4)] == [120, 300, 60, 100]       # one rider per quarter
    assert out["wait_stream_early"] == pytest.approx(160) and out["n_stream_early"] == 3
    assert out["wait_stream_late"] == 100 and out["n_stream_late"] == 1
    assert out["late_window_share"] == 0.0 and out["rating_mean"] == 5


def test_wait_metrics_with_no_trips_is_nan_not_an_error():
    out = wait_metrics([], streams=[("s", 0, 10)])
    assert math.isnan(out["wait_mean"]) and math.isnan(out["wait_q1"]) and out["n_stream_s"] == 0


# ---- output files ----------------------------------------------------------------------------------------------------------
def runs():
    a = [dict(arm="a", seed=s, repeat=0, ended_by="all_home", wait_mean=w, profit_per_trip=p, refuse_locked=5.0)
         for s, w, p in [(1, 100.0, 1.0), (2, 200.0, 2.0), (3, 300.0, 3.0)]]
    b = [dict(arm="b", seed=s, repeat=0, ended_by="all_home", wait_mean=w, profit_per_trip=p)
         for s, w, p in [(1, 80.0, 1.5), (2, 180.0, 2.5), (3, 280.0, 3.5)]]
    return {"a": a, "b": b}


def test_normalise_fills_absent_counts_with_zero_and_other_metrics_with_nan():
    r = runs()
    r["b"][0]["extra"] = 4.0
    normalise(r)
    assert r["b"][0]["refuse_locked"] == 0.0 and math.isnan(r["a"][0]["extra"])


def test_summary_has_mean_ci_and_the_paired_difference_to_the_first_arm():
    rows = summary_rows(["a", "b"], normalise(runs()))
    by = {(r["metric"], r["arm"]): r for r in rows}
    a, b = by[("wait_mean", "a")], by[("wait_mean", "b")]
    assert a["n"] == 3 and a["mean"] == 200 and a["diff_significant"] == "" and math.isnan(a["diff_vs_first"])
    assert b["mean"] == 180 and b["diff_vs_first"] == pytest.approx(-20) and b["diff_n"] == 3
    assert b["diff_significant"] == "yes"                   # -20 in every pair: the CI excludes 0
    assert by[("profit_per_trip", "b")]["diff_vs_first"] == pytest.approx(0.5)
    assert by[("profit_per_trip", "b")]["diff_ci95"] == 0.0


def test_the_csv_files_have_one_row_per_run_and_every_metric(tmp_path):
    r = normalise(runs())
    write_runs_csv(tmp_path / "runs.csv", r)
    write_summary_csv(tmp_path / "summary.csv", summary_rows(["a", "b"], r))
    rows = list(csv.DictReader(open(tmp_path / "runs.csv")))
    assert len(rows) == 6 and rows[0]["arm"] == "a" and float(rows[3]["wait_mean"]) == 80.0
    assert set(metric_names(r)) <= set(rows[0])
    summ = list(csv.DictReader(open(tmp_path / "summary.csv")))
    assert {x["metric"] for x in summ} == set(metric_names(r)) and len(summ) == 2 * len(metric_names(r))


def test_the_terminal_report_shows_arms_and_differences():
    txt = report(["a", "b"], normalise(runs()), metrics=("wait_mean", "profit_per_trip", "not_a_metric"))
    assert "wait_mean" in txt and "vs a: b -20" in txt and "*" in txt and "not_a_metric" not in txt


def test_hub_metrics_show_what_each_hub_got_and_how_unequal_it_is():
    ts = [dict(requested=0, boarded=100, hub="a"), dict(requested=0, boarded=300, hub="a"),
          dict(requested=0, boarded=600, hub="b"), dict(requested=0, boarded=50, hub="c")]
    out = hub_metrics(ts, ["a", "b", "c", "d"], {"b": 2})
    assert out["hub_a_n"] == 2 and out["hub_a_share"] == 0.5 and out["hub_a_wait_mean"] == 200
    assert out["hub_b_not_completed"] == 2 and out["hub_c_wait_mean"] == 50
    assert out["hub_d_n"] == 0 and math.isnan(out["hub_d_wait_mean"])          # a hub nobody used: no wait, and not in the fairness
    assert out["hubs_served"] == 3 and out["hub_wait_worst"] == 600 and out["hub_wait_max_over_min"] == 12
    assert 0 < out["hub_wait_gini"] < 1 and out["hub_wait_cv"] > 0


def test_hub_metrics_with_one_hub_has_no_fairness_number():
    out = hub_metrics([dict(requested=0, boarded=10, hub="a")], ["a"], {})
    assert out["hubs_served"] == 1 and math.isnan(out["hub_wait_gini"]) and math.isnan(out["hub_wait_cv"])


def test_every_shipped_plan_loads_and_every_arm_is_a_valid_scenario():
    plans = sorted(Path("config/experiments").glob("*.json"))
    assert len(plans) >= 8
    for p in plans:
        arms, opts = load_plan(str(p))
        for a in arms:
            a.scenario_for(1, opts.get("speed", 15))

"""Long scenarios: the cycle generator, and that every shipped long scenario loads (no simulation is run)."""
import glob
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from make_long_scenarios import SOURCES, repeat_cycles          # noqa: E402
from harness.scenario import Scenario                           # noqa: E402

BASE = {"name": "b", "duration": 14400,
        "demand": [{"id": "x", "start": 100, "end": 700, "count": 10}, {"id": "y", "start": 700, "end": 1000, "count": 5}],
        "forecasts": [{"at": 50, "hub": "h", "expected": 10, "horizon": 600}],
        "events": [{"at": 900, "type": "kill_hub", "hub": "h"}]}


def test_cycles_repeat_demand_forecasts_and_events_period_apart():
    out = repeat_cycles(BASE, cycles=3, period=3600)
    assert [(r["id"], r["start"], r["end"], r["count"]) for r in out["demand"]] == [
        ("x_c1", 100, 700, 10), ("y_c1", 700, 1000, 5), ("x_c2", 3700, 4300, 10), ("y_c2", 4300, 4600, 5),
        ("x_c3", 7300, 7900, 10), ("y_c3", 7900, 8200, 5)]
    assert [f["at"] for f in out["forecasts"]] == [50, 3650, 7250]
    assert [e["at"] for e in out["events"]] == [900, 4500, 8100]
    assert out["duration"] == 8200 + 7200 and out["name"] == "long-b"


def test_one_cycle_is_the_base_scenario_with_renamed_streams_and_the_input_is_untouched():
    before = json.dumps(BASE, sort_keys=True)
    out = repeat_cycles(BASE, cycles=1, period=3600)
    assert [(r["start"], r["end"], r["count"]) for r in out["demand"]] == [(100, 700, 10), (700, 1000, 5)]
    assert json.dumps(BASE, sort_keys=True) == before


def test_bad_cycle_arguments_are_refused():
    for cycles, period in ((0, 3600), (2, 0)):
        with pytest.raises(ValueError):
            repeat_cycles(BASE, cycles, period)


def test_every_shipped_long_scenario_loads_and_has_six_cycles_of_demand():
    files = sorted(glob.glob(str(ROOT / "config" / "scenarios" / "long_*.json")))
    assert len(files) == len(SOURCES)
    for f in files:
        sc = Scenario.load(f)
        base = Scenario.load(str(ROOT / "config" / "scenarios" / Path(f).name.replace("long_", "", 1)))
        assert len(sc.demand) == 6 * len(base.demand)
        assert sum(d.count for d in sc.demand) == 6 * sum(d.count for d in base.demand)
        assert sc.n_riders == base.n_riders and sc.n_drivers == base.n_drivers      # riders are re-used between requests
        assert sc.duration > 6 * 3600

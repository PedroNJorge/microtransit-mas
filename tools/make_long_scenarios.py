"""Long versions of the scenarios: the same demand cycle repeated, so each driver works many hours and meets the same load again.

    python tools/make_long_scenarios.py                      # writes config/scenarios/long_*.json (6 cycles of 1 h)
    python tools/make_long_scenarios.py --cycles 4 --period 3600

A cycle is the base scenario's own demand (request streams, forecasts, events) shifted by `period` seconds. The arrival rate
inside a cycle is the base one, so riders per driver-hour are unchanged and the first cycle is the base scenario; between the
bursts the drivers wait, which is where waiting costs and later learning show. The riders are re-used (a request goes to the first
idle rider), so the rider count stays the base one; `skipped` in the experiment report says if that was too few. The duration cap
is the last cycle's end + 2 h."""
import argparse
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("04_centre_drivers_suburb_requests", "brains_04_centre_drivers_suburb_requests",
           "05_antecipated_surge_reposition", "brains_05_antecipated_surge_reposition",
           "07_migrating_demand", "brains_07_migrating_demand",
           "brains_full_8hubs", "brains_nopool_8hubs")


def repeat_cycles(data: dict, cycles: int, period: float) -> dict:
    """A copy of the scenario with its demand, forecasts and events repeated `cycles` times, `period` sim seconds apart."""
    if cycles < 1 or period <= 0:
        raise ValueError("cycles must be >= 1 and period > 0")
    out = copy.deepcopy(data)
    out["name"] = f"long-{data.get('name', 'scenario')}"
    for key, time_keys in (("demand", ("start", "end")), ("forecasts", ("at",)), ("events", ("at",))):
        base = data.get(key)
        if not base:
            continue
        rows = []
        for c in range(cycles):
            for row in base:
                new = copy.deepcopy(row)
                for k in time_keys:
                    new[k] = row[k] + c * period
                if key == "demand" and "id" in new:
                    new["id"] = f"{row['id']}_c{c + 1}"
                rows.append(new)
        out[key] = rows
    ends = [r["end"] for r in out.get("demand", [])]
    out["duration"] = max(ends, default=cycles * period) + 7200
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--period", type=float, default=3600.0, help="sim seconds between the start of two cycles")
    args = ap.parse_args()
    for src in SOURCES:
        path = ROOT / "config" / "scenarios" / f"{src}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        out = repeat_cycles(data, args.cycles, args.period)
        dest = ROOT / "config" / "scenarios" / f"long_{src}.json"
        dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        n = sum(r.get("count", 0) for r in out.get("demand", []))
        print(f"{dest.relative_to(ROOT)}: {len(out.get('demand', []))} streams, {n} requests, cap {out['duration']:.0f} s")


if __name__ == "__main__":
    main()

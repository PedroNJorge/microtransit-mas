"""
src/harness/experiment.py   Scenario-based A/B experiments: many seeds and repeats, every metric, confidence intervals.

One simulation run is NOT reproducible (message timing is asynchronous: the same seed can give mean waits 90 s apart),
so one run per setting says almost nothing. This runner plays a scenario FILE for every "arm" (the file plus overrides)
on several demand seeds, several times each, and records, for every run, ALL the metrics the project has:

  * the viewer's metrics (harness/live_metrics.py): the cumulative value of every metric at the end of the run, plus the
    peaks / averages of the rolling-window values over the run and the full METRICS time series (one snapshot a minute);
  * everything the run report prints (harness/runner.py): wait split, wait by order of request, wait per demand stream,
    auction attempts and the drivers' refusals by reason, late joins, overflow / handovers, brain moves and their
    dispersion, riders still in progress, ...;
  * extra totals (revenue and cost in EUR, empty-driving cost, money conservation, clock health).

Everything goes to a folder (default experiments/<time>/):
    runs.csv       one row per run, one column per metric (more columns than you will ever read: pick what you need)
    summary.csv    metric x arm: n, mean, 95 % CI, sd, min, max, and the paired difference to the first arm
    series/*.json  the viewer's METRICS snapshots of every run (for time-series plots)
    config.json    what was run

Usage
    uv run python -m harness.experiment --seeds 1,2,3,4,5 \
        --arm none=config/scenarios/07_migrating_demand.json \
        --arm signal=config/scenarios/brains_07_migrating_demand.json \
        --arm prop=config/scenarios/brains_07_migrating_demand.json,drivers.brain_policy=proportional
    uv run python -m harness.experiment --plan config/experiments/07_brains.json

An arm is `name=scenario.json[,dotted.key=value,...]`: the overrides are dotted paths into the scenario's JSON
(`drivers.brain_policy=proportional`, `t_pool=60`, `late_join=false`, `clock.speed=15`); a value is read as JSON if it
parses (`true`, `60`, `[1,2]`) and as text otherwise. A plan file is the same thing as JSON:
    {"speed": 15, "seeds": [1,2,3], "repeats": 1,
     "arms": {"name": {"scenario": "path.json", "set": {"drivers.brain_policy": "proportional"}}}}
`--speed` (default 15) is applied to every arm before the arm's own overrides: results depend on the clock speed, so
every arm of an experiment runs at the same one. Runs are interleaved (repeat > seed > arm) so slow drift hits every arm.
"""
import argparse
import asyncio
import copy
import csv
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from common import clock
from common.costs import driver_cost, dwell_seconds
from common.settings import DRIVER_COST_PER_KM, DRIVER_WAGE_TARGET_PER_HOUR
from harness.launcher import Harness
from harness.live_metrics import driver_income_stats, gini, mover_stats, percentile
from harness.scenario import PROJECT_ROOT, Scenario, ScenarioError

# two-sided 95 % Student-t critical values by degrees of freedom (1..30); beyond that ~1.96
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]

# printed on the terminal (the CSVs have everything)
KEY_METRICS = (
    "completed", "unfinished", "wait_mean", "wait_p50", "wait_p90", "wait_max", "wait_to_match_mean",
    "wait_after_match_mean", "wait_q1", "wait_q2", "wait_q3", "wait_q4",
    "profit_per_trip", "revenue_per_trip", "earnings_per_driven_hour", "net_profit_total", "gini",
    "riders_per_vehicle", "pooled_share", "late_share", "co2_saved_all_kg", "co2_saved_all_pct",
    "km_loaded", "km_pickup", "km_return", "km_move", "km_reposition", "deadhead_share",
    "util_idle", "util_loaded", "util_cruising",
    "auction_attempts", "refuse_locked", "refuse_too_late", "late_joins",
    "brain_moves", "mover_dispersion", "mover_share_in_group", "hub_wait_gini", "hub_wait_cv", "hub_wait_max_over_min",
    "hub_wait_worst", "fleet_income_hour", "driver_income_hour_median", "driver_wage_share", "driver_income_gini",
    "driver_utilisation_mean", "fleet_initial", "fleet_end", "fleet_mean", "exits", "entries", "turnover_per_hour",
    "single_bid_share", "speed_achieved", "sim_seconds",
)
COUNT_PREFIXES = ("fail_", "refuse_", "outcome_")      # a count that is absent in a run was zero


# ---- statistics (pure, unit-tested) ---------------------------------------------------------------------------------

def mean_ci(values: Sequence[float]):
    """(mean, half-width of the 95 % CI of the mean); half-width is nan for fewer than 2 values."""
    n = len(values)
    if n == 0:
        return math.nan, math.nan
    m = sum(values) / n
    if n < 2:
        return m, math.nan
    t = _T95[n - 2] if n - 1 <= len(_T95) else 1.96
    return m, t * statistics.stdev(values) / math.sqrt(n)


def repeat_noise(runs: List[dict], metric: str) -> float:
    """Average standard deviation of `metric` between repeats of the SAME seed (the run-to-run noise);
    nan if no seed has two repeats."""
    by_seed: Dict[int, List[float]] = {}
    for r in runs:
        if not math.isnan(r.get(metric, math.nan)):
            by_seed.setdefault(r["seed"], []).append(r[metric])
    sds = [statistics.stdev(v) for v in by_seed.values() if len(v) > 1]
    return sum(sds) / len(sds) if sds else math.nan


def paired_difference(runs_a: List[dict], runs_b: List[dict], metric: str):
    """mean(b - a) over runs with the same (seed, repeat), its CI half-width and the pair count."""
    a = {(r["seed"], r["repeat"]): r.get(metric, math.nan) for r in runs_a}
    diffs = []
    for r in runs_b:
        base = a.get((r["seed"], r["repeat"]), math.nan)
        v = r.get(metric, math.nan)
        if not math.isnan(v) and not math.isnan(base):
            diffs.append(v - base)
    m, h = mean_ci(diffs)
    return m, h, len(diffs)


def significant(mean: float, half: float) -> bool:
    return not math.isnan(half) and abs(mean) > half


# ---- arms ---------------------------------------------------------------------------------------------------------------

def parse_value(text: str):
    """`true` -> True, `60` -> 60, `[1,2]` -> [1, 2], anything that is not JSON stays text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def set_path(data: dict, dotted: str, value) -> None:
    """data["a"]["b"] = value for "a.b" (creating the dicts on the way)."""
    keys = dotted.split(".")
    node = data
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


class Arm:
    def __init__(self, name: str, scenario: str, overrides: Optional[Dict[str, object]] = None):
        self.name, self.scenario, self.overrides = name, scenario, dict(overrides or {})

    def __repr__(self):
        return f"Arm({self.name!r}, {self.scenario!r}, {self.overrides!r})"

    def data_for(self, seed: int, speed: Optional[float]) -> dict:
        """The scenario's JSON for one run: the file, then the shared speed, then the arm's overrides, then the seed."""
        path = Path(self.scenario)
        if not path.is_absolute():
            path = path if path.exists() else PROJECT_ROOT / path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise ScenarioError(f"arm {self.name!r}: cannot read {path}: {e}") from None
        data = copy.deepcopy(data)
        if speed is not None:
            set_path(data, "clock.speed", speed)
        for k, v in self.overrides.items():
            set_path(data, k, v)
        data["seed"] = seed
        data["name"] = f"{data.get('name', path.stem)}/{self.name}/seed{seed}"
        return data

    def scenario_for(self, seed: int, speed: Optional[float]) -> Scenario:
        return Scenario.from_dict(self.data_for(seed, speed), default_name=Path(self.scenario).stem)


def parse_arm(text: str) -> Arm:
    """`name=scenario.json[,dotted.key=value,...]`"""
    name, eq, rest = text.partition("=")
    if not eq or not name.strip() or not rest.strip():
        raise SystemExit(f"--arm {text!r}: expected name=scenario.json[,key=value,...]")
    parts = [p for p in rest.split(",") if p.strip()]
    overrides = {}
    for item in parts[1:]:
        k, e, v = item.partition("=")
        if not e or not k.strip():
            raise SystemExit(f"--arm {name!r}: override {item!r} is not key=value")
        overrides[k.strip()] = parse_value(v.strip())
    return Arm(name.strip(), parts[0].strip(), overrides)


def load_plan(path: str) -> Tuple[List[Arm], dict]:
    """A plan file -> (arms, {speed, seeds, repeats, timeout})."""
    raw = json.loads(Path(path).read_text())
    arms_raw = raw.get("arms")
    if not isinstance(arms_raw, dict) or not arms_raw:
        raise SystemExit(f"{path}: 'arms' must be a non-empty object {{name: {{scenario, set}}}}")
    default_scenario = raw.get("scenario")
    arms = []
    for name, spec in arms_raw.items():
        spec = spec or {}
        scenario = spec.get("scenario", default_scenario)
        if not scenario:
            raise SystemExit(f"{path}: arm {name!r} has no 'scenario' (and the plan has none)")
        arms.append(Arm(name, scenario, spec.get("set", {})))
    opts = {k: raw[k] for k in ("speed", "seeds", "repeats", "timeout") if k in raw}
    return arms, opts


# ---- collecting the metrics of one run --------------------------------------------------------------------------------

def _num(x) -> float:
    if x is None:
        return math.nan
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    return float(x)


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else math.nan


def _pct(xs, p: float) -> float:
    v = percentile(list(xs), p)
    return math.nan if v is None else float(v)


def trip_table(h) -> List[dict]:
    """One row per completed trip: when it was requested, matched, boarded, dropped; and what its window was."""
    rows = []
    for r in (a for a in h.agents.values() if a.role == "rider"):
        for t in r.trips:
            if t.outcome != "completed" or not t.dropped_at:
                continue
            rows.append(dict(requested=t.requested_at, matched=t.matched_at or None, boarded=t.boarded_at,
                             dropped=t.dropped_at, window_end=t.request.time_window_end, hub=t.request.origin_node,
                             rating=(t.rating.score if t.rating is not None else None)))
    rows.sort(key=lambda x: x["requested"])
    return rows


def wait_metrics(trips: List[dict], streams) -> Dict[str, float]:
    """Waits (request -> boarding) in every way the report and the viewer show them, and a few more. `streams`: the
    demand streams as (id, start, end)."""
    out: Dict[str, float] = {}
    waits = [t["boarded"] - t["requested"] for t in trips]
    out["wait_mean"] = _mean(waits)
    out["wait_sd"] = statistics.pstdev(waits) if len(waits) > 1 else math.nan
    for label, p in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        out[f"wait_{label}"] = _pct(waits, p)
    out["wait_min"] = float(min(waits)) if waits else math.nan
    out["wait_max"] = float(max(waits)) if waits else math.nan
    out["wait_to_match_mean"] = _mean(t["matched"] - t["requested"] for t in trips if t["matched"] is not None)
    out["wait_after_match_mean"] = _mean(t["boarded"] - t["matched"] for t in trips if t["matched"] is not None)
    out["ride_mean"] = _mean(t["dropped"] - t["boarded"] for t in trips)
    out["late_window_share"] = _mean(1.0 if t["boarded"] > t["window_end"] else 0.0 for t in trips)
    out["rating_mean"] = _mean(t["rating"] for t in trips if t["rating"] is not None)
    n = len(trips)
    for q in range(4):                                         # quarters of the riders, earliest request first
        part = trips[q * n // 4:(q + 1) * n // 4]
        out[f"wait_q{q + 1}"] = _mean(t["boarded"] - t["requested"] for t in part)
    for sid, lo, hi in streams:                                # per demand stream (e.g. the phases of scenario 07)
        part = [t for t in trips if lo <= t["requested"] < hi]
        out[f"wait_stream_{sid}"] = _mean(t["boarded"] - t["requested"] for t in part)
        out[f"n_stream_{sid}"] = float(len(part))
    return out


def hub_metrics(trips: List[dict], hubs: Sequence[str], not_completed: Dict[str, int]) -> Dict[str, float]:
    """Equity: what each hub's riders got, and how unequal that is. `hubs`: every hub of the run; `not_completed`: trips that
    did not finish (failed, or still travelling) per hub. Fairness numbers use only hubs that had completed trips."""
    out: Dict[str, float] = {}
    n_all = len(trips)
    means = []
    for hub in hubs:
        mine = [t["boarded"] - t["requested"] for t in trips if t.get("hub") == hub]
        key = hub.replace(" ", "_")
        out[f"hub_{key}_n"] = float(len(mine))
        out[f"hub_{key}_share"] = len(mine) / n_all if n_all else math.nan
        out[f"hub_{key}_wait_mean"] = _mean(mine)
        out[f"hub_{key}_wait_p90"] = _pct(mine, 0.9)
        out[f"hub_{key}_not_completed"] = float(not_completed.get(hub, 0))
        if mine:
            means.append(sum(mine) / len(mine))
    out["hubs_served"] = float(len(means))
    out["hub_wait_gini"] = gini(means) if len(means) > 1 and gini(means) is not None else math.nan
    out["hub_wait_cv"] = (statistics.pstdev(means) / _mean(means)) if len(means) > 1 and _mean(means) > 0 else math.nan
    out["hub_wait_max_over_min"] = (max(means) / min(means)) if len(means) > 1 and min(means) > 0 else math.nan
    out["hub_wait_worst"] = max(means) if means else math.nan
    return out


def auction_metrics(h) -> Dict[str, float]:
    hubs = [a for a in h.agents.values() if a.role == "microhub"]
    ctxs = [c for hub in hubs for c in hub.finished]
    out: Dict[str, float] = {"auctions_total": float(len(ctxs))}
    for outcome, n in Counter(c.outcome for c in ctxs).items():
        out[f"outcome_{outcome}"] = float(n)
    for reason, n in Counter(str(r) for c in ctxs for r in c.refusals.values()).items():
        out[f"refuse_{reason}"] = float(n)
    out["bids_per_auction"] = _mean(len(c.bids) for c in ctxs)
    matched = [c for c in ctxs if c.outcome == "matched"]
    out["single_bid_share"] = _mean(1.0 if len(c.bids) == 1 else 0.0 for c in matched)
    out["called_per_auction"] = _mean(len(c.recipients) for c in ctxs)
    out["pools_total"] = float(sum(len(hub.pools) for hub in hubs))
    out["late_joins"] = float(sum(len(hub.late_joined) for hub in hubs))
    out["handovers_adopted"] = float(sum(len(hub.adopted) for hub in hubs))
    out["overflow_offers"] = float(sum(len(hub.overflow_tried) for hub in hubs))
    return out


def brain_metrics(h) -> Dict[str, float]:
    drivers = [a for a in h.agents.values() if a.role == "driver"]
    brains = [a for a in h.agents.values() if a.role == "driver_brain"]
    out: Dict[str, float] = {"drivers": float(len(drivers)), "brains": float(len(brains)),
                             "brain_moves": float(sum(len(getattr(d, "cruises", [])) for d in drivers)),
                             "brain_commands": float(sum(len(b.commands) for b in brains)),
                             "brain_refusals": float(sum(len(b.refusals) for b in brains)),
                             "brain_percepts": float(sum(b.percepts_received for b in brains)),
                             "markup_changes": float(sum(len(b.markups) for b in brains))}
    ms = mover_stats([b.command_times for b in brains if hasattr(b, "command_times")])
    out["mover_ticks"] = float(ms["ticks"]) if ms else 0.0
    out["mover_mean"] = ms["mean"] if ms else math.nan
    out["mover_dispersion"] = ms["dispersion"] if ms else math.nan
    out["mover_share_in_group"] = ms["share_in_group"] if ms else math.nan
    trips = [len(d.completed) for d in drivers]
    out["trips_per_driver_mean"] = _mean(trips)
    out["trips_per_driver_max"] = float(max(trips, default=0))
    out["trips_per_driver_min"] = float(min(trips, default=0))
    return out


def fleet_stats(market_log: Sequence[tuple], initial: int, now: float) -> Dict[str, float]:
    """Fleet size over the run from the labour market's events (sim time, "exit"|"enter", driver, fleet after)."""
    size, t_prev, area = float(initial), 0.0, 0.0
    lo = hi = size
    for t, kind, _, after in sorted(market_log, key=lambda e: e[0]):
        area += size * (min(t, now) - t_prev)
        size, t_prev = float(after), min(t, now)
        lo, hi = min(lo, size), max(hi, size)
    area += size * max(now - t_prev, 0.0)
    exits = sum(1 for e in market_log if e[1] == "exit")
    entries = sum(1 for e in market_log if e[1] == "enter")
    hours = now / 3600.0
    return {"fleet_initial": float(initial), "fleet_end": size, "fleet_min": lo, "fleet_max": hi,
            "fleet_mean": area / now if now > 0 else float(initial), "exits": float(exits), "entries": float(entries),
            "turnover_per_hour": (exits + entries) / hours if hours > 0 else 0.0}


def money_metrics(h, collector) -> Dict[str, float]:
    """Totals in EUR: revenue, the cost model's cost of loaded and of empty driving, money conservation."""
    pools, empty = collector.pool_records(), collector.empty_records()
    revenue = sum(y[2] for y in collector.payout_records())
    loaded_cost = sum(driver_cost((p["pickup_m"] + p["loaded_m"]) / 1000.0, p["seconds"], dwell_seconds(p["riders"]))
                      for p in pools)
    empty_cost = sum(driver_cost(m / 1000.0, s) for _, m, s in empty)
    out = {"revenue_total": revenue, "cost_loaded_total": loaded_cost, "cost_empty_total": empty_cost,
           "net_profit_total": revenue - loaded_cost - empty_cost, "vehicle_trips": float(len(pools))}
    out.update(driver_income_stats(collector.driver_records(clock.now()), DRIVER_WAGE_TARGET_PER_HOUR, DRIVER_COST_PER_KM))
    pay = next((a for a in h.agents.values() if a.role == "payment"), None)
    if pay is not None:
        try:
            out["money_conserved"] = 1.0 if pay.ledger.check_conservation() else 0.0
        except Exception:
            out["money_conserved"] = math.nan
    return out


def series_metrics(history: List[dict]) -> Dict[str, float]:
    """The viewer's time series reduced to numbers: peaks and averages of the rolling-window values and queues."""
    out: Dict[str, float] = {"snapshots": float(len(history))}
    if not history:
        return out
    for key in ("wait_mean", "wait_p90", "wait_max", "profit_per_trip", "riders_per_vehicle"):
        vals = [s["roll"].get(key) for s in history if s["roll"].get(key) is not None]
        out[f"roll_{key}_max"] = float(max(vals)) if vals else math.nan
        out[f"roll_{key}_mean"] = _mean(vals)
    for key in ("queue_len", "idle_drivers"):
        vals = [s["now"][key] for s in history if key in s["now"]]
        out[f"now_{key}_max"] = float(max(vals)) if vals else math.nan
        out[f"now_{key}_mean"] = _mean(vals)
    return out


def collect(h, sc: Scenario, real_seconds: float, ended_by: str) -> Dict[str, float]:
    """Every metric of a finished (or cut-off) run, as {name: number}. The harness must still be up."""
    collector = h.metrics
    snap = collector.snapshot()                                         # the viewer's numbers, now
    row: Dict[str, float] = {k: _num(v) for k, v in snap["cum"].items()}   # cumulative, every viewer metric
    for k, v in snap["failures"].items():
        row[f"fail_{str(k).replace(' ', '_')}"] = float(v)
    row["fail_total"] = float(sum(snap["failures"].values()))
    for k, v in snap["now"].items():
        row[f"end_{k}"] = _num(v)
    riders = [a for a in h.agents.values() if a.role == "rider"]
    trips = trip_table(h)
    runner = getattr(h, "runner", None)
    streams = [(d.id, float(d.start), float(d.end)) for d in sc.demand] if sc.demand else []
    row.update(wait_metrics(trips, streams))
    hub_ids = sorted(a.hub_id for a in h.agents.values() if a.role == "microhub")
    lost = Counter(t.request.origin_node for r in riders for t in r.trips if t.outcome and t.outcome != "completed")
    lost.update(r.trip.request.origin_node for r in riders if r.trip is not None)
    row.update(hub_metrics(trips, hub_ids, lost))
    row["completed"] = float(len(trips))
    row["unfinished"] = float(sum(1 for r in riders if r.trip is not None))
    row["riders"] = float(len(riders))
    row["issued"] = float(runner.summary.issued) if runner is not None else math.nan
    row["skipped"] = float(runner.summary.skipped) if runner is not None else math.nan
    row.update(auction_metrics(h))
    row.update(fleet_stats(getattr(h, "market_log", []), sc.n_drivers, clock.now()))
    row.update(brain_metrics(h))
    row.update(money_metrics(h, collector))
    row.update(series_metrics(collector.history))
    sim_end = clock.now()
    row["sim_seconds"] = sim_end
    row["real_seconds"] = real_seconds
    row["speed"] = float(sc.speed)
    row["speed_achieved"] = sim_end / real_seconds if real_seconds > 0 else math.nan
    row["ended_all_home"] = 1.0 if ended_by == "all_home" else 0.0
    row["ended_by_cap"] = 1.0 if ended_by != "all_home" else 0.0
    return row


# ---- running --------------------------------------------------------------------------------------------------------------

async def wait_until_done(h, sc: Scenario, limit_real: float) -> str:
    """The launcher's own end condition: demand all issued and every rider home; otherwise the scenario's duration (or
    `limit_real` real seconds, whichever comes first). Returns "all_home" or "cap"."""
    t_end = time.monotonic() + limit_real
    cap_sim = sc.duration or math.inf
    while time.monotonic() < t_end and clock.now() < cap_sim:
        r = h.runner
        if r is not None and sc.demand and r.finished() and \
                all(a.trip is None for a in h.agents.values() if a.role == "rider"):
            return "all_home"
        await asyncio.sleep(1.0)
    return "cap"


CHECKPOINT_EVERY = 1800.0          # sim seconds between per-driver checkpoints (the calibration log)


async def sample_drivers(h, out: List[dict], every: float = CHECKPOINT_EVERY) -> None:
    """Every `every` sim seconds append one cumulative row per driver (t, revenue, metres, driving_s, trips, calls, bids,
    wins). Rolling-window numbers are differences between checkpoints, computed offline (tools/calibrate.py)."""
    k = 1
    while True:
        while clock.now() < k * every:
            await asyncio.sleep(0.25)
        for r in h.metrics.driver_records(k * every):
            out.append(dict(r, t=k * every))
        k += 1


async def run_once(sc: Scenario, limit_real: float, checkpoints: Optional[List[dict]] = None,
                   market: Optional[List[tuple]] = None) -> Tuple[Dict[str, float], List[dict], str]:
    clock.reset(sc.speed)
    t0 = time.monotonic()
    async with Harness(sc, start_server=True, viewer=False) as h:
        sampler = asyncio.create_task(sample_drivers(h, checkpoints)) if checkpoints is not None else None
        try:
            ended = await wait_until_done(h, sc, limit_real)
        finally:
            if sampler:
                sampler.cancel()
        row = collect(h, sc, time.monotonic() - t0, ended)
        series = list(h.metrics.history)
        if market is not None:
            market.extend(h.market_log)
    return row, series, ended


def run_limit(sc: Scenario, timeout: float) -> float:
    """Real seconds a run may take: the scenario's duration at its speed, plus setup (or the user's --timeout)."""
    if timeout:
        return timeout
    return (sc.duration / sc.speed if sc.duration else 3600.0) + 120.0


async def experiment(arms: List[Arm], seeds: List[int], repeats: int, speed: Optional[float], timeout: float,
                     out_dir: Path, progress=print) -> Dict[str, List[dict]]:
    series_dir = out_dir / "series"
    series_dir.mkdir(parents=True, exist_ok=True)
    first = arms[0].scenario_for(seeds[0], speed)
    server = Harness(first, viewer=False)                    # one XMPP server for the whole experiment
    await server.ensure_server()
    out: Dict[str, List[dict]] = {a.name: [] for a in arms}
    total, k = len(arms) * len(seeds) * repeats, 0
    try:
        for rep in range(repeats):
            for seed in seeds:
                for arm in arms:                                   # interleaved: slow drift hits every arm alike
                    k += 1
                    tag = f"[{k}/{total}] arm={arm.name} seed={seed} repeat={rep}"
                    t0 = time.time()
                    try:
                        sc = arm.scenario_for(seed, speed)
                        limit = run_limit(sc, timeout)
                        cps: List[dict] = []
                        mkt: List[tuple] = []
                        row, series, ended = await asyncio.wait_for(run_once(sc, limit, cps, mkt), limit + 120.0)
                    except asyncio.TimeoutError:
                        progress(f"{tag}: TIMED OUT (run skipped)")
                        continue
                    except Exception as e:                         # a crashed run is reported, not hidden
                        progress(f"{tag}: FAILED {type(e).__name__}: {e}")
                        continue
                    row.update(arm=arm.name, seed=seed, repeat=rep, ended_by=ended)
                    out[arm.name].append(row)
                    (series_dir / f"{arm.name}_s{seed}_r{rep}.json").write_text(json.dumps(series))
                    write_driver_checkpoints(out_dir / "drivers" / f"{arm.name}_s{seed}_r{rep}.csv", cps)
                    if mkt:
                        write_market_log(out_dir / "market" / f"{arm.name}_s{seed}_r{rep}.csv", mkt)
                    progress(f"{tag}: {ended}, wait {row['wait_mean']:.0f}s (max {row['wait_max']:.0f}s), "
                             f"profit/trip {row['profit_per_trip']:.2f}, unfinished {row['unfinished']:.0f}, "
                             f"{time.time() - t0:.0f}s real")
    finally:
        await server._stop_server()
    return out


# ---- output -----------------------------------------------------------------------------------------------------------------

META = ("arm", "seed", "repeat", "ended_by")


def normalise(runs: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """Make every row have every column: an absent count (a refusal reason that never happened) is 0, anything else nan."""
    cols = {k for v in runs.values() for r in v for k in r}
    for v in runs.values():
        for r in v:
            for c in cols:
                if c not in r:
                    r[c] = 0.0 if c.startswith(COUNT_PREFIXES) else math.nan
    return runs


def metric_names(runs: Dict[str, List[dict]]) -> List[str]:
    return sorted({k for v in runs.values() for r in v for k in r} - set(META))


def write_driver_checkpoints(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["t", "driver", "revenue", "trips", "metres", "driving_s", "hours", "calls", "bids", "wins"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def write_market_log(path: Path, events: Sequence[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "event", "driver", "fleet_after"])
        w.writerows(events)


def write_runs_csv(path: Path, runs: Dict[str, List[dict]]) -> None:
    cols = list(META) + metric_names(runs)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for v in runs.values():
            for r in v:
                w.writerow([("" if isinstance(r.get(c), float) and math.isnan(r[c]) else r.get(c, "")) for c in cols])


def summary_rows(arms: List[str], runs: Dict[str, List[dict]]) -> List[dict]:
    """One row per metric and arm: n, mean, ci95, sd, min, max; for the arms after the first also the paired difference
    to the first arm (same seed and repeat), its CI, the pair count and whether the CI excludes 0."""
    rows = []
    first = arms[0]
    for m in metric_names(runs):
        for a in arms:
            vals = [r[m] for r in runs[a] if not math.isnan(r.get(m, math.nan))]
            mean, ci = mean_ci(vals)
            row = dict(metric=m, arm=a, n=len(vals), mean=mean, ci95=ci,
                       sd=statistics.stdev(vals) if len(vals) > 1 else math.nan,
                       min=min(vals, default=math.nan), max=max(vals, default=math.nan),
                       diff_vs_first=math.nan, diff_ci95=math.nan, diff_n=0, diff_significant="")
            if a != first:
                d, h, n = paired_difference(runs[first], runs[a], m)
                row.update(diff_vs_first=d, diff_ci95=h, diff_n=n, diff_significant="yes" if significant(d, h) else "no")
            rows.append(row)
    return rows


def write_summary_csv(path: Path, rows: List[dict]) -> None:
    cols = ["metric", "arm", "n", "mean", "ci95", "sd", "min", "max", "diff_vs_first", "diff_ci95", "diff_n",
            "diff_significant"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if isinstance(r[c], float) and math.isnan(r[c]) else r[c] for c in cols])


def fmt(m: float, h: float, digits: int = 1) -> str:
    if math.isnan(m):
        return "n/a"
    return f"{m:.{digits}f}" + ("" if math.isnan(h) else f" ±{h:.{digits}f}")


def digits_for(value: float) -> int:
    if math.isnan(value):
        return 1
    a = abs(value)
    return 0 if a >= 100 else 1 if a >= 10 else 2 if a >= 1 else 3


def report(arms: List[str], runs: Dict[str, List[dict]], metrics: Sequence[str] = KEY_METRICS) -> str:
    """The terminal table: mean ±95 % CI per arm and, below, the paired difference to the first arm (* = CI excludes 0)."""
    first = arms[0]
    lines = [f"\n== {sum(len(v) for v in runs.values())} runs; mean ±95% CI over runs; 'vs {first}' = paired difference "
             f"(same seed+repeat), * = CI excludes 0 ==\n",
             f"{'metric':<24}" + "".join(f"{a:>24}" for a in arms) + f"{'noise (sd of repeats)':>24}"]
    have = set(metric_names(runs))
    for m in metrics:
        if m not in have:
            continue
        means = {a: mean_ci([r[m] for r in runs[a] if not math.isnan(r[m])]) for a in arms}
        digits = digits_for(next((v[0] for v in means.values() if not math.isnan(v[0])), math.nan))
        row = f"{m:<24}" + "".join(f"{fmt(*means[a], digits):>24}" for a in arms)
        noise = [x for x in (repeat_noise(runs[a], m) for a in arms) if not math.isnan(x)]
        row += f"{(f'{sum(noise) / len(noise):.{digits}f}' if noise else 'n/a'):>24}"
        lines.append(row)
        for a in arms[1:]:
            d, h, n = paired_difference(runs[first], runs[a], m)
            if n:
                star = "*" if significant(d, h) else " "
                lines.append(f"{'':<24}  vs {first}: {a} {d:+.{digits}f} ±{'n/a' if math.isnan(h) else f'{h:.{digits}f}'} "
                             f"(n={n}){star}")
    return "\n".join(lines)


# ---- command line --------------------------------------------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", action="append", default=[], help="name=scenario.json[,dotted.key=value,...]  (repeatable; "
                                                                "the first is the baseline of the paired differences)")
    ap.add_argument("--plan", default="", help="a JSON plan file instead of (or with) --arm")
    ap.add_argument("--seeds", default="", help="comma-separated demand seeds (default 1,2,3)")
    ap.add_argument("--repeats", type=int, default=0, help="runs per (arm, seed) (default 1)")
    ap.add_argument("--speed", type=float, default=0.0, help="clock speed for every arm (default 15)")
    ap.add_argument("--timeout", type=float, default=0.0, help="real seconds before a run is cut off "
                                                               "(default: scenario duration / speed + 120)")
    ap.add_argument("--out", default="", help="output folder (default experiments/<date-time>)")
    ap.add_argument("--dry-run", action="store_true", help="check the arms and print the plan, run nothing")
    a = ap.parse_args(argv)

    arms: List[Arm] = []
    opts: dict = {}
    if a.plan:
        arms, opts = load_plan(a.plan)
    arms += [parse_arm(x) for x in a.arm]
    if not arms:
        ap.error("give at least one --arm (the first is the baseline) or a --plan file")
    names = [x.name for x in arms]
    if len(set(names)) != len(names):
        ap.error("arm names must be unique")
    seeds = [int(s) for s in (a.seeds or ",".join(str(s) for s in opts.get("seeds", [1, 2, 3]))).split(",") if s]
    repeats = a.repeats or int(opts.get("repeats", 1))
    speed = a.speed or float(opts.get("speed", 15.0))
    timeout = a.timeout or float(opts.get("timeout", 0.0))

    try:
        scs = [(x, x.scenario_for(seeds[0], speed)) for x in arms]            # validates every arm before running anything
    except ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    total = len(arms) * len(seeds) * repeats
    est = sum(run_limit(sc, timeout) for _, sc in scs) * len(seeds) * repeats / 3600.0
    print(f"{len(arms)} arm(s) x {len(seeds)} seed(s) x {repeats} repeat(s) = {total} runs at clock speed {speed:g}; "
          f"at most {est:.1f} h if every run used its whole cap (they usually end when every rider is home)")
    for x, sc in scs:
        print(f"  {x.name}: {x.scenario} {x.overrides or ''} (duration cap {("%g s" % sc.duration) if sc.duration else "none, ends when every rider is home (1 h real-time cutoff)"})")
    if a.dry_run:
        return 0

    out_dir = Path(a.out) if a.out else Path("experiments") / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(
        {"arms": [dict(name=x.name, scenario=x.scenario, set=x.overrides) for x in arms], "seeds": seeds,
         "repeats": repeats, "speed": speed, "timeout": timeout}, indent=1))
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("spade.Agent").setLevel(logging.ERROR)
    runs = asyncio.run(experiment(arms, seeds, repeats, speed, timeout, out_dir))
    if not any(runs.values()):
        print("no run finished", file=sys.stderr)
        return 1
    runs = normalise(runs)
    write_runs_csv(out_dir / "runs.csv", runs)
    write_summary_csv(out_dir / "summary.csv", summary_rows(names, runs))
    print(report(names, runs))
    bad = [r for v in runs.values() for r in v if r.get("money_conserved") == 0.0]
    if bad:
        print(f"\nWARNING: money was NOT conserved in {len(bad)} run(s)")
    capped = [r for v in runs.values() for r in v if r.get("ended_by_cap") == 1.0]
    if capped:
        print(f"\nNOTE: {len(capped)} run(s) ended at the cap, not with every rider home (see the 'unfinished' column)")
    print(f"\nall {len(metric_names(runs))} metrics per run: {out_dir / 'runs.csv'}\n"
          f"mean / CI / paired differences per metric and arm: {out_dir / 'summary.csv'}\n"
          f"viewer time series: {out_dir / 'series'}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

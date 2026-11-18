"""
src/harness/experiment.py   A/B experiments over many seeds and repeats, with confidence intervals.

One simulation run is NOT reproducible (message timing is asynchronous: the same seed can give mean
pickup times 90 s apart), so a single run per setting says almost nothing. This runner executes every
"arm" on several demand seeds, several times each, and reports each metric as a mean with a 95 %
confidence interval, plus the paired difference against the first arm (same seed and repeat index) and
the noise between repeats of the same seed.

    uv run python -m harness.experiment \
        --arm base --arm brains:brains=true --arm shortwin:t_pool=60 \
        --seeds 1,2,3 --repeats 2 --riders 24 --drivers 16 --span 1800 --out results.json

An arm is `name` or `name:key=value,key=value`; keys are the demo's options (`brains`, `t_pool`,
`riders`, `drivers`, `demand`, `hub_policy`, `legacy_market`, `solo_share`, `span`, `speed`, ...).
Everything not set by an arm comes from the shared options or the demo defaults.
"""
import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
import time
from typing import Dict, List, Optional, Sequence

from harness.demo_simulation import build_parser, run
from harness.launcher import Harness
from harness.scenario import Scenario
from harness.metrics import PHASES, summarize

# two-sided 95 % Student-t critical values by degrees of freedom (1..30); beyond that ~1.96
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]

METRICS = ("completion", "wait_mean", "wait_median", "wait_p90", *(f"{p}_mean" for p in PHASES),
           "pooled_share", "cruises_per_ride", "markup_changes_per_ride", "fare_mean", "monopoly_share", "price_over_reserve")


# ---- statistics (pure, unit-tested) ---------------------------------------------------------------------

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
        if not math.isnan(r[metric]):
            by_seed.setdefault(r["seed"], []).append(r[metric])
    sds = [statistics.stdev(v) for v in by_seed.values() if len(v) > 1]
    return sum(sds) / len(sds) if sds else math.nan


def paired_difference(runs_a: List[dict], runs_b: List[dict], metric: str):
    """mean(b - a) over runs with the same (seed, repeat), its CI half-width and the pair count."""
    a = {(r["seed"], r["repeat"]): r[metric] for r in runs_a}
    diffs = [r[metric] - a[(r["seed"], r["repeat"])] for r in runs_b
             if (r["seed"], r["repeat"]) in a and not math.isnan(r[metric]) and not math.isnan(a[(r["seed"], r["repeat"])])]
    m, h = mean_ci(diffs)
    return m, h, len(diffs)


def significant(mean: float, half: float) -> bool:
    return not math.isnan(half) and abs(mean) > half


def pct(values: Sequence[float], f: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(f * len(v)))] if v else math.nan


def run_metrics(res: dict) -> Dict[str, float]:
    """One run's raw result -> the scalar metrics we compare."""
    nan = math.nan
    waits, n_done = res["waits"], res["completed"]
    out = {"completion": n_done / res["requested"] if res["requested"] else nan,
           "wait_mean": sum(waits) / len(waits) if waits else nan,
           "wait_median": statistics.median(waits) if waits else nan,
           "wait_p90": pct(waits, 0.9),
           "pooled_share": res["pooled_riders"] / n_done if n_done else nan,
           "cruises_per_ride": (res["cruises"] + res.get("returns", 0)) / n_done if n_done else nan,   # brain cruises + fixed returns
           "markup_changes_per_ride": res.get("markup_changes", 0) / n_done if n_done else nan,
           "fare_mean": sum(res["fares"]) / len(res["fares"]) if res["fares"] else nan,
           "monopoly_share": res["single_bid"] / res["matched"] if res["matched"] else nan,
           "price_over_reserve": (sum(res["price_over_reserve"]) / len(res["price_over_reserve"])
                                  if res["price_over_reserve"] else nan)}
    phases = summarize(res["breakdowns"])
    for p in PHASES:
        out[f"{p}_mean"] = phases[p][0] if phases else nan
    return out


def parse_arm(text: str, base) -> argparse.Namespace:
    """`name[:k=v,k=v]` -> a copy of `base` with the overrides (values coerced to the option's type)."""
    name, _, opts = text.partition(":")
    ns = argparse.Namespace(**vars(base))
    ns.arm = name
    for item in filter(None, opts.split(",")):
        k, eq, v = item.partition("=")
        if not eq or not hasattr(ns, k):
            raise SystemExit(f"arm {name!r}: unknown option {item!r}; options: {sorted(vars(base))}")
        cur = getattr(ns, k)
        if isinstance(cur, bool):
            val = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(v)
        elif isinstance(cur, float):
            val = float(v)
        else:
            val = v
        setattr(ns, k, val)
    return ns


# ---- reporting -------------------------------------------------------------------------------------------------

def fmt(m: float, h: float, digits: int = 0) -> str:
    if math.isnan(m):
        return "n/a"
    return f"{m:.{digits}f}" + ("" if math.isnan(h) else f" ±{h:.{digits}f}")


def report(arms: List[str], runs: Dict[str, List[dict]], metrics: Sequence[str] = METRICS) -> str:
    lines = []
    first = arms[0]
    lines.append(f"\n== {sum(len(v) for v in runs.values())} runs; mean ±95% CI over runs; "
                 f"'vs {first}' = paired difference (same seed+repeat), * = CI excludes 0 ==\n")
    head = f"{'metric':<20}" + "".join(f"{a:>22}" for a in arms) + f"{'noise (sd of repeats)':>24}"
    lines.append(head)
    for m in metrics:
        digits = 2 if m in ("completion", "pooled_share", "monopoly_share", "price_over_reserve", "cruises_per_ride",
                            "fare_mean") else 0
        row = f"{m:<20}"
        for a in arms:
            mm, hh = mean_ci([r[m] for r in runs[a] if not math.isnan(r[m])])
            row += f"{fmt(mm, hh, digits):>22}"
        pooled_noise = [repeat_noise(runs[a], m) for a in arms]
        pooled_noise = [x for x in pooled_noise if not math.isnan(x)]
        row += f"{(f'{sum(pooled_noise) / len(pooled_noise):.{digits}f}' if pooled_noise else 'n/a'):>24}"
        lines.append(row)
        for a in arms[1:]:
            d, h, n = paired_difference(runs[first], runs[a], m)
            if n:
                star = "*" if significant(d, h) else " "
                lines.append(f"{'':<20}  vs {first}: {a} {d:+.{digits}f} ±{'n/a' if math.isnan(h) else f'{h:.{digits}f}'} "
                             f"(n={n}){star}")
    return "\n".join(lines)


# ---- driver ------------------------------------------------------------------------------------------------------------

async def experiment(arms: List[argparse.Namespace], seeds: List[int], repeats: int,
                     progress=print, run_timeout: float = 0.0) -> Dict[str, List[dict]]:
    # one XMPP server for the whole experiment (each run finds the port open and leaves it alone)
    server = Harness(Scenario.load())
    await server.ensure_server()
    try:
        return await _experiment(arms, seeds, repeats, progress, run_timeout)
    finally:
        await server._stop_server()


async def _experiment(arms, seeds, repeats, progress, run_timeout) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {a.arm: [] for a in arms}
    total, k = len(arms) * len(seeds) * repeats, 0
    for rep in range(repeats):
        for seed in seeds:
            for arm in arms:                                   # interleaved: slow drift hits every arm alike
                k += 1
                ns = argparse.Namespace(**vars(arm))
                ns.seed, ns.quiet = seed, True
                t0 = time.time()
                try:
                    # a normal run takes ~span/speed seconds of real time (+ setup); give up well beyond that
                    limit = run_timeout or ns.span / ns.speed * 4 + 240
                    res = await asyncio.wait_for(run(ns), limit)
                except asyncio.TimeoutError:
                    progress(f"[{k}/{total}] arm={arm.arm} seed={seed} repeat={rep}: TIMED OUT after {limit:.0f}s "
                             f"(run skipped)")
                    continue
                except Exception as e:                         # a crashed run is reported, not hidden
                    progress(f"[{k}/{total}] arm={arm.arm} seed={seed} repeat={rep}: FAILED {type(e).__name__}: {e}")
                    continue
                row = run_metrics(res)
                row.update(seed=seed, repeat=rep, arm=arm.arm, conserved=res["conserved"])
                out[arm.arm].append(row)
                progress(f"[{k}/{total}] arm={arm.arm} seed={seed} repeat={rep}: "
                         f"wait {row['wait_mean']:.0f}s (pickup {row['pickup_mean']:.0f}s), "
                         f"completed {res['completed']}/{res['requested']}, {time.time() - t0:.0f}s")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    base_parser = build_parser()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], parents=[base_parser], add_help=True,
                                 conflict_handler="resolve")
    ap.add_argument("--arm", action="append", default=[], help="name[:key=value,...]  (repeatable; first = baseline)")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--run-timeout", type=float, default=0.0,
                    help="real seconds before a single run is abandoned (default: 4 x span/speed + 240)")
    ap.add_argument("--out", default="", help="write the raw per-run metrics to this JSON file")
    a = ap.parse_args(argv)
    if len(a.arm) < 1:
        ap.error("give at least one --arm (the first is the baseline), e.g. --arm base --arm brains:brains=true")
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("spade.Agent").setLevel(logging.ERROR)
    arms = [parse_arm(x, a) for x in a.arm]
    names = [x.arm for x in arms]
    if len(set(names)) != len(names):
        ap.error("arm names must be unique")
    seeds = [int(s) for s in a.seeds.split(",") if s]
    runs = asyncio.run(experiment(arms, seeds, a.repeats, run_timeout=a.run_timeout))
    print(report(names, {n: runs[n] for n in names}))
    bad = [r for v in runs.values() for r in v if not r["conserved"]]
    if bad:
        print(f"\nWARNING: money was NOT conserved in {len(bad)} run(s)")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(runs, f, indent=1)
        print(f"\nraw per-run metrics written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

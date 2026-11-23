"""Calibration numbers from the per-driver checkpoint CSVs (experiment output `drivers/*.csv`).

    python tools/calibrate.py experiments/<run>/drivers            # all CSVs in the folder
    python tools/calibrate.py a.csv b.csv --window 7200

Each CSV has cumulative rows (t, driver, revenue, trips, metres, driving_s, hours, calls, bids, wins) every 1800 sim s.
For every driver and every window (default 2 h = 4 checkpoints, sliding by one checkpoint) the rolling values are differences of
cumulative rows:
    net_per_hour   = (revenue - COST_PER_KM * km) / hours      gross_per_hour = revenue / hours
    rolling_surplus = (revenue - COST_PER_KM * km) - WAGE * hours      (euros over the window)
    win_rate       = wins / bids        trips_per_hour
The suggested S* is the P75 of rolling_surplus; use calibration seeds different from the evaluation seeds."""
import argparse
import csv
import statistics
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common.settings import DRIVER_COST_PER_KM, DRIVER_WAGE_TARGET_PER_HOUR  # noqa: E402

FIELDS = ("revenue", "trips", "metres", "driving_s", "calls", "bids", "wins")


def load_rows(paths: Iterable[Path]) -> List[dict]:
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                d = {"file": str(p), "driver": r["driver"], "t": float(r["t"])}
                d.update({k: float(r[k]) for k in FIELDS})
                rows.append(d)
    return rows


def rolling_windows(rows: List[dict], window: float = 7200.0, wage: float = DRIVER_WAGE_TARGET_PER_HOUR,
                    per_km: float = DRIVER_COST_PER_KM) -> List[dict]:
    """One record per (file, driver, window end). Windows are `window` sim seconds long, ending at each checkpoint t >= window;
    the window start is the checkpoint exactly `window` earlier (cumulative rows are differenced)."""
    by: Dict[tuple, Dict[float, dict]] = {}
    for r in rows:
        by.setdefault((r["file"], r["driver"]), {})[r["t"]] = r
    out = []
    for (f, d), pts in by.items():
        for t, end in sorted(pts.items()):
            start = pts.get(t - window)
            if start is None and abs(t - window) < 1e-6:           # the run's start: everything is still zero
                start = {k: 0.0 for k in FIELDS}
            if start is None:
                continue
            diff = {k: end[k] - start[k] for k in FIELDS}
            hours = window / 3600.0
            net = diff["revenue"] - per_km * diff["metres"] / 1000.0
            out.append(dict(file=f, driver=d, t=t, hours=hours, net=net, gross=diff["revenue"],
                            net_per_hour=net / hours, gross_per_hour=diff["revenue"] / hours,
                            surplus=net - wage * hours, trips_per_hour=diff["trips"] / hours,
                            win_rate=(diff["wins"] / diff["bids"]) if diff["bids"] > 0 else None,
                            bids=diff["bids"], calls=diff["calls"]))
    return out


def quantile(xs: List[float], q: float) -> Optional[float]:
    """Linear-interpolation quantile (same convention as numpy's default)."""
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarise(windows: List[dict]) -> Dict[str, Optional[float]]:
    s = [w["surplus"] for w in windows]
    wr = [w["win_rate"] for w in windows if w["win_rate"] is not None]
    out: Dict[str, Optional[float]] = {"n_windows": float(len(windows))}
    for name, xs in (("surplus", s), ("net_per_hour", [w["net_per_hour"] for w in windows]),
                     ("gross_per_hour", [w["gross_per_hour"] for w in windows]),
                     ("trips_per_hour", [w["trips_per_hour"] for w in windows]), ("win_rate", wr)):
        out[f"{name}_median"] = quantile(xs, 0.5)
        out[f"{name}_p25"] = quantile(xs, 0.25)
        out[f"{name}_p75"] = quantile(xs, 0.75)
    out["S_star_suggested"] = out["surplus_p75"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="drivers/*.csv files or a folder of them")
    ap.add_argument("--window", type=float, default=7200.0, help="window length in sim seconds (default 7200)")
    a = ap.parse_args(argv)
    files: List[Path] = []
    for p in map(Path, a.paths):
        files += sorted(p.glob("*.csv")) if p.is_dir() else [p]
    w = rolling_windows(load_rows(files), a.window)
    if not w:
        print("no complete window (runs shorter than the window?)")
        return 1
    for k, v in summarise(w).items():
        print(f"{k:22s} {'n/a' if v is None else f'{v:10.3f}'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import calibrate  # noqa: E402

HDR = ["t", "driver", "revenue", "trips", "metres", "driving_s", "hours", "calls", "bids", "wins"]


def write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(HDR); w.writerows(rows)


def test_rolling_window_hand_computed(tmp_path):
    p = tmp_path / "a.csv"
    # one driver, cumulative every 1800 s; window 7200 = checkpoints 0 and 7200 -> need t=0 row too
    write(p, [[1800, "d1", 2, 1, 5000, 600, 0.5, 4, 3, 1],
              [3600, "d1", 5, 2, 10000, 1200, 1, 8, 6, 2],
              [5400, "d1", 7, 3, 15000, 1800, 1.5, 12, 9, 3],
              [7200, "d1", 10, 4, 20000, 2400, 2, 16, 12, 4],
              [9000, "d1", 11, 4, 22000, 2500, 2.5, 18, 13, 4]])
    w = calibrate.rolling_windows(calibrate.load_rows([p]), 7200.0, wage=4.96, per_km=0.15)
    assert [x["t"] for x in w] == [7200.0, 9000.0]      # no t=0 row: the start is taken as all zeros
    a = w[0]                      # revenue 10, 20 km -> net 10-3 = 7, minus 4.96*2 = 9.92 -> -2.92
    assert abs(a["net"] - 7.0) < 1e-9 and abs(a["surplus"] - (7.0 - 9.92)) < 1e-9
    assert abs(a["net_per_hour"] - 3.5) < 1e-9 and abs(a["win_rate"] - 4 / 12) < 1e-9
    assert abs(a["trips_per_hour"] - 2.0) < 1e-9
    b = w[1]                      # from 1800 to 9000: revenue 9, 17 km -> 9-2.55=6.45 ; wins 3 of 10
    assert abs(b["net"] - 6.45) < 1e-9 and abs(b["win_rate"] - 0.3) < 1e-9


def test_quantile_and_summary():
    assert calibrate.quantile([1, 2, 3, 4], 0.5) == 2.5
    assert calibrate.quantile([1, 2, 3, 4, 5], 0.75) == 4
    assert calibrate.quantile([], 0.5) is None
    s = calibrate.summarise([dict(surplus=x, net_per_hour=0, gross_per_hour=0, trips_per_hour=0, win_rate=None)
                             for x in (1, 2, 3, 4, 5)])
    assert s["S_star_suggested"] == 4 and s["win_rate_median"] is None


def test_checkpoint_csv_roundtrip(tmp_path):
    from harness.experiment import write_driver_checkpoints
    p = tmp_path / "x" / "r.csv"
    write_driver_checkpoints(p, [dict(t=1800.0, driver="d1", revenue=1.0, trips=1, metres=100.0, driving_s=10.0,
                                      hours=0.5, calls=2, bids=2, wins=1)])
    rows = calibrate.load_rows([p])
    assert rows[0]["wins"] == 1.0 and rows[0]["t"] == 1800.0

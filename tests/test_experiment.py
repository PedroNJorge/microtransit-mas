"""Experiment runner: statistics, arm parsing and aggregation (no simulation is run)."""
import math

import pytest

from harness.demo_simulation import build_parser
from harness.experiment import (mean_ci, paired_difference, parse_arm, repeat_noise, report, run_metrics,
                                significant)


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


def test_parse_arm_coerces_types():
    base = build_parser().parse_args([])
    ns = parse_arm("b:brains=true,t_pool=60,demand=uniform,solo_share=0.5", base)
    assert ns.arm == "b" and ns.brains is True and ns.t_pool == 60.0 and ns.demand == "uniform" and ns.solo_share == 0.5
    assert parse_arm("plain", base).brains is False
    with pytest.raises(SystemExit):
        parse_arm("x:nonsense=1", base)


def test_run_metrics_and_report():
    res = dict(requested=4, completed=4, waits=[100, 200, 300, 400], pooled_riders=2, cruises=8,
               fares=[3.0, 5.0], matched=4, single_bid=1, price_over_reserve=[0.5, 0.7], breakdowns=[])
    m = run_metrics(res)
    assert m["wait_mean"] == 250 and m["pooled_share"] == 0.5 and m["cruises_per_ride"] == 2
    assert m["monopoly_share"] == 0.25 and m["completion"] == 1.0
    runs = {"a": [dict(m, seed=s, repeat=0) for s in (1, 2)], "b": [dict(m, seed=s, repeat=0, wait_mean=200.0) for s in (1, 2)]}
    txt = report(["a", "b"], runs, metrics=("wait_mean",))
    assert "wait_mean" in txt and "vs a: b -50" in txt

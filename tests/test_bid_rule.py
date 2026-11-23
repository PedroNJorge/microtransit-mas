import math

import pytest

from agents.bid_rule import time_value, window_surplus
from agents.driver import DriverAgent

W, KM = 4.96, 0.15


def test_time_value_is_wage_behind_zero_when_ahead_and_linear_between():
    assert time_value(-3.0, W, 5.0) == W
    assert time_value(0.0, W, 5.0) == W
    assert time_value(5.0, W, 5.0) == 0.0
    assert time_value(9.0, W, 5.0) == 0.0
    assert math.isclose(time_value(2.5, W, 5.0), W / 2)
    assert time_value(0.0, W, 0.0) == W and time_value(0.01, W, 0.0) == 0.0     # scale 0: hard step


def test_window_surplus_hand_computed_three_trips():
    # now = 3 h, window 2 h -> counts events with 3600 < t <= 10800; hours = 2
    pays = [(1000, 9.0), (4000, 2.0), (7000, 3.0), (10000, 1.5)]       # the first is out of the window
    kms = [(1000, 30000.0), (4000, 6000.0), (7000, 10000.0), (10000, 4000.0)]
    s = window_surplus(10800, 7200, W, KM, pays, kms)
    # revenue 2+3+1.5 = 6.5 ; km 20 -> 3.00 ; wage 4.96*2 = 9.92  -> 6.5 - 3 - 9.92
    assert math.isclose(s, 6.5 - 3.0 - 9.92)
    # a young run: 1 h in, everything counts, hours = 1
    assert math.isclose(window_surplus(3600, 7200, W, KM, [(100, 5.0)], [(100, 2000.0)]), 5.0 - 0.3 - 4.96)


def test_bid_price_with_the_rule_on_and_off():
    off = DriverAgent("d@x", "p", start_hub="h")
    on = DriverAgent("d@x", "p", start_hub="h", surplus_scale=5.0)
    assert off.time_price(0.0) == off.cost_per_second
    assert math.isclose(on.time_price(1000.0), W / 3600.0)                 # nothing earned yet: behind -> full wage
    on.payouts = [type("S", (), {"driver_payout": 20.0})()]
    on.payout_times = [900.0]
    # now = 3600: revenue 20, no km, wage 4.96 -> surplus 15.04 >= 5 -> time is free, only the km floor remains
    assert on.time_price(3600.0) == 0.0
    # partly ahead: add 7.04 EUR of km cost-free revenue gap -> surplus 2.5 -> half the wage
    on.payouts[0].driver_payout = 4.96 + 2.5
    assert math.isclose(on.time_price(3600.0), (W / 2) / 3600.0)


def test_a_trip_still_in_progress_does_not_break_the_rolling_surplus():
    d = DriverAgent("d@x", "p", start_hub="h", surplus_scale=5.0)
    d.trip_log = {"p1": {"pickup_m": 100.0, "loaded_m": 0.0, "done": None},      # running: no end time yet
                  "p2": {"pickup_m": 1000.0, "loaded_m": 2000.0, "done": 1800.0}}
    assert math.isclose(d.rolling_surplus(3600.0), -0.15 * 3.0 - W)             # only the finished trip's 3 km, 1 h on duty
    assert d.rolling_net(3600.0) == pytest.approx((-0.45, 1.0))

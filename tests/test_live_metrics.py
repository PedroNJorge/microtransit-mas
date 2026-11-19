"""Live metrics: the definitions, on a small hand-made run."""
import math
from types import SimpleNamespace as NS

import pytest

from agents.microhub import PoolMetric
from common.costs import driver_cost
from common.ontology import Settlement
from harness.live_metrics import MetricsCollector, gini, percentile


def jid(name):
    return NS(bare=name)


def build():
    """One pool of 2 riders (one joined late) and one solo pool, two drivers, one hub, payment."""
    a1 = NS(dropped=["r1", "r2"], riders=[("r1", "q1", "n1", "p1"), ("r2", "q2", "n2", "p2")])
    a2 = NS(dropped=["r3"], riders=[("r3", "q3", "n3", "p3")])
    d1 = NS(role="driver", jid=jid("d1"), activity="on_trip", odo_s={"loaded": 300.0, "pickup": 100.0, "other": 100.0},
            odo_sub_m={"return": 300.0, "move": 200.0, "reposition": 0.0}, finished_trips={"p1": a1}, odo_m={"loaded": 3000.0, "pickup": 1000.0, "other": 500.0},
            trip_log={"p1": dict(pickup_m=1000.0, loaded_m=3000.0, pickup_s=100.0, loaded_s=300.0, done=500.0)},
            payouts=[Settlement("e1", "d1", 6.0, 0.0), Settlement("e2", "d1", 4.0, 1.0, 0.0, 0.0, 2.0, 0.5)], payout_times=[510.0, 510.0])
    d2 = NS(role="driver", jid=jid("d2"), activity="idle", finished_trips={"p3": a2}, odo_m={"loaded": 2000.0, "pickup": 500.0, "other": 0.0},
            trip_log={"p3": dict(pickup_m=500.0, loaded_m=2000.0, pickup_s=50.0, loaded_s=200.0, done=400.0)},
            payouts=[Settlement("e3", "d2", 5.0)], payout_times=[410.0])
    # pool p1: riders alone would drive 2000 + 2000 = 4000 m, shared 3000 m
    hub = NS(role="microhub", emission_g_per_km=150.0, queue=[1, 2], adopted=["x"], finished=[NS(attempt=0), NS(attempt=2)],
             pool_metrics=[PoolMetric("p1", 2, 3000.0, 4000.0), PoolMetric("p3", 1, 2000.0, 2000.0)],
             idle_driver_count=lambda now: 1)
    ledger = NS(subsidy_spent=1.0, pooling_spent=0.5)
    pay = NS(role="payment", ledger=ledger)

    def trip(rid, requested, boarded, dropped, matched, settle):
        req = NS(origin_node="h", destination_home="n")
        return NS(outcome="completed", dropped_at=dropped, boarded_at=boarded, requested_at=requested, matched_at=matched,
                  settlement=settle, fare=0.0, request=req, reason="")
    net = NS(travel_time=lambda a, b, t: 120.0)
    riders = [NS(role="rider", network=net, trips=[trip("r1", 0, 300, 700, 100, d1.payouts[0])]),
              NS(role="rider", network=net, trips=[trip("r2", 200, 400, 710, 350, d1.payouts[1])]),
              NS(role="rider", network=net, trips=[trip("r3", 0, 200, 420, 50, d2.payouts[0]),
                                                    NS(outcome="failed", reason="NO_MATCH", dropped_at=0, request=None)])]
    agents = {x: a for x, a in zip("abcdefg", [d1, d2, hub, pay, *riders])}
    return MetricsCollector(NS(agents=agents), window=600.0)


def test_pooling_and_co2_definitions():
    c = build().snapshot(1000.0)["cum"]
    assert c["riders_per_vehicle"] == 1.5                                   # 3 riders / 2 vehicle-trips
    assert c["pooled_share"] == pytest.approx(2 / 3) and c["late_share"] == pytest.approx(1 / 3)
    assert c["size_1"] == 0.5 and c["size_2"] == 0.5
    # loaded leg: (4000 - 3000) m x 150 g/km = 0.15 kg
    assert c["co2_saved_loaded_kg"] == pytest.approx(0.15)
    # all miles: + one extra pickup leg the second rider would have needed alone (1000 m) -> 2000 m = 0.30 kg
    assert c["co2_saved_all_kg"] == pytest.approx(0.30)
    assert c["co2_saved_all_pct"] == pytest.approx(2000 / (4000 + 2 * 1000 + 2000 + 500))
    assert c["pax_km_per_veh_km"] == pytest.approx(6000 / (4000 + 2500))


def test_rider_and_driver_metrics():
    s = build().snapshot(1000.0)
    c = s["cum"]
    assert c["fare_gross"] == pytest.approx(5.0)                            # (6 + 4 + 5) / 3
    assert c["subsidy"] == pytest.approx(1 / 3) and c["incentive"] == pytest.approx(0.5 / 3)
    assert c["wait_mean"] == pytest.approx((300 + 200 + 200) / 3) and c["wait_max"] == 300
    assert c["pooling_discount"] == 2.0
    assert c["completion_rate"] == pytest.approx(3 / 4) and s["failures"] == {"NO_MATCH": 1}
    assert c["revenue_per_trip"] == pytest.approx(7.5)                      # 15 EUR / 2 trips
    cost = (driver_cost(4.0, 400, 35) + driver_cost(2.5, 250, 25)) / 2        # km + hourly wage + boarding/alighting
    assert c["profit_per_trip"] == pytest.approx(7.5 - cost)
    assert c["deadhead_share"] == pytest.approx(2000 / 7000)                # (pickup 1500 + other 500) / all 7000
    assert c["gini"] == pytest.approx(0.0, abs=0.35)
    assert s["now"] == {"queue_len": 2, "idle_drivers": 1, "failover_adopted": 1, "drivers": 2}
    assert c["auction_attempts"] == 2.0 and c["pooling_eur_per_kg"] == pytest.approx(0.5 / 0.15)


def test_rolling_window_only_sees_recent_events():
    m = build()
    late = m.snapshot(1000.0)
    assert late["roll"]["riders_per_vehicle"] == 2.0                        # window (400, 1000]: only pool p1 (ended 500)
    assert late["cum"]["riders_per_vehicle"] == 1.5
    assert build().snapshot(2000.0)["roll"]["riders_per_vehicle"] is None   # window (1400, 2000]: nothing ended
    early = build().snapshot(700.0)["roll"]                                 # window (100, 700]: both pools
    assert early["riders_per_vehicle"] == 1.5


def test_utilisation_is_sampled_per_interval():
    m = build()
    m.snapshot(0.0)
    s = m.snapshot(60.0)["cum"]
    assert s["util_loaded"] == 0.5 and s["util_idle"] == 0.5                # one driver on a trip, one idle


def test_helpers():
    assert gini([1, 1, 1, 1]) == 0 and gini([0, 0, 0, 10]) == pytest.approx(0.75)
    assert gini([5]) is None and percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9 and percentile([], 0.9) is None


def test_empty_km_by_cause_and_revenue_per_hour_driven():
    c = build().snapshot(1000.0)["cum"]
    assert c["km_return"] == pytest.approx(0.3) and c["km_move"] == pytest.approx(0.2) and c["km_reposition"] == 0.0
    assert c["km_return"] + c["km_move"] + c["km_reposition"] == pytest.approx(c["km_other"])
    assert c["earnings_per_driven_hour"] == pytest.approx(15.0 / (500.0 / 3600.0))      # only d1 reports its seconds

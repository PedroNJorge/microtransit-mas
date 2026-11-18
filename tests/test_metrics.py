from types import SimpleNamespace

import pytest

from harness.metrics import PHASES, summarize, wait_breakdown


def trip(**kw):
    base = dict(requested_at=100.0, arrived_at=480.0, boarded_at=500.0)
    base.update(kw)
    return SimpleNamespace(**base)


HIST = [(100.0, "IDLE", "REQUESTING"), (101.0, "REQUESTING", "AWAITING_POOL"), (220.0, "AWAITING_POOL", "DECIDING")]
TIMES = {"queued": 230.0, "auction_start": 240.0, "matched": 260.0}


def test_phases_are_consecutive_and_add_up_to_the_wait():
    b = wait_breakdown(trip(), TIMES, HIST)
    assert b["batch"] == 120 and b["confirm"] == 10 and b["queue"] == 10 and b["auction"] == 20
    assert b["pickup"] == 220 and b["boarding"] == 20
    assert b["total"] == 400 and b["check"] == pytest.approx(0.0)
    assert sum(b[p] for p in PHASES) == b["total"]


def test_repooled_rider_uses_its_last_proposal():
    hist = HIST + [(225.0, "DECIDING", "AWAITING_POOL"), (228.0, "AWAITING_POOL", "DECIDING")]
    assert wait_breakdown(trip(), TIMES, hist)["batch"] == 128


def test_unboarded_or_incomplete_trips_have_no_breakdown():
    assert wait_breakdown(trip(boarded_at=0.0), TIMES, HIST) is None
    assert wait_breakdown(trip(), {"queued": 1.0}, HIST) is None
    assert wait_breakdown(trip(), TIMES, []) is None


def test_driver_already_at_the_pickup_before_the_match_costs_no_negative_time():
    b = wait_breakdown(trip(arrived_at=250.0), TIMES, HIST)        # arrival message beat the MatchResult
    assert b["pickup"] == 0 and b["check"] == pytest.approx(0.0)


def test_summary_shares_add_up():
    rows = [wait_breakdown(trip(), TIMES, HIST), wait_breakdown(trip(boarded_at=600.0, arrived_at=580.0), TIMES, HIST)]
    s = summarize(rows)
    assert sum(s[p][3] for p in PHASES) == pytest.approx(1.0)
    assert s["total"][0] == 450

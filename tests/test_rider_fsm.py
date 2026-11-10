"""Rider state machine (no XMPP): legal transitions, ignoring illegal ones, evaluation and rating policy."""
import pytest

from agents.rider import (
    AWAITING_MATCH, AWAITING_POOL, DECIDING, DRIVER_EN_ROUTE, IDLE, IN_TRIP, RATING, REQUESTING, TRANSITIONS, TripRecord,
    RiderAgent, default_rating,
)
from common import clock
from common.graph import build_grid_network
from common.ontology import MatchResult, PoolProposal, TripRequest


@pytest.fixture(autouse=True)
def _clock():
    clock.reset(); clock.start(speed=10)
    yield
    clock.reset()


def rider(**kw):
    return RiderAgent("r@localhost", "pw", hub_jids={"n1_1": "hub@localhost"}, network=build_grid_network(), **kw)


def record(**kw):
    req = TripRequest("q", "r@localhost", "n1_1", "n2_2", 0.0, 1000.0, 300.0, 10.0, False, False)
    return TripRecord(req, "hub@localhost", **kw)


def test_every_state_can_return_to_idle_except_idle_itself():
    for s, nxt in TRANSITIONS.items():
        assert (IDLE in nxt) == (s != IDLE) or s == IDLE


def test_the_happy_path_is_the_only_forward_path():
    forward = [IDLE, REQUESTING, AWAITING_POOL, DECIDING, AWAITING_MATCH, DRIVER_EN_ROUTE, IN_TRIP, RATING, IDLE]
    for a, b in zip(forward, forward[1:]):
        assert b in TRANSITIONS[a]


def test_illegal_transition_is_ignored_and_state_kept():
    r = rider()
    assert not r.go(IN_TRIP) and r.state == IDLE
    assert r.go(REQUESTING) and not r.go(DECIDING) and r.state == REQUESTING


def test_request_trip_chooses_the_covering_hub_and_blocks_a_second_trip():
    r = rider()
    req = r.request_trip("n2_2", 10.0)
    assert req.origin_node == "n1_1" and req.rider_id == "r@localhost" and r.state == REQUESTING
    assert r.request_trip("n2_2", 10.0) is None


def test_no_covering_hub_means_no_request():
    r = rider()
    assert r.request_trip("n5_5", 10.0) is None and r.state == IDLE


@pytest.mark.parametrize("fare,wait,detour,expected", [
    (10.0, 100.0, 0.0, ""), (10.01, 100.0, 0.0, "fare_too_high"),
    (5.0, 5000.0, 0.0, "wait_too_long"), (5.0, 100.0, 301.0, "detour_too_large"),
])
def test_pool_evaluation(fare, wait, detour, expected):
    r, t = rider(), record()
    assert r.evaluate_pool(t, PoolProposal("p", "q", ["a", "b"], fare, wait, detour)) == expected


def test_default_rating_penalises_a_late_pickup_and_a_window_miss():
    t = record()
    t.match = MatchResult("p", "d@localhost", 5.0, pickup_eta=100.0, rider_fares={})
    t.matched_at, t.boarded_at = 0.0, 150.0
    assert default_rating(t) == 5                               # 50 s late: within tolerance
    t.boarded_at = 400.0
    assert default_rating(t) == 4                               # 300 s late
    t.boarded_at = 1500.0
    assert default_rating(t) == 3                               # and after the window closed


def test_a_match_does_not_start_the_trip_only_boarding_does():
    assert IN_TRIP not in TRANSITIONS[AWAITING_MATCH]            # you cannot be "in trip" straight from the match
    assert TRANSITIONS[DRIVER_EN_ROUTE] == {IN_TRIP, IDLE}

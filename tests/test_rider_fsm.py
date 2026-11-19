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
    assert TRANSITIONS[DRIVER_EN_ROUTE] == {IN_TRIP, AWAITING_POOL, IDLE}    # AWAITING_POOL: a late join fell through


def _refuse_outside_coverage(r):
    """What a REFUSE(outside_coverage) from the rider's hub leads to (no XMPP)."""
    t = r.trip
    t.tried_hubs = ["n1_1"]
    r.retry_after_coverage_refusal(t)
    return t


def test_an_outside_coverage_refusal_ends_the_trip_by_default():
    r = rider()
    r.request_trip("n2_2", 10.0)
    assert r.retry_coverage == 0                       # the plain rider keeps its old behaviour (REFUSED)


def test_after_an_outside_coverage_refusal_a_patient_rider_waits_and_asks_again():
    r = rider(retry_coverage=3600.0)
    r.request_trip("n2_2", 10.0)
    t = _refuse_outside_coverage(r)                    # the only hub it knows already refused: nobody else to ask
    assert r.state == REQUESTING and t.retry_at > clock.now() and t.tried_hubs == []
    assert r._deadline >= 3600.0


def test_after_an_outside_coverage_refusal_a_rider_tries_another_covering_hub():
    r = RiderAgent("r@localhost", "pw", hub_jids={"n1_1": "a@localhost", "n1_4": "b@localhost"},
                   network=build_grid_network(), retry_coverage=3600.0)
    r.request_trip("n1_2", 10.0, hub_id="n1_1")        # n1_2 is covered by both hubs
    t = _refuse_outside_coverage(r)
    assert t.hub_jid == "b@localhost" and t.retry_at == 0.0 and "n1_4" in t.tried_hubs


def test_relative_tolerance_caps_the_request_at_a_share_of_the_solo_ride():
    r = rider(detour_ratio=0.5)
    req = r.request_trip("n2_2", 10.0, detour_tolerance=600.0)
    direct = build_grid_network().travel_time("n1_1", "n2_2")
    assert req.detour_tolerance == pytest.approx(0.5 * direct) and req.detour_tolerance < 600.0


def test_the_cap_still_applies_when_the_ratio_would_allow_more():
    r = rider(detour_ratio=100.0)
    assert r.request_trip("n2_2", 10.0, detour_tolerance=90.0).detour_tolerance == 90.0


def test_without_a_ratio_the_tolerance_is_what_the_request_says():
    assert rider().request_trip("n2_2", 10.0, detour_tolerance=600.0).detour_tolerance == 600.0


def _matched_rider():
    r, t = rider(), record()
    r.trip = t
    r.state = DRIVER_EN_ROUTE
    t.pool_id = "pool-late"
    t.match = MatchResult("pool-late", "d@localhost", 3.0, pickup_eta=100.0, rider_fares={})
    t.matched_at = 10.0
    return r, t


def test_a_late_join_that_falls_through_after_the_match_sends_the_rider_back_to_the_queue():
    from common.ontology import FailureReason, Handover
    r, t = _matched_rider()
    r.on_handover(t, "hub@localhost", Handover("q", "n1_1", "n1_1", "hub@localhost", FailureReason.NO_MATCH))
    assert r.state == AWAITING_POOL and t.match is None and t.pool_id == ""


def test_a_handover_to_another_hub_is_still_ignored_once_a_driver_is_on_the_way():
    from common.ontology import FailureReason, Handover
    r, t = _matched_rider()
    r.hubs["n1_4"] = "other@localhost"
    r.on_handover(t, "other@localhost", Handover("q", "n1_1", "n1_4", "other@localhost", FailureReason.HUB_FAILED))
    assert r.state == DRIVER_EN_ROUTE and t.match is not None

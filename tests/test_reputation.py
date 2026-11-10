"""ReputationAgent logic, no XMPP: scores, bounds, registry matching, idempotence, authorisation."""
import random

import pytest

from agents.reputation import ReputationAgent
from common.ontology import Rating, ReputationScore, TripCompleted

HUB, DRV = "hub@localhost", "drv@localhost"


def agent(**kw):
    return ReputationAgent("rep@localhost", "pw", hub_jids=[HUB], **kw)


def trip(tid="t1", riders=("r1@localhost",), driver=DRV):
    return TripCompleted(tid, "pool-" + tid, driver, list(riders), 0.0)


def rate(a, tid="t1", rider="r1@localhost", score=5, driver=DRV, sender=None):
    return a.on_rating(Rating(tid, rider, driver, score), sender or rider)


def test_unrated_driver_has_no_published_score():
    a = agent()
    assert a.published(DRV) is None and a.score_of(DRV) == 3.0


def test_one_rating_is_shrunk_towards_the_prior():
    a = agent()
    a.on_trip(trip(), HUB)
    out = rate(a, score=5)
    assert a.published(DRV).score == pytest.approx((2 * 3 + 5) / 3, abs=1e-3)
    assert {to for to, _ in out} == {DRV, HUB}                       # ratee and every subscribed hub
    assert all(isinstance(s, ReputationScore) and s.num_ratings == 1 for _, s in out)


def test_min_ratings_gate_publication():
    a = agent(min_ratings=2)
    a.on_trip(trip("t1"), HUB); a.on_trip(trip("t2"), HUB)
    assert rate(a, "t1") == [] and a.published(DRV) is None
    assert rate(a, "t2") != [] and a.published(DRV).num_ratings == 2


def test_rating_before_the_registry_is_parked_then_applied():
    a = agent()
    assert rate(a) == [] and a.count_of(DRV) == 0
    out = a.on_trip(trip(), HUB)
    assert a.count_of(DRV) == 1 and out


def test_same_rater_same_trip_counts_once():
    a = agent()
    a.on_trip(trip(), HUB)
    rate(a, score=5)
    assert rate(a, score=1) == [] and a.count_of(DRV) == 1


def test_only_the_rater_can_send_the_rating():
    a = agent()
    a.on_trip(trip(), HUB)
    assert rate(a, sender="mallory@localhost") == []
    assert a.rejected[-1][1] == "not_authorised" and a.count_of(DRV) == 0


def test_rater_must_have_been_on_the_trip_and_ratee_must_be_its_driver():
    a = agent()
    a.on_trip(trip(), HUB)
    rate(a, rider="stranger@localhost")
    rate(a, driver="other@localhost")
    assert [why for _, why in a.rejected] == ["not_a_participant", "not_a_participant"]
    assert a.count_of(DRV) == 0


def test_registry_from_an_unknown_hub_is_ignored():
    a = agent()
    a.on_trip(trip(), "evil@localhost")
    assert rate(a) == [] and a.count_of(DRV) == 0                    # parked forever: no trip is known


def test_self_rating_is_impossible():
    with pytest.raises(ValueError):
        Rating("t", DRV, DRV, 5)


@pytest.mark.parametrize("seed", range(20))
def test_score_is_always_within_1_and_5_and_monotone_in_ratings(seed):
    rng = random.Random(seed)
    a = agent()
    last = None
    for i in range(30):
        a.on_trip(trip(f"t{i}"), HUB)
        s = rng.randint(1, 5)
        rate(a, f"t{i}", score=s)
        pub = a.published(DRV)
        assert 1.0 <= pub.score <= 5.0 and pub.num_ratings == i + 1
        if last is not None:                     # a higher rating never lowers the score and vice versa
            assert (pub.score - last) * (s - last) >= 0 or s == round(last)
        last = pub.score


def test_many_perfect_ratings_approach_but_never_exceed_5():
    a = agent()
    for i in range(500):
        a.on_trip(trip(f"t{i}"), HUB); rate(a, f"t{i}", score=5)
    assert 4.98 < a.published(DRV).score <= 5.0

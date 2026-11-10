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


# ======================= ID-07: accept early, publish late =======================
from common.ontology import SettlementComplete

PAY = "pay@localhost"


def gated(**kw):
    return ReputationAgent("rep@localhost", "pw", hub_jids=[HUB], payment_jid=PAY, t_settlement_max=600.0, **kw)


def trip_at(a, tid="t1", at=100.0, riders=("r1@localhost",), driver=DRV):
    a.on_trip(TripCompleted(tid, "pool-" + tid, driver, list(riders), at), HUB, now=at)


def settle(a, tid="t1", sender=PAY, now=0.0):
    return a.on_settlement(SettlementComplete("pool-" + tid, DRV, ["e"], 5.0, 0.0, 0.0), sender, now)


def test_rating_before_settlement_is_accepted_but_not_published():
    a = gated(); trip_at(a)
    out = a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert out == [] and a.published(DRV) is None and a.count_of(DRV) == 0 and a.held_count(DRV) == 1


def test_settlement_publishes_the_held_rating_unflagged():
    a = gated(); trip_at(a)
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    out = settle(a, now=120.0)
    assert {to for to, _ in out} == {DRV, HUB}
    s = a.published(DRV)
    assert s.num_ratings == 1 and not s.pending_settlement and s.score == pytest.approx(11 / 3, abs=1e-3)


def test_t_settlement_max_publishes_with_the_pending_flag():
    a = gated(); trip_at(a, at=100.0)
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert a.sweep(now=699.0) == [] and a.published(DRV) is None           # 599 s after completion
    out = a.sweep(now=700.0)                                               # exactly T_settlement_max
    assert out and all(s.pending_settlement for _, s in out) and a.published(DRV).pending_settlement


def test_late_settlement_removes_the_flag_in_a_subsequent_update():
    a = gated(); trip_at(a, at=100.0)
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    a.sweep(now=800.0)
    assert a.published(DRV).pending_settlement
    out = settle(a, now=900.0)
    assert out and not out[0][1].pending_settlement and a.published(DRV).num_ratings == 1   # same rating, flag gone


def test_a_rating_that_arrives_after_the_deadline_is_published_flagged_at_once():
    a = gated(); trip_at(a, at=100.0)
    out = a.on_rating(Rating("t1", "r1@localhost", DRV, 4), "r1@localhost", now=900.0)
    assert out and out[0][1].pending_settlement


def test_a_rating_after_settlement_is_published_at_once():
    a = gated(); trip_at(a); settle(a)
    out = a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=130.0)
    assert out and not out[0][1].pending_settlement


def test_settlement_that_beats_the_trip_registry_still_counts():
    a = gated()
    settle(a)                                              # Payment was faster than the hub
    trip_at(a)
    out = a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert out and not out[0][1].pending_settlement


def test_only_the_trusted_payment_agent_can_settle():
    a = gated(); trip_at(a)
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert settle(a, sender="mallory@localhost") == [] and a.published(DRV) is None


def test_settlement_of_another_pool_does_not_release_the_rating():
    a = gated(); trip_at(a, "t1"); trip_at(a, "t2")
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert settle(a, "t2") == [] and a.published(DRV) is None


def test_published_score_counts_only_published_ratings():
    a = gated(); trip_at(a, "t1"); trip_at(a, "t2")
    a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    a.on_rating(Rating("t2", "r1@localhost", DRV, 1), "r1@localhost", now=110.0)
    settle(a, "t1")
    s = a.published(DRV)
    assert s.num_ratings == 1 and s.score == pytest.approx(11 / 3, abs=1e-3)      # the held 1-star is invisible


def test_driver_rating_of_a_rider_follows_the_same_gate_and_needs_a_registered_trip():     # ID-08
    a = gated()
    early = a.on_rating(Rating("t1", DRV, "r1@localhost", 5), DRV, now=50.0)      # trip not completed yet
    assert early == [] and a.published("r1@localhost") is None
    trip_at(a)                                                                    # registered: counted, still held
    assert a.published("r1@localhost") is None and a.held_count("r1@localhost") == 1
    out = settle(a)
    assert {to for to, _ in out} == {"r1@localhost", HUB} and a.published("r1@localhost").num_ratings == 1


def test_a_rider_cannot_rate_another_rider():
    a = gated(); trip_at(a, riders=("r1@localhost", "r2@localhost"))
    a.on_rating(Rating("t1", "r1@localhost", "r2@localhost", 1), "r1@localhost", now=110.0)
    assert a.rejected[-1][1] == "not_a_participant"


def test_without_a_payment_agent_there_is_nothing_to_wait_for():
    a = agent(); trip_at(a)
    out = a.on_rating(Rating("t1", "r1@localhost", DRV, 5), "r1@localhost", now=110.0)
    assert out and not out[0][1].pending_settlement


@pytest.mark.parametrize("seed", range(25))
def test_nothing_is_published_before_its_conditions_in_any_order(seed):
    """Random interleaving: a rating is visible iff its pool is settled or its deadline has passed."""
    import random
    rng = random.Random(seed)
    a = gated()
    state = {}                                            # tid -> (completed_at, settled_at or None)
    now = 0.0
    for i in range(6):
        tid = f"t{i}"
        at = rng.uniform(0, 1500)
        state[tid] = [at, None]
    events = [("trip", t) for t in state] + [("rate", t) for t in state] + [("settle", t) for t in state]
    rng.shuffle(events)
    for kind, tid in events:
        now += rng.uniform(0, 400)
        if kind == "trip":
            trip_at(a, tid, at=state[tid][0])
        elif kind == "rate":
            a.on_rating(Rating(tid, "r1@localhost", DRV, rng.randint(1, 5)), "r1@localhost", now=now)
        else:
            settle(a, tid, now=now); state[tid][1] = now
        a.sweep(now=now)
        for e in a.entries.values():
            t_done = e.trip.completed_at
            settled = e.trip.pool_id in a.settled_pools
            assert e.published == (settled or now - t_done >= 600.0), (kind, tid, now)
            if e.published:
                assert (e.state == "published_pending") == (not settled)

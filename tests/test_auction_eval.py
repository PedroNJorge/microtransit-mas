"""
Bid evaluation (pure, no XMPP): validity, ranking, determinism, both pricing rules, and a numeric
check of the two properties the report needs to prove: individual rationality (IR) and dominant-
strategy incentive compatibility (IC) of the second-score payment. See docs/auction_mechanism.md.
"""
import itertools
import random

import pytest

from common.auction import (
    AuctionRules, BidReject, DEFAULT_RULES, FIRST_PRICE, SECOND_SCORE, check_bid,
    evaluate_bids, score_bid, split_fare,
)
from common.ontology import CFP, Bid

RULES = AuctionRules(pricing=SECOND_SCORE, value_of_time=0.004, value_of_detour=0.004,
                     value_of_reputation=0.25)
EXACT = AuctionRules(pricing=SECOND_SCORE, quantize=False)


def cfp(**kw):
    base = dict(pool_id="p1", hub_id="hub", pickup_node="hub", rider_destinations=["h1"],
                rider_time_windows={"r1": [0.0, 3600.0]}, passenger_count=1,
                auction_deadline=30.0, max_total_fare=20.0, max_detour=300.0, attempt=0)
    base.update(kw)
    return CFP(**base)


def bid(driver, price, eta=0.0, detour=0.0, rep=None, cap=4, pool="p1", attempt=0, bid_id=None):
    return Bid(bid_id or f"b-{driver}", driver, pool, price, eta, detour, cap, attempt,
               reputation=rep)


def winner(result):
    return result.winner_bid.driver_id if result.winner else None


# ---- rules --------------------------------------------------------------------------------------

def test_rules_validation():
    with pytest.raises(ValueError):
        AuctionRules(pricing="vcg")
    for field in ("value_of_time", "value_of_detour", "value_of_reputation"):
        for bad in (-1, float("nan"), float("inf"), True, "1"):
            with pytest.raises(ValueError):
                AuctionRules(**{field: bad})
    with pytest.raises(ValueError):
        AuctionRules(default_reputation=0.5)


# ---- scoring ----------------------------------------------------------------------------------------

def test_cost_is_price_plus_money_value_of_the_other_attributes():
    s = score_bid(bid("d1", 8.0, eta=120, detour=60, rep=4.0), RULES)
    # 0.004*120 + 0.004*60 + 0.25*(5-4) = 0.48 + 0.24 + 0.25
    assert s.penalty == pytest.approx(0.97) and s.cost == pytest.approx(8.97)


def test_unknown_or_junk_reputation_uses_the_default():
    for r in (None, float("nan")):
        s = score_bid(bid("d1", 8.0, rep=r), RULES)
        assert s.reputation == RULES.default_reputation
    assert score_bid(bid("d1", 8.0, rep=9.0), RULES).reputation == 5.0      # clamped to [1, 5]
    assert score_bid(bid("d1", 8.0, rep=0.0), RULES).reputation == 1.0


def test_quality_can_outweigh_price():
    near = bid("near", 10.0, eta=60)          # cost 10.24
    far = bid("far", 9.5, eta=600)            # cost 11.90
    assert winner(evaluate_bids(cfp(), [near, far], RULES)) == "near"


def test_price_wins_when_time_and_detour_are_worth_nothing():
    rules = AuctionRules(value_of_time=0, value_of_detour=0, value_of_reputation=0)
    res = evaluate_bids(cfp(), [bid("a", 9.0, eta=1), bid("b", 8.0, eta=3000)], rules)
    assert winner(res) == "b"


def test_reputation_breaks_a_price_tie_by_cost():
    res = evaluate_bids(cfp(), [bid("low", 9.0, rep=2.0), bid("high", 9.0, rep=5.0)], RULES)
    assert winner(res) == "high"


# ---- determinism ----------------------------------------------------------------------------------------

def test_winner_does_not_depend_on_arrival_order():
    bids = [bid("a", 9.0, eta=100), bid("b", 8.5, eta=200), bid("c", 9.5, eta=0),
            bid("d", 8.0, eta=400, rep=2.0)]
    results = {(winner(r), r.price, tuple(s.bid.driver_id for s in r.ranking))
               for r in (evaluate_bids(cfp(), list(p), RULES) for p in itertools.permutations(bids))}
    assert len(results) == 1


def test_tie_break_order_is_cost_then_price_then_eta_then_driver_id():
    # same cost 10.0: (price 10, eta 0) vs (price 8, eta 500 -> +2.0) vs (price 9, eta 250 -> +1.0)
    res = evaluate_bids(cfp(), [bid("a", 10.0), bid("b", 8.0, eta=500), bid("c", 9.0, eta=250)], RULES)
    assert [s.bid.driver_id for s in res.ranking] == ["b", "c", "a"]          # lower price first
    same = evaluate_bids(cfp(), [bid("z", 9.0, eta=100), bid("a", 9.0, eta=100)], RULES)
    assert [s.bid.driver_id for s in same.ranking] == ["a", "z"]              # then driver id


# ---- validity ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("make,reason", [
    (lambda: bid("d", 5.0, pool="other"), BidReject.WRONG_POOL),
    (lambda: bid("d", 5.0, attempt=1), BidReject.STALE_ATTEMPT),
    (lambda: bid("d", 25.0), BidReject.OVER_RESERVE),
    (lambda: bid("d", 20.001), BidReject.OVER_RESERVE),                       # rounds up past 20.00
    (lambda: bid("d", 5.0, cap=0), BidReject.NO_CAPACITY),
    (lambda: bid("d", 5.0, detour=301), BidReject.DETOUR_TOO_LARGE),
    (lambda: bid("d", 5.0, eta=4000), BidReject.TOO_LATE),
    (lambda: bid("d", 5.0, eta=float("inf")), BidReject.BAD_NUMBERS),
    (lambda: bid("d", 5.0, eta=float("nan")), BidReject.BAD_NUMBERS),
    (lambda: bid("d", 5.0, eta=-1), BidReject.BAD_NUMBERS),
])
def test_invalid_bids_are_rejected_with_a_reason(make, reason):
    assert check_bid(cfp(), make(), now=0.0, rules=RULES) == reason
    res = evaluate_bids(cfp(), [make(), bid("ok", 9.0)], RULES)
    assert winner(res) == "ok" and [(b.driver_id, r) for b, r in res.rejected] == [("d", reason)]


def test_bid_exactly_at_the_limits_is_valid():
    assert check_bid(cfp(), bid("d", 20.0, eta=3600, detour=300), 0.0, RULES) is None
    assert check_bid(cfp(passenger_count=3), bid("d", 5.0, cap=3), 0.0, RULES) is None


def test_lateness_is_judged_from_now_and_every_riders_window():
    c = cfp(rider_time_windows={"r1": [0, 3600], "r2": [0, 500]}, passenger_count=2)
    assert check_bid(c, bid("d", 5.0, eta=400), now=0.0, rules=RULES) is None
    assert check_bid(c, bid("d", 5.0, eta=400), now=200.0, rules=RULES) == BidReject.TOO_LATE


def test_duplicate_bids_from_one_driver_count_once():
    res = evaluate_bids(cfp(), [bid("d", 9.0, bid_id="b2"), bid("d", 5.0, bid_id="b1")], RULES)
    assert len(res.ranking) == 1 and [r for _, r in res.rejected] == [BidReject.DUPLICATE]


def test_no_valid_bids():
    res = evaluate_bids(cfp(), [bid("d", 99.0)], RULES)
    assert res.winner is None and res.price is None and res.payment_to("d") == 0.0
    assert evaluate_bids(cfp(), [], RULES).winner is None


# ---- pricing -------------------------------------------------------------------------------------------------------

def test_first_price_pays_the_winning_bid():
    rules = AuctionRules(pricing=FIRST_PRICE)
    res = evaluate_bids(cfp(), [bid("a", 8.0), bid("b", 9.0)], rules)
    assert winner(res) == "a" and res.price == 8.0


def test_second_score_pays_the_threshold_price():
    # costs: a = 8.00 + 0.004*120 = 8.48 ; b = 9.00 + 0.004*60 = 9.24 ; a pays 8 + (9.24-8.48)
    a, b = bid("a", 8.0, eta=120), bid("b", 9.0, eta=60)
    res = evaluate_bids(cfp(), [a, b], RULES)
    assert winner(res) == "a" and res.price == 8.76
    # that price is exactly the threshold: bidding 8.76 still wins, one cent more loses
    assert winner(evaluate_bids(cfp(), [bid("a", 8.76, eta=120), b], RULES)) == "a"
    assert winner(evaluate_bids(cfp(), [bid("a", 8.77, eta=120), b], RULES)) == "b"


def test_payment_is_the_same_whatever_the_winner_bids():
    b = bid("b", 9.0, eta=60)
    prices = {evaluate_bids(cfp(), [bid("a", p, eta=120), b], RULES).price
              for p in (0.0, 1.0, 5.0, 8.0, 8.76)}
    assert prices == {8.76}


def test_single_bidder_is_paid_the_reserve_under_second_score_and_its_bid_under_first_price():
    only = [bid("a", 6.0)]
    assert evaluate_bids(cfp(max_total_fare=14.0), only, RULES).price == 14.0
    assert evaluate_bids(cfp(max_total_fare=14.0), only, AuctionRules(pricing=FIRST_PRICE)).price == 6.0


def test_payment_never_exceeds_the_reserve():
    res = evaluate_bids(cfp(max_total_fare=9.0), [bid("a", 5.0), bid("b", 8.9, eta=3000)], RULES)
    assert winner(res) == "a" and res.price == 9.0                    # threshold ~ 14.9, capped


def test_payment_in_whole_cents_and_never_below_the_bid():
    res = evaluate_bids(cfp(), [bid("a", 8.333), bid("b", 8.3341, eta=1)], RULES)
    assert res.price * 100 == pytest.approx(round(res.price * 100))
    assert res.price >= 8.34                                          # bid rounded UP to the cent


def test_excluding_a_defaulted_winner_reprices_among_the_rest():
    bids = [bid("a", 7.0), bid("b", 8.0), bid("c", 9.5)]
    first = evaluate_bids(cfp(), bids, RULES)
    assert (winner(first), first.price) == ("a", 8.0)
    second = evaluate_bids(cfp(), bids, RULES, excluded=["a"])
    assert (winner(second), second.price) == ("b", 9.5)               # a no longer sets the price
    assert [(b.driver_id, r) for b, r in second.rejected] == [("a", BidReject.EXCLUDED)]
    assert evaluate_bids(cfp(), bids[:1], RULES, excluded=["a"]).winner is None


# ---- the game theory, checked numerically ---------------------------------------------------------------------------

def random_instance(rng, rules):
    n = rng.randint(2, 5)
    reserve = round(rng.uniform(8, 25), 2)
    c = cfp(max_total_fare=reserve, max_detour=300.0)
    attrs = [dict(eta=rng.uniform(0, 900), detour=rng.uniform(0, 300), rep=rng.uniform(1, 5))
             for _ in range(n)]
    costs = [round(rng.uniform(2, 18), 2) for _ in range(n)]            # private: true cost
    others = [round(rng.uniform(0, reserve + 4), 2) for _ in range(n)]  # arbitrary rival bids
    return c, attrs, costs, others, reserve


def utility(c, rules, attrs, others, i, my_price, my_cost):
    bids = [bid(f"d{j}", others[j], **attrs[j]) for j in range(len(others)) if j != i]
    bids.append(bid(f"d{i}", my_price, **attrs[i]))
    res = evaluate_bids(c, bids, rules)
    return res.payment_to(f"d{i}") - my_cost if winner(res) == f"d{i}" else 0.0


@pytest.mark.parametrize("seed", range(40))
def test_individual_rationality_of_the_winner(seed):
    rng = random.Random(seed)
    for rules in (EXACT, RULES, AuctionRules(pricing=FIRST_PRICE)):
        c, attrs, costs, others, _ = random_instance(rng, rules)
        bids = [bid(f"d{j}", others[j], **attrs[j]) for j in range(len(others))]
        res = evaluate_bids(c, bids, rules)
        if res.winner:
            assert res.price >= res.winner.bid.price - 1e-9              # paid at least its bid
            assert res.price <= c.max_total_fare + 1e-9                   # never above the reserve
        # truthful bidders never lose money: bid == cost  =>  profit >= 0
        for i in range(len(costs)):
            assert utility(c, rules, attrs, others, i, costs[i], costs[i]) >= -1e-9


@pytest.mark.parametrize("seed", range(60))
def test_truthful_bidding_is_a_dominant_strategy_under_second_score(seed):
    """Whatever the others bid, no deviation beats bidding the true cost (exact arithmetic)."""
    rng = random.Random(1000 + seed)
    c, attrs, costs, others, reserve = random_instance(rng, EXACT)
    for i in range(len(costs)):
        truthful = utility(c, EXACT, attrs, others, i, costs[i], costs[i])
        grid = [k * (reserve + 6) / 120 for k in range(121)] + [costs[i] * f for f in (0.5, 0.9, 1.1, 2.0)]
        for dev in grid:
            assert utility(c, EXACT, attrs, others, i, dev, costs[i]) <= truthful + 1e-9, \
                (seed, i, dev, costs[i])


@pytest.mark.parametrize("seed", range(30))
def test_cent_rounding_changes_a_deviations_gain_by_at_most_a_cent(seed):
    rng = random.Random(5000 + seed)
    c, attrs, costs, others, reserve = random_instance(rng, RULES)
    for i in range(len(costs)):
        truthful = utility(c, RULES, attrs, others, i, costs[i], costs[i])
        for k in range(0, int((reserve + 4) * 100), 37):
            assert utility(c, RULES, attrs, others, i, k / 100, costs[i]) <= truthful + 0.0101


def test_under_first_price_shading_pays_which_is_why_second_score_is_used():
    """Two otherwise identical drivers with costs 5 and 7: the cheap one gains by bidding above cost."""
    first = AuctionRules(pricing=FIRST_PRICE, quantize=False)
    rival = bid("rival", 7.0)
    honest = evaluate_bids(cfp(), [bid("me", 5.0), rival], first)
    shaded = evaluate_bids(cfp(), [bid("me", 6.99), rival], first)
    assert winner(honest) == winner(shaded) == "me"
    assert shaded.price - 5.0 > honest.price - 5.0 + 1.9                  # ~2 EUR instead of 0
    # the same two bids under second-score: shading changes nothing
    second = AuctionRules(pricing=SECOND_SCORE, quantize=False)
    assert evaluate_bids(cfp(), [bid("me", 5.0), rival], second).price == \
        evaluate_bids(cfp(), [bid("me", 6.99), rival], second).price == 7.0


def test_the_winner_is_the_most_efficient_bidder_when_all_bid_truthfully():
    """VCG efficiency: with truthful bids the winner minimises (true cost + penalty)."""
    rng = random.Random(7)
    for _ in range(100):
        c, attrs, costs, _, _ = random_instance(rng, EXACT)
        bids = [bid(f"d{j}", costs[j], **attrs[j]) for j in range(len(costs))]
        res = evaluate_bids(c, bids, EXACT)
        if res.winner:
            best = min(score_bid(b, EXACT).cost for b in bids
                       if check_bid(c, b, 0.0, EXACT) is None)
            assert res.winner.cost == pytest.approx(best)


# ---- fare split -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fare,n", [(10.0, 1), (10.0, 2), (10.0, 3), (0.01, 3), (7.77, 4), (0.0, 2)])
def test_split_fare_sums_exactly_and_is_fair_to_the_cent(fare, n):
    riders = [f"r{i}" for i in range(n)]
    split = split_fare(fare, riders)
    assert sum(round(v * 100) for v in split.values()) == round(fare * 100)
    assert max(split.values()) - min(split.values()) <= 0.0100001
    assert split == split_fare(fare, list(reversed(riders)))               # order independent


def test_split_fare_no_riders():
    assert split_fare(10.0, []) == {}

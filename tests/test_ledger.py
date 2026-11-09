"""Ledger rules (no XMPP): hold / settle / refund / expiry, conservation, idempotency."""
import dataclasses
import random

import pytest

from common.ledger import Ledger, LedgerError, Reason, cents, money
from common.ontology import EscrowStatus

HUB = "hub_a@x"
OTHER_HUB = "hub_b@x"


def make(**kw):
    kw.setdefault("default_rider_balance", 100.0)
    return Ledger(**kw)


def hold(l, req, rider, amount, pool="p1", elig=False, hub=HUB):
    return l.hold(req, rider, pool, amount, elig, hub)[0]


def err(fn, *a, **kw):
    with pytest.raises(LedgerError) as e:
        fn(*a, **kw)
    return e.value.reason


# ---- money helpers ---------------------------------------------------------------------

@pytest.mark.parametrize("x,c", [(0, 0), (1, 100), (12.34, 1234), (0.005, 1), (0.004, 0),
                                 (1.665, 167), (2.675, 268), (19.99, 1999)])
def test_cents_rounds_half_up(x, c):
    assert cents(x) == c and money(c) == c / 100


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "5", None])
def test_cents_rejects_junk(bad):
    assert err(cents, bad) == Reason.INVALID_AMOUNT


# ---- hold ------------------------------------------------------------------------------------

def test_hold_moves_money_into_escrow():
    l = make()
    r = hold(l, "q1", "r1", 20)
    assert l.balance("r1") == 80 and l.escrow_balance(r.escrow_id) == 20
    assert l.status(r.escrow_id) == EscrowStatus.HELD and l.check_conservation()


def test_hold_is_idempotent_per_request():
    l = make()
    a, created_a = l.hold("q1", "r1", "p1", 20, False, HUB)
    b, created_b = l.hold("q1", "r1", "p1", 20, False, HUB)
    assert a == b and created_a and not created_b
    assert l.balance("r1") == 80                                    # charged once


def test_same_request_id_with_different_terms_is_refused():
    l = make()
    hold(l, "q1", "r1", 20)
    assert err(l.hold, "q1", "r1", "p1", 25, False, HUB) == Reason.DUPLICATE_REQUEST
    assert err(l.hold, "q1", "r2", "p1", 20, False, HUB) == Reason.DUPLICATE_REQUEST
    assert l.balance("r1") == 80


def test_insufficient_funds_and_exact_balance():
    l = make(default_rider_balance=10.0)
    assert err(l.hold, "q1", "r1", "p1", 10.01, False, HUB) == Reason.INSUFFICIENT_FUNDS
    assert l.balance("r1") == 10.0                                  # wallet opened, nothing moved
    r = hold(l, "q2", "r1", 10.0)
    assert l.balance("r1") == 0 and r.amount == 10.0


@pytest.mark.parametrize("amount", [0, -5, float("nan"), float("inf")])
def test_invalid_amounts(amount):
    assert err(make().hold, "q1", "r1", "p1", amount, False, HUB) == Reason.INVALID_AMOUNT


def test_unknown_rider_refused_without_default_balance():
    l = Ledger()
    assert err(l.hold, "q1", "ghost", "p1", 5, False, HUB) == Reason.UNKNOWN_ACCOUNT
    l.open_wallet("ghost", 50)
    hold(l, "q1", "ghost", 5)
    assert l.balance("ghost") == 45


def test_records_are_immutable():
    r = hold(make(), "q1", "r1", 20)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.amount_c = 1


# ---- settle ----------------------------------------------------------------------------------

def two_rider_pool(**kw):
    l = make(subsidy_discount=0.5, **kw)
    hold(l, "q1", "r1", 20, elig=True)
    hold(l, "q2", "r2", 15)
    return l


def test_settle_pays_driver_returns_excess_and_rebates_subsidy():
    l = two_rider_pool()
    out = l.settle("p1", "d1", ["r1", "r2"], {"r1": 12.0, "r2": 9.0}, HUB)
    assert (out.total_paid, out.total_subsidy, out.total_returned) == (21.0, 6.0, 14.0)
    assert l.balance("d1") == 21.0                                   # = sum of fares, no deduction
    assert l.balance("r1") == 100 - 20 + 8 + 6                        # excess + rebate
    assert l.balance("r2") == 100 - 15 + 6                            # excess only (not eligible)
    assert l.fund_balance == 0 and l.subsidy_spent == 6.0
    assert [l.status(x.escrow_id) for x in l.pool_escrows("p1")] == [EscrowStatus.SETTLED] * 2
    assert all(l.escrow_balance(x.escrow_id) == 0 for x in l.pool_escrows("p1"))
    assert l.check_conservation()


def test_settle_without_fares_charges_the_full_hold():
    l = two_rider_pool()
    out = l.settle("p1", "d1", ["r1", "r2"], None, HUB)
    assert out.total_paid == 35.0 and out.total_returned == 0.0
    assert l.balance("d1") == 35.0


def test_driver_payout_never_depends_on_subsidy():
    for discount in (0.0, 0.5, 1.0):
        l = Ledger(default_rider_balance=100, subsidy_discount=discount)
        hold(l, "q1", "r1", 10, elig=True)
        l.settle("p1", "d1", ["r1"], {"r1": 10.0}, HUB)
        assert l.balance("d1") == 10.0
        assert l.balance("r1") == 90 + 10 * discount


def test_partial_settlement_leaves_other_escrows_held():
    l = two_rider_pool()
    l.settle("p1", "d1", ["r1"], {"r1": 20.0}, HUB)
    e1, e2 = l.pool_escrows("p1")
    assert l.status(e1.escrow_id) == EscrowStatus.SETTLED and l.status(e2.escrow_id) == EscrowStatus.HELD
    out = l.settle("p1", "d1", ["r1", "r2"], {"r1": 20.0, "r2": 15.0}, HUB)   # r1 is a replay line
    assert not out.replayed and l.balance("d1") == 35.0                       # r1 NOT paid twice
    assert l.check_conservation()


def test_settlement_is_idempotent():
    l = two_rider_pool()
    first = l.settle("p1", "d1", ["r1", "r2"], {"r1": 12.0, "r2": 9.0}, HUB)
    snapshot = (l.balance("d1"), l.balance("r1"), l.balance("r2"), len(l.entries))
    again = l.settle("p1", "d1", ["r1", "r2"], {"r1": 12.0, "r2": 9.0}, HUB)
    assert again.replayed and again.lines == first.lines
    assert (l.balance("d1"), l.balance("r1"), l.balance("r2"), len(l.entries)) == snapshot


def test_settle_is_atomic():
    l = two_rider_pool()
    before = (len(l.entries), l.balance("d1"), l.balance("r1"))
    # r2's fare exceeds its hold: NOTHING may move, not even r1's valid part
    assert err(l.settle, "p1", "d1", ["r1", "r2"], {"r1": 12.0, "r2": 15.01}, HUB) \
        == Reason.FARE_EXCEEDS_ESCROW
    assert (len(l.entries), l.balance("d1"), l.balance("r1")) == before
    assert all(l.status(r.escrow_id) == EscrowStatus.HELD for r in l.pool_escrows("p1"))


@pytest.mark.parametrize("fares,reason", [
    ({"r1": 12.0}, Reason.INVALID_FARE),                      # r2 has no fare
    ({"r1": -1.0, "r2": 9.0}, Reason.INVALID_FARE),
    ({"r1": float("nan"), "r2": 9.0}, Reason.INVALID_AMOUNT),
])
def test_bad_fares_are_refused(fares, reason):
    l = two_rider_pool()
    assert err(l.settle, "p1", "d1", ["r1", "r2"], fares, HUB) == reason
    assert l.balance("d1") == 0


def test_only_the_escrows_own_hub_can_settle():
    l = two_rider_pool()
    assert err(l.settle, "p1", "d1", ["r1"], None, OTHER_HUB) == Reason.NOT_AUTHORISED
    assert err(l.settle, "p1", "d1", ["r1"], None, "") == Reason.NOT_AUTHORISED
    assert err(l.settle, "p1", "d1", ["r1"], None, "d1") == Reason.NOT_AUTHORISED   # driver
    assert l.balance("d1") == 0


def test_unbound_escrow_can_be_settled_by_anyone():
    l = make()
    l.hold("q1", "r1", "p1", 5, False, "")
    assert l.settle("p1", "d1", ["r1"], None, "whoever").total_paid == 5.0


def test_unknown_pool_and_rider_without_escrow():
    l = two_rider_pool()
    assert err(l.settle, "nope", "d1", ["r1"], None, HUB) == Reason.UNKNOWN_POOL
    assert err(l.settle, "p1", "d1", ["stranger"], None, HUB) == Reason.RIDER_WITHOUT_ESCROW
    assert err(l.settle, "p1", "d1", [], None, HUB) == Reason.RIDER_WITHOUT_ESCROW


def test_refunded_escrow_cannot_be_settled():
    l = two_rider_pool()
    l.refund("p1", [], HUB)
    assert err(l.settle, "p1", "d1", ["r1"], None, HUB) == Reason.NOT_HELD
    assert l.balance("d1") == 0


def test_zero_fare_trip_returns_everything():
    l = make(subsidy_discount=0.5)
    hold(l, "q1", "r1", 10, elig=True)
    out = l.settle("p1", "d1", ["r1"], {"r1": 0.0}, HUB)
    assert (out.total_paid, out.total_subsidy, out.total_returned) == (0.0, 0.0, 10.0)
    assert l.balance("r1") == 100


# ---- subsidy fund ------------------------------------------------------------------------

def test_finite_budget_runs_out_and_records_the_shortfall():
    l = Ledger(default_rider_balance=100, subsidy_budget=7.0, subsidy_discount=0.5)
    for i in (1, 2):
        l.hold(f"q{i}", f"r{i}", "p1", 10, True, HUB)
    out = l.settle("p1", "d1", ["r1", "r2"], None, HUB)
    assert [x.subsidy_c for x in out.lines] == [500, 200]            # 5.00 then the last 2.00
    assert l.fund_balance == 0 and l.subsidy_spent == 7.0 and l.subsidy_shortfall == 3.0
    assert l.balance("d1") == 20.0                                    # driver unaffected
    assert l.check_conservation()


def test_unlimited_budget_is_funded_on_demand():
    l = Ledger(default_rider_balance=100, subsidy_discount=0.5)
    hold(l, "q1", "r1", 10, elig=True)
    l.settle("p1", "d1", ["r1"], None, HUB)
    assert l.subsidy_spent == 5.0 and l.fund_balance == 0 and l.check_conservation()


def test_subsidy_rounding_half_up():
    l = Ledger(default_rider_balance=100, subsidy_discount=0.5)
    hold(l, "q1", "r1", 3.33, elig=True)
    assert l.settle("p1", "d1", ["r1"], None, HUB).lines[0].subsidy_c == 167   # 1.665 -> 1.67


def test_zero_budget_means_no_subsidies_and_negative_is_invalid():
    l = Ledger(default_rider_balance=100, subsidy_budget=0.0, subsidy_discount=0.5)
    hold(l, "q1", "r1", 10, elig=True)
    out = l.settle("p1", "d1", ["r1"], None, HUB)
    assert out.total_subsidy == 0.0 and l.subsidy_shortfall == 5.0 and l.balance("d1") == 10.0
    with pytest.raises(ValueError):
        Ledger(subsidy_budget=-1)


def test_fund_top_up_and_discount_validation():
    l = Ledger()
    l.fund_subsidy(5)
    assert l.fund_balance == 5.0 and l.check_conservation()
    assert err(l.fund_subsidy, 0) == Reason.INVALID_AMOUNT
    for bad in (-0.1, 1.1, True, "x"):
        with pytest.raises(ValueError):
            l.set_subsidy_discount(bad)


# ---- refund --------------------------------------------------------------------------------------

def test_refund_returns_the_full_hold():
    l = two_rider_pool()
    items = l.refund("p1", [], HUB)
    assert [i.ok for i in items] == [True, True] and [i.amount for i in items] == [20.0, 15.0]
    assert l.balance("r1") == l.balance("r2") == 100 and l.balance("d1") == 0
    assert l.check_conservation()


def test_refund_listed_escrows_only():
    l = two_rider_pool()
    e1, e2 = l.pool_escrows("p1")
    items = l.refund("p1", [e2.escrow_id], HUB)
    assert [i.escrow_id for i in items] == [e2.escrow_id]
    assert l.status(e1.escrow_id) == EscrowStatus.HELD and l.balance("r1") == 80


def test_refund_replay_moves_nothing():
    l = two_rider_pool()
    l.refund("p1", [], HUB)
    n = len(l.entries)
    e1 = l.pool_escrows("p1")[0]
    items = l.refund("p1", [e1.escrow_id], HUB)
    assert items[0].ok and items[0].replayed and len(l.entries) == n


def test_settled_escrow_cannot_be_refunded():
    l = two_rider_pool()
    l.settle("p1", "d1", ["r1"], None, HUB)
    e1, e2 = l.pool_escrows("p1")
    items = l.refund("p1", [e1.escrow_id, e2.escrow_id], HUB)
    assert [(i.ok, i.reason) for i in items] == [(False, Reason.ALREADY_SETTLED), (True, "")]
    assert l.balance("r1") == 80 + 10 and l.balance("d1") == 20.0     # hold 20, rebate 10
    assert l.balance("r2") == 100 and l.check_conservation()


def test_refund_all_judges_every_escrow_of_the_pool():
    l = two_rider_pool()
    l.settle("p1", "d1", ["r1"], None, HUB)                          # r1 settled, r2 still held
    first = l.refund("p1", [], HUB)
    assert [(i.ok, i.reason, i.replayed) for i in first] == [
        (False, Reason.ALREADY_SETTLED, False), (True, "", False)]
    again = l.refund("p1", [], HUB)                                   # nothing is held any more
    assert [(i.ok, i.reason, i.replayed) for i in again] == [
        (False, Reason.ALREADY_SETTLED, False), (True, "", True)]
    assert l.balance("r2") == 100 and l.check_conservation()


def test_refund_authorisation_and_validation():
    l = two_rider_pool()
    e1 = l.pool_escrows("p1")[0]
    assert l.refund("p1", [e1.escrow_id], OTHER_HUB)[0].reason == Reason.NOT_AUTHORISED
    assert l.refund("p1", [e1.escrow_id], "r1")[0].reason == Reason.NOT_AUTHORISED   # the rider
    assert l.refund("other-pool", [e1.escrow_id], HUB)[0].reason == Reason.POOL_MISMATCH
    assert l.refund("p1", ["esc-999999"], HUB)[0].reason == Reason.UNKNOWN_ESCROW
    assert err(l.refund, "nope", [], HUB) == Reason.UNKNOWN_POOL
    assert l.balance("r1") == 80                                      # nothing moved


# ---- expiry ----------------------------------------------------------------------------------------

def test_expired_holds_are_swept_back():
    t = [0.0]
    l = Ledger(default_rider_balance=100, now=lambda: t[0])
    hold(l, "q1", "r1", 10)
    t[0] = 50
    hold(l, "q2", "r2", 10)
    t[0] = 100
    assert [r.request_id for r in l.expired(100, 90)] == ["q1"]
    swept = l.refund_expired(100, 90)
    assert [(r.request_id, i.ok) for r, i in swept] == [("q1", True)]
    assert l.balance("r1") == 100 and l.balance("r2") == 90
    assert l.refund_expired(100, 90) == [] and l.check_conservation()


def test_settled_escrows_never_expire():
    t = [0.0]
    l = Ledger(default_rider_balance=100, now=lambda: t[0])
    hold(l, "q1", "r1", 10)
    l.settle("p1", "d1", ["r1"], None, HUB)
    assert l.expired(10_000, 1) == []


# ---- audit trail --------------------------------------------------------------------------------------

def test_entries_are_sequential_and_conserved():
    l = two_rider_pool()
    l.settle("p1", "d1", ["r1", "r2"], {"r1": 12.0, "r2": 9.0}, HUB)
    assert [e.seq for e in l.entries] == list(range(1, len(l.entries) + 1))
    assert all(e.cents >= 0 for e in l.entries)
    kinds = {e.kind for e in l.entries}
    assert {"endowment", "hold", "payout", "return", "subsidy", "subsidy_funding"} <= kinds


# ---- fuzz: conservation survives any sequence of operations -----------------------------------------

@pytest.mark.parametrize("seed", range(8))
def test_random_operations_conserve_money(seed):
    rng = random.Random(seed)
    t = [0.0]
    l = Ledger(default_rider_balance=60, subsidy_budget=rng.choice([None, 0.0, 25.0]),
               subsidy_discount=rng.choice([0.0, 0.3, 0.5, 1.0]), now=lambda: t[0])
    riders = [f"r{i}" for i in range(6)]
    pools = [f"p{i}" for i in range(5)]
    for step in range(400):
        t[0] += rng.random() * 30
        op = rng.choice(["hold", "hold", "settle", "refund", "sweep"])
        pool = rng.choice(pools)
        try:
            if op == "hold":
                l.hold(f"q{step}", rng.choice(riders), pool, round(rng.uniform(0.01, 40), 2),
                       rng.random() < 0.5, HUB)
            elif op == "settle":
                members = [r.rider for r in l.pool_escrows(pool)]
                chosen = rng.sample(members, rng.randint(0, len(members))) if members else []
                fares = {r: round(rng.uniform(0, 45), 2) for r in chosen} if rng.random() < 0.7 else None
                l.settle(pool, f"d{rng.randint(1, 3)}", chosen, fares, HUB)
            elif op == "refund":
                l.refund(pool, [], HUB)
            else:
                l.refund_expired(t[0], 200)
        except LedgerError:
            pass
        assert l.check_conservation(), (seed, step, op)
    # every escrow ends in exactly one state and a closed escrow holds nothing
    for r in l._escrows.values():
        if l.status(r.escrow_id) != EscrowStatus.HELD:
            assert l.escrow_balance(r.escrow_id) == 0
        else:
            assert l.escrow_balance(r.escrow_id) == r.amount

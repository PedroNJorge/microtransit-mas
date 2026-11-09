"""
Unit tests of the driver's provisional-lock state machine (common/lock.py): CC-01 .. CC-09.
Pure Python, no XMPP, no clock: time is a plain number passed to every call.
"""
import random

import pytest

from common.lock import DriverLock, LockReason, LockState

T = 60.0
HUB, HUB2 = "hub_a@localhost", "hub_b@localhost"


def make(**kw):
    events = []
    lock = DriverLock(T, on_change=lambda o, n, l, r: events.append((o, n, r)), **kw)
    return lock, events


def test_new_lock_is_free():
    lock, _ = make()
    assert lock.state == LockState.FREE and lock.holder is None


def test_acquire_makes_it_provisional_with_expiry():
    lock, ev = make()
    assert lock.acquire("p1", HUB, "b1", 0, now=10).ok
    assert lock.state == LockState.PROVISIONAL and lock.holder.expires_at == 10 + T
    assert ev == [(LockState.FREE, LockState.PROVISIONAL, "acquired")]       # CC-07


def test_second_cfp_while_locked_is_refused_locked():                        # CC-01 / CC-09
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    for pool, hub in [("p2", HUB), ("p2", HUB2), ("p1", HUB2)]:
        d = lock.acquire(pool, hub, "b2", 0, 1)
        assert not d.ok and d.reason == LockReason.LOCKED
    assert lock.holder.pool_id == "p1"                                        # holder untouched


def test_refused_acquire_does_not_extend_or_change_the_lock():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.acquire("p2", HUB2, "b2", 0, 30)
    assert lock.holder.expires_at == T and len(ev) == 1


def test_duplicate_cfp_is_idempotent_and_does_not_extend_expiry():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    d = lock.acquire("p1", HUB, "b1-again", 0, 30)
    assert d.ok and d.replay and lock.holder.bid_id == "b1" and lock.holder.expires_at == T
    assert len(ev) == 1


def test_retry_attempt_replaces_the_provisional_lock():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    d = lock.acquire("p1", HUB, "b2", 1, 20)
    assert d.ok and not d.replay
    assert lock.holder.bid_id == "b2" and lock.holder.attempt == 1 and lock.holder.expires_at == 20 + T
    assert ev[-1][2] == "replaced"


def test_older_attempt_is_refused_stale():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 1, 0)
    d = lock.acquire("p1", HUB, "b0", 0, 1)
    assert not d.ok and d.reason == LockReason.STALE_ATTEMPT


def test_retry_cannot_replace_a_firm_lock():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 5)
    d = lock.acquire("p1", HUB, "b2", 1, 6)
    assert not d.ok and d.reason == LockReason.LOCKED and lock.state == LockState.FIRM


# ---- CC-02 expiry --------------------------------------------------------------------------

def test_provisional_lock_expires_exactly_at_t_provisional():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert lock.tick(T - 0.001) is None and lock.state == LockState.PROVISIONAL
    expired = lock.tick(T)
    assert expired.pool_id == "p1" and lock.state == LockState.FREE
    assert ev[-1] == (LockState.PROVISIONAL, LockState.FREE, "expired")      # CC-07 on release
    assert lock.tick(T + 1) is None                                           # only once


def test_expired_lock_allows_a_new_bid_on_another_cfp():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert lock.acquire("p2", HUB2, "b2", 0, T + 1).ok
    assert lock.holder.pool_id == "p2"


def test_every_call_applies_expiry_first_without_an_explicit_tick():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert not lock.blocks("p2", HUB, 0, T + 1)
    assert lock.state == LockState.FREE


def test_accept_after_expiry_is_refused_lock_expired():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    d = lock.commit("p1", HUB, T + 5)
    assert not d.ok and d.reason == LockReason.LOCK_EXPIRED and lock.state == LockState.FREE


def test_accept_without_any_lock_is_refused_no_lock():
    lock, _ = make()
    d = lock.commit("never-bid", HUB, 0)
    assert not d.ok and d.reason == LockReason.NO_LOCK


def test_accept_at_the_last_moment_before_expiry_commits():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert lock.commit("p1", HUB, T - 0.001).ok and lock.state == LockState.FIRM


def test_firm_lock_does_not_expire():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 1)
    assert lock.tick(10_000) is None and lock.state == LockState.FIRM and lock.holder.expires_at is None


# ---- CC-03 commit --------------------------------------------------------------------------

def test_commit_makes_the_lock_firm_and_refuses_everything_else():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    d = lock.commit("p1", HUB, 5)
    assert d.ok and lock.state == LockState.FIRM
    assert ev[-1] == (LockState.PROVISIONAL, LockState.FIRM, "committed")
    assert lock.acquire("p2", HUB, "b2", 0, 6).reason == LockReason.LOCKED   # CC-03: reject all other CFPs


def test_commit_is_idempotent():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 5)
    d = lock.commit("p1", HUB, 6)
    assert d.ok and d.replay and len(ev) == 2


def test_commit_for_another_pool_or_hub_is_refused_and_changes_nothing():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    for pool, hub in [("p2", HUB), ("p1", HUB2)]:
        d = lock.commit(pool, hub, 5)
        assert not d.ok and d.reason == LockReason.NOT_HOLDER
    assert lock.state == LockState.PROVISIONAL


# ---- CC-04 release -------------------------------------------------------------------------

def test_reject_releases_a_provisional_lock_and_driver_can_bid_again():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert lock.release("p1", HUB, 5)
    assert lock.state == LockState.FREE and ev[-1] == (LockState.PROVISIONAL, LockState.FREE, "rejected")
    assert lock.acquire("p2", HUB2, "b2", 0, 6).ok


def test_release_is_idempotent_and_harmless_when_nothing_is_held():
    lock, ev = make()
    assert not lock.release("p1", HUB, 0)
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert lock.release("p1", HUB, 1) and not lock.release("p1", HUB, 2)
    assert len(ev) == 2


def test_reject_for_a_different_pool_or_hub_does_not_free_the_lock():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert not lock.release("p2", HUB, 1) and not lock.release("p1", HUB2, 1)
    assert lock.state == LockState.PROVISIONAL


def test_reject_never_frees_a_firm_lock():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 1)
    assert not lock.release("p1", HUB, 2) and lock.state == LockState.FIRM


# ---- abort / finish (CC-06) ----------------------------------------------------------------

def test_abort_frees_either_kind_but_only_for_the_holder():
    for commit in (False, True):
        lock, _ = make()
        lock.acquire("p1", HUB, "b1", 0, 0)
        if commit:
            lock.commit("p1", HUB, 1)
        assert not lock.abort("p1", HUB2, 2) and not lock.abort("p9", HUB, 2)
        assert lock.abort("p1", HUB, 3) and lock.state == LockState.FREE


def test_finish_only_frees_a_firm_lock_of_that_pool():
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    assert not lock.finish("p1", 1)                      # still provisional
    lock.commit("p1", HUB, 2)
    assert not lock.finish("p2", 3) and lock.state == LockState.FIRM
    assert lock.finish("p1", 4) and lock.state == LockState.FREE
    assert not lock.finish("p1", 5)


def test_no_double_booking_across_a_whole_trip():                           # CC-06
    lock, _ = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 1)
    for t in (5, 100, 5000):
        assert lock.acquire("p2", HUB2, "x", 0, t).reason == LockReason.LOCKED
    lock.finish("p1", 6000)
    assert lock.acquire("p2", HUB2, "x", 0, 6001).ok


# ---- listener / history --------------------------------------------------------------------

def test_listener_is_called_once_per_state_change_in_order():
    lock, ev = make()
    lock.acquire("p1", HUB, "b1", 0, 0)
    lock.commit("p1", HUB, 1)
    lock.finish("p1", 2)
    lock.acquire("p2", HUB, "b2", 0, 3)
    lock.tick(3 + T)
    assert [(o.value, n.value) for o, n, _ in ev] == [
        ("FREE", "PROVISIONAL"), ("PROVISIONAL", "FIRM"), ("FIRM", "FREE"),
        ("FREE", "PROVISIONAL"), ("PROVISIONAL", "FREE")]
    assert [h[3] for h in lock.history] == ["acquired", "committed", "finished", "acquired", "expired"]


def test_invalid_t_provisional():
    with pytest.raises(ValueError):
        DriverLock(0)


# ---- property: random message storms never break the invariants ----------------------------

@pytest.mark.parametrize("seed", range(40))
def test_random_sequences_keep_the_invariants(seed):
    rng = random.Random(seed)
    lock, ev = make()
    t, firm_pool = 0.0, None
    for _ in range(200):
        t += rng.choice([0, 0.5, 5, 30, 61])
        pool, hub = rng.choice(["p1", "p2", "p3"]), rng.choice([HUB, HUB2])
        op = rng.choice(["acquire", "commit", "release", "abort", "finish", "tick"])
        before = lock.holder
        if op == "acquire":
            d = lock.acquire(pool, hub, f"b{rng.random()}", rng.choice([0, 1, 2]), t)
            if not d.ok:
                assert d.reason in (LockReason.LOCKED, LockReason.STALE_ATTEMPT)
        elif op == "commit":
            lock.commit(pool, hub, t)
        elif op == "release":
            lock.release(pool, hub, t)
        elif op == "abort":
            lock.abort(pool, hub, t)
        elif op == "finish":
            lock.finish(pool, t)
        else:
            lock.tick(t)
        h = lock.holder
        # CC-01: one lock; provisional always has an expiry in the future, firm never has one
        if h:
            assert (h.expires_at is None) == (h.state == LockState.FIRM)
            if h.state == LockState.PROVISIONAL:
                assert h.expires_at > t - 1e-9 or lock.tick(t) is not None
        # a firm lock is only ever removed by abort/finish, never by acquire/release/commit/tick
        if before and before.state == LockState.FIRM and op in ("acquire", "commit", "release", "tick"):
            assert lock.holder is not None and lock.holder.pool_id == before.pool_id
    # listener saw consistent from->to pairs
    for o, n, _ in ev:
        assert o != n or _ == "replaced"

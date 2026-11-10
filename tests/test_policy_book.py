"""PolicyBook (RI-04): versions, idempotence, conflicts, effective_from."""
import pytest

from common.ontology import Policy
from common.policy import CONFLICT, INVALID, STALE, PolicyBook, validate


def pol(version=1, cap=25.0, start=0.0, **kw):
    return Policy(f"p{version}", version, start, {"max": cap}, kw.get("zones", []),
                  kw.get("subsidy", {"eligible_discount": 0.5}))


def test_first_policy_is_accepted_and_is_active_from_its_start():
    b = PolicyBook()
    assert b.offer(pol(1, start=100.0)).ok
    assert b.active(99.0) is None and b.fare_cap(99.0) is None
    assert b.fare_cap(100.0) == 25.0


def test_newer_version_replaces_older_in_force():
    b = PolicyBook()
    b.offer(pol(1, 25.0)); b.offer(pol(2, 10.0, start=50.0))
    assert b.fare_cap(10.0) == 25.0 and b.fare_cap(50.0) == 10.0 and b.latest.version == 2


def test_repeat_of_the_same_policy_is_idempotent():
    b = PolicyBook()
    p = pol(1)
    assert b.offer(p).ok and b.offer(p).ok and len(b.by_version) == 1


def test_stale_version_is_refused():
    b = PolicyBook()
    b.offer(pol(3))
    a = b.offer(pol(2))
    assert not a.ok and a.reason == STALE and b.latest.version == 3


def test_same_version_different_content_is_a_conflict():
    b = PolicyBook()
    b.offer(pol(1, 25.0))
    a = b.offer(pol(1, 20.0))
    assert not a.ok and a.reason == CONFLICT and b.fare_cap(0.0) == 25.0


@pytest.mark.parametrize("bad", [pol(1, cap=0.0), pol(1, cap=-1.0), pol(1, cap=float("nan")),
                                 pol(1, subsidy={"eligible_discount": 1.5}), pol(-1)])
def test_invalid_policies_are_refused(bad):
    b = PolicyBook()
    assert b.offer(bad).reason == INVALID and not b.by_version and validate(bad)


def test_a_future_version_does_not_hide_the_one_in_force():
    b = PolicyBook()
    b.offer(pol(1, 25.0, start=0.0)); b.offer(pol(2, 5.0, start=1000.0))
    assert b.fare_cap(999.0) == 25.0 and b.fare_cap(1000.0) == 5.0

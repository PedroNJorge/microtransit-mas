"""Late join, driver rules (no network): when a driver refuses, and how a provisional joiner is undone."""
from types import SimpleNamespace as NS

from agents.driver import Assignment, DriverAgent, PendingJoin
from common.ontology import LateJoinRequest


def driver(**kw):
    d = NS(late_join=True, activity="to_pickup", capacity=4, cost_rate=0.004, bid_markup=0.0,
           log=lambda *_: None)
    d.__dict__.update(kw)
    d.late_join_problem = lambda a, s, p: DriverAgent.late_join_problem(d, a, s, p)
    d.undo_late_join = lambda a, j: DriverAgent.undo_late_join(d, a, j)
    return d


def assignment(**kw):
    a = Assignment("hub@x", NS(pool_id="p1"), NS(route=["n1_1", "n1_2"]))
    a.riders = [("r1@x", "q1", "n1_2", "p1")]
    a.__dict__.update(kw)
    return a


def ask(extra_pay=1.0, extra_seconds=100.0, pool="p1"):
    return LateJoinRequest(pool, "p2", "q2", "r2@x", "n1_3", ["n1_1", "n1_2", "n1_3"], extra_seconds, extra_pay)


def test_driver_takes_a_joiner_it_is_paid_for():
    assert DriverAgent.late_join_problem(driver(), assignment(), "hub@x", ask()) == ""


def test_driver_refuses_with_a_reason():
    p = DriverAgent.late_join_problem
    assert p(driver(late_join=False), assignment(), "hub@x", ask()) == "not_assigned"
    assert p(driver(), None, "hub@x", ask()) == "not_assigned"
    assert p(driver(), assignment(), "other@x", ask()) == "not_assigned"
    assert p(driver(), assignment(), "hub@x", ask(pool="zzz")) == "not_assigned"
    assert p(driver(), assignment(frozen=True), "hub@x", ask()) == "too_late"
    assert p(driver(activity="on_trip"), assignment(), "hub@x", ask()) == "too_late"
    assert p(driver(capacity=1), assignment(), "hub@x", ask()) == "full"
    assert p(driver(), assignment(), "hub@x", ask(extra_pay=0.1)) == "underpaid"            # 100 s x 0.004 = 0.40
    assert p(driver(bid_markup=2.0), assignment(), "hub@x", ask(extra_pay=1.0)) == "underpaid"   # 0.40 x 3 = 1.20


def test_undo_restores_riders_and_route():
    d, a = driver(), assignment()
    rider = ("r2@x", "q2", "n1_3", "p2")
    a.pending["p2"] = PendingJoin(rider, ["n1_1", "n1_2"], 100.0)
    a.riders.append(rider)
    a.route = ["n1_1", "n1_2", "n1_3"]
    assert DriverAgent.undo_late_join(d, a, "p2") is True
    assert rider not in a.riders and a.route == ["n1_1", "n1_2"] and not a.pending
    assert DriverAgent.undo_late_join(d, a, "p2") is False                                 # nothing left to undo

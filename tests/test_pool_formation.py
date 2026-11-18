"""Milestone 9: batch window bookkeeping and the hub's pooling plan (no network traffic)."""
import pytest

from common.graph import build_grid_network
from common.ontology import TripRequest
from protocols.pool_formation import QueuedRequest, batch_due, plan_batch, take_batch, window_closes_at

HUB = "n1_1"


def q(i, dest, at, tol=300.0):
    return QueuedRequest(TripRequest(f"r{i}", f"rider{i}", HUB, dest, 0.0, 3600.0, tol, 20.0, True, False, at),
                         f"rider{i}@localhost", at)


class FakeHub:
    hub_id, solver, max_pool_size, detour_weight = HUB, "exact", 4, 1.0
    network = build_grid_network()


def test_window_opens_with_the_first_request():
    queue = [q(1, "n1_2", 10.0), q(2, "n1_3", 50.0)]
    assert window_closes_at(queue, 180.0) == 190.0
    assert not batch_due(queue, 180.0, 189.0, max_batch=8)
    assert batch_due(queue, 180.0, 190.0, max_batch=8)


def test_full_batch_closes_early_and_empty_queue_never_due():
    assert batch_due([q(i, "n1_2", 0.0) for i in range(8)], 180.0, 1.0, max_batch=8)
    assert not batch_due([], 180.0, 1e9, max_batch=8)


def test_take_batch_is_fifo_and_leaves_the_rest():
    queue = [q(3, "n1_2", 30.0), q(1, "n1_2", 10.0), q(2, "n1_2", 20.0)]
    batch = take_batch(queue, 2)
    assert [b.req.request_id for b in batch] == ["r1", "r2"]
    assert [b.req.request_id for b in queue] == ["r3"]


def test_plan_batch_pools_compatible_and_separates_the_rest():
    entries = [q(1, "n1_2", 0), q(2, "n1_3", 0), q(3, "n3_1", 0, tol=0.0)]
    part, by_id = plan_batch(FakeHub(), entries, now=0.0)
    groups = sorted(g.request_ids for g in part.groups)
    assert ("r1", "r2") in groups and ("r3",) in groups
    assert set(by_id) == {"r1", "r2", "r3"}


def test_plan_batch_without_network_is_all_solo():
    class NoNet(FakeHub):
        network = None
    part, by_id = plan_batch(NoNet(), [q(1, "n1_2", 0)], now=0.0)
    assert part.groups == () and part.unserved == ("r1",)

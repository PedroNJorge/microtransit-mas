"""Unit tests for EnvironmentAgent logic. No XMPP server needed (stub network)."""
import math

import pytest

from agents.environment import EnvironmentAgent
from common import clock
from common.ontology import TrafficUpdate, deserialize, serialize


class StubNetwork:
    """Implements the two-method TrafficNetwork contract."""

    def __init__(self, weights):
        self.weights = dict(weights)
        self.applied = []

    def snapshot(self, update_id, timestamp):
        return TrafficUpdate(update_id, timestamp, dict(self.weights))

    def apply_traffic_update(self, upd):
        self.applied.append(upd)
        self.weights.update(upd.edge_weights)


W = {"a|b": 90.0, "b|a": 90.0, "b|c": 40.0}


@pytest.fixture
def net():
    return StubNetwork(W)


@pytest.fixture
def env(net):
    clock.reset()
    clock.start(speed=10)
    return EnvironmentAgent("env@localhost", "pw", net, full_every=4)


def test_first_update_is_full_then_deltas_when_nothing_changes(env):
    u1, u2, u3 = (env.next_traffic_update() for _ in range(3))
    assert u1.full_snapshot and u1.edge_weights == W
    assert not u2.full_snapshot and u2.edge_weights == {}
    assert not u3.full_snapshot and u3.edge_weights == {}


def test_every_nth_update_is_full(env):
    kinds = [env.next_traffic_update().full_snapshot for _ in range(9)]
    assert kinds == [True, False, False, False, True, False, False, False, True]


def test_full_every_one_means_always_full(net):
    env = EnvironmentAgent("e@localhost", "pw", net, full_every=1)
    assert all(env.next_traffic_update().full_snapshot for _ in range(3))


def test_delta_contains_only_changed_edges(env, net):
    env.next_traffic_update()
    net.weights["b|c"] = 55.0
    d = env.next_traffic_update()
    assert not d.full_snapshot and d.edge_weights == {"b|c": 55.0}
    assert env.next_traffic_update().edge_weights == {}          # change reported once


def test_receiver_reconstructs_state_from_full_plus_deltas(env, net):
    state = {}
    for step in range(10):
        if step in (2, 5):
            net.weights["a|b"] += 10
        if step == 7:
            net.weights["b|c"] *= 2
        u = env.next_traffic_update()
        if u.full_snapshot:
            state = dict(u.edge_weights)
        else:
            state.update(u.edge_weights)
        assert state == net.weights


def test_ids_increase_and_timestamps_do_not_go_back(env):
    ups = [env.next_traffic_update() for _ in range(3)]
    assert [u.update_id for u in ups] == ["tu-1", "tu-2", "tu-3"]
    assert ups[1].timestamp >= ups[0].timestamp <= ups[2].timestamp


def test_current_full_update_is_complete_and_uses_latest_id(env, net):
    env.next_traffic_update()
    net.weights["a|b"] = 100.0
    env.next_traffic_update()
    full = env.current_full_update()
    assert full.full_snapshot and full.update_id == "tu-2"
    assert full.edge_weights == net.weights


def test_updates_survive_serialisation(env, net):
    env.next_traffic_update()
    net.weights["a|b"] = 95.0
    for u in (env.current_full_update(), env.next_traffic_update()):
        assert deserialize(serialize(u), TrafficUpdate, strict=True) == u


# ---- traffic model ---------------------------------------------------------

def test_traffic_model_changes_are_applied_and_reported(net):
    clock.reset(); clock.start(speed=10)
    calls = []

    def model(weights, t):
        calls.append((dict(weights), t))
        return {"a|b": weights["a|b"] * 2} if len(calls) == 2 else {}

    env = EnvironmentAgent("e@localhost", "pw", net, traffic_model=model, full_every=50)
    assert env.next_traffic_update().edge_weights == W            # tick 1: model returns {}
    u2 = env.next_traffic_update()                                 # tick 2: a|b doubles
    assert u2.edge_weights == {"a|b": 180.0} and not u2.full_snapshot
    assert net.weights["a|b"] == 180.0 and len(net.applied) == 1
    assert env.next_traffic_update().edge_weights == {}


@pytest.mark.parametrize("bad", [0, -5, math.inf, math.nan, "x"])
def test_traffic_model_invalid_weight_rejected(net, bad):
    env = EnvironmentAgent("e@localhost", "pw", net, traffic_model=lambda w, t: {"a|b": bad})
    with pytest.raises(ValueError):
        env.next_traffic_update()
    assert net.weights == W                                        # nothing applied


def test_traffic_model_unknown_edge_rejected(net):
    env = EnvironmentAgent("e@localhost", "pw", net, traffic_model=lambda w, t: {"zz|yy": 5.0})
    with pytest.raises(KeyError):
        env.next_traffic_update()


# ---- construction / subscribers -------------------------------------------------

def test_subscribers_deduplicated_and_removable(net):
    env = EnvironmentAgent("e@localhost", "pw", net, subscribers=["x@l", "x@l", "y@l"])
    assert env.subscribers == ["x@l", "y@l"]
    env.add_subscriber("x@l")
    env.add_subscriber("z@l")                      # agent not running: just recorded
    env.remove_subscriber("y@l")
    env.remove_subscriber("nobody@l")
    assert env.subscribers == ["x@l", "z@l"]


@pytest.mark.parametrize("kw", [{"t_traffic": 0}, {"t_traffic": -1}, {"full_every": 0}])
def test_invalid_parameters_rejected(net, kw):
    with pytest.raises(ValueError):
        EnvironmentAgent("e@localhost", "pw", net, **kw)

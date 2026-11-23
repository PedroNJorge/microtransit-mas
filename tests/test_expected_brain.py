"""The expected-wins brain (policy "expected"): the euro-gain function, the driver's value per win and demand/supply/distance
percept, and the AgentSpeak plan that decides from the gains. No XMPP."""
import pytest

pytest.importorskip("spade_bdi")

from agents.brain import DriverBrain
from agents.driver import DriverAgent
from agents.expected_wins import VALUE_PRIOR, move_gains, value_per_win, win_chance
from common import clock
from common.graph import build_grid_network
from common.ontology import BrainPercept, HubLoad

DRIVER = "driver@localhost"


@pytest.fixture(autouse=True)
def sim_clock():
    clock.reset()
    clock.start(speed=30)
    yield
    clock.reset()


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


# ---- the pure functions ------------------------------------------------------------------------------------------------
def test_win_chance_is_an_equal_share_of_the_free_drivers():
    assert win_chance(3, includes_me=False) == pytest.approx(1 / 4)            # three there, I would be the fourth
    assert win_chance(2, includes_me=True) == pytest.approx(1 / 2)             # I am one of the two here
    assert win_chance(0, includes_me=True) == 1.0 and win_chance(0, includes_me=False) == 1.0


def test_value_per_win_starts_at_the_prior_and_follows_experience():
    assert value_per_win(0.0, 0) == VALUE_PRIOR
    assert value_per_win(15.0, 5) == pytest.approx((2.0 * 5 + 15.0) / 10)      # 2.5
    assert value_per_win(-100.0, 1) == 0.0                                     # never negative


def test_move_gain_worked_example():
    # I wait at hub_b with one other free driver (supply 2, demand 2.5 requests); hub_a has 3 free drivers and 10 requests, 3 km away.
    # stay: 2.5 x 1/2 = 1.25 wins ; move: 10 x 1/4 = 2.5 wins ; gain = 2.0 x (2.5 - 1.25) - 0.15 x 3 - 0.30 = 1.75
    g = move_gains(["hub_a", "hub_b"], "hub_b", [10.0, 2.5], [3.0, 2.0], [3000.0, 0.0], 2.0, 0.15, 0.30)
    assert set(g) == {"hub_a"} and g["hub_a"] == pytest.approx(1.75)
    # hub_a already has 9 free drivers: 10 x 1/10 = 1.0 win, below the 1.25 of staying: negative
    g = move_gains(["hub_a", "hub_b"], "hub_b", [10.0, 2.5], [9.0, 2.0], [3000.0, 0.0], 2.0, 0.15, 0.30)
    assert g["hub_a"] == pytest.approx(2.0 * (1.0 - 1.25) - 0.45 - 0.30) and g["hub_a"] < 0


def test_unknown_distances_and_missing_signal_give_no_gain():
    assert move_gains(["a", "b"], "b", [], [], [], 2.0, 0.15, 0.3) == {}
    assert move_gains(["a", "b"], "b", [9.0, 1.0], [0.0, 1.0], [-1.0, 0.0], 2.0, 0.15, 0.3) == {}


# ---- the driver's percept -----------------------------------------------------------------------------------------------
def driver(net):
    return DriverAgent("d@localhost", "pw", start_hub="n1_1", network=net, hub_jids=[], brain_jid="b@localhost")


def test_win_value_prior_then_own_experience(net):
    d = driver(net)
    assert d.win_value() == pytest.approx(2.0)
    d.payouts = [type("S", (), {"driver_payout": 9.0})()]
    d.trip_log = {"p1": {"pickup_m": 1000.0, "loaded_m": 3000.0, "done": 100.0}}        # 4 km -> 0.60 EUR of km cost
    assert d.win_value() == pytest.approx((2.0 * 5 + 9.0 - 0.6) / 6, abs=1e-3)


def test_percept_carries_demand_supply_distance_and_value(net):
    d = driver(net)
    assert d.percept(10.0).demands == [] and d.percept(10.0).win_value == pytest.approx(2.0)      # no signal heard
    d.loads["n4_4"] = (HubLoad("n4_4", 4.0, 1.0, 2.0, 5.0), 5.0)                     # 4 waiting, 1/min, 2 free drivers
    p = d.percept(10.0)
    i = p.hub_ids.index("n4_4")
    assert p.demands[i] == pytest.approx(1.0 * 5 + max(4.0 - 2.0, 0.0))                # arrivals over 300 s + waiting that the 2 free drivers cannot cover
    assert p.supplies[i] == 2.0 and p.dists_m[i] > 0
    assert p.dists_m[p.hub_ids.index("n1_1")] == 0.0                                   # standing on its own hub


# ---- the plan --------------------------------------------------------------------------------------------------------------
class FixedDice:
    def __init__(self, u):
        self.u = u

    def random(self):
        return self.u


def make_brain(u=0.0, **kw):
    b = DriverBrain("brain@localhost", "pw", DRIVER, policy="expected", **kw)
    b.rng = FixedDice(u)
    return b


def pump(brain, steps=30):
    for _ in range(steps):
        for trigger, goal_type, term, intention in list(brain.bdi_intention_buffer):
            brain.bdi_agent.call(trigger, goal_type, term, intention)
            brain.bdi_intention_buffer.popleft()
        brain.bdi_agent.step()


def percept(state="idle", at="hub_b", hubs=("hub_a", "hub_b", "hub_c"), etas=(300.0, 0.0, 50.0), demands=(10.0, 2.5, 0.0),
            supplies=(3.0, 2.0, 0.0), dists=(3000.0, 0.0, 400.0), value=2.0, t=0.0):
    return BrainPercept(DRIVER, state, at, at if at in hubs else "", "hub_b", list(hubs), list(etas), [0.0] * len(hubs), t,
                        [d - s for d, s in zip(demands, supplies)] if demands else [], list(demands), list(supplies),
                        list(dists), value)


def feed(brain, **kw):
    brain.apply_percept(percept(**kw))
    pump(brain)


def test_it_moves_where_the_expected_euro_gain_is_positive_and_largest():
    b = make_brain()
    feed(b)                                                    # hub_a: +1.75 ; hub_c: 0 requests, 0 - 1.25 wins -> negative
    assert list(b.commands) == ["hub_a"]


def test_it_stays_when_the_target_is_crowded_or_too_far():
    b = make_brain()
    feed(b, supplies=(9.0, 2.0, 0.0))                          # 10 x 1/10 = 1 win < 1.25 here
    assert list(b.commands) == []
    b = make_brain()
    feed(b, dists=(30000.0, 0.0, 400.0), etas=(2500.0, 0.0, 50.0))     # 30 km unpaid = 4.5 EUR and over 900 s away
    assert list(b.commands) == []


def test_a_low_value_per_win_makes_the_driver_stay():
    b = make_brain()
    feed(b, value=0.4)                                         # 0.4 x 1.25 = 0.5 - 0.45 - 0.30 < 0
    assert list(b.commands) == []


def test_it_stays_put_when_no_signal_was_heard_and_obeys_cooldown_and_state():
    b = make_brain()
    feed(b, demands=(), supplies=(), dists=())
    assert list(b.commands) == []
    feed(b, state="busy", t=10.0)
    assert list(b.commands) == []
    feed(b, t=20.0)
    assert list(b.commands) == ["hub_a"]
    feed(b, at="hub_a", etas=(0.0, 300.0, 350.0), demands=(10.0, 20.0, 0.0), supplies=(3.0, 0.0, 0.0), dists=(0.0, 3000.0, 3400.0), t=60.0)
    assert list(b.commands) == ["hub_a"]                       # cooling (600 s)


def test_herding_control_the_driver_moves_only_if_its_draw_is_below_one_over_the_free_drivers_here():
    b = make_brain(u=0.49)
    feed(b)                                                    # supply here = 2 -> chance 0.5 ; 0.49 < 0.5: moves
    assert list(b.commands) == ["hub_a"]
    b = make_brain(u=0.5)
    feed(b)                                                    # 0.5 < 0.5 is false: stays
    assert list(b.commands) == []
    b = make_brain(u=0.9)
    feed(b, supplies=(3.0, 1.0, 0.0), demands=(10.0, 1.0, 0.0))     # alone here: chance 1 -> always moves if the gain is positive
    assert list(b.commands) == ["hub_a"]

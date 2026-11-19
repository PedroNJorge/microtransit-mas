"""Hub-load signal (v2 brain): cells, the hub's HubLoad snapshot, the driver's deficit percept, and the AgentSpeak plans
that decide from them. No XMPP: agents are only constructed."""
import pytest

pytest.importorskip("spade_bdi")

from agents.brain import DriverBrain
from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from common import clock
from common.cells import deficit, nearest_hub
from common.graph import build_grid_network
from common.ontology import BrainPercept, DriverState, DriverStatus, HubLoad, TripRequest

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


# ---- cells and deficit -----------------------------------------------------------------------------------------------
def test_deficit_is_expected_demand_minus_supply():
    assert deficit(waiting=3, rate_per_min=1.0, supply=2) == pytest.approx(3 + 5 - 2)       # 300 s horizon
    assert deficit(0, 0.0, 4) == -4


def test_a_node_belongs_to_the_nearest_hub(net):
    cache = {}
    assert nearest_hub(net, "n1_2", cache) == "n1_1"
    assert nearest_hub(net, "n4_3", cache) == "n4_4"
    assert cache["n1_2"] == "n1_1"


# ---- the hub's snapshot ----------------------------------------------------------------------------------------------
def hub(net, hub_id="n1_1"):
    return MicrohubAgent("hub@localhost", "pw", hub_id=hub_id, network=net)


def status(loc, state=DriverState.IDLE, target=""):
    return DriverStatus("d@localhost", loc, state, 4, 0, 1e9, 0.0, target)


def test_snapshot_counts_idle_drivers_of_the_cell_and_cruisers_heading_in(net):
    h, now = hub(net), 100.0
    h.registry = {
        "a@localhost": (status("n1_2"), now),                                    # idle in the cell
        "b@localhost": (status("n4_3"), now),                                    # idle in another cell
        "c@localhost": (status("n4_3", target="n1_1"), now),                     # cruising here from another cell
        "d@localhost": (status("n1_2", target="n4_4"), now),                     # leaving: no longer ours
        "e@localhost": (status("n1_2", DriverState.ON_TRIP), now),               # busy
        "f@localhost": (status("n1_2"), now - 1000.0),                           # stale
    }
    load = h.load_snapshot(now)
    assert isinstance(load, HubLoad) and load.hub_id == "n1_1" and load.supply == 2.0


def test_snapshot_reports_arrivals_per_minute_over_ten_minutes_and_forgets_old_ones(net):
    h = hub(net)
    h.arrivals.extend([0.0, 100.0, 400.0, 590.0])
    assert h.load_snapshot(600.0).rate == pytest.approx(4 / 10.0)
    assert h.load_snapshot(900.0).rate == pytest.approx(2 / 10.0)                # 0 and 100 are older than 10 minutes


def test_waiting_counts_the_queue(net):
    from protocols.pool_formation import QueuedRequest
    h = hub(net)
    r = TripRequest("r1", "rider@localhost", "n1_1", "n1_3", 0.0, 3600.0, 600.0, 25.0, True, False, 0.0)
    h.queue.append(QueuedRequest(r, "rider@localhost", 0.0))
    assert h.load_snapshot(10.0).waiting == 1.0


# ---- the driver's percept --------------------------------------------------------------------------------------------
def driver(net):
    return DriverAgent("d@localhost", "pw", start_hub="n1_1", network=net, hub_jids=[], brain_jid="b@localhost")


def test_no_load_heard_means_no_deficits(net):
    assert driver(net).percept(10.0).deficits == []


def test_percept_carries_one_deficit_per_hub_and_ignores_stale_loads(net):
    d = driver(net)
    d.loads["n4_4"] = (HubLoad("n4_4", 4.0, 0.0, 1.0, 5.0), 5.0)
    d.loads["n1_4"] = (HubLoad("n1_4", 9.0, 0.0, 0.0, 5.0), 5.0 - 1000.0)        # received long ago
    p = d.percept(10.0)
    got = dict(zip(p.hub_ids, p.deficits))
    assert got["n4_4"] == pytest.approx(3.0) and got["n1_4"] == 0.0


def test_a_cruising_driver_tells_its_target_in_its_status(net):
    d = driver(net)
    assert d.status().target == ""
    d.activity, d.cruise_target = "cruising", "n4_4"
    assert d.status().target == "n4_4"


# ---- the plans --------------------------------------------------------------------------------------------------------
def make_brain(**kw):
    return DriverBrain("brain@localhost", "pw", DRIVER, policy="signal", **kw)


def pump(brain, steps=30):
    for _ in range(steps):
        for trigger, goal_type, term, intention in list(brain.bdi_intention_buffer):
            brain.bdi_agent.call(trigger, goal_type, term, intention)
            brain.bdi_intention_buffer.popleft()
        brain.bdi_agent.step()


def percept(state="idle", at="hub_a", hubs=("hub_a", "hub_b", "hub_c"), etas=(0.0, 300.0, 50.0),
            deficits=(-5.0, 8.0, 0.0), t=0.0):
    return BrainPercept(DRIVER, state, at, at if at in hubs else "", "hub_a", list(hubs), list(etas),
                        [0.0] * len(hubs), t, list(deficits))


def feed(brain, **kw):
    brain.apply_percept(percept(**kw))
    pump(brain)


def test_it_moves_to_a_cell_that_is_clearly_short_of_drivers():
    b = make_brain()
    feed(b)                                                    # b: min(8, 5) - 1 - 0.002*300 = 3.4 > 0 ; c has no deficit
    assert list(b.commands) == ["hub_b"]


def test_it_stays_when_the_gain_does_not_pay_for_the_drive():
    b = make_brain()
    feed(b, deficits=(-1.0, 8.0, 0.0))                         # min(8, 1) - 1 - 0.6 < 0: its cell has hardly a driver to spare
    assert list(b.commands) == []


def test_it_stays_put_when_no_hub_load_was_heard():
    b = make_brain()
    feed(b, deficits=())
    assert list(b.commands) == []


def test_a_hub_further_than_max_drive_is_never_chosen():
    b = make_brain()
    feed(b, etas=(0.0, 900.0, 50.0), deficits=(-5.0, 50.0, 0.0))
    assert list(b.commands) == []


def test_with_no_demand_anywhere_nobody_moves():
    b = make_brain()                                           # supply only: the old rule spread the drivers evenly
    feed(b, deficits=(-5.0, -3.0, -3.0))
    assert list(b.commands) == []


def test_it_does_not_leave_a_cell_that_has_no_driver_to_spare():
    b = make_brain()
    feed(b, deficits=(2.0, 9.0, 0.0))                          # its own cell is short too
    assert list(b.commands) == []


def test_standing_in_a_deficit_cell_means_staying():
    b = make_brain()
    feed(b, deficits=(5.0, 4.0, 0.0))                          # its own cell is the shortest of drivers
    assert list(b.commands) == []


def test_a_cruising_driver_is_not_redirected_and_a_busy_one_is_not_commanded():
    b = make_brain()
    feed(b)
    feed(b, state="cruising", at="x", etas=(250.0, 100.0, 60.0), deficits=(0.0, 0.0, 9.0), t=500.0)
    feed(b, state="busy", deficits=(0.0, 0.0, 9.0), t=600.0)
    assert list(b.commands) == ["hub_b"]


def test_the_cooldown_blocks_a_second_move_and_then_ends():
    b = make_brain()
    feed(b)
    feed(b, state="idle", at="hub_b", etas=(300.0, 0.0, 250.0), deficits=(5.0, -4.0, 9.0), t=30.0)
    assert list(b.commands) == ["hub_b"]                       # 30 s after the last move: cooling (600 s)
    feed(b, state="idle", at="hub_b", etas=(300.0, 0.0, 250.0), deficits=(5.0, -4.0, 9.0), t=700.0)
    assert list(b.commands) == ["hub_b", "hub_c"]                  # 600 s cooldown over; one command, to the best cell (c: min(9, 4) - 1 - 0.5 = 2.5 beats a: 2.4)


def test_the_policy_name_is_checked():
    with pytest.raises(ValueError):
        DriverBrain("brain@localhost", "pw", DRIVER, policy="nope")


# ---- v3: move with probability needed / spare --------------------------------------------------------------------------
class FixedDice:
    def __init__(self, u):
        self.u = u

    def random(self):
        return self.u


def make_prop(u):
    b = DriverBrain("brain@localhost", "pw", DRIVER, policy="proportional")
    b.rng = FixedDice(u)
    return b


def test_proportional_driver_moves_when_its_draw_is_below_needed_over_spare():
    b = make_prop(0.5)
    feed(b, deficits=(-8.0, 4.0, 0.0))                         # needs 4 of its 8 spare drivers: chance 0.5 -> 0.5 < 0.5 is false
    assert list(b.commands) == []
    b = make_prop(0.49)
    feed(b, deficits=(-8.0, 4.0, 0.0))
    assert list(b.commands) == ["hub_b"]


def test_proportional_driver_always_moves_when_the_need_covers_all_the_spare():
    b = make_prop(0.999)
    feed(b, deficits=(-5.0, 8.0, 0.0))                         # min(8, 5) / 5 = 1
    assert list(b.commands) == ["hub_b"]


def test_proportional_driver_still_needs_the_gain_test():
    b = make_prop(0.0)
    feed(b, deficits=(-1.0, 8.0, 0.0))                         # same stay as v2: its cell has hardly a driver to spare
    assert list(b.commands) == []


def test_a_fresh_draw_comes_with_each_change_of_the_published_load():
    b = make_prop(0.9)
    feed(b, deficits=(-8.0, 4.0, 0.0))
    assert list(b.commands) == []
    b.rng = FixedDice(0.1)
    feed(b, deficits=(-8.0, 4.0, 0.0))                         # same load: same draw, nothing new
    assert list(b.commands) == []
    feed(b, deficits=(-8.0, 4.5, 0.0), t=60.0)                 # load changed: new draw 0.1 < 0.5625
    assert list(b.commands) == ["hub_b"]
    assert [round(u, 1) for _, u in b.draws] == [0.9, 0.1]
    assert b.command_times == [60.0]


def test_the_default_dice_differ_between_brains_but_repeat_for_one_jid():
    a1 = DriverBrain("a@localhost", "pw", DRIVER, policy="proportional")
    a2 = DriverBrain("a@localhost", "pw", DRIVER, policy="proportional")
    b = DriverBrain("b@localhost", "pw", DRIVER, policy="proportional")
    assert a1.rng.random() == a2.rng.random()
    assert a1.rng.random() != b.rng.random()

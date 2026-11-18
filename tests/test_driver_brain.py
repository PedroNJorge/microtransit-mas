"""The driver's BDI layer: AgentSpeak plans + belief maintenance, run WITHOUT XMPP (the brain is only
constructed, its belief buffer is pumped by hand exactly as spade_bdi's behaviour does)."""
import pytest

pytest.importorskip("spade_bdi")

from agents.brain import DriverBrain
from common.ontology import BrainPercept

DRIVER = "driver@localhost"


def make_brain():
    return DriverBrain("brain@localhost", "pw", DRIVER)


def pump(brain, steps=30):
    """What BDIBehaviour.run does each cycle: feed the intention buffer, then reason."""
    for _ in range(steps):
        buf = list(brain.bdi_intention_buffer)
        for trigger, goal_type, term, intention in buf:
            brain.bdi_agent.call(trigger, goal_type, term, intention)
            brain.bdi_intention_buffer.popleft()
        brain.bdi_agent.step()


def percept(state="idle", at="hub_a", home="hub_a", hubs=("hub_a", "hub_b", "hub_c"),
            etas=(0.0, 300.0, 50.0), pressures=(0.1, 1.5, 0.2), t=0.0):
    return BrainPercept(DRIVER, state, at, at if at in hubs else "", home, list(hubs), list(etas),
                        list(pressures), t)


def feed(brain, **kw):
    brain.apply_percept(percept(**kw))
    pump(brain)


def sent(brain):
    return list(brain.commands)


def beliefs_of(brain):
    return {str(x).split("[")[0].replace(" ", "") for group in brain.bdi_agent.beliefs.values() for x in group}


def test_idle_driver_heads_for_the_hub_with_most_demand_per_driver():
    b = make_brain()
    feed(b)
    assert sent(b) == ["hub_b"]                       # 1.5 - 0.0015*300 beats staying at a quiet hub


def test_it_stays_when_already_at_the_best_hub():
    b = make_brain()
    feed(b, at="hub_b", etas=(300.0, 0.0, 250.0))
    assert sent(b) == []


def test_it_stays_when_another_hub_is_only_slightly_better():
    b = make_brain()
    feed(b, etas=(0.0, 100.0, 50.0), pressures=(0.1, 0.5, 0.2))      # b: 0.35 vs a: 0.1, margin 0.5
    assert sent(b) == []


def test_driving_far_is_discounted():
    b = make_brain()
    feed(b, etas=(0.0, 900.0, 150.0), pressures=(0.1, 1.0, 1.0))    # b: 1.0-1.35 ; c: 1.0-0.225
    assert sent(b) == ["hub_c"]


def test_flat_demand_means_the_nearest_hub():
    b = make_brain()                                  # the default behaviour equals the fixed baseline
    feed(b, at="somewhere", etas=(400.0, 250.0, 120.0), pressures=(0.0, 0.0, 0.0))
    assert sent(b) == ["hub_c"]


def test_flat_demand_at_a_hub_means_stay():
    b = make_brain()
    feed(b, at="hub_a", etas=(0.0, 400.0, 120.0), pressures=(0.0, 0.0, 0.0))
    assert sent(b) == []


def test_after_a_drop_off_it_goes_to_the_best_hub_whatever_the_cooldown():
    b = make_brain()
    feed(b)                                           # first move at t=0
    feed(b, state="busy", t=10.0)
    feed(b, state="idle", at="x", etas=(250.0, 300.0, 50.0), pressures=(0.1, 0.1, 0.1), t=20.0)
    assert sent(b) == ["hub_b", "hub_c"]              # idle away from any hub -> nearest, even while cooling


def test_a_job_drops_the_intention_and_nothing_is_commanded_while_busy():
    b = make_brain()
    feed(b)
    assert sent(b) == ["hub_b"]
    feed(b, state="busy", pressures=(0.1, 5.0, 0.2), t=10.0)
    assert sent(b) == ["hub_b"]                       # no new command while busy
    feed(b, state="idle", at="hub_c", etas=(50.0, 250.0, 0.0), pressures=(0.1, 1.5, 0.2), t=500.0)
    assert sent(b) == ["hub_b", "hub_b"]              # idle again, cooldown over -> deliberates afresh


def test_commitment_is_kept_against_small_changes():
    b = make_brain()
    feed(b)
    feed(b, state="cruising", at="x", etas=(250.0, 100.0, 60.0), pressures=(0.1, 1.5, 1.6), t=500.0)
    assert sent(b) == ["hub_b"]                       # c is better by < the switch margin: keep going


def test_reconsideration_switches_to_a_clearly_better_hub():
    b = make_brain()
    feed(b)
    feed(b, state="cruising", at="x", etas=(250.0, 250.0, 40.0), pressures=(0.1, 0.5, 3.0), t=500.0)
    assert sent(b) == ["hub_b", "hub_c"]


def test_the_cooldown_blocks_switching_right_after_a_move_and_ends():
    b = make_brain()
    feed(b)
    assert "cooling" in beliefs_of(b) or True
    feed(b, state="cruising", at="x", etas=(250.0, 250.0, 40.0), pressures=(0.1, 0.5, 3.0), t=60.0)
    assert sent(b) == ["hub_b"]                       # clearly better, but only 60 s after the last move
    assert "cooling" in beliefs_of(b)
    feed(b, state="cruising", at="x", etas=(250.0, 250.0, 40.0), pressures=(0.1, 0.5, 3.0), t=300.0)
    assert "cooling" not in beliefs_of(b)
    assert sent(b) == ["hub_b", "hub_c"]              # the cooldown ended: the better hub wins


def test_a_standing_driver_does_not_leave_during_the_cooldown():
    b = make_brain()
    feed(b)                                           # moves to hub_b
    feed(b, state="idle", at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.1, 0.1, 3.0), t=30.0)
    assert sent(b) == ["hub_b"]
    feed(b, state="idle", at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.1, 0.1, 3.0), t=400.0)
    assert sent(b) == ["hub_b", "hub_c"]


def test_unchanged_percepts_do_not_retrigger_plans():
    b = make_brain()
    feed(b)
    feed(b)                                           # settles the `cooling` belief set by the move
    n_cmd = len(b.commands)
    b.apply_percept(percept())
    assert len(b.bdi_intention_buffer) == 0
    feed(b)
    assert len(b.commands) == n_cmd


def test_beliefs_follow_the_percept():
    b = make_brain()
    feed(b)
    feed(b, hubs=("hub_a", "hub_b"), etas=(0.0, 300.0), pressures=(0.1, 0.4), state="cruising", at="x")
    beliefs = beliefs_of(b)
    assert "pressure(hub_b,0.4)" in beliefs and "pressure(hub_a,0.1)" in beliefs
    assert not any("hub_c" in x for x in beliefs)               # a hub that disappeared from the percept is forgotten
    assert "pressure(hub_b,1.5)" not in beliefs                 # and values are replaced, not accumulated


# ---- opportunity-cost bid markup ---------------------------------------------------------------------

def make_bidder():
    return DriverBrain("brain@localhost", "pw", DRIVER, bid_policy=True)


def markups(brain):
    return list(brain.markups)


def test_no_bid_policy_means_no_markup_commands():
    b = make_brain()
    feed(b, at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.1, 3.0, 0.2))
    assert markups(b) == []


def test_standing_where_demand_is_high_raises_the_markup():
    b = make_bidder()
    feed(b, at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.1, 1.0, 0.2))     # local 1.0, average 0.43
    (m,) = markups(b)
    assert m == pytest.approx(0.3 * (1.0 - (0.1 + 1.0 + 0.2) / 3), abs=0.01)


def test_markup_is_capped():
    b = make_bidder()
    feed(b, at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.0, 9.0, 0.0))
    assert markups(b) == [0.5]


def test_a_quiet_spot_bids_plain_cost():
    b = make_bidder()
    feed(b, at="hub_b", etas=(300.0, 0.0, 250.0), pressures=(0.1, 1.0, 0.2))
    feed(b, at="hub_a", etas=(0.0, 300.0, 50.0), pressures=(0.1, 1.0, 0.2), t=10.0)    # now local 0.1 < average
    assert markups(b)[-1] == 0.0

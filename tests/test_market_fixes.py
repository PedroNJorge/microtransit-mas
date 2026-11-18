"""Post-M9 robustness: tariff reserve, pickup-radius driver selection, retry wait, adaptive window,
demand generator, balanced hub choice. No XMPP traffic: agents are only constructed."""
import random
from types import SimpleNamespace

import pytest

from agents.microhub import MicrohubAgent
from agents.rider import balanced_hub_policy
from common import clock
from common.graph import build_grid_network
from common.ontology import DriverState, DriverStatus, TripRequest
from common.settings import DRIVER_BASE_COST, DRIVER_COST_PER_SECOND
from harness.demand import DemandGenerator
from protocols.pool_formation import QueuedRequest, batch_due

HUB = "n1_1"


@pytest.fixture(scope="module")
def net():
    return build_grid_network()


@pytest.fixture(autouse=True)
def sim_clock():
    clock.reset()
    clock.start(speed=30)
    yield
    clock.reset()


def hub(net, **kw):
    return MicrohubAgent("hub@localhost", "pw", hub_id=HUB, network=net, **kw)


def req(dest, tol=600.0, end=3600.0):
    return TripRequest("r1", "rider@localhost", HUB, dest, 0.0, end, tol, 25.0, True, False, 0.0)


def status(jid, loc, state=DriverState.IDLE):
    return DriverStatus(jid, loc, state, 4, 0, 1e9, 0.0)


def test_tariff_reserve_is_off_by_default_and_bounded_when_on(net):
    assert hub(net).tariff_reserve([req("n1_3")], [], 0) is None
    h = hub(net, reserve_margin=1.5, pickup_allowance=300.0)
    r = h.tariff_reserve([req("n1_3")], ["n1_3"], 0)
    route = net.travel_time(HUB, "n1_3")
    assert r == pytest.approx(1.5 * (DRIVER_BASE_COST + DRIVER_COST_PER_SECOND * (route + 300)), abs=0.011)
    assert r < 25.0                                              # far below the riders' max fare
    assert h.tariff_reserve([req("n1_3")], ["n1_3"], 1) > r      # a retry allows a longer pickup leg


def test_a_driver_at_the_hub_can_always_bid_under_the_tariff_reserve(net):
    h = hub(net, reserve_margin=1.5)
    reserve = h.tariff_reserve([req("n1_3")], ["n1_3"], 0)
    cost_at_hub = DRIVER_BASE_COST + DRIVER_COST_PER_SECOND * net.travel_time(HUB, "n1_3")
    assert cost_at_hub <= reserve


def test_pickup_radius_filters_far_drivers_and_widens_on_retry(net):
    h = hub(net, max_pickup_eta=100.0)
    h.driver_jids = ["near@localhost", "far@localhost", "busy@localhost", "unknown@localhost"]
    now = clock.now()
    h.registry = {"near@localhost": (status("near@localhost", "n1_2"), now),
                  "far@localhost": (status("far@localhost", "n4_4"), now),
                  "busy@localhost": (status("busy@localhost", "n1_1", DriverState.EN_ROUTE_PICKUP), now)}
    assert h.eligible_drivers(now) == ["near@localhost", "unknown@localhost"]
    far_eta = h.hub_eta("far@localhost", HUB, now)
    assert far_eta > 300
    h.max_pickup_eta = far_eta - 300
    assert "far@localhost" not in h.eligible_drivers(now)             # first attempt: too far
    assert "far@localhost" in h.eligible_drivers(now, attempt=1)      # the retry widens the radius


def test_without_radius_everyone_idle_is_called(net):
    h = hub(net)
    h.driver_jids = ["far@localhost"]
    h.registry = {"far@localhost": (status("far@localhost", "n4_4"), clock.now())}
    assert h.eligible_drivers(clock.now()) == ["far@localhost"]


def test_retry_wait_is_longer_when_drivers_were_locked_elsewhere(net):
    h = hub(net, retry_delay=20.0, t_auction=60.0, t_award=40.0)
    ctx = SimpleNamespace(recipients=["a", "b", "c", "d"], refusals={"a": "locked", "b": "locked", "c": "over_reserve"})
    assert h.retry_wait(ctx) == 100.0
    ctx.refusals = {"a": "over_reserve", "b": "over_reserve"}
    assert h.retry_wait(ctx) == 20.0
    assert h.retry_wait(SimpleNamespace(recipients=[], refusals={})) == 100.0       # nobody free to call
    assert hub(net).retry_wait(ctx) == 0.0                                          # disabled by default


def test_adaptive_window_closes_for_an_urgent_rider():
    q = [QueuedRequest(req("n1_2", end=500.0), "x", 0.0)]
    assert not batch_due(q, 180.0, 100.0, 8)
    assert not batch_due(q, 180.0, 100.0, 8, slack=120.0)
    assert batch_due(q, 180.0, 400.0, 8, slack=120.0)          # window ends in 100 s < slack


def test_demand_is_seeded_clustered_and_relative(net):
    homes = net.nodes_of_type("residential")
    a = DemandGenerator(net, homes, seed=4, hotspots=2, spread=5, background=0.0).generate(40, 1000.0)
    b = DemandGenerator(net, homes, seed=4, hotspots=2, spread=5, background=0.0).generate(40, 1000.0)
    assert a == b
    assert len({d.destination for d in a}) <= 10                      # 2 hotspots x 5 homes
    assert all(0 <= d.at <= 1000 for d in a) and [d.at for d in a] == sorted(d.at for d in a)
    assert all(0.2 <= d.tol_frac <= 0.5 for d in a)
    uni = DemandGenerator(net, homes, seed=4, clustered=False).generate(200, 1000.0)
    assert len({d.destination for d in uni}) > 20


def test_balanced_policy_spreads_over_close_hubs_but_not_far_ones():
    choose = balanced_hub_policy(random.Random(1))
    picks = {choose([("a", 100.0), ("b", 120.0), ("far", 900.0)]) for _ in range(200)}
    assert picks == {"a", "b"}
    assert choose([("only", 50.0)]) == "only"

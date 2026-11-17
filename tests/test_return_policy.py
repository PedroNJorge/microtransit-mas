"""Fixed baseline: after a trip an idle driver drives back to a hub (no BDI)."""
import pytest

from agents.driver import DriverAgent
from common import clock
from common.graph import build_grid_network


@pytest.fixture
def net():
    return build_grid_network()


def mk(net, policy, start="n1_1"):
    return DriverAgent("d@localhost", "pw", start_hub=start, network=net, hub_jids=[], return_policy=policy)


def test_none_stays_put(net):
    d = mk(net, "none"); d.location = "n3_3"
    assert d.return_target() == ""


def test_start_hub_returns_to_start(net):
    d = mk(net, "start_hub"); d.location = "n3_3"
    assert d.return_target() == "n1_1"


def test_nearest_hub_picks_closest(net):
    clock.reset(); clock.start(speed=30)
    try:
        d = mk(net, "nearest_hub"); d.location = "n1_1"
        assert d.return_target() == "n1_1"
        hubs = sorted(net.hubs)
        if len(hubs) > 1:
            d.location = hubs[-1]
            assert d.return_target() == hubs[-1]
    finally:
        clock.reset()

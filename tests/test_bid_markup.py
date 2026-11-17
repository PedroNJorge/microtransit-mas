"""Opportunity-cost markup: bids are cost * (1 + markup); the markup is set by the brain's BidPolicy."""
import pytest

from agents.driver import DriverAgent
from common import clock
from common.graph import build_grid_network
from common.ontology import BidPolicy, CFP


@pytest.fixture
def net():
    return build_grid_network()


def driver(net):
    return DriverAgent("d@localhost", "pw", start_hub="n1_1", network=net, hub_jids=[])


def cfp(fare=20.0):
    return CFP("pool-1", "hub@localhost", "n1_1", ["n1_3"], {"req-0": [0.0, 1e9]}, 1,
               clock.now() + 300, fare, 600.0, 0, rider_jids=["rider@localhost"])


def test_default_markup_bids_at_cost(net):
    d = driver(net)
    offer, why = d.calculate_bid(cfp(), 0.0)
    assert why is None and d.bid_markup == 0.0
    cost = d.base_cost + d.cost_per_second * (0.0 + offer.route_time)
    assert offer.bid.price == pytest.approx(cost, abs=0.011)


def test_markup_raises_the_price_proportionally(net):
    d = driver(net)
    base = d.calculate_bid(cfp(), 0.0)[0].bid.price
    d.bid_markup = 0.25
    assert d.calculate_bid(cfp(), 0.0)[0].bid.price == pytest.approx(base * 1.25, abs=0.011)


def test_markup_above_the_reserve_means_no_bid(net):
    d = driver(net)
    price = d.calculate_bid(cfp(), 0.0)[0].bid.price
    d.bid_markup = 0.5
    offer, why = d.calculate_bid(cfp(fare=price * 1.2), 0.0)
    assert offer is None and why == "over_reserve"


def test_bidpolicy_validates_range():
    with pytest.raises(ValueError):
        BidPolicy("d@localhost", 1.5)


# ---- map viewer payload ---------------------------------------------------------------------------

def test_viz_payload_has_what_the_map_needs():
    from common.graph import TransitNetwork
    net = TransitNetwork.from_file("config/porto_graph.json")
    hub = sorted(net.hubs)[0]
    d = DriverAgent("driver_07@localhost", "pw", start_hub=hub, network=net, hub_jids=[])
    p = d.viz_payload()
    assert set(p) == {"driver_id", "state", "lat", "lon", "passengers", "capacity", "rider_ids"}
    assert p["driver_id"] == "driver_07" and p["state"] == "IDLE" and p["rider_ids"] == []
    assert 40 < p["lat"] < 42 and -9 < p["lon"] < -8                      # Porto
    d.activity = "cruising"
    assert d.viz_payload()["state"] == "CRUISING"
    d.activity = "repositioning"
    assert d.viz_payload()["state"] == "REPOSITIONING"
    d.broadcast_viz()                                                     # no event loop: must simply do nothing


def test_viz_payload_is_none_without_coordinates(net):
    d = driver(net)                                                       # the test grid has no lat/lon
    assert d.viz_payload() is None

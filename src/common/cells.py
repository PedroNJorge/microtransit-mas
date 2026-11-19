"""Hub cells and the deficit drivers read from a hub's published load.

A hub's CELL is the part of the map nearer to it than to any other hub (a Voronoi cell by straight-line distance;
by travel time where a node has no coordinates). It is only a way to say whose drivers a driver is: a free driver
standing in a cell is supply of that cell's hub, a free driver cruising to a hub is supply of that hub.

DEFICIT of a cell = demand expected over the next HUB_LOAD_HORIZON seconds - supply, where
    demand = requests waiting for a driver + arrival rate x horizon.
Positive: the cell is short of drivers (riders will wait); negative: it has drivers to spare."""
from __future__ import annotations

import math
from typing import Dict, Optional

from common.settings import HUB_LOAD_HORIZON


def _coords(network, node: str):
    data = network.g.nodes.get(node) if network is not None else None
    if data and data.get("lat") is not None and data.get("lon") is not None:
        return float(data["lat"]), float(data["lon"])
    return None


def nearest_hub(network, node: str, cache: Optional[Dict[str, str]] = None) -> str:
    """The hub whose cell `node` is in ('' if the network has no hubs)."""
    if cache is not None and node in cache:
        return cache[node]
    best, best_d = "", math.inf
    here = _coords(network, node)
    for hub in sorted(network.hubs) if network is not None else []:
        there = _coords(network, hub)
        if here is not None and there is not None:
            dy = (here[0] - there[0]) * 111.2
            dx = (here[1] - there[1]) * 111.2 * math.cos(math.radians((here[0] + there[0]) / 2))
            d = math.hypot(dx, dy)
        else:
            try:
                d = network.travel_time(node, hub, 0.0)
            except KeyError:
                continue
        if d < best_d:
            best, best_d = hub, d
    if cache is not None:
        cache[node] = best
    return best


def deficit(waiting: float, rate_per_min: float, supply: float, horizon: float = HUB_LOAD_HORIZON) -> float:
    return waiting + rate_per_min * horizon / 60.0 - supply

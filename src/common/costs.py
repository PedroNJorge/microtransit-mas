"""Driver cost model: the driver's true cost of a piece of work.

    cost = base + per_km x km + per_second x (driving seconds + dwell seconds)

Defaults come from common/settings.py (0.18 EUR/km, 5.60 EUR/h, no fixed fee). Driving is charged whether the vehicle
is loaded, on its way to a pickup or empty (cruising, repositioning); boarding and alighting are charged too
(DWELL_PICKUP once, DWELL_DROPOFF per drop-off). Idle time is free."""
from __future__ import annotations

from common.settings import (DRIVER_BASE_COST, DRIVER_COST_PER_KM, DRIVER_COST_PER_SECOND, DWELL_DROPOFF, DWELL_PICKUP,
                             NOMINAL_SPEED_KMH)


def dwell_seconds(dropoffs: int, pickup: bool = True) -> float:
    return (DWELL_PICKUP if pickup else 0.0) + DWELL_DROPOFF * max(0, dropoffs)


def driver_cost(km: float, seconds: float, dwell: float = 0.0, per_km: float = DRIVER_COST_PER_KM,
                per_second: float = DRIVER_COST_PER_SECOND, base: float = DRIVER_BASE_COST) -> float:
    return base + per_km * km + per_second * (seconds + dwell)


def rate_per_second(per_km: float = DRIVER_COST_PER_KM, per_second: float = DRIVER_COST_PER_SECOND) -> float:
    """EUR per second of driving at the nominal urban speed (distance and time folded into one rate), for estimates
    where only seconds are known (posted solo tariff, late-join pay, repositioning incentive)."""
    return per_second + per_km * NOMINAL_SPEED_KMH / 3600.0

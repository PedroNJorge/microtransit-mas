"""The driver's rolling-surplus bid rule (pure functions, no agent state).

    rolling_surplus = window_revenue - cost_per_km * window_km - wage * window_hours      (euros)
    tau (EUR/h)     = wage * clamp(1 - rolling_surplus / scale, 0, 1)                       the price put on the driver's time
    bid             = base + cost_per_km * km + tau/3600 * seconds     (km cost is a floor: tau >= 0 never lowers it)

A driver at or behind the wage yardstick (surplus <= 0) values its time at the full wage; one that is `scale` euros ahead
values it at 0 and bids km cost only; in between it is linear. scale = 0 is the hard step (tau = wage if surplus <= 0 else 0).
The window is the last `window` seconds (or since the start while the run is younger), counted as duty time: window_hours is
the window length, not only the driving time. Costs are booked when a trip ends (empty driving when it is driven)."""
from typing import Iterable, Tuple


def window_net(now: float, window: float, per_km: float, payments: Iterable[Tuple[float, float]],
               km_events: Iterable[Tuple[float, float]], since: float = 0.0) -> Tuple[float, float]:
    """(revenue - km cost, hours) over the last `window` seconds, or since `since` (when the driver went on duty) if younger.
    payments: (time, euros) pairs; km_events: (time, metres) pairs. Events with now - window < time <= now count."""
    lo = now - window
    revenue = sum(e for t, e in payments if lo < t <= now)
    km = sum(m for t, m in km_events if lo < t <= now) / 1000.0
    hours = max(min(window, now - since), 0.0) / 3600.0
    return revenue - per_km * km, hours


def window_surplus(now: float, window: float, wage: float, per_km: float,
                   payments: Iterable[Tuple[float, float]], km_events: Iterable[Tuple[float, float]],
                   since: float = 0.0) -> float:
    net, hours = window_net(now, window, per_km, payments, km_events, since)
    return net - wage * hours


def time_value(surplus: float, wage: float, scale: float) -> float:
    """EUR per hour the driver puts on its time, from its rolling surplus."""
    if scale <= 0:
        return wage if surplus <= 0 else 0.0
    return wage * min(1.0, max(0.0, 1.0 - surplus / scale))

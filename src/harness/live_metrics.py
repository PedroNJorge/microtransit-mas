"""
src/harness/live_metrics.py   The metrics of a run: one definition, used by the WebSocket viewer and by the experiments.
(metrics.py is the older, separate "where does a rider's wait go" breakdown.)

A `MetricsCollector` looks at the agents of a Harness every `every` sim seconds and produces a snapshot:

    {"t": sim seconds, "window": W,
     "cum":  {metric: value over the whole run so far},
     "roll": {metric: value over the last W sim seconds},
     "now":  {queue_len, idle_drivers, ...}}            # instantaneous

Snapshots are kept in `history`, sent to the viewer (event "METRICS", replayed to a browser that connects later) and can be
written to a file by an experiment. Values that cannot be computed yet (no trip so far) are None.

Definitions (units: EUR, km, kg, seconds)
  Pooling and environment
    riders_per_vehicle     riders / completed vehicle-trips (a pool, late joiners included)
    pax_km_per_veh_km      direct passenger-km (each rider's own hub->home route) / vehicle-km of the trips (loaded + pickup)
    pooled_share           share of riders that rode in a vehicle with at least one other rider
    late_share             share of riders that joined a pool after it had a driver
    size_1 .. size_4       share of vehicle-trips with 1..4 riders
    co2_saved_loaded_kg    LOADED-LEG saving: sum(riders' separate routes - shared route) x g/km. What the incentive pays on.
    co2_saved_all_kg       ALL-MILES saving: the loaded-leg saving plus the pickup legs the riders would each have needed
                           alone ((riders - 1) x the pool's pickup leg). Cruising / repositioning is the same in both worlds.
    co2_saved_all_pct      all-miles saving / what the same trips would have driven alone (pickup + loaded)
    km_loaded, km_pickup, km_other   vehicle-km by activity (other = cruising, returning, repositioning)
  Riders (per rider)
    fare_gross / subsidy / incentive / fare_net   what the driver was paid for the rider; rebate; CO2 incentive; gross - both
    fare_per_min           gross fare per minute of the rider's direct ride
    pooling_discount       solo reference fare - fare paid (pooled riders only)
    wait_mean / wait_p90 / wait_max   request -> boarding
    detour_mean            ride time / direct ride time
    completion_rate        completed / ended trips (cumulative only);  failures = ended trips by reason
  Drivers (per vehicle-trip unless stated)
    revenue_per_trip, profit_per_trip (revenue - base cost - cost per second x (pickup + loaded seconds))
    earnings_per_hour      revenue per driver per hour
    util_<state>           share of driver-time loaded / pickup / idle / cruising / repositioning (sampled each tick)
    deadhead_share         empty km (pickup + other) / all km
    gini                   inequality of the drivers' earnings (0 = equal)
  Market
    time_to_match          request -> matched
    auction_attempts       mean auction attempts per pool (cumulative)
    subsidy_spent, pooling_spent, pooling_eur_per_kg   fund spending (cumulative)
    now: queue_len, idle_drivers, failover_adopted
"""
import asyncio
import logging
import math
from typing import Dict, List, Optional

from common import clock
from common.settings import DRIVER_BASE_COST, DRIVER_COST_PER_SECOND, EMISSION_G_PER_KM

logger = logging.getLogger(__name__)

STATES = ("loaded", "pickup", "idle", "cruising", "repositioning")
ODO_KINDS = ("loaded", "pickup", "other")


def gini(xs: List[float]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    n, total = len(xs), sum(xs)
    if n < 2 or total <= 0:
        return None
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs)) / (n * total)


def mean(xs) -> Optional[float]:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def percentile(xs, p: float) -> Optional[float]:
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, math.ceil(p * len(xs)) - 1))]


def _activity_state(d) -> str:
    return {"on_trip": "loaded", "to_pickup": "pickup", "cruising": "cruising",
            "repositioning": "repositioning"}.get(getattr(d, "activity", "idle"), "idle")


class MetricsCollector:
    def __init__(self, harness, window: float = 600.0, every: float = 60.0, broadcast: bool = True):
        self.h = harness
        self.window, self.every, self.broadcast = window, every, broadcast
        self.history: List[dict] = []
        self._util: List[tuple] = []             # (t, {state: seconds}) per interval
        self._odo: List[tuple] = []              # (t, {kind: metres}) cumulative totals
        self._last_t: Optional[float] = None
        self._task: Optional[asyncio.Task] = None

    def _by_role(self, role: str):
        return [a for a in self.h.agents.values() if getattr(a, "role", "") == role]

    # ---- records: what happened, with the sim time it happened ---------------------------------------------------
    def rider_records(self) -> List[dict]:
        recs = []
        for r in self._by_role("rider"):
            for t in getattr(r, "trips", []):
                if t.outcome != "completed" or not t.dropped_at:
                    continue
                s = t.settlement
                gross = s.driver_payout if s is not None else t.fare
                subsidy = s.subsidy_applied if s is not None else 0.0
                incentive = getattr(s, "pooling_incentive", 0.0) if s is not None else 0.0
                direct = None
                if getattr(r, "network", None) is not None:
                    try:
                        d = r.network.travel_time(t.request.origin_node, t.request.destination_home, t.requested_at)
                        direct = None if math.isinf(d) else d
                    except KeyError:
                        pass
                recs.append(dict(
                    t=t.dropped_at, gross=gross, subsidy=subsidy, incentive=incentive, net=gross - subsidy - incentive,
                    discount=(s.pooling_discount if s is not None else 0.0), direct=direct,
                    wait=t.boarded_at - t.requested_at, ride=t.dropped_at - t.boarded_at,
                    match=(t.matched_at - t.requested_at) if t.matched_at else None))
        return recs

    def failures(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self._by_role("rider"):
            for t in getattr(r, "trips", []):
                if t.outcome and t.outcome != "completed":
                    key = t.reason or t.outcome
                    out[key] = out.get(key, 0) + 1
        return out

    def pool_records(self) -> List[dict]:
        """One record per vehicle-trip that has ended."""
        metric = {m.pool_id: m for h in self._by_role("microhub") for m in h.pool_metrics}
        recs = []
        for d in self._by_role("driver"):
            for pool, a in d.finished_trips.items():
                log, m = d.trip_log.get(pool), metric.get(pool)
                if not log or log.get("done") is None:
                    continue
                late = sum(1 for x in a.riders if x[3] != pool and x[0] in a.dropped)
                recs.append(dict(
                    t=log["done"], riders=len(a.dropped), late=late,
                    shared_m=m.shared_m if m else log["loaded_m"], alone_m=m.alone_m if m else log["loaded_m"],
                    pickup_m=log["pickup_m"], seconds=log["pickup_s"] + log["loaded_s"]))
        return recs

    def payout_records(self) -> List[tuple]:
        return [(t, str(d.jid.bare), s.driver_payout) for d in self._by_role("driver")
                for s, t in zip(d.payouts, d.payout_times)]

    # ---- the numbers ---------------------------------------------------------------------------------------------
    def compute(self, now: float, lo: float, riders, pools, payouts) -> Dict[str, Optional[float]]:
        """Every event-based metric over events with lo < time <= now."""
        R = [r for r in riders if lo < r["t"] <= now]
        P = [p for p in pools if lo < p["t"] <= now and p["riders"] > 0]
        Y = [y for y in payouts if lo < y[0] <= now]
        g = next((h.emission_g_per_km for h in self._by_role("microhub")), EMISSION_G_PER_KM)
        out: Dict[str, Optional[float]] = {}
        n_riders = sum(p["riders"] for p in P)

        out["riders_per_vehicle"] = n_riders / len(P) if P else None
        veh_m = sum(p["shared_m"] + p["pickup_m"] for p in P)
        out["pax_km_per_veh_km"] = sum(p["alone_m"] for p in P) / veh_m if veh_m > 0 else None
        out["pooled_share"] = sum(p["riders"] for p in P if p["riders"] > 1) / n_riders if n_riders else None
        out["late_share"] = sum(p["late"] for p in P) / n_riders if n_riders else None
        for k in (1, 2, 3, 4):
            out[f"size_{k}"] = sum(1 for p in P if p["riders"] == k) / len(P) if P else None
        saved_loaded = sum(p["alone_m"] - p["shared_m"] for p in P)
        saved_pickup = sum((p["riders"] - 1) * p["pickup_m"] for p in P)
        baseline = sum(p["alone_m"] + p["riders"] * p["pickup_m"] for p in P)
        out["co2_saved_loaded_kg"] = saved_loaded * g / 1e6 if P else None
        out["co2_saved_all_kg"] = (saved_loaded + saved_pickup) * g / 1e6 if P else None
        out["co2_saved_all_pct"] = (saved_loaded + saved_pickup) / baseline if baseline > 0 else None

        out["fare_gross"] = mean(r["gross"] for r in R)
        out["subsidy"] = mean(r["subsidy"] for r in R)
        out["incentive"] = mean(r["incentive"] for r in R)
        out["fare_net"] = mean(r["net"] for r in R)
        out["fare_per_min"] = mean(r["gross"] / (r["direct"] / 60.0) for r in R if r["direct"])
        out["pooling_discount"] = mean(r["discount"] for r in R if r["discount"] > 0)
        waits = [r["wait"] for r in R]
        out["wait_mean"], out["wait_p90"], out["wait_max"] = mean(waits), percentile(waits, 0.9), max(waits, default=None)
        out["detour_mean"] = mean(r["ride"] / r["direct"] for r in R if r["direct"])
        out["time_to_match"] = mean(r["match"] for r in R if r["match"] is not None)

        revenue = sum(y[2] for y in Y)
        cost = sum(DRIVER_BASE_COST + DRIVER_COST_PER_SECOND * p["seconds"] for p in P)
        out["revenue_per_trip"] = revenue / len(P) if P else None
        out["profit_per_trip"] = (revenue - cost) / len(P) if P else None
        n_drivers = max(1, len(self._by_role("driver")))
        span = now - max(lo, 0.0)
        out["earnings_per_hour"] = revenue / n_drivers / (span / 3600.0) if span > 0 else None
        by_driver = {str(x.jid.bare): 0.0 for x in self._by_role("driver")}
        for _, d, amount in Y:
            by_driver[d] = by_driver.get(d, 0.0) + amount
        out["gini"] = gini(list(by_driver.values()))
        return out

    @staticmethod
    def _window_sum(series: List[tuple], now: float, lo: float) -> Dict[str, float]:
        tot: Dict[str, float] = {}
        for t, d in series:
            if lo < t <= now:
                for k, v in d.items():
                    tot[k] = tot.get(k, 0.0) + v
        return tot

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = clock.now() if now is None else now
        drivers = self._by_role("driver")
        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        util = {s: 0.0 for s in STATES}
        for d in drivers:
            util[_activity_state(d)] += dt
        odo = {k: sum(d.odo_m[k] for d in drivers) for k in ODO_KINDS}
        self._util.append((now, util))
        self._odo.append((now, odo))
        self._last_t = now

        riders, pools, payouts = self.rider_records(), self.pool_records(), self.payout_records()
        lo = now - self.window
        cum = self.compute(now, -math.inf, riders, pools, payouts)
        roll = self.compute(now, lo, riders, pools, payouts)
        for metrics, since in ((cum, -math.inf), (roll, lo)):
            u = self._window_sum(self._util, now, since)
            tot = sum(u.values())
            for s in STATES:
                metrics[f"util_{s}"] = u.get(s, 0.0) / tot if tot > 0 else None
            before = next((o for t, o in reversed(self._odo) if t <= since), dict.fromkeys(ODO_KINDS, 0.0))
            km = {k: (odo[k] - before[k]) / 1000.0 for k in ODO_KINDS}
            metrics["km_loaded"], metrics["km_pickup"], metrics["km_other"] = km["loaded"], km["pickup"], km["other"]
            allkm = sum(km.values())
            metrics["deadhead_share"] = (km["pickup"] + km["other"]) / allkm if allkm > 0 else None

        hubs = self._by_role("microhub")
        trips = [t for r in self._by_role("rider") for t in getattr(r, "trips", []) if t.outcome]
        done = sum(1 for t in trips if t.outcome == "completed")
        cum["completion_rate"] = done / len(trips) if trips else None
        cum["auction_attempts"] = mean(c.attempt + 1 for h in hubs for c in getattr(h, "finished", []))
        pay = next(iter(self._by_role("payment")), None)
        cum["subsidy_spent"] = pay.ledger.subsidy_spent if pay is not None else None
        cum["pooling_spent"] = pay.ledger.pooling_spent if pay is not None else None
        kg = cum["co2_saved_loaded_kg"]
        cum["pooling_eur_per_kg"] = cum["pooling_spent"] / kg if kg and cum["pooling_spent"] is not None else None
        snap = {"t": now, "window": self.window, "cum": cum, "roll": roll, "failures": self.failures(),
                "now": {"queue_len": sum(len(h.queue) for h in hubs),
                        "idle_drivers": sum(h.idle_driver_count(now) for h in hubs),
                        "failover_adopted": sum(len(h.adopted) for h in hubs),
                        "drivers": len(drivers)}}
        self.history.append(snap)
        return snap

    # ---- running -------------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        from common.ws_server import broadcast_event
        while True:
            await clock.sim_sleep(self.every)
            try:
                snap = self.snapshot()
                if self.broadcast:
                    await broadcast_event("METRICS", snap)
            except Exception:                              # one bad snapshot must not stop the run
                logger.exception("metrics snapshot failed")

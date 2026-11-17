"""
src/harness/metrics.py   Where does a rider's wait go?

wait = boarded_at - requested_at, split into consecutive phases (all in sim seconds, from timestamps the
agents record as they work):

  batch        request -> the pool proposal reaches the rider (batch window T_pool, or ~0 for a rider who
               does not pool; includes re-pooling after a co-rider declined)
  confirm      proposal -> the pool is waiting for an auction (rider accepts, hub confirms, escrow is held)
  queue        waiting in the hub's auction queue behind other pools (one auction at a time per hub)
  auction      first auction start -> match (CFP, bids, award, retries and their pauses)
  pickup       match -> the driver reports arrival at the pickup (the driver's drive; includes the
               DriverArriving message)
  boarding     arrival -> the rider is on board (rider walks to the car + boarding dwell)

The phases add up to the total by construction; `check` is the rounding left over.
"""
from typing import Dict, Optional

PHASES = ("batch", "confirm", "queue", "auction", "pickup", "boarding")


def wait_breakdown(trip, pool_times: Dict[str, float], history) -> Optional[Dict[str, float]]:
    """`trip` = rider TripRecord, `pool_times` = MicrohubAgent PoolRecord.times of its pool,
    `history` = RiderAgent.history [(time, from, to)]. None if the trip never boarded or a milestone is missing."""
    if not trip.boarded_at:
        return None
    proposal = [t for t, _, to in history if to == "DECIDING" and t >= trip.requested_at]
    need = ("queued", "auction_start", "matched")
    if not proposal or any(k not in pool_times for k in need):
        return None
    t = [trip.requested_at, proposal[-1], pool_times["queued"], pool_times["auction_start"],
         pool_times["matched"], max(trip.arrived_at, pool_times["matched"]), trip.boarded_at]
    out = {name: max(0.0, b - a) for name, a, b in zip(PHASES, t, t[1:])}
    out["total"] = trip.boarded_at - trip.requested_at
    out["check"] = out["total"] - sum(out[p] for p in PHASES)
    return out


def summarize(rows):
    """rows: list of breakdown dicts -> {phase: (mean, median, p90)} plus each phase's share of the mean total."""
    out = {}
    n = len(rows)
    if not n:
        return out
    mean_total = sum(r["total"] for r in rows) / n
    for k in (*PHASES, "total"):
        v = sorted(r[k] for r in rows)
        out[k] = (sum(v) / n, v[n // 2], v[min(n - 1, int(0.9 * n))],
                  (sum(v) / n) / mean_total if mean_total else 0.0)
    return out

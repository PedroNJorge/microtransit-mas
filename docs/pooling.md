# Pooling (Milestone 9)

## Problem and why OR rather than a heuristic
Last-mile pooling is a degenerate Dial-a-Ride Problem: every pickup is the hub, so only the drop-off
sequence is a decision, and a vehicle carries at most 4 riders. At that size exact OR methods are
cheap and provably optimal, so `common/pooling.py` uses them:

| Decision | Method | Optimal? |
|---|---|---|
| Drop-off order of a group | enumerate all permutations (<= 4!) with side constraints | yes |
| Which riders share a vehicle | set-partition DP over bitmasks (3^n, n <= `MAX_BATCH` = 8) | yes |
| Add one rider to a running route | `insert_rider`: cheapest of all positions | per insertion |
| Baseline | `greedy_partition` (insertion-based) | no; tests assert exact <= greedy |

OR-Tools is not installable in this environment and is not needed at this size. Solvers sit behind
`SOLVERS` / `register_solver`, so a CP-SAT or routing backend can replace the exact one for the full
multi-hub DARP without touching the protocol.

## Constraints and detour
* Compatibility filter: pickup windows must share an instant (`common_departure`), and every rider's
  experienced detour must stay within its own `detour_tolerance`.
* Experienced detour of rider i = ride time on the shared route - direct hub->home time (seconds).
* Marginal detour of a rider (Environmental Model 2.1) = route time with it - route time without it;
  reported in `GroupPlan.marginal` and used by `insert_rider` to pick positions.
* Objective: vehicle seconds + `DETOUR_WEIGHT` x total rider detour seconds; ties broken by stop order.

## Protocol (`protocols/pool_formation.py`)
1. A request with `pooling_willingness=True` is queued: `agree("queued")`, `inform(request_queued:<s>)`.
   Others get an immediate solo pool, as before.
2. The batch window closes `T_POOL` after the first queued request (or at `MAX_BATCH` waiting).
3. The solver partitions the batch; every group becomes a pool, each member gets a `PoolProposal`
   (shared route, fare ceiling, own detour, wait).
4. All accept -> pool confirmed, riders escrow. A decline or silence -> **partial acceptance**: that pool
   is dissolved, the decliner is told `Pool DISSOLVED`, the rest are told `Pool FORMING` (back in the
   queue, rider state DECIDING -> AWAITING_POOL) and are immediately re-pooled (new pool id). A lone
   remainder travels solo (`solo_fallback`). Every round removes at least one rider, so it terminates.
5. A rider no group can serve (window closed, unreachable) is released with `Pool DISSOLVED`.

## Money
* Fare split is proportional to each rider's direct hub->home time, in whole cents, summing exactly to
  the auction price (largest remainder, `split_fare_weighted`).
* The auction reserve is `fare_ceiling`: the largest total whose weighted split gives nobody more than
  their own `max_fare` (so a rider's escrow, which equals max_fare, always covers its share).
* The CFP carries the hub's planned `stop_order`; drivers price exactly that order.
* Pooling discount: for pooled trips the hub sends Payment a posted solo reference fare per rider
  (`rate_per_second() x direct time`, i.e. per-km and hourly cost folded at 30 km/h, capped at max_fare). `Settlement.pooling_discount`
  = reference - fare charged (>= 0). Informational: the driver is always paid the auction price.

## Hub auctions are serialized
Two simultaneous auctions called the same idle drivers; the driver holding the first provisional lock
must refuse the second (CC-09), starving it of bids. The hub now runs one auction at a time (retries go
to the front of the line).

## Known limits
* The batch window does not close early for a rider whose own window is about to end.
* Detour is measured on the static grid travel times at the departure instant.
* Partial acceptance asks already-accepting riders to accept again (the new pool has a new id and route).


## Relative detour tolerance and the batch wait

**Tolerance relative to the solo ride.** A rider's tolerance is `min(cap, ratio x direct ride time)`. The cap is the
request's `detour_tolerance` (default 600 s); the ratio is the rider's `detour_ratio` (scenario key `detour_ratio`,
default `DETOUR_RATIO = 1.0`; `null` = the cap only). The rider computes it when it builds the request, so the hub, the
solver and the `detour_tolerance` field are unchanged. A 3-minute trip now accepts at most 180 s extra, a 20-minute trip
at most the 600 s cap.

**The batch wait counts.** For a SHARED group the hub requires, for every member,
`extra ride seconds + seconds already waited for the batch <= tolerance`. A rider alone is never refused for having
waited. The wait is already spent when the plan is made, so it decides who may share but is not part of the cost the
partition minimises (that would count a sunk cost). `GroupPlan.wait` reports it.

## Footprint (vehicle distance and CO2)

For every matched pool the hub records (`MicrohubAgent.pool_metrics`) the metres of the shared route from the hub
through the drop-offs against the metres the same riders would have driven in separate vehicles
(`common.pooling.footprint`, edge `length` from the graph). Only the loaded leg is counted: both numbers start at the hub,
so the difference is what sharing saved. CO2 = saved km x `EMISSION_G_PER_KM` (150 g/km, a placeholder for a small petrol
car; `emission_g_per_km` on the hub). The run report prints:

```
pooling: 43 of 80 matched riders shared a vehicle (17 pools, 2.5 riders each)
vehicle distance, loaded leg: 240.9 km shared vs 342.7 km if everyone rode alone: 101.8 km saved, 15.27 kg CO2 (150 g/km)
```

Not counted yet: the driver's empty legs (to the hub, cruising, repositioning), vehicle type, and occupancy-dependent emissions.

## Late join and CO2 incentive

A rider can also join a pool whose driver is still on the way to the hub, and riders of shared trips can be paid from a pooling
fund per kg of CO2 saved. See `docs/late_join.md`.

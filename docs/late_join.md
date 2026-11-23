# Late join and the CO2 pooling incentive

Two additions to pooling. Both are on by default in the scenarios except the incentive (rate 0 = off).

## Late join

A rider asks for a trip while a pool of the same hub is already **assigned** to a driver who has **not reached the hub yet**.
If the rider fits, it joins that pool instead of waiting for a batch and an auction of its own.

Flow (`LateJoinProtocol`, `protocols` are in `agents/microhub.py` and `agents/driver.py`):

1. Hub `offer_late_join`: for every assigned pool whose driver is at least 15 s from the hub, `common.pooling.late_join_terms`
   asks whether the group "riders already in + the joiner" is feasible at the driver's expected arrival. Feasible means the
   time windows overlap and **each rider's detour stays within its own tolerance**: the riders already in are not asked
   again, they only have to stay inside the tolerance they set. The pool with the least extra driving wins. One joiner at a
   time per pool.
2. Hub -> driver `request(LateJoinRequest)` (new stop order, extra seconds, extra pay). The driver is free to **refuse**:
   `not_assigned`, `too_late` (already at the hub), `full`, or `underpaid` (extra pay below `extra seconds x cost per
   second x (1 + its markup)`). If it agrees it applies the change **provisionally** (route and rider list).
3. Hub -> rider: the usual `agree(Pool)` and `PoolProposal` (route, fare, wait = the driver's ETA, detour). The rider accepts
   under the same rules as any pool (fare <= max fare, wait fits the window, detour <= tolerance), then escrows.
4. Escrow in -> `commit_late_join`: hub -> driver `LateJoinDecision(confirmed)`, the joiner gets its `MatchResult`, the
   riders already in get `PoolAmended` (informed only: route, old and new fare).
5. If the rider declines, stays silent or fails to escrow, the hub tells the driver to undo it
   (`LateJoinDecision(confirmed=False)`). If nothing arrives, the driver undoes it itself after `T_JOIN_HOLD` (240 sim s)
   while waiting at the hub. After the driver arrives nobody can join (`frozen`).
6. If the driver refuses or does not answer within `T_LATE_REPLY`, the request goes the normal way (batch queue or solo pool).

### Money

* The driver is offered the extra driving at its variable cost + `late_join_markup` (0.3), no separate bonus.
* New total = old total + extra pay. It is split by direct hub->home time like any pool. A rider already in pays
  `min(old fare, new share)`, so **nobody's fare goes up**; the joiner pays what remains (never below the extra pay). If that is
  above the joiner's max fare the join is not offered.
* The joiner has its own pool id and escrow. At the end the hub settles the original pool with the first riders and
  the joiner's pool with the same driver (`TripCompleted` per pool), so the ledger needs no change. The driver ends up with the
  sum of all fares.
* The pool footprint (vehicle km and CO2) is re-measured with the joiner.

### Known limits

* If a joiner is confirmed but the driver had already undone it (the confirmation arrived after the hold expired), the hub
  abandons the join: refund, the old fares are restored, and the rider is told (`Handover` from the hub to itself) to start
  over: its request goes the normal way (batch or own pool) and it escrows again. It does not lose the trip.
* A rider that declines a late-join proposal is dissolved like any solo pool; it is not re-queued.
* Scenario key: `"late_join": true|false` (default true). Hub parameters: `late_join`, `late_join_markup`, `t_late_reply`.

## CO2 pooling incentive

A rider of a shared trip is paid from a **pooling fund** (separate from the subsidy fund), in proportion to the CO2 the sharing
saves.

* At settlement the hub takes the footprint of the pool (`PoolMetric`: loaded vehicle metres shared vs the riders' separate
  routes), `kg saved = saved metres x g/km / 1e6`, `incentive = rate x kg`, and splits it by **experienced delay** (detour seconds + 1,
  so whoever gave up more time gets more). Late joiners share the same pot.
* It travels in `TripCompleted.pool_incentives`; Payment credits it from the pooling fund to the rider's wallet on top of the
  fare. It never reduces the driver's payout. `Settlement.pooling_incentive` shows it.
* Scenario (`payment` section): `pool_incentive_per_kg` (EUR per kg, default 0 = off) and `pooling_budget` (EUR, `null` =
  unlimited). If the fund runs dry the unpaid part is reported (`Ledger.pooling_shortfall`).
* The harness report prints `pooling incentive: X EUR paid ...` and `late joins: N`.

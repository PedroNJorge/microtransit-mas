# Rider agent, pool proposal and reputation (Milestone 7)

Code: `src/agents/rider.py`, `src/agents/reputation.py`, hub side in `src/agents/microhub.py`.
Tests: `tests/integration/test_single_trip.py` (done-when), `tests/test_rider_fsm.py`, `tests/test_reputation.py`.

## The single-trip pipeline

    Rider            Hub                      Payment        Driver          Reputation
    request ------>                                                           
            <------ agree(Pool) + inform(request_queued)
            <------ propose(PoolProposal)
    accept-proposal ->
            <------ inform(pool_confirmed)
    EscrowRequest --------------------------->
                    <---- inform(Escrow HELD) (ID-02)
                    cfp ---------------------------------> propose / refuse
                    accept-proposal ---------------------> confirm
            <------ inform(MatchResult)
            <------------------------- DriverArriving / RiderReady / RiderBoarded / RiderDroppedOff ---
    Rating ---------------------------------------------------------------------> 
                    <---- TripCompleted ------------------- 
                    TripCompleted --------> settle         TripCompleted (trip registry) ----------->
            <----------------------------- Settlement
                    <-------------------------------------------- ReputationScore (the ratee: rider or driver, and the hubs)

## Rider state machine

IDLE -> REQUESTING -> AWAITING_POOL -> DECIDING -> AWAITING_MATCH -> DRIVER_EN_ROUTE -> IN_TRIP -> RATING -> IDLE.
`DRIVER_EN_ROUTE` runs from the MatchResult to boarding; `IN_TRIP` is the ride itself (boarding to drop-off).
Any waiting state can fall back to IDLE (refused, declined, dissolved, payment failed, unmatched, timed out).
Illegal transitions are refused by `go()`; each finished trip is kept as a `TripRecord` with outcome and timings.

* **Pool proposal.** `PoolProposal.split_fare` is the *fare ceiling* (the auction reserve): the final fare can only be
  lower. The rider checks ceiling <= max fare, expected wait fits its window, added detour <= its tolerance, and
  rejects with a reason otherwise. Silence for `T_pool_response` counts as a reject.
* **Escrow after confirmation.** The fare is escrowed only once the pool is confirmed, before any CFP (ID-02).
* **Patience.** A rider that makes no progress for `patience` sim seconds gives up (`timed_out`).
* **Rating policy** is pluggable (`rate_with`); default 5 stars, -1 for a pickup 120 s later than promised, -1 for
  boarding after the window closed.

## Reputation

* Score = (2 x 3.0 + sum of ratings) / (2 + n), so one 5-star driver is 3.67, not 5. Bounded to [1, 5].
* A rating counts only if the hub registered the trip (TripCompleted over ReputationProtocol), the sender IS the rater,
  the rater rode the trip and the ratee drove it. A rating that beats the registry message is parked and applied
  when the trip arrives. One rating per (trip, rater).
* The hub stores the published score and uses it in bid evaluation instead of the driver's claim.

## Not yet (Milestone 8 / 9)
* ID-07: ratings are published as soon as they are valid; the settlement gate and `pending_settlement` flag come in M8.
* ID-08 (driver rates rider) and the `query-if` path are implemented on the Reputation side only; the hub uses push updates.
* Pools are single trips; batching (`T_pool`), compatibility filters and partial acceptance are Milestone 9.
* There is no demand generator: riders only request when `request_trip()` is called.

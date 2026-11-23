# Driver agent and the provisional lock (Milestone 6)

Code: `src/common/lock.py` (pure state machine), `src/agents/driver.py`, hub side in `src/agents/microhub.py`.
Tests: `tests/test_driver_fsm.py` (lock, 67 tests incl. a 40-seed random-sequence property),
`tests/test_driver_agent.py` (message level), `tests/test_driver_trip.py` (done-when, end to end).

## Lock state machine

    FREE --acquire--> PROVISIONAL --commit (accept-proposal)--> FIRM --finish/abort--> FREE
                         | reject / expiry (T_provisional) / abort / retry-replace
                         v
                        FREE

| Rule | Where it is enforced |
|---|---|
| CC-01 one lock | `DriverLock` holds a single `Lock`; `acquire` refuses anything else |
| CC-02 expiry | `tick(now)` runs first in every call and in the driver's housekeeping loop |
| CC-03 firm, refuse the rest | `commit` -> FIRM; FIRM never expires and blocks every CFP until `finish/abort` |
| CC-04 release | `release` (reject-proposal) frees PROVISIONAL only, only for the holding hub and pool |
| CC-06 no double booking | the FIRM lock lasts the whole trip |
| CC-07 broadcast | `on_change` / `announce()`: DriverStatus to every hub on each state change + heartbeat |
| CC-08 pre-CFP check | `MicrohubAgent.eligible_drivers` skips drivers whose fresh status is not IDLE |
| CC-09 `refuse(locked)` | `handle_cfp` asks `lock.blocks()` before doing anything else |
| CC-10 ETA | hub recomputes the ETA from the last DriverStatus; its value is the one scored |
| GC-13 | a reposition request is refused while a lock is held or a trip is under way |

Decisions worth knowing
* **Late accept.** An accept-proposal that arrives after the lock expired is refused (`lock_expired`); the hub
  treats it like any failed award and prices the remaining bids. T_provisional (60 s) must exceed
  T_award + latency (GC-17); the defaults are 60 > 10.
* **Retry.** A CFP of a higher attempt from the same hub for the same pool replaces the old provisional lock;
  a duplicate CFP returns the same bid and does not extend the expiry; an older attempt is refused.
* **Truthful price.** `price = ceil_cents(base + rate * (seconds to pickup + route seconds))` is the driver's cost;
  a driver whose cost exceeds the CFP reserve refuses (`over_reserve`), so it is never paid below cost.
* **Driver refusals (reasons):** `locked`, `repositioning`, `offline`, `no_capacity` (GC-01), `shift_ending` (GC-04),
  `too_late` (GC-05), `detour_too_large` (GC-06), `unreachable`, `over_reserve`, `unknown_hub`, `no_network`.

## Trip (TripExecutionProtocol)

confirm -> drive to the pickup -> `DriverArriving` -> wait for `RiderReady` (T_no_show) -> board (`RiderBoarded`)
-> drive the planned order, `RiderDroppedOff` at each stop -> `TripCompleted` to the hub -> lock FREE, state IDLE
(at the last drop-off, no automatic return to a hub).
The hub settles using ITS OWN record (`MatchResult.rider_fares`), only for riders the driver reports as dropped
off, and only if the report comes from the assigned driver; then it asks Payment to settle.
Failures: driver cannot reach a stop -> `TripFailed(DRIVER_FAILED)` -> hub informs riders and refunds;
no-show -> `TripFailed(RIDER_CANCELLED, rider)` -> hub refunds that rider's escrow.

## Known limitations
* **No-show in a pool:** the driver is paid only the shares of riders who boarded, which can be below the
  auction price. A compensation rule belongs to the cancellation milestone.
* Re-auction after a driver failure is not implemented (riders are informed and refunded).
* Drivers share the in-process `TransitNetwork` that the Environment updates instead of rebuilding it from TrafficUpdate messages.
* While repositioning a driver refuses CFPs; interruptible repositioning is a Milestone 10 topic.
* A driver standing at the hub can announce its arrival a moment before the rider receives the MatchResult.

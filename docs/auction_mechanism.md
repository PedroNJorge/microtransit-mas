# Match auction: mechanism, utilities, and proofs (Milestone 5)

Code: `src/common/auction.py` (pure evaluation), `src/protocols/match_auction.py` (FSM), `src/agents/microhub.py`.
Tests: `tests/test_auction_eval.py` (rule + numeric IR/IC checks), `tests/test_match_auction.py` (protocol).

## 1. Setting

One hub, one pool of riders (one trip), `n` drivers who each may submit one bid. A reverse auction: the
hub buys a ride, the cheapest *effective* offer wins. The hub publishes a **reserve** `R` (the riders'
joint maximum fare, `CFP.max_total_fare`) and the evaluation rule before bidding.

Bid of driver `i`: price `p_i` (EUR), ETA to pickup `e_i` (s), detour `d_i` (s), reputation `r_i ∈ [1,5]`.
`e_i`, `d_i` and `r_i` are **verified by the hub** (reputation is the hub's record; ETA and detour are computed
by the hub from the driver's reported position and the graph from Milestone 6; in M5 the stub drivers
self-report them, so the incentive guarantees below are conditional on that verification).
The only private, strategic quantity is the price (the driver's true cost `c_i`).

## 2. Utilities

| Agent | Utility | Notes |
|---|---|---|
| Driver `i` | `u_i = P_i − c_i` if it wins and is paid `P_i`; `0` otherwise | `c_i` = true cost of serving (fuel, time, wear); `P_i` = payment |
| Rider `k` | `v_k − f_k − λ_t·w_k − λ_d·δ_k` | `f_k` fare share, `w_k` wait, `δ_k` detour; rejects if `f_k > maxfare_k` |
| Hub | `Σ_k f_k − P_w` (surplus), subject to `P_w ≤ R` | with `Σ f_k = P_w` the hub is budget-balanced; subsidy is paid by the fund |

**Score (money-denominated, quasi-linear).** Each non-price attribute is converted to EUR with fixed
exchange rates (`VALUE_OF_TIME`, `VALUE_OF_DETOUR`, `VALUE_OF_REPUTATION` in `settings.py`):

    penalty_i = a·e_i + b·d_i + g·(r_max − r_i)         (≥ 0)
    cost_i    = p_i + penalty_i                          (lower is better; the "score")

This is the feedback's `Score = w1·Price + w2·ETA + w3·Detour + w4·Reputation`, with the weights given a unit
(EUR per second / per rating point) so that price and quality are commensurable. This makes the auction
a standard quasi-linear one, which is what the VCG argument needs.

## 3. Rule

* Winner: `w = argmin cost_i` (ties: lower price, lower ETA, lower driver id; deterministic).
* Let `s_2 = min_{j≠w} cost_j` (`+∞` if there is none).
* **Payment (second-score / threshold price):** `P_w = min(R, p_w + (s_2 − cost_w))`
  `= min(R, s_2 − penalty_w)`.
* Losers receive 0. (`first_price` mode, kept for comparison, pays `p_w`.)

`P_w` is the highest price at which `w` would still have won, i.e. the single-item
Vickrey–Clarke–Groves payment in the "score" space (Che 1993 second-score auction): `w`'s externality
on the others is the cost increase `s_2 − cost_w` they suffer because `w` took the job.
Combinatorial VCG over many pools is deliberately not used: one pool per auction, and a driver holds one
provisional lock (CC-01), so there are no bundle bids and the single-item case is exact.

## 4. Individual Rationality (IR)

**Claim.** A truthful driver never loses money: if `i` wins, `P_i ≥ c_i`, hence `u_i ≥ 0`; if it loses, `u_i = 0`.

*Proof.* Truthful means `p_i = c_i`. Winning requires `cost_i ≤ cost_j` for all `j`, so `s_2 − cost_w ≥ 0` and
`p_w + (s_2 − cost_w) ≥ p_w = c_w`. The reserve cap can only bind if `R < p_w + (s_2 − cost_w)`; but a bid is
only *valid* if `p_w ≤ R` (`over_reserve` rejection), so `min(R, ·) ≥ p_w = c_w`. In all cases `P_w ≥ c_w`. ∎

More generally `P_w ≥ p_w` for any valid bid, so no driver is paid below its own ask; the settlement
(Payment agent) pays exactly `final_fare` to the driver, so IR carries over to the money flow (`driver_payout ≥ final_fare`).
Cent rounding: bids are rounded up and the reserve down, and the payment is clamped to `[ceil(bid), floor(R)]`,
so IR holds exactly in cents.

## 5. Incentive Compatibility (dominant-strategy)

**Claim.** Reporting `p_i = c_i` is a weakly dominant strategy for every driver, whatever the others bid.

*Proof.* Fix the others' bids and define `T_i = min(R, s_{−i} − penalty_i)` where `s_{−i} = min_{j≠i} cost_j`.
`i` wins iff `p_i + penalty_i < s_{−i}` (up to ties) and `p_i ≤ R`, i.e. iff `p_i < T_i`; if it wins it is paid `T_i`,
**which does not depend on `p_i`**. So `i` faces a take-it-or-leave-it price `T_i`:

* if `c_i < T_i`: bidding `c_i` wins and earns `T_i − c_i > 0`; any `p_i < T_i` gives the same outcome;
  any `p_i > T_i` loses (0). Truth is optimal.
* if `c_i > T_i`: bidding `c_i` loses (0); any bid that wins pays `T_i < c_i` (loss). Truth is optimal.
* if `c_i = T_i`: indifferent.

Overbidding never raises the price paid (independent of bid) and can only lose the job; underbidding can
only win jobs at a payment below cost. ∎

The property tests (`test_auction_eval.py`) check this numerically: for 60 random markets, no deviation
from a grid of misreports ever increases utility, and the same test fails (verified by mutation) when the
payment is replaced by the winner's own bid. With the first-price rule the test finds profitable shading.

**Cent-rounding exception.** With integer cents a driver can gain at most one cent (< €0.0101) by misreporting;
the test bounds it.

## 6. Hub and rider side

* Hub: pays `P_w ≤ R`; budget-balanced because the riders' shares sum to `P_w` exactly (`split_fare`,
  leftover cents to the first sorted riders); the hub never subsidises from its own pocket.
* Riders: every fare share is ≤ their `max_fare` because `R = n·min(max_fare)` (the equal split never exceeds
  any rider's maximum). The riders' money is held in escrow before the CFP (ID-02) and refunded if the
  auction fails (FE-01).
* Efficiency: the winner minimises `cost`, i.e. total EUR-equivalent cost over drivers, so with truthful
  prices the allocation is efficient (VCG efficiency).

## 7. Assumptions and limitations (state these in the report)

1. IC holds for the **price** only. Time, detour and reputation must be hub-verified. If a driver could lie
   about ETA the rule would have to use it as a commitment (penalise lateness) — M6 computes it.
2. **Thin markets:** with a single valid bidder the payment is the reserve `R` (threshold price). That is
   IC and IR but costly for riders; options: a lower reserve, a minimum number of bidders before the award,
   or an escalating retry. `AuctionRules` leaves this as a policy choice.
3. Quasi-linearity requires the exchange rates to be fixed ex ante and common knowledge.
4. Collusion, risk-aversion and drivers who bid on several pools at once are out of scope (one pool per
   auction, one provisional lock per driver).
5. Riders do not bid; rider-side truthfulness (about `max_fare`, time windows) is not claimed.

## Thin markets (single bidder)
With second-score pricing a lone valid bidder has no runner-up to set the price. The first version paid it the reserve (the
riders' maximum fare or the tariff reserve), which over-pays: in the 12-driver start of the labour-market runs 38 % of matched
auctions had one bid and profit per trip looked 5x too high. Now (`lone_margin`, default 0.10) a lone bidder is paid its own bid
x 1.10, at most the reserve; `"lone_margin": null` in a scenario restores the old rule, `0` pays exactly the bid.

The price of this fix: the payment of a lone bidder depends on its own bid, so truthful bidding is dominant only when at least
two valid bids arrive (the proofs of section 5 are stated for that case and tested with `lone_margin=None`); a driver that
expects to be alone could gain by bidding up to reserve / 1.10. Our drivers bid at cost, so this is a theoretical caveat, but the
report should state it. `single_bid_share` (summary) says how often it applies; the pure first-price auction would remove the
issue but pays a driver exactly its bid, so it needs a markup rule first (see the markup question).

## Driver cost model (the true cost `c_i`)

    c = DRIVER_BASE_COST + DRIVER_COST_PER_KM x km + DRIVER_COST_PER_HOUR/3600 x (driving s + boarding/alighting s)

* `DRIVER_COST_PER_KM = 0.15` EUR: fuel and wear (assumption, tune per scenario).
* `DRIVER_COST_PER_HOUR = 4.96` EUR: the driver's time at the net Portuguese minimum wage (2027). Charged for ALL driving
  (to the pickup, loaded, empty cruising/repositioning) and for boarding (`DWELL_PICKUP` once) and alighting
  (`DWELL_DROPOFF` each). Idle time is free.
* `DRIVER_BASE_COST = 0`: no arbitrary fixed fee per trip. The opportunity cost of committing to a job (a better one
  might have come) is the `bid_markup` set by the brain, not part of `c`.
* Where only seconds are known (posted solo tariff, late-join extra pay, repositioning incentive) distance is folded into
  time at `NOMINAL_SPEED_KMH = 30`: `common.costs.rate_per_second()` = 0.00263 EUR/s.
* Drivers can override `cost_per_km` / `cost_per_second` / `base_cost` in the constructor.


## Late invitations (drivers that become idle during the auction)

The call for proposals goes to the drivers the hub knows to be IDLE when the auction opens. A driver that finishes its trip a
moment later used to miss the auction and only came in at the retry. Now (`late_invite`, default on; scenario key `late_invite`):

* While fewer than `late_min_bids` (2) bids are in, the bidding window stays open for `late_hold` (15 sim s) after the call, even if
  every driver called has answered. With two bids the auction closes at once, as before. Fewer than two bids is the thin market in
  which the winner is paid the reserve, so more bidders are worth a short wait.
* Every `t_late_poll` (2 sim s) the hub looks at the drivers' fresh status (a driver announces every state change at once) and sends
  the same call, with the same deadline, to each driver that is now IDLE, was not called yet and is within the pickup radius.
* A late driver bids like any other; the lock rules do not change (it locks when it receives the call). A bid that arrives after the
  award is rejected as before.
* Not done: a driver still on a trip cannot bid for after the trip ("option B": it would need to report when and where it will be
  free and the lock to hold the job until then).

## The price of a driver's time, and the wage target

The bid is `cost x (1 + markup)` with `cost = km x 0.15 + seconds x time_value/3600` (see the driver cost model above). Two
numbers that used to be one are now separate:

* **`time_value_per_hour`** (scenario key `drivers.time_value_per_hour`, default = the wage target 4.96): what a driver puts on its
  own driving and boarding time when it bids. At 0 the bid is the kilometre cost only: the right marginal cost for a driver who would
  otherwise sit idle (the wage is a cost of the hour, not of the job), the wrong one when time could be spent on another paying job.
* **`DRIVER_WAGE_TARGET_PER_HOUR`** (4.96): the yardstick of the income metrics (docs/metrics.md): what a driver should earn per hour
  on duty, idle hours included.

With second-score pricing the price a winner is paid does not depend on its own bid, so a lower time value makes a driver win
more jobs, not earn more per job; competition between near-identical drivers pushes the price towards the kilometre cost. Whether
the fleet still reaches the wage is what `fleet_income_hour` and `driver_wage_share` show.


## Rolling-surplus bid rule (optional, `drivers.surplus_scale_hours`)

Off by default (the time price stays the fixed wage target). When on, a driver prices its time from how its last 2 duty hours went:

    rolling_surplus = revenue - 0.15 EUR/km x km - 4.96 EUR/h x hours          (window 2 h, or since the start)
    tau (EUR/h)     = wage x clamp(1 - rolling_surplus / S*, 0, 1)             S* = surplus_scale_hours x wage
    bid             = base + 0.15 x km + tau/3600 x seconds                     (the km cost is a floor: tau >= 0)

Behind the wage yardstick (surplus <= 0) the driver bids its full cost; S* euros ahead it values its time at 0 and bids km cost
only; in between it is linear. S* = 0 is a hard step at 0. Costs are booked when a trip ends (empty driving when it is driven);
hours are duty time (the window length). In the long scenarios most drivers are behind the yardstick (median surplus about
-4.5 EUR per 2 h in the calibration runs), so the rule changes bids only for drivers that are ahead; it matters more once exit
lets the fleet shrink. `bid_markup` (set by the brain) is unchanged. Code: `agents/bid_rule.py`; tests: `tests/test_bid_rule.py`.

### The rolling-surplus rule and incentive compatibility
Under second-score pricing the payment does not depend on the winner's own bid, so bidding the true cost is dominant. The
rolling-surplus rule makes the wage part of that cost fall as the driver gets ahead. Read against the fixed wage yardstick this is a
deliberate behavioural deviation from the dominant strategy (a driver that shades wins extra jobs at payments below its yardstick
cost); read against the driver's own valuation of its time (reference-dependent preferences: Kőszegi-Rabin 2006, Farber 2008 -
citations to be verified) it is truthful bidding. The km cost is a floor, so the driver never bids below its cash outlay. The
other bidders' dominant strategy is unaffected by one driver's rule. The report should state it as an assumption about drivers,
not as an optimisation.

### Late invitations and the lock
A driver invited late to one auction may also have a call from another auction in flight; it holds one provisional lock (CC-01),
so whichever call arrives first gets it and the other is refused `locked` (counted in `refuse_locked`). This adds a small
arrival-order dependence in thin auctions. Late invitations go only to drivers whose fresh status is IDLE (a driver still
finishing a trip is not invited: that is the not-yet-built option B).

### The reserve
The base reserve is `n x min(max_fare)` so that an equal split is individually rational for every rider (below the joint willingness
to pay when max fares differ); the tariff reserve and the regulator cap then lower it further. `refuse_over_reserve` shows how
often bids exceed it (hundreds per long run), so a sensitivity arm with the reserve at the sum of max fares is worth running.

// driver.asl -- the BDI layer of a driver: WHERE TO WAIT while idle.
//
// The reactive driver agent (bidding, locks, trips) is untouched. It sends this brain percepts:
//   state(idle | cruising | busy)   at(Node)   at_hub(Hub) (only while standing on a hub)
//   home(Hub)                       eta(Hub, Seconds)   pressure(Hub, P)   cooling (a move was just made)
// for EVERY hub of the network. pressure = requests per available driver the driver has observed around
// that hub recently (from the calls for proposals it received; 0 for a hub it has heard nothing from).
// The brain answers with the action .cruise_to(Hub), a self-directed move the driver abandons the
// moment it wins a job.
//
// DESIRE: be where requests are, per available driver, without driving far for it.
//   value(H) = pressure(H) - drive_cost * eta(H)
// With no demand anywhere the value is highest at the NEAREST hub, so the default behaviour is the
// "return to the nearest hub" rule that the fixed baseline uses. A hub with real demand overrides it.
//
// INTENTION: cruising_to(Hub). Commitment: a standing driver leaves its hub, and a cruising driver
// changes its target, only for a hub that is better by `switch_margin`, and not during the cooldown
// that follows every move (the `cooling` belief). Without these two the brain re-decided on every
// shift in pressure and drove ~2 moves per ride for nothing.

// ---- tunables (beliefs of the brain, so tests and experiments can change them) -------------------
drive_cost(0.0015).      // pressure units given up per second of driving
switch_margin(0.5).      // a new target must beat the current hub / intention by this much

// ---- value of waiting at a hub ---------------------------------------------------------------------
value(H, V) :- pressure(H, P) & eta(H, E) & drive_cost(C) & V = P - C * E.
best(H, V)  :- .findall(v(V0, H0), value(H0, V0), L) & L \== [] & .max(L, v(V, H)).

// ---- opportunity cost of a job (only when the brain owns the bid markup: belief bid_policy) ---------------
// Where the driver stands there is some demand per driver (local), against the average over all hubs.
// A driver standing where demand is above average gives up more by leaving: its true cost of a job is
// higher, so it bids  cost * (1 + markup)  with  markup = gain * (local - average), capped. A driver in a
// quiet place bids plain cost. Jobs then go to drivers whose opportunity cost is lowest, a decentralised
// way of keeping every hub covered. Under the second-score rule bidding one's true cost is optimal.
markup_gain(0.3).
max_markup(0.5).
nearest(H, E) :- .findall(e(E0, H0), eta(H0, E0), L) & L \== [] & .min(L, e(E, H)).
local_pressure(P) :- nearest(H, _) & pressure(H, P).
total([], 0).
total([H|T], S)   :- total(T, S1) & S = S1 + H.
avg_pressure(A)   :- .findall(P, pressure(_, P), L) & .length(L, N) & N > 0 & total(L, S) & A = S / N.

+!tune : bid_policy & local_pressure(P) & avg_pressure(A) & P > A & markup_gain(G) & max_markup(X) & G * (P - A) < X <-
    .set_markup(G * (P - A)).
+!tune : bid_policy & local_pressure(P) & avg_pressure(A) & P > A & max_markup(X) <-
    .set_markup(X).
+!tune : bid_policy <- .set_markup(0).
+!tune <- true.

// ---- reactions to the world ---------------------------------------------------------------------------
+state(idle) : cruising_to(_) <- -cruising_to(_); !consider; !tune.   // idle again: any earlier intention is over
+state(idle)        <- !consider; !tune.
+pressure(_, _)     : state(idle) | state(cruising) <- !consider; !tune.
-cooling            : state(idle) | state(cruising) <- !consider.   // the cooldown ended: may move again
+state(busy) : cruising_to(_) <- -cruising_to(_).    // a job: drop the intention
+at_hub(H)          : cruising_to(H) <- -cruising_to(H). // arrived: intention achieved

// ---- deliberation ---------------------------------------------------------------------------------------
+!consider : state(busy) <- true.

// nothing known about any hub: go back to the home hub if away from every hub
+!consider : not best(_, _) & home(H) & not at_hub(_) & not cruising_to(_) & state(idle) <-
    +cruising_to(H);
    .cruise_to(H).

// cruising: change target only for a clearly better hub, and not right after the last move
+!consider : cruising_to(Cur) & not cooling & best(H, V) & H \== Cur & value(Cur, VC)
             & switch_margin(M) & V > VC + M <-
    -cruising_to(Cur);
    +cruising_to(H);
    .cruise_to(H).
+!consider : cruising_to(_) <- true.

// standing on a hub: stay, unless another hub is clearly better and the cooldown is over
+!consider : state(idle) & at_hub(Cur) & not cooling & best(H, V) & H \== Cur & value(Cur, VC)
             & switch_margin(M) & V > VC + M <-
    +cruising_to(H);
    .cruise_to(H).
+!consider : state(idle) & at_hub(_) <- true.

// idle somewhere that is no hub (e.g. after a drop-off): go to the best hub (nearest if demand is flat)
+!consider : state(idle) & best(H, V) <-
    +cruising_to(H);
    .cruise_to(H).

+!consider <- true.

// driver_expected.asl -- the BDI layer of a driver, version 4: WHERE TO WAIT, by expected earnings in euros.
//
// v2/v3 count drivers (deficits) and charge a generic cost per second of driving. Here the brain is given, per hub, the
// expected NET GAIN in euros of waiting there instead of here (computed from the hubs' published load by
// agents/expected_wins.py):
//     gain(H) = value x (expected wins at H - expected wins here) - 0.15 EUR/km x unpaid distance - margin
// with expected wins = requests expected x chance of winning one (free drivers share the requests equally) and `value` =
// the driver's own mean net earnings per trip. The brain gets from its driver:
//   state(idle | cruising | busy)   at_hub(Hub)   home(Hub)   eta(Hub, Seconds)   gain(Hub, EUR)   cooling
//   draw(U)  move_chance(P)
//   (and pressure(Hub, P), still used for the bid markup)
// and answers with the action .cruise_to(Hub).
//
// HERDING: the drivers of a cell run this same code on the same signal, so all of them would leave together. Each moves only
// if its draw U (fresh with every published load) is below P = 1 / (free drivers in its cell): on average one leaves per load,
// and the next load, with the cruiser counted as supply of its target, shows whether another move still pays.
//
// DESIRE: earn more by waiting where requests are many and free drivers few, without paying for empty driving that does not
// come back. INTENTION: cruising_to(Hub); a cruising driver is not re-deliberated (its hub counts it as supply of the target
// at once) and a move starts a cooldown. Nothing heard (no gain beliefs): stay put.

// ---- tunables --------------------------------------------------------------------------------------------------
max_drive(900).          // never move for a hub further away than this many seconds

// ---- what to do -------------------------------------------------------------------------------------------------
candidate(H, G) :- gain(H, G) & G > 0 & eta(H, E) & max_drive(X) & E < X.
best_move(H, G) :- .findall(g(G0, H0), candidate(H0, G0), L) & L \== [] & .max(L, g(G, H)).

// ---- opportunity cost of a job (only when the brain owns the bid markup: belief bid_policy) ----------------------
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

// ---- reactions to the world ----------------------------------------------------------------------------------------
+state(idle) : cruising_to(_) <- -cruising_to(_); !consider; !tune.
+state(idle)        <- !consider; !tune.
+gain(_, _)         : state(idle) <- !consider.
+pressure(_, _)     : state(idle) | state(cruising) <- !tune.
-cooling            : state(idle) <- !consider.
+state(busy) : cruising_to(_) <- -cruising_to(_).
+at_hub(H)          : cruising_to(H) <- -cruising_to(H).

// ---- deliberation --------------------------------------------------------------------------------------------------
+!consider : state(busy) <- true.
+!consider : state(cruising) <- true.
+!consider : state(idle) & not cooling & best_move(H, G) & draw(U) & move_chance(P) & U < P <- !go(H).
+!consider <- true.

// one move at a time
+!go(H) : cruising_to(_) <- true.
+!go(H) <-
    +cruising_to(H);
    .cruise_to(H).

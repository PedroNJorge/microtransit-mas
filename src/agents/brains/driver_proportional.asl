// driver_proportional.asl -- the BDI layer of a driver, version 3: v2's decision, but NOT every driver acts on it.
//
// Same beliefs and same gain test as driver_signal.asl, plus one more belief: draw(U), a number in [0,1) the brain draws
// whenever the published load changes. All drivers of a cell see the same deficits, so a deterministic rule sends them
// ALL (the overshoot of v2). Here a driver that passes the gain test moves only with probability
//     chance = min(deficit(H), spare(here)) / spare(here)
// i.e. the share of the cell's spare drivers that the short cell needs. If every spare driver follows the rule, the
// expected number of movers equals the number needed. Each driver reasons about its own move as evidence about its
// peers' moves (they run this same code on this same signal): it does not assume it is the only one reacting.
// The number of movers is still random (binomial, not exact); the next rung (a shared ranking published by the hub)
// would make it exact.

// ---- tunables --------------------------------------------------------------------------------------------------
move_cost(0.002).        // drivers of benefit given up per second of driving (500 s of driving = one driver)
margin(1).               // a move must help by at least this many drivers more than it costs
max_drive(900).          // never move for a hub further away than this many seconds

// ---- the cell the driver stands in and what moving there would gain ------------------------------------------
cell(H) :- .findall(e(E0, H0), eta(H0, E0), L) & L \== [] & .min(L, e(_, H)).
lesser(A, B, A) :- A <= B.
lesser(A, B, B) :- A > B.
gain(H, G) :- deficit(H, DH) & DH > 0 & cell(C) & H \== C & deficit(C, DC) & DC < 0 & eta(H, E) & move_cost(K) & margin(M)
              & lesser(DH, 0 - DC, B) & G = B - M - K * E.
candidate(H, G) :- gain(H, G) & G > 0 & eta(H, E) & max_drive(X) & E < X.
best_move(H, G) :- .findall(g(G0, H0), candidate(H0, G0), L) & L \== [] & .max(L, g(G, H)).

// the probability that a driver which passed the gain test actually moves: needed drivers / drivers to spare
chance(H, P) :- deficit(H, DH) & cell(C) & deficit(C, DC) & lesser(DH, 0 - DC, B) & P = B / (0 - DC).

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
+deficit(_, _)      : state(idle) <- !consider.
+pressure(_, _)     : state(idle) | state(cruising) <- !tune.
-cooling            : state(idle) <- !consider.
+state(busy) : cruising_to(_) <- -cruising_to(_).
+at_hub(H)          : cruising_to(H) <- -cruising_to(H).

// ---- deliberation --------------------------------------------------------------------------------------------------
+!consider : state(busy) <- true.
+!consider : state(cruising) <- true.
+!consider : state(idle) & not cooling & best_move(H, G) & chance(H, P) & draw(U) & U < P <- !go(H).
+!consider <- true.

// one move at a time: several reasons to deliberate can fire together (a new deficit, the end of the cooldown)
+!go(H) : cruising_to(_) <- true.
+!go(H) <-
    +cruising_to(H);
    .cruise_to(H).

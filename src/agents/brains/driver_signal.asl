// driver_signal.asl -- the BDI layer of a driver, version 2: WHERE TO WAIT, from what the hubs publish.
//
// Every hub publishes how loaded its cell is (requests waiting, arrival rate, free drivers counted for it; a cell is the
// part of the map nearer to that hub than to any other). The driver turns it into a DEFICIT per hub: drivers the cell
// is short of over the next few minutes (positive = riders will wait there, negative = drivers to spare). The brain
// gets, from its driver:
//   state(idle | cruising | busy)   at_hub(Hub)   home(Hub)   eta(Hub, Seconds)   deficit(Hub, D)   cooling
//   (and pressure(Hub, P), still used for the bid markup)
// and answers with the action .cruise_to(Hub).
//
// DESIRE: put free drivers where riders will otherwise wait, without emptying a cell that needs them. A free driver STAYS
// where it is (empty driving is paid by nobody) unless a cell is SHORT of drivers (deficit > 0) while its own cell has
// drivers to spare (deficit < 0). Moving one driver then helps by at most min(short(H), spare(here)) drivers, and costs
// move_cost per second of driving:
//   gain(H) = min(deficit(H), -deficit(here)) - margin - move_cost * eta(H)    must be positive.
// (An earlier version compared the raw deficits; with no demand anywhere that spread the drivers evenly over the map, and
// the first riders found them away from where they were needed.)
// INTENTION: cruising_to(Hub). A cruising driver is not re-deliberated (its hub counts it as supply of the target at
// once, which is what stops all idle drivers rushing to the same hot cell), and a move starts a cooldown.
// Without any HubLoad heard the brain does nothing: the driver stays put.

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
+!consider : state(idle) & not cooling & best_move(H, G) <- !go(H).
+!consider <- true.

// one move at a time: several reasons to deliberate can fire together (a new deficit, the end of the cooldown)
+!go(H) : cruising_to(_) <- true.
+!go(H) <-
    +cruising_to(H);
    .cruise_to(H).

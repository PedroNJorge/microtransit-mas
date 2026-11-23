# BDI layer for drivers (idle repositioning)

## Why
The reactive `DriverAgent` (bid, lock, drive) never moves while idle, so after a trip it waits wherever
the last drop-off was. Where to wait is a decision about the future (where will requests appear?), which is
what BDI is for. Hubs do not tell drivers where to go: the driver decides, and may be wrong.

## Architecture (two agents per driver)
```
Driver (reactive)  --inform(BrainPercept)-->  DriverBrain (SPADE-BDI, AgentSpeak: agents/brains/driver.asl)
                   <--request(CruiseCommand)--
Hub --inform(DemandSignal)--> drivers it did not call (so uncovered demand is still visible)
```
* `agents/brain.py` `DriverBrain(BDIAgent)`: turns percepts into beliefs (only changes are asserted), exposes
  the action `.cruise_to(Hub)` (sends a `CruiseCommand`).
* `agents/driver.py`: `brain_jid=` enables it. The driver observes every CFP and DemandSignal, keeps a
  10-minute window and sends `pressure(hub)` = sum of 1/callees over requests seen (requests per available
  driver), plus `eta(hub)` and its state. It executes `CruiseCommand` as `activity="cruising"`.
* A cruising driver stays AVAILABLE: it still reports IDLE, bids on calls from wherever it is, and a won job
  stops the cruise at the next junction. It refuses to cruise while it holds any lock (`locked`).
* Only the brain's JID is obeyed; hubs' signals only count from known hubs.

## The mind (driver.asl)
* Belief: `state`, `at_hub`, `home`, `eta(H,s)`, `pressure(H,p)`.
* Desire: be where requests are per available driver, without driving far:
  `value(H) = pressure - drive_cost * eta`, ignoring hubs below `min_pressure`.
* Intention: `cruising_to(H)`, kept until reached, until a job arrives (`state(busy)`), or until a hub is better
  by `switch_margin` (reconsideration). Close enough (`stay_radius`) -> stay. No information -> go home.

## Install
`uv add spade_bdi` (0.3.3; pulls `agentspeak`). Tests that need it are skipped when it is missing.

## Experiment
`uv run python -m harness.demo_simulation --riders 60 --drivers 24 --span 1200 --t-pool 150 --seed N [--brains]`
prints mean/median/p90 rider wait and the number of cruise moves. Results are NOT deterministic across runs
(asynchronous message timing); compare several seeds and repeats before concluding anything.

## First results (60 riders, 24 drivers, Porto, clustered demand; mean wait in s)
| seed | no brains | with brains |
|---|---|---|
| 1 | 452 | 345, 426 (two runs) |
| 2 | 456 | 571, 484 |
| 3 | 247 | 262 |

No robust improvement yet: differences are inside the run-to-run noise. Observed limits: capacity shortage at
a hot hub cannot be fixed by repositioning; the policy churns (about 2 to 5 cruise moves per driver).
Next steps: cooldown/hysteresis, supply-aware targets (avoid herding), a batch runner for A/B over many seeds.


## Version 2: the hub-load signal (`brain_policy: "signal"`, the scenario default)
Version 1 (`"pressure"`, `agents/brains/driver.asl`) guessed demand from the calls for proposals the driver happened to
hear. Version 2 (`driver_signal.asl`) reads what the hubs publish.

* **HubLoad** (`common/ontology.py`): about once a simulated minute (`T_LOAD`) every hub sends each driver that is up its
  requests waiting for a driver, the arrival rate per minute over the last 10 minutes, and its supply: the free drivers
  standing in its cell plus the free drivers cruising to it. A cell is the part of the map nearer to that hub than to any
  other (`common/cells.py`, straight-line distance). A cruising driver says where it is heading in its DriverStatus
  (`target`), so the hub counts it as supply of the target at once; this is what limits herding.
* **Deficit** of a cell = waiting + arrival rate x `HUB_LOAD_HORIZON` (300 s) - supply. Positive: short of drivers.
* **Rule**: stay, unless a cell is short (deficit > 0) while the driver's own cell has drivers to spare (deficit < 0):
  `gain = min(deficit(H), -deficit(here)) - margin - move_cost x eta(H)` must be positive, the drive at most 900 s,
  and not within 600 s of the last move. No signal heard: stay. (The first version compared raw deficits and so spread the
  drivers evenly over the map when there was no demand; it lost to staying put.)

### Results so far (one run each, speed 60, same seed; mean wait / profit per trip / brain moves)
| scenario | stay put | pressure brain | signal brain v2 |
|---|---|---|---|
| 07 migrating demand (west -> centre -> east) | 261 s / +0.51 / 0 | 211 s / -0.44 / 57 | 210 s / -0.08 / 29 |
| 04 centre drivers, suburb requests | 344 s / +0.35 / 0 | n/a | 398 s / -0.42 / 31 |
| 03 burst on one hub | 758 s / +2.20 / 0 | 1016 s / n/a | 885 s / +2.48 / 6 |

With steady or symmetric demand (04, 03) staying put is as good or better: the hubs cover the map densely and trips
are hub-local. With demand that moves over time (07) both brains cut the wait by about a fifth, in the later phases by
about 100-150 s, at the price of unpaid empty driving; v2 does so with half the moves. Single runs: differences
under about 15 % in wait or 0.4 EUR in profit are inside the run-to-run noise.

## Scenario 07: migrating demand
`config/scenarios/07_migrating_demand.json` (stay put), `brains_07_migrating_demand.json` (signal brain),
`brains_07_migrating_demand_pressure.json` (pressure brain). 24 drivers start at the two western hubs; 30 requests per
phase: west hubs (0-1200 s), the four central hubs (1200-2400 s), the two eastern hubs (2400-3600 s). Staying put leaves
the fleet where the demand was.

## Version 3: move with probability needed / spare (`"brain_policy": "proportional"`)

Every driver of a cell sees the same published deficits and runs the same code, so v2's deterministic rule sends all
eligible drivers to the short cell (overshoot). In v3 a driver that passes the gain test moves only with probability
`min(deficit(H), spare(here)) / spare(here)`, using a draw the brain refreshes whenever the published load changes
(`draw(U)`, seeded from the brain's JID, so runs repeat). If every spare driver follows the rule, the expected number of
movers equals the number needed. This is the design reading of dependency equilibria (Portakal and Sturmfels,
arXiv 2201.05506): a driver treats its own move as evidence about its identical peers' moves instead of assuming it acts
alone. It is *not* exact: the number of movers is binomial. The next rung (the hub publishes a ranking of its idle
drivers and a driver moves if its rank is within the need) would make it exact and is not built yet.

The run report adds a line with the moves per minute and their dispersion (variance over mean: 1 is chance, below 1 is
evener than chance, above 1 is herding) and the share of moves that had another driver move in the same minute.
Overshoot (movers beyond the deficit) is not measured yet.


## Version 4: expected net euros (`"brain_policy": "expected"`)
v2/v3 count drivers and charge a generic cost per second of driving, so they move a lot and the empty km are unpaid. v4 asks, per
hub H, whether waiting there is expected to pay more than waiting here (`agents/expected_wins.py`):

    wins(H)   = demand(H) x 1 / (supply(H) + 1)       demand = arrivals over 300 s + waiting requests its free drivers cannot cover
    wins(here)= demand(here) x 1 / max(supply(here), 1)
    gain(H)   = value x (wins(H) - wins(here)) - 0.15 EUR/km x distance_km - margin       move if the best gain is > 0

`value` is the driver's mean net earnings per trip (payment - km cost), a running mean from a prior of 2.0 EUR worth 5 trips;
`margin` is `drivers.brain_margin_eur` (default 0.30). The win chance is an equal-share model, not an estimate. Herding: identical
drivers of a cell would all leave together, so each moves only if its draw (fresh with every published load) is below
1 / (free drivers in its cell). Smoke run (one seed, speed 60, 07 migrating demand): 41 moves against 26 for the signal brain, more
unpaid km and a slightly longer wait - not an improvement yet; the plan `brains_expected_long.json` compares them properly.

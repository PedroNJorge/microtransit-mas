# Experiments (`python -m harness.experiment`)

A single run is not reproducible (message timing is asynchronous), and results depend on the clock speed. The runner plays
scenario **files** for several **arms**, **seeds** and repeats, at one shared speed (default 15), and records every metric.

```
uv run python -m harness.experiment --plan config/experiments/07_brains.json
uv run python -m harness.experiment --seeds 1,2,3,4,5 \
    --arm none=config/scenarios/07_migrating_demand.json \
    --arm signal=config/scenarios/brains_07_migrating_demand.json \
    --arm prop=config/scenarios/brains_07_migrating_demand.json,drivers.brain_policy=proportional
uv run python -m harness.experiment --plan config/experiments/07_brains.json --dry-run     # check the arms, run nothing
```

An **arm** is `name=scenario.json[,dotted.key=value,...]`: the overrides are dotted paths into the scenario JSON
(`drivers.brain_policy=proportional`, `t_pool=60`, `late_join=false`). The order is: the file, the shared `--speed`, the arm's
overrides (so an arm may set `clock.speed` itself), then the seed. The first arm is the baseline of the paired differences.
Runs are interleaved (repeat > seed > arm). A run ends when every rider is home, or at the scenario's `duration` (the run is then
marked `ended_by_cap` and `unfinished` counts the riders still travelling).

## Output (`experiments/<date-time>/`)

| file | content |
|---|---|
| `runs.csv` | one row per run, one column per metric (about 125) |
| `summary.csv` | per metric and arm: n, mean, 95 % CI, sd, min, max; and the paired difference to the first arm, its CI, pair count, `diff_significant` |
| `series/<arm>_s<seed>_r<repeat>.json` | the viewer's METRICS snapshots of that run (one a minute: `cum`, `roll`, `failures`, `now`) |
| `config.json` | the arms, seeds, speed |

## Metric groups in `runs.csv`

* **Viewer metrics, cumulative at the end of the run** (names as in `live_metrics.py`): `riders_per_vehicle`, `pooled_share`,
  `late_share`, `size_1..4`, `co2_saved_*`, `fare_*`, `wait_mean/p90/max`, `detour_mean`, `time_to_match`, `revenue_per_trip`,
  `profit_per_trip`, `earnings_per_hour`, `earnings_per_driven_hour`, `gini`, `km_loaded/pickup/other/return/move/reposition`,
  `deadhead_share`, `util_*`, `completion_rate`, `auction_attempts`, `subsidy_spent`, `pooling_spent`, ...
* **Failures:** `fail_<reason>`, `fail_total`. **End state:** `end_queue_len`, `end_idle_drivers`, ...
* **Waits** (request to boarding, completed trips): mean, sd, min, p50, p90, p95, p99, max; `wait_to_match_mean` and
  `wait_after_match_mean` (the report's split); `wait_q1..q4` (quarters of the riders by request order); `wait_stream_<id>` and
  `n_stream_<id>` per demand stream (the phases of scenario 07); `late_window_share`; `rating_mean`; `ride_mean`.
* **Auctions and hubs:** `auctions_total`, `outcome_<matched|retry|failed>`, `refuse_<reason>` (locked, too_late, over_reserve...),
  `bids_per_auction`, `single_bid_share`, `called_per_auction`, `pools_total`, `late_joins`, `handovers_adopted`, `overflow_offers`.
* **Brains and drivers:** `brain_moves`, `brain_commands`, `brain_refusals`, `brain_percepts`, `markup_changes`,
  `mover_ticks`, `mover_mean`, `mover_dispersion`, `mover_share_in_group`, `trips_per_driver_mean/min/max`.
* **Money:** `revenue_total`, `cost_loaded_total`, `cost_empty_total`, `net_profit_total` (EUR, with the driver cost model),
  `vehicle_trips`, `money_conserved` (1 = the ledger balances).
* **Over the run** (from the time series): `roll_<metric>_max/_mean` of the rolling-window waits, profit and riders per vehicle;
  `now_queue_len_max/_mean`, `now_idle_drivers_max/_mean`.
* **Clock health:** `sim_seconds`, `real_seconds`, `speed`, `speed_achieved` (sim seconds per real second: well below `speed`
  means the machine could not keep up and the waits are distorted), `ended_all_home`, `ended_by_cap`, `unfinished`.

Not measured yet: overshoot (brain moves beyond the deficit) and how closely the drivers' supply tracks demand.

## Per-hub metrics (equity)

Every run also records, for each hub: `hub_<id>_n` (trips), `hub_<id>_share` (share of all trips), `hub_<id>_wait_mean`, `hub_<id>_wait_p90`, `hub_<id>_not_completed`. Across hubs that served riders: `hubs_served`, `hub_wait_gini`, `hub_wait_cv`, `hub_wait_max_over_min` and `hub_wait_worst` — how unequal the waits are between hubs (0 Gini = every hub waits the same).


## Long scenarios and the new plans

`python tools/make_long_scenarios.py` writes `config/scenarios/long_*.json` (04, 05, 07 and the 8-hub scenarios, with and without
brains): the base scenario's demand, forecasts and events repeated 6 times, one cycle per simulated hour, so a run lasts about 6
simulated hours (25-30 real minutes at speed 15) and each driver works many trips. The arrival rate inside a cycle is the base one;
between the bursts drivers wait. Riders are re-used (a request goes to the first idle rider), so the rider count is the base count;
`skipped` in the report would say if that was too few. `--cycles` and `--period` change the length.

Plans: `long_04.json`, `long_05.json`, `long_07.json`, `long_8hubs.json` (arms: no_brain, no_brain_tv0, signal, signal_tv0; tv0 = drivers
value their time at 0 EUR/h in a bid) and `09_late_invite.json` (calling drivers that become idle during an auction, on and off, on 07
and on the 8-hub scenario). Compare an arm only with the other arms of the same plan.


## Calibration log

Every run also writes `drivers/<arm>_s<seed>_r<rep>.csv`: one cumulative row per driver every 30 sim minutes (t, revenue, trips,
metres, driving_s, hours, calls, bids, wins). `python tools/calibrate.py <run>/drivers` turns them into rolling 2 h windows
(net and gross earnings per hour, rolling surplus = net earnings − 4.96 €/h × hours, win rate, trips/hour) and prints the
median/P25/P75 and a suggested S* (P75 of the surplus). Plan `config/experiments/calibration_long.json` (seeds 11–13, default
behaviour) is for this; keep these seeds out of the evaluation runs.


## Labour market: exit and entry (`"labour_market": {...}`)

Optional; a scenario without it keeps a fixed fleet. Every `check_every` (1800 sim s) the market looks at each driver's rolling net
hourly earnings (revenue - 0.15 EUR/km, per duty hour, last 2 h):

* **exit**: a driver on duty at least `min_tenure` (3600 s) whose earnings were below `exit_ratio` x 4.96 (default 0.65) at
  `consecutive` (2) checks in a row leaves with probability `p_exit` (0.15) per check - only if it is idle with no lock, and never
  below `min_fleet` (3). Leaving is final.
* **entry**: if the average of the eligible drivers is above `entry_ratio` x 4.96 (1.10), one reserve driver comes on duty at the hub
  with the largest deficit (`common.cells.deficit`). The `reserve` drivers are offline agents started with the scenario
  (`"labour_market": {"reserve": 12}`), so no agent is spawned mid-run; they cost little CPU but do add agents.

These are behavioural rules, not estimated from data. Each exit/entry is logged to `market/<arm>_s<seed>_r<rep>.csv`
(t, event, driver, fleet_after). Metrics: `fleet_initial`, `fleet_end`, `fleet_min`, `fleet_max`, `fleet_mean` (time-weighted),
`exits`, `entries`, `turnover_per_hour`. Driver income per hour counts only the hours a driver was on duty. A smoke run with extreme
settings (p_exit 1, exit line = wage, 1 check) shrank the fleet from 24 to 8 at the first check and the mean wait went from about
260 s to 1400 s: exit without guards hurts riders, which is why the defaults need `consecutive`, `p_exit < 1` and `min_fleet`.
Plan: `config/experiments/labour_long.json` (fixed, market, market + rolling-surplus bid rule; 3 seeds, about 4.5 h).

`single_bid_share` (share of matched auctions with exactly one bid) is in the summary: a lone bidder is paid the reserve, so
watch it as the fleet shrinks (a rule of thumb: fine under 5 % at baseline and under 15 % at equilibrium; above 30 % the reserve
payment distorts the results).

Entry on waiting riders (optional, `entry_wait`): a shortage shows in waiting times, not in income (fares do not rise), so entry
on income alone reacts late when the fleet starts short (12 drivers: first entry after 2 h, mean wait 1,086 s). With
`"entry_wait": 600, "entry_min_waiting": 3, "max_enter": 2`, whenever at least 3 riders have waited more than 600 s for a pickup at
a check, 2 reserve drivers come on duty at the hubs with the largest deficit. Off by default. Plan `small_start_entry.json`.

# Metrics and the viewer panel

One definition of every metric, in `src/harness/live_metrics.py` (`MetricsCollector`). The WebSocket viewer draws it live and
the experiments can write the same snapshots to a file, so the numbers in the viewer and in the thesis are the same.

## How it works
* The launcher starts a collector; every 60 sim seconds it reads the agents and builds a snapshot:
  `cum` (whole run so far), `roll` (last 10 sim minutes), `now` (queue lengths, idle drivers), `failures`.
* It is sent as event `METRICS`; the server keeps all of them and replays them to a browser that connects later.
* Drivers keep an odometer by activity (`odo_m`, `odo_s`: loaded / pickup / other) and a per-trip log (`trip_log`),
  and the time of each payout (`payout_times`). Riders already keep timestamps.
* The end-of-run report prints the CO2 saving (loaded leg and all miles) and driver revenue/profit/deadhead/Gini.

## Definitions
Units: EUR, km, kg, seconds. A value is `null` when it cannot be computed yet.

| Group | Metric | Definition |
|---|---|---|
| Pooling | riders_per_vehicle | riders / completed vehicle-trips (late joiners count) |
| | pax_km_per_veh_km | direct passenger-km (each rider's own hub->home route) / (loaded + pickup) vehicle-km |
| | pooled_share, late_share | riders who shared a vehicle; riders who joined after the pool had a driver |
| | size_1..size_4 | share of vehicle-trips with 1..4 riders |
| CO2 | co2_saved_loaded_kg | sum(riders' separate routes - shared route) x g/km. **The incentive pays on this**: it is what pooling controls |
| | co2_saved_all_kg | the above + the pickup legs the riders would each have needed alone: (riders - 1) x the pool's pickup leg. **The honest footprint** |
| | co2_saved_all_pct | all-miles saving / what the same trips would have driven alone (pickup + loaded) |
| | km_loaded / km_pickup / km_other | vehicle-km by activity (other = cruising, returning, repositioning) |
| Riders | fare_gross, subsidy, incentive, fare_net | what the driver was paid for the rider; subsidy rebate; CO2 incentive; gross minus both |
| | fare_per_min | gross fare per minute of the direct ride |
| | pooling_discount | solo reference fare - fare paid (pooled riders only) |
| | wait_mean / p90 / max | request -> boarding |
| | detour_mean | ride time / direct ride time |
| | completion_rate | completed / ended trips (cumulative only); `failures` = ended trips by reason |
| Drivers | revenue_per_trip | payouts / vehicle-trips |
| | profit_per_trip | revenue - driver cost, per vehicle-trip; cost = 0.15 EUR/km x km + 4.96 EUR/h x (driving + boarding/alighting hours), for the trip legs AND the empty driving (cruising, repositioning, returning) in the window |
| | earnings_per_hour | revenue per driver per hour of the whole shift (idle included) |
| | earnings_per_driven_hour | revenue per hour actually driven (comparable with the 4.96 EUR/h wage) |
| | km_return, km_move, km_reposition | empty km without a rider by cause: back to the nearest hub / to a better hub chosen by the brain / ordered by a hub (km_other is their sum) |
| | util_loaded / pickup / idle / cruising / repositioning | share of driver-time, sampled once per tick |
| | deadhead_share | (pickup + other km) / all km |
| | gini | inequality of the drivers' earnings (0 = equal) |
| Market | time_to_match | request -> matched |
| | auction_attempts | mean attempts per pool (cumulative only) |
| | subsidy_spent, pooling_spent, pooling_eur_per_kg | fund spending (cumulative only); incentive cost per kg of loaded-leg CO2 |
| | now.queue_len, idle_drivers, failover_adopted | instantaneous |

Rider metrics are per rider, driver metrics per vehicle-trip. An event belongs to the window in which it ended
(a rider when dropped off, a vehicle-trip when it completed).

### Assumption of the all-miles baseline
What solo vehicles would have driven to reach each rider cannot be observed. We assume each rider alone would need its own pickup
leg as long as the pool's actual one (n solo vehicles do need n pickup legs). This favours pooling a little. Cruising and
repositioning are assumed the same in both worlds, so they cancel out.

## Viewer
`porto_graph_map.html` has a thin strip at the bottom (riders per vehicle, CO2 saved all miles, mean wait, profit per trip, always
cumulative) and four tabs: **Pooling & environment**, **Riders**, **Drivers**, **Market & system**. A tab opens a side panel
with its charts; clicking it again closes it; opening another closes the first. The panel has one switch,
Cumulative / Rolling 10 min, for all its charts (charts marked "cumulative only" ignore it). Gaps in a line mean the metric
was undefined at that time.


## Driver income (experiments; `live_metrics.driver_income_stats`)

Income of a driver = payments received - 0.15 EUR/km x every kilometre it drove (loaded, to pickups, empty). Time is not charged:
the wage is what the income should reach per hour ON DUTY, idle hours included (a driver is on duty from the start of the run to the
end of the run or its `shift_end`). Compare with `DRIVER_WAGE_TARGET_PER_HOUR` = 4.96 EUR/h. Because the hours are those of the run,
short runs and runs that end early (all riders home) lower the rate: compare arms of the same scenario only.

| Metric | Meaning |
|---|---|
| `driver_income_hour_mean/median/p10/min/max` | EUR per hour on duty, over drivers |
| `fleet_income_hour` | total income of all drivers / total hours on duty |
| `driver_wage_share` | share of drivers whose income per hour reaches the wage target |
| `driver_income_gini` | inequality of driver incomes (incomes below 0 counted as 0) |
| `driver_utilisation_mean` | driving seconds / seconds on duty (idle time is what is left) |
| `driver_km_mean` | km driven per driver |
| `driver_trips_zero_share` | share of drivers that completed no trip |
| `driver_revenue_total` | payments to all drivers |

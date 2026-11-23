# Microtransit MAS

A decentralised **last-mile microtransit** multi-agent system for Porto. Riders request trips from micro-hubs; hubs pool riders
and run a reverse auction among nearby drivers; drivers price bids from their own cost; payment, reputation and a regulator are
agents of their own. Optional BDI "brains" let idle drivers reposition themselves using only the demand signal the hubs publish.
The design follows the **Gaia** methodology (role, interaction and agent models), and the agents are built with
[SPADE](https://spade-mas.readthedocs.io/) (XMPP messaging).

## Requirements

* Python 3.12 or 3.13
* [uv](https://docs.astral.sh/uv/) (dependencies are installed from the project file)
* Nothing else: the launcher starts SPADE's own XMPP server when nothing listens on the configured port.

```bash
uv sync                         # core dependencies + the test tools (pytest)
uv sync --extra maps            # optional: also the map tools (osmnx, folium, ...) for tools/build_graph.py and tools/view.py
```

The core dependencies are SPADE, SPADE-BDI, networkx, websockets and SQLAlchemy (needed by SPADE's XMPP server).

## Run a scenario

```bash
uv run python -m harness.launcher --scenario config/scenarios/01_random_disturbance.json
uv run python -m harness.launcher --check        # start, ping every agent, print a table, stop
uv run python -m harness.launcher --run-for 60   # stop by itself after 60 real seconds
```

`harness.launcher` is the main entry point: the tests, the experiments and the Docker image all use it. The run ends by itself
when every rider is home (or at the scenario's `duration`) and prints a report. To watch it live, open `porto_graph_map.html` in a
browser (the launcher's `--open` does it for you): it connects to the simulation's WebSocket on `127.0.0.1:8765` and shows drivers
and live metrics.

**Optional, richer map.** `uv run python viz_demo.py config/scenarios/06_hub_failure.json` runs a scenario with extra map overlays
(street closures, traffic, hub load, repositioning and forecasts, hub failures). It is a demo tool: it may add riders to fill the map
and it is not used for experiments. Use the launcher for everything else.

Scenarios are plain JSON files in `config/scenarios/`:

| File | What it tests |
|---|---|
| `01_random_disturbance` | random disturbances on the road network |
| `02_heavy_traffic` | congestion |
| `03_burst_one_hub` | a demand burst at one hub |
| `04_centre_drivers_suburb_requests` | supply in the centre, demand in the suburbs |
| `05_antecipated_surge_reposition` | forecast surge and hub-driven repositioning |
| `06_hub_failure` | a hub goes down; neighbours take over its riders |
| `07_migrating_demand` | demand that moves between hubs over time |
| `long_*` | the 04, 05, 07 and 8-hub scenarios repeated for 6 simulated hours (`tools/make_long_scenarios.py`) |
| `full_8hubs` | a small 8-hub smoke scenario (4 drivers, 4 riders): not a baseline for the others |
| `brains_*` | the same scenarios with the driver brains switched on |

Choose the brain with `drivers.brain_policy`: `signal` (default), `proportional`, `pressure` or `expected` (expected net euros;
it moved more than `signal` without paying off, see `docs/bdi_driver.md`).

Scenarios 01, 02, 03, 06 and 07 and the pooling comparison send requests to all 8 micro-hubs; 04 uses 4 of them and 05 only one.

### Optional market features (off by default)

| Feature | Setting | Doc |
|---|---|---|
| Drivers leave and enter the market by income and waiting riders | top-level `labour_market` | `docs/experiments.md` |
| Rolling-surplus bid rule | `drivers.surplus_scale_hours` | `docs/auction_mechanism.md` |
| Payment to a lone bidder | `lone_margin` (default 0.10, `null` pays the reserve) | `docs/auction_mechanism.md` |
| Rider subsidy and pooling incentive | `payment.subsidy_discount`, `pool_incentive_per_kg` | `docs/experiments.md` |

## Run with Docker (nothing to install but Docker)

```bash
docker build -t microtransit-mas .
docker run --rm -p 8765:8765 microtransit-mas --scenario config/scenarios/07_migrating_demand.json
```

Then open `porto_graph_map.html` in your browser on the host: it connects to the simulation on `ws://127.0.0.1:8765`.
The image installs only the core dependencies (no test or map tools).
The demo with the extra map overlays also runs in the image (`-it` lets Ctrl-C stop it):

```bash
docker run --rm -it -p 8765:8765 --entrypoint python microtransit-mas viz_demo.py config/scenarios/06_hub_failure.json
```
Anything after the image name goes to the launcher (`--check`, `--run-for 60`, ...). Results of experiments, which are
sensitive to load, are best produced natively (see below), not in a container.

## Run the experiments

A single run is not reproducible (message timing is asynchronous), so experiments repeat each scenario over several seeds and
compare **arms** (scenario + overrides) with paired differences and 95% confidence intervals.

```bash
uv run python -m harness.experiment --plan config/experiments/07_brains.json --dry-run   # check the plan
uv run python -m harness.experiment --plan config/experiments/07_brains.json --seeds 1,2,3
bash scripts/run_overnight.sh                                                           # several plans in a row
```

The plans used in the project are in `config/experiments/`: `01`-`08` (brains and pooling on each scenario), `long_*` (6-cycle runs),
`calibration_long`, `labour_long`, `labour_sweep`, `small_start_*`, `s_star_sweep`, `brains_expected_long` and the `speed_probe` plans.
All the results are in `experiments.zip` (unzip it to `experiments/`). The earlier scenario runs (01-08) were made before the cost
model and lone-bidder changes, so compare arms only within the same batch.

Results go to `experiments/<date-time>/`: `runs.csv` (one row per run, about 125 metrics), `summary.csv` (mean, CI, difference to
the first arm), `series/` (per-minute viewer snapshots) and `config.json`. Run at clock speed 15; at speed 60 the load distorts
waits. See `docs/experiments.md` and `docs/metrics.md`.

## Tests

```bash
uv run pytest tests -q                  # everything (integration tests start an XMPP server by themselves)
uv run pytest tests -q -m "not integration"
uv sync --extra maps && uv run pytest tests/test_build_graph.py -q   # skipped unless the map tools are installed
```

## Project layout

```
microtransit-mas/
├── config/
│   ├── porto_graph.json          # the road network (nodes = cells, edges = road links) used by the scenarios
│   ├── graph.json, scenario.json # small default graph and scenario for demos
│   ├── scenarios/                # one JSON file per scenario (see the table above)
│   └── experiments/              # experiment plans: arms, seeds, speed
├── src/
│   ├── agents/                   # the agents (one class per Gaia role)
│   │   ├── microhub.py           # hub: pools riders, runs the auction, publishes the demand signal
│   │   ├── driver.py             # driver: FSM, bidding, trips; owns the lock
│   │   ├── rider.py              # rider: request, wait, trip, rating
│   │   ├── payment.py            # escrow, settlement, refunds
│   │   ├── reputation.py         # ratings and reputation scores
│   │   ├── regulator.py          # fares and policy
│   │   ├── environment.py        # traffic updates
│   │   ├── bid_rule.py           # rolling-surplus bid rule (pure functions)
│   │   ├── brain.py              # BDI brain of a driver (runs the .asl programs)
│   │   ├── expected_wins.py      # expected-wins brain: gain per hub in expected euros (pure functions)
│   │   ├── brains/               # AgentSpeak programs: driver.asl (pressure), driver_signal.asl, driver_proportional.asl, driver_expected.asl
│   │   ├── base_agent.py, stubs.py  # shared agent base (liveness ping)
│   │   ├── registry.py           # role name -> agent class, used by the launcher (not a yellow-page service)
│   ├── protocols/                # the interactions between agents
│   │   ├── match_auction.py      # CFP / bids / award
│   │   ├── pool_formation.py     # grouping riders into pools
│   │   ├── escrow.py, settlement.py, refund.py, payment_common.py
│   │   ├── inter_hub.py          # overflow and hand-over between hubs
│   │   ├── reposition.py         # hub-driven repositioning
│   │   └── base.py
│   ├── common/                   # shared building blocks
│   │   ├── settings.py           # every constant (costs, timings, tariffs)
│   │   ├── costs.py, auction.py, pooling.py, policy.py
│   │   ├── graph.py, cells.py    # road network and cell grid
│   │   ├── clock.py              # simulation clock (speed factor)
│   │   ├── ledger.py, lock.py, ontology.py, messages.py
│   │   └── ws_server.py          # WebSocket feed for the viewer
│   └── harness/                  # running and measuring
│       ├── launcher.py           # starts a scenario (python -m harness.launcher)
│       ├── scenario.py           # scenario file parsing and validation
│       ├── runner.py             # end condition and the final report
│       ├── experiment.py         # scenario-based experiment runner
│       ├── labour_market.py      # drivers leaving and entering the market (optional)
│       ├── live_metrics.py       # the metrics (viewer and experiments)
│       ├── demand.py, active.py, ping.py, metrics.py
│       └── demo_*.py             # small demos of single parts
├── Dockerfile, .dockerignore     # run a scenario in a container (see above)
├── scripts/run_overnight.sh      # runs several experiment plans one after another
├── tools/build_graph.py          # builds porto_graph.json from map data (needs --extra maps)
├── tools/view.py                 # draws the static base map from porto_graph.json (needs --extra maps); saves to tools/, never to the root
├── tools/calibrate.py            # rolling 2 h earnings/surplus/win-rate statistics from drivers/*.csv
├── tools/make_long_scenarios.py  # writes the long_* scenarios (demand cycles repeated)
├── porto_graph_map.html          # the live viewer (open in a browser)
├── viz_demo.py                   # optional: a scenario with extra map overlays (see above)
├── data/raw/                     # raw map data used to build the road graph
├── experiments.zip               # every experiment run (unzip to experiments/)
├── tests/                        # unit tests; tests/integration/ for multi-agent flows
├── docs/                         # one document per mechanism (see below)
│   └── gaia/                     # the Gaia design models (roles, interactions, environment, agents)
└── pytest.ini
```

## Documentation (`docs/`)

| Document | Content |
|---|---|
| `auction_mechanism.md` | the reverse auction, bids, prices, reserve and floor |
| `pooling.md` | how riders are grouped |
| `driver_lock.md` | one auction at a time per driver |
| `bdi_driver.md` | the driver brains and their policies |
| `inter_hub.md`, `late_join.md` | overflow, failover, riders that join late |
| `reputation_and_regulation.md`, `rider_and_reputation.md` | ratings, regulator |
| `metrics.md`, `experiments.md` | every metric, and how to run experiments |
| `gaia/` | the Gaia design models. Section 6 of `gaia/Agent Model.md` lists where the final system differs from the design (for example: no Directory Facilitator, the labour market, the lone-bidder rule) |

## Cost model

A driver's bid is `0.15 EUR/km x km + time value x driving and dwell time`. The time value defaults to the wage target of 4.96 EUR/h and can be
lowered per scenario (`drivers.time_value_per_hour`, 0 = kilometre cost only); the wage target is also the yardstick of the driver-income metrics.
Constants are in `src/common/settings.py`. A driver refuses a job when its cost is above the rider group's maximum fare.
Hubs also call drivers that become idle while an auction is open (`late_invite`, see `docs/auction_mechanism.md`).

Driver income is protected per trip (a winner is paid at least its bid), not per hour: idle time is unpaid, and in the experiments the
median income stays below the wage target. The regulator's `DRIVER_FLOOR` is only a check that a fare cap is not published below 1 EUR.

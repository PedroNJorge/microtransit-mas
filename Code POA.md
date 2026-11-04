# Plan of Action (POA) for coding

This is the most important part of your query. Below is a **phased POA** designed to prevent the classic MAS-student failure mode: trying to build everything at once and ending up with nothing running.

## Decisions to make before touching code

| Decision                      | Recommendation                                  | Why                                                                                        |
| ----------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **Language / framework**      | Python 3.12, SPADE 4.x                          | Matches your design; SPADE 3 uses asyncio; well-documented.                                |
| **XMPP server**               | Prosody (local) or SPADE's built-in test server | Local Prosody is stable, scriptable, and gives you real XMPP debugging.                    |
| **Simulation clock strategy** | **asyncio-based scaled time** (not simpy)       | Simplest path. See "The simpy trap" below.                                                 |
| **Graph library**             | `networkx`                                      | Matches Environmental Model.                                                               |
| **Serialization**             | JSON via `dataclasses.asdict()`                 | Matches ontology note.                                                                     |
| **Testing**                   | `pytest` + pytest-asyncio                       | Unit-test the ontology, auction evaluation, and driver state machine _before_ integration. |
| **Observability**             | SPADE web interface + logging to JSON files     | You will need to _see_ messages flowing.                                                   |
| **Repo layout**               | Monorepo, src-layout                            | See §3.1.                                                                                  |

### The simpy trap

Your Environmental Model mentions simpy. **I strongly recommend you don't use simpy.** Here is why:

- SPADE 3 already runs its own `asyncio` event loop.
    
- simpy is _also_ an event loop.
    
- Running simpy inside a SPADE agent (the Environment Agent) means **two schedulers** competing for control flow, which causes deadlocks and irreproducible races.
    

**Recommended alternative — scaled asyncio:**

```python

SIMULATION_SPEED = 10  # 10x real time
async def sim_sleep(sim_seconds: float):
    await asyncio.sleep(sim_seconds / SIMULATION_SPEED)
```
Every agent that "waits" (auction deadline, batching window, traffic update interval) calls `sim_sleep()`. Time is globally consistent because it derives from real time divided by a constant. No dual scheduler. No simpy dependency. Deadlines behave exactly as specified in your Organizational Rules.

If you _must_ have event-driven control (e.g., an event queue that can jump in time), add a tiny `SimulationClock` agent later — but for the MVP, scaled asyncio is the right call.

## 3.1. Repository structure

text

microtransit-mas/
├── src/
│   ├── common/
│   │   ├── ontology.py        # dataclasses: TripRequest, Bid, Pool, ...
│   │   ├── messages.py        # helpers: make_cfp(), make_propose(), ...
│   │   ├── graph.py           # networkx graph loader + travel times
│   │   ├── clock.py           # sim_sleep(), SIMULATION_SPEED
│   │   └── logging.py         # structured JSON logger
│   ├── agents/
│   │   ├── rider.py
│   │   ├── driver.py
│   │   ├── microhub.py
│   │   ├── payment.py
│   │   ├── reputation.py
│   │   ├── regulator.py
│   │   └── environment.py
│   ├── harness/
│   │   ├── launcher.py        # start all agents
│   │   ├── scenario.py        # load scenario config (riders, drivers, graph)
│   │   └── clock.py           # simulation clock (if not in common/)
│   └── protocols/
│       ├── trip_request.py    # FSMBehaviour for each protocol
│       ├── match_auction.py
│       ├── pool_formation.py
│       └── ...
├── tests/
│   ├── test_ontology.py
│   ├── test_auction_eval.py
│   ├── test_driver_fsm.py
│   └── integration/
│       └── test_single_trip.py
├── config/
│   ├── graph.json             # nodes, edges, weights
│   ├── scenario.json          # riders, drivers, homes
│   └── policy.json            # fare caps, equity zones
├── logs/                      # JSON logs per run
├── pyproject.toml
└── README.md

## 3.2. Milestone roadmap

Nine milestones, each with a **clear deliverable** and a **definition of done**. Do not move to the next until the current is green.

### Milestone 1 — Ontology + Contracts (2–3 days)

**Goal:** All dataclasses defined, serializable, tested.

**Deliverables:**

- `common/ontology.py` with every concept from System Decomposition §5.
    
- `common/messages.py` with helpers: `make_cfp(...)`, `make_propose(...)`, `make_inform(...)` that set `performative`, `protocol`, `ontology` metadata.
    
- `tests/test_ontology.py`: roundtrip test for each dataclass.
    

**Done when:** all ontology tests pass; a message built by `make_cfp` can be parsed back into a `CFP` dataclass.

### Milestone 2 — Graph + Clock + Environment stub (2–3 days)

**Goal:** Load the road network, expose travel times, and have a working simulation clock.

**Deliverables:**

- `common/graph.py`: load `config/graph.json` into `networkx.DiGraph`, precompute all-pairs shortest paths (with `networkx.all_pairs_dijkstra`), expose `travel_time(a, b)`.
    
- `common/clock.py`: `sim_sleep()`, `now()` (simulation time), `SIMULATION_SPEED`.
    
- `agents/environment.py`: broadcasts static `TrafficUpdateProtocol` messages every `T_traffic` (initially no traffic variation — just constant travel times).
    

**Done when:** a test driver agent running alone receives a traffic update every `T_traffic` simulated seconds.

### Milestone 3 — Agent skeletons + launcher (2–3 days)

**Goal:** All seven agents start, register with the DF, and exchange heartbeat pings.

**Deliverables:**

- `agents/*.py` with minimal `Agent` subclasses — each one is essentially a "hello world" that registers with the DF and responds to a `ping` message with a `pong`.
    
- `harness/launcher.py`: starts all agents from `config/scenario.json`.
    
- `harness/scenario.py`: parses scenario config.
    

**Done when:** you can run `python -m harness.launcher` and see all agents registered in the DF, and a manual ping from `spade` CLI reaches any agent.

### Milestone 4 — Payment Agent + Escrow + Settlement (3–4 days)

**Goal:** Full money flow works in isolation. **Start here because it has no complex state.**

**Deliverables:**

- `agents/payment.py`: implements `EscrowProtocol`, `SettlementProtocol`, `RefundProtocol`. In-memory `dict` for escrow accounts and wallets.
    
- `protocols/escrow.py`, `protocols/settlement.py`, `protocols/refund.py`: FSMBehaviour implementations.
    
- `tests/integration/test_payment_flow.py`: rider escrows → microhub informs trip completed → driver paid, rider refunded, subsidy disbursed.
    

**Done when:** an integration test exercises hold → settle → disbursement in both success and failure (refund) paths.

### Milestone 5 — Microhub Agent + simple auction (4–5 days)

**Goal:** Microhub runs a complete reverse auction, but with **stub drivers** (a single test driver that always bids).

**Deliverables:**

- `agents/microhub.py`: implements `MatchAuctionProtocol` as an FSMBehaviour.
    
- `protocols/match_auction.py`: FSM with states `Start → CFP_Sent → Collecting_Bids → Evaluating → Awarding → Informing → End`.
    
- Bid evaluation function: multi-attribute weighted score (price, wait time, detour, reputation). **Unit-test this separately.**
    
- `tests/test_auction_eval.py`: given fixed bids, the winning bid is deterministic.
    

**Done when:** a single rider request at a microhub triggers a CFP, gets a stub bid, the auction awards, and the rider is informed. Log shows the full protocol.

### Milestone 6 — Driver Agent (5–7 days)

**Goal:** Real driver agents bid, hold locks, and execute trips.

**Deliverables:**

- `agents/driver.py`: implements `MatchAuctionProtocol` (contractor side), `DriverStatusProtocol`, `TripExecutionProtocol`, `RepositionProtocol`.
    
- Provisional lock state machine per CC-01 to CC-09. **Unit-test the lock behaviour separately** — this is where most bugs live.
    
- `tests/test_driver_fsm.py`: lock acquisition, expiry, release, rejection on `refuse(reason: locked)`.
    

**Done when:** two drivers compete for one auction; one wins, one receives `reject-proposal`; the winner drives to pickup, boards, drops off, and returns to idle.

### Milestone 7 — Rider Agent (3–4 days)

**Goal:** Real rider agents request, accept pools, and rate.

**Deliverables:**

- `agents/rider.py`: implements `TripRequestProtocol`, `PoolFormationProtocol` (responder), and `ReputationProtocol` (rater).
    
- `tests/integration/test_single_trip.py`: rider requests → microhub auctions → driver executes → settlement → rating → reputation update.
    

**Done when:** the full single-trip pipeline works end-to-end with three agents and Payment.

### Milestone 8 — Reputation + Regulator (3–4 days)

**Goal:** Ratings published with `pending_settlement` flag (ID-07); regulator imposes fare caps and audits.

**Deliverables:**

- `agents/reputation.py`: implements `ReputationProtocol`, bounded scores, `pending_settlement` flag per ID-07.
    
- `agents/regulator.py`: implements `PolicyProtocol`, `AuditProtocol`.
    
- `tests/integration/test_reputation_flow.py`: rating submitted before settlement → not published → published after settlement.
    

**Done when:** a rider's reputation score does not become visible to microhubs before settlement, and becomes visible after.

### Milestone 9 — Pooling (4–5 days)

**Goal:** Two or more riders share a driver, respecting detour tolerance and time windows.

**Deliverables:**

- `protocols/pool_formation.py`: batch window (`T_pool`), compatibility filter (time window overlap, detour tolerance), pool proposal, partial-acceptance handling.
    
- Insertion heuristic in microhub: given a route and a new rider, compute `detour(r)` per the Environmental Model §2.1 formula.
    
- `tests/integration/test_pooled_trip.py`: two riders pooled; detour constraint respected; fare split proportional; both rated.
    

**Done when:** you can demonstrate a trip where detour stays within tolerance for both riders and pooling discount appears in settlement.

### Milestone 10 — Repositioning (3–4 days)

**Goal:** Idle drivers may reposition for incentives.

**Deliverables:**

- `protocols/reposition.py`: microhub requests, driver accepts/refuses, driver arrives and broadcasts.
    
- A simple repositioning heuristic in the microhub: if idle drivers exist and predicted demand at another staging point exceeds threshold, issue a reposition request.
    

**Done when:** an idle driver at a staging point accepts a reposition request and moves, and this is reflected in `DriverStatusProtocol` broadcasts.

### Milestone 11 — Inter-hub coordination (3–4 days)

**Goal:** Hubs absorb overflow and recover from failure.

**Deliverables:**

- `protocols/inter_hub.py`: demand forecasts, overflow requests.
    
- Failure recovery: kill a microhub agent, neighbour picks up its pending requests within `T_failover`.
    

**Done when:** an integration test kills a hub and observes neighbouring hubs absorbing requests.

### Milestone 12 — Experiments & evaluation (1–2 weeks)

**Goal:** Produce the results you promised in the theme.

**Deliverables:**

- Scenario configurations that vary: fleet size, pooling willingness, equity zone weights, pricing rule (first-price vs. second-price), auction weight configurations per hub.
    
- Metric collection: wait time, detour, occupancy, cost, Gini coefficient on service, driver utilisation.
    
- Plots and a report section.
    

**Done when:** you can produce a comparison chart showing, e.g., "First-price auction yields 15% higher occupancy than second-price but 8% higher wait time in low-density zones."

---

## 3.3. Coding advice (concrete)

1. **Write the FSM as an explicit state enum**, not as implicit `if` branches. Debugging `MatchAuctionProtocol` on a single message string is trivial; debugging it across 50 branches is not.
    
2. **Log every message** to `logs/run_{timestamp}/messages.jsonl` with `(sim_time, sender, receiver, performative, protocol, content_hash)`. You will spend far more time debugging message flow than logic.
    
3. **Test the auction evaluator in isolation** before wiring it into the microhub. Given `[bid_a, bid_b, bid_c]` and known weights, the expected winner must be assertable. This is your highest-value unit test.
    
4. **Never test with more than 2 drivers and 2 riders** until Milestone 9 passes. Multi-agent bugs at scale are symptoms of bugs at scale 2.
    
5. **Treat `common/ontology.py` as sacred.** Any change to it must be reflected in a `test_ontology.py` change. This prevents serialization drift between agents.
    
6. **Use SPADE's web interface** (`http://localhost:8000/spade`) constantly — you can watch message exchange live and pause/resume agents.
    
7. **Keep the Environment Agent dumb.** It should only emit traffic updates on a timer. All real computation (auction evaluation, route insertion, pool formation) belongs in microhubs.
    
8. **Don't implement timers manually.** Use `asyncio` `await sim_sleep(...)` inside behaviours. Never `threading.Timer`.
    
9. **Version your scenarios.** Every experiment run should have an immutable `scenario.json`. Otherwise you cannot reproduce results.
    
10. **Commit daily.** MAS projects fail by losing a working state; not by lack of features.
    

## 3.4. Definition of Done (project level)

You can consider the code project complete when:

- □
    
    You can run `python -m harness.launcher --scenario config/scenarios/baseline.json` and observe a complete simulated day.
    
- □
    
    Every protocol in the Interaction Model has a corresponding FSM in code.
    
- □
    
    Every global constraint (GC-01 to GC-17) has at least one test asserting it is not violated.
    
- □
    
    Every failure path (FE-01 to FE-12) has at least one test exercising it.
    
- □
    
    You can generate a metrics report (wait time, detour, occupancy, Gini) from any run.
    
- □
    
    You can produce a comparison plot for at least two auction rules or two pricing schemes.
    

---

## 3.5. My top three warnings

1. **Do not use simpy inside the Environment Agent.** This is the single biggest trap. Use scaled `asyncio.sleep` per §3.0.
    
2. **Do not implement the auction evaluator and the auction FSM simultaneously.** The FSM is orchestration; the evaluator is business logic. Test them independently first.
    
3. **Do not skip Milestone 3 (agent skeletons + launcher).** A working launcher that starts seven dumb agents is worth more than three smart agents you cannot run together.
    

Once Milestone 1 (ontology) is green, everything downstream becomes mechanical. The ontology is where you will spend the most thinking time, and it is the only part of the code that needs to be perfect.
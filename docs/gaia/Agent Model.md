---
tags:
  - MAS
  - Gaia
  - AgentModel
  - ServicesModel
  - SPADE
  - Microtransit
status: Detailed Design Phase - Code Ready
---

# Gaia Agent Model & Services Model: Microtransit MAS

This document represents the **Detailed Design Phase** of the Gaia methodology. It maps the abstract *Roles* defined during the Analysis Phase to concrete *Agent Classes* to be implemented in SPADE (Python). It also defines the **Services** (methods and behaviours) each agent provides.

---

## 1. Agent Class Mapping

A single SPADE agent class may play multiple roles. The table below defines the concrete agent classes, the roles they adopt from the Analysis Phase, and their instance counts in the simulation.

| SPADE Agent Class | Gaia Roles Played | Runtime Instances | Description |
| :--- | :--- | :--- | :--- |
| `RiderAgent` | Rider (Transit, Financial) | Many (dynamic) | Represents a user requesting a trip and paying fares. |
| `DriverAgent` | Driver (Transit, Financial) | Many (dynamic) | Represents an autonomous vehicle/driver earning payouts. |
| `MicrohubAgent` | Microhub (Transit), Auctioneer (Transit), Coordinator (Inter-Hub) | Several (fixed graph nodes) | Local mediator at a graph node. |
| `PaymentAgent` | Payment (Financial) | 1 (Centralized) | Escrow, settlement, and subsidy disbursement. |
| `RegulatorAgent` | Regulator (Regulation) | 1 (Centralized) | Sets policy, audits, and enforces equity. |
| `ReputationAgent` | Reputation (Trust) | 1 (Centralized) | Tracks and publishes trust scores. |
| `EnvironmentAgent` | Environment (Simulation) | 1 (Centralized) | Simulates traffic, disruptions, and time. |

> **Note on Centralized Agents:** While the physical matching and dispatch are completely decentralised (handled by independent `MicrohubAgent`s), the infrastructure services (Payment, Trust, Regulation) are simulated as single, centralised utility agents to simplify the MVP. In a production system, these could be implemented as a distributed ledger (blockchain), a federated reputation network, or a government API.

---

## 2. Agent Relationships and Discovery

To maintain the decentralised nature of the transit operations, agents do not hardcode each other's addresses. 

- **Directory Facilitator (DF):** All agents register with the SPADE DF upon startup.
  - `DriverAgent` registers with its current location, capacity, and availability state.
  - `MicrohubAgent` registers its node ID and coverage area.
  - Centralized agents (Payment, Regulator, Reputation) register their service types.
- **Discovery Mechanism:** 
  - Riders find Microhubs by querying the DF for the hub matching their origin node.
  - Microhubs find Drivers by querying the DF for agents with the `Driver` service within their coverage area.
- **Communication Topology:** 
  - Star topology per hub (Hub ↔ Riders, Hub ↔ Drivers).
  - Full mesh for Inter-Hub Coordination (Hub ↔ Hub).
  - Broadcast from Environment, Regulator, and Reputation agents to all.

---

## 3. Agent Lifecycle

Standard SPADE lifecycle management (`setup()`, `teardown()`):

1. **Startup (`setup`)**: Agent authenticates with XMPP server, registers with DF, and spawns its initial `Behaviour` classes.
2. **Operation**: Agents execute behaviours (Cyclic, OneShot, or FSM) reacting to incoming ACL messages and internal timers.
3. **Failure / Shutdown (`teardown`)**: Agents deregister from the DF. If a `MicrohubAgent` fails, neighbouring hubs detect the DF deregistration and trigger overflow absorption (FE-07).
   > **Important Implementation Note:** SPADE does not instantly detect if an agent crashes abruptly (e.g., an unhandled Python exception) without a heartbeat mechanism. The **Simulation Harness (`main.py`)** is responsible for gracefully crashing or stopping agents (e.g., `await agent.stop()`) and ensuring the DF is updated so that failure recovery protocols (FE-07) can trigger.

---

## 4. Services Model (Folded In)

This section defines the specific methods (services) each agent class must implement, along with their **Pre-conditions** (guards) and **Post-conditions** (effects). In SPADE, these map directly to methods called inside `Behaviour` classes.

### 4.1. `MicrohubAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `receive_trip_request(request)` | Receives a rider request and queues it for pooling. | Rider has sufficient funds (ID-01). | Request added to batching pool. |
| `form_pool(batch)` | Aggregates compatible requests into a pool. | Batch size >= `min_pool_size` (GC-11). | Returns a locked `Pool` object; initiates auction. |
| `run_auction(pool)` | Executes the `MatchAuctionProtocol` FSM. | Escrow confirmed for all riders (ID-02). | Winning driver assigned; riders and drivers notified (GC-09). |
| `evaluate_bids(bids)` | Multi-attribute scoring (price, ETA, reputation). | At least one valid bid received. | Returns the winning bid (or None). |
| `request_reposition(driver)` | Sends `RepositionProtocol` message. | Driver is idle and not locked (GC-13). | Driver moves to staging point. |

> **Unit Testing Tip:** `evaluate_bids(bids, weights) -> Bid` should be implemented as a **pure Python function** outside the SPADE `FSMBehaviour` class. This allows you to write fast `pytest` unit tests without needing to boot up the SPADE XMPP server.

### 4.2. `DriverAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `handle_cfp(cfp)` | Evaluates a Call for Proposal and decides to bid. | Driver is idle and not locked (CC-01). | Sends `propose` or `refuse`. |
| `calculate_bid(pool)` | Computes price, ETA, and detour based on traffic. | Driver has capacity and shift time (GC-01, GC-04). | Returns a `Bid` object. |
| `commit_trip(assignment)` | Locks the provisional bid into a firm commitment. | Received `accept-proposal`. | Driver state = COMMITTED (CC-03). |
| `execute_trip(route)` | Moves to pickup, boards, and drops off riders. | Trip is committed. | Riders dropped off; triggers `trip_completed` inform. |

### 4.3. `RiderAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `create_request()` | Generates a trip request (time window, max fare). | Rider state = IDLE. | Sends `request` to Microhub. |
| `evaluate_pool(proposal)` | Accepts or rejects a pool proposal based on fare/detour. | Received `propose` from hub. | Sends `accept-proposal` or `reject-proposal`. |
| `pay_fare(amount)` | Triggers `EscrowProtocol` with Payment Agent. | Match is confirmed. | Funds locked in escrow (GC-02). |
| `rate_driver(score)` | Sends rating to Reputation Agent. | Trip is completed. | Rating recorded (ID-07). |

### 4.4. `PaymentAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `hold_escrow(fare)` | Locks rider funds in a temporary account. | Rider has sufficient balance. | Returns `escrow_id`; notifies hub (ID-02). |
| `settle_trip(escrow_id)` | Releases funds to driver and applies subsidies. | Received `trip_completed` (ID-06). | Driver paid; escrow closed (GC-03). |
| `process_refund(escrow_id)` | Returns funds to rider. | Trip cancelled or failed (FE-01). | Rider refunded; escrow closed. |

### 4.5. `EnvironmentAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `tick_clock()` | Advances simulation time (or steps the event loop). | Simulation is running. | Global clock incremented. |
| `update_traffic()` | Mutates road network edge weights. | Timer interval `T_traffic` elapsed. | Broadcasts `TrafficUpdateProtocol` (GC-12). |
| `trigger_disruption()` | Creates accidents or road closures. | Random interval. | Broadcasts `DisruptionProtocol`. |

### 4.6. `RegulatorAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `publish_policy()` | Broadcasts fare caps and equity rules. | System initialised. | All hubs/drivers receive `PolicyProtocol` (RI-04). |
| `audit_hub(hub_id)` | Requests logs and checks for compliance. | Timer interval `T_audit` elapsed. | Penalties applied if non-compliant (RI-05). |

### 4.7. `ReputationAgent` Services

| Service / Method | Description | Pre-condition | Post-condition |
| :--- | :--- | :--- | :--- |
| `record_rating(agent, score)` | Accepts a rating from a rider or driver. | Trip is completed (may be pending settlement). | Score stored in a pending queue. |
| `publish_rating(agent)` | Publishes the updated trust score. | Settlement confirmed or `T_settlement_max` timeout reached (ID-07). | Score published; moving average updated. |
| `query_reputation(agent)` | Returns the current published trust score. | Received `query-if`. | Sends `inform` with bounded score. |

---

## 5. SPADE Implementation Blueprint

When you open your IDE, your directory structure should map directly to this Agent Model:

```text
microtransit_mas/
├── main.py                 # Simulation Harness (starts agents, steps clock)
├── common/
│   ├── ontology.py         # Dataclasses (TripRequest, Bid, Pool, Policy)
│   └── settings.py         # Parameters (T_pool, max_capacity, etc.)
├── agents/
│   ├── base_agent.py       # Common setup, DF registration, ACL helpers
│   ├── rider.py            # RiderAgent (RiderAgent(spade.agent.Agent))
│   ├── driver.py           # DriverAgent
│   ├── microhub.py         # MicrohubAgent
│   ├── payment.py          # PaymentAgent
│   ├── regulator.py        # RegulatorAgent
│   ├── reputation.py       # ReputationAgent
│   └── environment.py      # EnvironmentAgent
└── behaviours/
    ├── auction_fsm.py      # FSMBehaviour for MatchAuctionProtocol
    ├── pooling_fsm.py      # FSMBehaviour for PoolFormationProtocol
    └── trip_fsm.py         # FSMBehaviour for TripExecutionProtocol
```

### 5.1. Behaviour Mapping

|Service / Protocol|SPADE Behaviour Class|Type|
|---|---|---|
|`run_auction` (Hub)|`AuctionFSM`|`FSMBehaviour`|
|`handle_cfp` (Driver)|`HandleCFPBehaviour`|`CyclicBehaviour`|
|`form_pool` (Hub)|`PoolingBehaviour`|`TimeOutBehaviour` (every `T_pool`)|
|`execute_trip` (Driver)|`TripExecutionFSM`|`FSMBehaviour`|
|`update_traffic` (Env)|`TrafficUpdateBehaviour`|`PeriodicBehaviour` (every `T_traffic`)|
|`publish_policy` (Reg)|`PolicyBehaviour`|`OneShotBehaviour` (on startup)|

---

## 6. Implementation Notes and Deviations (final system, November 2026)

Sections 1 to 5 are the **design** as it was planned. This section records where the implementation differs from it or goes
beyond it, so the two can be read together. The per-mechanism details are in `docs/`.

### 6.1. Discovery: no Directory Facilitator

SPADE 4 has no Directory Facilitator, so section 2 was not implemented as written.

- **Scenario-driven addresses.** The scenario file fixes the JID of every fixed agent (hubs, payment, regulator, reputation,
  environment). Agents do not search for each other. The launcher derives who needs whom from that roster.
- **Drivers announce themselves.** A driver sends a `DriverStatus` message to the hubs at every state change (idle, cruising,
  busy, offline, with position and ETAs). This is how a hub knows which drivers it can call, including for late invitations.
- **`agents/registry.py` is not a yellow-page service.** It only maps a role name to its agent class, for the launcher.
- **Liveness.** `HeartbeatProtocol` (ping, pong) lets the launcher check that every agent is up and is the right kind of agent.

### 6.2. The driver is two agents

The BDI driver of section 4 is split in two:

- `DriverAgent` (reactive): bids, holds the lock, drives, and reports. It observes every call and demand signal.
- `DriverBrain` (SPADE-BDI, AgentSpeak programs in `agents/brains/`): receives a `BrainPercept` and answers with a
  `CruiseCommand`. Its intention is `cruising_to(hub)`. Four policies exist: pressure, signal, proportional, expected-wins.

Hubs do **not** tell BDI drivers where to go. They publish `HubLoad` (every 60 s, to all drivers) and `DemandSignal` (to
drivers that were not called for a waiting request), and each driver decides whether moving pays. A cruising driver stays
available and refuses to cruise while it holds a lock. The hub-driven `RepositionProtocol` (request with an incentive,
agree or refuse) still exists but is opt-in.

### 6.3. The driver lock

Each driver holds **one** provisional lock at a time (`common/lock.py`, rules CC-01 to CC-09): `PROVISIONAL` (a bid in
flight, expires after `T_PROVISIONAL`) then `FIRM` (after `accept-proposal`). A call that arrives while the lock is held is
answered `refuse (locked)`. A driver therefore cannot win two auctions at once. Bidding on several pools at once is out of
scope.

### 6.4. Auction

- Reverse auction (FIPA Contract Net) with second-score pricing: the winner is paid
  `min(reserve, own bid + gap to the second score)`. Truthful bidding on price is dominant **with two or more valid bids**.
- **Lone bidder.** With one valid bid there is no runner-up. The first version paid the reserve, which over-paid. The final
  rule pays `bid x (1 + lone_margin)`, capped at the reserve (`lone_margin = 0.10`). Incentive compatibility does not hold
  in that case. `single_bid_share` is reported to show how often it happens.
- **Late invitations.** If fewer than two bids are in, the auction stays open for `late_hold` (15 s) and, every
  `t_late_poll` (2 s), the hub sends the same call to drivers that have become idle and are within range.
- The reserve is `n x min(max_fare)` of the riders in the pool, lowered by the tariff reserve and the regulator cap.

### 6.5. Driver pricing

A driver's bid is its km cost (0.15 EUR/km) plus a time price. The default time price is the wage target (4.96 EUR/h). An
optional **rolling-surplus bid rule** lowers the time price when the driver's recent surplus is below a reference. It is a
behavioural assumption about drivers, not an optimisation, and is off by default.

### 6.6. Labour market (not in the design)

An optional process in `harness/labour_market.py` runs every 1800 simulated seconds. It is part of the **harness, not an
agent**: drivers do not negotiate entry or exit.

- **Exit:** an idle driver with no lock leaves if its rolling net income stays below `exit_ratio x wage` for
  `consecutive` checks (with probability `p_exit`), and never below `min_fleet`.
- **Entry:** a reserve (offline) driver enters at the hub with the largest supply deficit when the mean net income is above
  `entry_ratio x wage`, or when riders have been waiting too long (`entry_wait`).
- The fleet size therefore emerges from income and waiting times.

### 6.7. Payment and subsidies

- Escrow, settlement and refund follow the design. The driver is paid exactly the auction price.
- **Subsidy.** An eligible rider is rebated `subsidy_discount x fare` (0.5 in the scenarios) from a subsidy fund, **on top
  of** the driver's fare. It never reduces driver pay. Eligibility is a per-request probability in the demand settings (default 0).
- An optional pooling incentive pays riders of a shared trip per kg of CO2 saved (default off).
- The subsidy is a rider-side measure and does not raise driver income.

### 6.8. Reputation and rating

- A rider rates 1 to 5 after drop-off. The default rule is 5, minus one for a pickup more than 120 s late, minus one for
  boarding after the end of the rider's time window.
- A valid rating is stored at once but published only when payment confirms settlement, or after `T_settlement_max`
  (flagged `pending_settlement`). The hub uses the published score, never the driver's claim, as a penalty term when ranking bids.

### 6.9. Regulator

The regulator audits each hub every `T_AUDIT` (3600 s) for fares above the cap in force and reports the service share per
destination zone. Penalties: 10 EUR per fare above the cap and 50 EUR for not cooperating. `DRIVER_FLOOR` (1.0 EUR) is only
a safety check: a fare cap below it is refused. It is **not** a minimum fare or an income guarantee for drivers.

### 6.10. Actual project layout

```text
microtransit-mas/
├── src/
│   ├── agents/       # rider, driver, brain, microhub, payment, regulator, reputation, environment, base_agent, registry
│   ├── protocols/    # match_auction, pool_formation, escrow, settlement, refund, reposition, inter_hub
│   ├── common/       # ontology, settings, auction, lock, ledger, pooling, policy, graph, cells, clock, ws_server
│   └── harness/      # launcher, scenario, runner, experiment, labour_market, live_metrics
├── config/           # scenarios, experiment plans, the Porto road graph
├── tools/            # graph building, calibration, long-scenario generator
└── docs/             # one document per mechanism
```

### 6.11. Known limits

- Incentive compatibility fails with a single bidder (6.4).
- A driver finishing a trip cannot bid for a job after the trip (not built).
- The labour market, bid rule and brains were evaluated mainly on the migrating-demand scenario.
- Results depend on the simulation speed (validated at 15); different code versions are not strictly comparable.

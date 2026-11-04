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

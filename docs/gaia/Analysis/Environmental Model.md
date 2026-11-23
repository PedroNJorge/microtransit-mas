---
tags:
  - MAS
  - Gaia
  - EnvironmentalModel
  - SPADE
  - Microtransit
status: Analysis Phase - Complete
---

# Gaia Environmental Model: Microtransit MAS

This document defines the **Environmental Model** for the decentralised last-mile microtransit MAS. According to the Gaia methodology, the Environmental Model identifies the resources, entities, and percepts that exist in the world, and how agents can interact with them.

> **Revision note:** This version adds Gaia-standard environmental properties, a formal graph definition, a time model, resource allocation rules, observability boundaries, a clearer separation of agent actions vs. environment transitions, an expanded percept list, an expanded action list, a SPADE implementation mapping, and a hub lifecycle subsection. The Environment/Environment Agent boundary has been rewritten for consistency with Section 6.

---

## 1. Environment Overview

The environment is a **spatial, temporal, and economic landscape** composed of:

- A graph of **microhub nodes** (physical pickup points).
- **Off-graph home locations** (rider destinations).
- A **road network** that determines travel times, distances, and detours.
- A **financial system** that handles escrow, fares, and subsidies.
- **Dynamic conditions** such as traffic, weather, and disruptions.

The environment itself is **passive**: it is the context in which agents operate. An active software component, the **Environment Agent**, simulates changes to this passive environment and broadcasts them via `TrafficUpdateProtocol` and `DisruptionProtocol`. The Environment Agent is not the environment; it is an observer and simulator of the environment.

### 1.1 Environmental Properties (Gaia Standard)

| Property | This system | Justification |
| :--- | :--- | :--- |
| **Discrete / Continuous** | Discrete | Time advances in ticks or events; nodes and staging points are discrete. |
| **Deterministic / Non-deterministic** | Non-deterministic | Rider arrivals, driver decisions, traffic variability, and disruptions are stochastic. |
| **Static / Dynamic** | Dynamic | Traffic, demand, driver locations, and disruptions change during execution. |
| **Accessible / Inaccessible** | Partially accessible | Agents observe local state (own location, own hub demand) but not the whole system. |
| **Observable** | Partially observable | Drivers see traffic on their route; microhubs see demand at their node; no agent sees everything. |
| **Bounded / Unbounded** | Bounded | The road network, fleet, and hub set are finite. |
| **Turn-based / Real-time** | Event-driven | The simulation advances on events (requests, bids, traffic updates, completions). |

---

## 2. Environmental Entities (Passive Objects)

These are the physical or logical objects that exist in the system. They do not make decisions; they are acted upon by agents.

| Entity | Description | Interacted By |
| :--- | :--- | :--- |
| **Microhub Node** | A physical pickup point in the graph. | Microhub Agent, Rider Agent, Driver Agent |
| **Home Location** | An off-graph destination for a rider. | Rider Agent, Driver Agent |
| **Road Segment** | A connection between two nodes with a travel time and distance. | Traversed by Driver Agent; updated by Environment Agent |
| **Staging Point** | A strategic waiting area between microhubs. | Driver Agent (repositioning), Microhub Agent (incentives) |
| **Vehicle** | The physical car used by a driver. Has a fixed capacity. | Driver Agent |
| **Escrow Account** | A temporary holding account for rider fares. | Payment Agent, Rider Agent |
| **Wallet** | A digital balance for riders and drivers. | Payment Agent, Rider Agent, Driver Agent |
| **Virtual Hub** | A temporary pickup point instantiated by a Microhub Agent when demand spikes outside existing hubs. | Microhub Agent, Rider Agent, Driver Agent |

> **Note on capacity:** Vehicle capacity is a property of the Vehicle entity. "Available seats" is a derived state (`max_capacity − current_passengers`), not an independent entity.

### 2.1 Road Network Formalisation

The road network is a **directed weighted graph** `G = (N, E, W)`:

- `N` is the set of **microhub nodes** (fixed at system initialisation, plus any virtual hubs).
- `E ⊆ N × N` is the set of **road segments** (directed edges).
- `W: E → ℝ⁺ × ℝ⁺` assigns each edge a pair `(travel_time, distance)`.
- The **topology is static**; edge weights are updated dynamically by `TrafficUpdateProtocol`.
- **Home assignment:** each home is assigned to its nearest node by **road network distance** (not Euclidean). Multiple homes may be assigned to the same node, forming a home cluster.
- **Detour formula:** For a rider `r` already on a route, detour is computed as:
  `detour(r) = route_time_with_r − route_time_without_r`
  Detour tolerance (GC-06) is expressed as a maximum allowed `detour(r)`.
- **Sparse vs. complete:** the graph is assumed sparse; not every pair of nodes is directly connected. Shortest paths are computed using Dijkstra or precomputed all-pairs shortest paths.

### 2.2 Hub Lifecycle

- **Physical microhubs:** fixed at system initialisation and never destroyed.
    
- **Virtual hubs:** may be instantiated dynamically by a Microhub Agent when demand in an uncovered area exceeds a configurable threshold. Virtual hubs are destroyed when demand falls below a lower threshold for a sustained period.
    
- **Destruction safety:** a virtual hub **must not be destroyed while any of the following hold**:
    
    - There are **pending rider requests** queued at that node (not yet matched or rejected).
        
    - There are **assigned but not-yet-completed trips** whose pickup or drop-off is at that node.
        
    - There is an **active `MatchAuctionProtocol`** currently running at that node.  
        The virtual hub must remain active until all associated trips have reached a terminal state (completed, cancelled, or refunded). This mirrors the spirit of GC-02 (no trip in progress without escrow) and FE-04 (trip cancellation on re-negotiation failure).
        
- **Reactivation:** a virtual hub that was about to be destroyed but received a new request within the destruction grace period must be retained (do not oscillate between instantiate/destroy).
    
- **Hub failure:** a physical or virtual hub may go offline; pending requests are absorbed by neighbouring hubs per FE-07.
    
- **Coverage areas:** each hub has a coverage radius over nodes, defined in terms of road-network distance. Virtual hubs inherit a coverage area from their instantiating agent.
---

## 3. Environmental Resources

Resources are things that are consumed, produced, or exchanged. They are finite and must be managed.

| Resource | Description | Consumed By | Produced By |
| :--- | :--- | :--- | :--- |
| **Seats** | Consumable abstraction of vehicle capacity. | Riders | Drivers (supply) |
| **Money (Fares)** | Payment for trips. | Drivers (payout) | Riders (payment) |
| **Subsidies** | External funds for eligible riders (inflow, not produced internally). | Riders | Regulator Agent |
| **Reputation** | Trust score affecting future matches. Derived from ratings. | Drivers, Riders | Reputation Agent |
| **Fuel / Battery** | Energy consumed by vehicles (optional; may be ignored for MVP). | Drivers | Environment (simulated) |
| **Simulated Time** | The dimension along which all events are scheduled. | All agents | Environment (clock) |

> **Note on time:** "Time" is an environmental dimension, not a consumable resource. It is treated in Section 3.1 (Time Model) rather than as a tradeable good.

### 3.1 Time Model

- **Representation:** discrete event-driven simulation with a **global simulation clock** measured in simulated seconds.
- **Advancement:** the clock advances when an event occurs (request arrival, bid submission, traffic update, trip completion). No fixed-step ticking is required.
- **Agent clocks:** each agent maintains a local clock, synchronised to the global clock with tolerance `T_clock` (see TS-01).
- **Deadlines:** all timeouts in the Organizational Rules (`T_award`, `T_provisional`, `T_pool`, etc.) are expressed in simulated seconds.
- **Time horizons:** shift durations, time windows, and batching windows are all measured in simulated seconds.
- **Monotonicity:** time only moves forward; trips cannot be scheduled in the past.

### 3.2 Resource Allocation Rules

| Resource | Allocation mechanism |
| :--- | :--- |
| **Seats** | Allocated through `MatchAuctionProtocol` (the winning driver commits capacity). |
| **Money (Fares)** | Held in escrow via `EscrowProtocol`; released via `SettlementProtocol`. |
| **Subsidies** | Defined by the Regulator; disbursed by the Payment Agent via `SettlementProtocol` per RI-06. |
| **Reputation** | Maintained globally by the Reputation Agent; may be weighted per-hub when used in bid evaluation. |
| **Fuel / Battery** | Simulated; consumed on `Move` and `Reposition` actions. |
| **Simulated Time** | Not allocated; every agent experiences the same global clock. |

---

## 4. Environmental Percepts (Observable State)

Percepts are the aspects of the environment that agents can observe. These are inputs to decision-making processes.

| Percept | Description | Granularity / Frequency | Observable By |
| :--- | :--- | :--- | :--- |
| **Traffic Conditions** | Current congestion levels and travel times. | Per segment; every `T_traffic` | Environment Agent, Microhub Agent, Driver Agent |
| **Disruptions** | Accidents, road closures, weather events. | Per event | Environment Agent, Microhub Agent, Driver Agent |
| **Driver Location** | Current position of a driver (node or staging point). | On change or every `T_status` | Microhub Agent, Driver Agent |
| **Driver Availability** | Idle, provisional, committed, busy, offline. | On change or every `T_status` | Microhub Agent, Driver Agent |
| **Driver Shift Remaining** | Remaining shift time for each driver. | On change or every `T_status` | Microhub Agent, Driver Agent |
| **Rider Demand** | Number of pending requests at a hub. | On change | Microhub Agent |
| **Pool Composition** | Anonymised count of riders in a pool; identities hidden until match. | Per pool proposal | Rider Agent, Microhub Agent |
| **Driver ETA** | Estimated time until pickup. | On match or on traffic update | Rider Agent, Microhub Agent |
| **Fare Estimate** | Estimated fare before accepting a pool proposal. | Per pool proposal | Rider Agent |
| **Subsidy Eligibility Status** | Whether a rider qualifies for a subsidy and the amount. | Per request | Rider Agent, Regulator Agent |
| **Fare Caps** | Maximum allowed fare per zone. | Per policy update | Regulator Agent, Microhub Agent |
| **Equity Zones** | Zones requiring minimum service levels. | Per policy update | Regulator Agent, Microhub Agent |
| **Reputation Scores** | Trust scores of riders and drivers (bounded bands). | On query or update | Reputation Agent, Microhub Agent, Driver Agent |

### 4.1 Observability Boundaries

- **Riders** observe only their own request, their pool proposal, their fare estimate, their subsidy status, and their matched driver's ETA. They do **not** observe other riders' identities (privacy — GC-15, SEC-05).
- **Drivers** observe the CFP they are bidding on, their own status, and traffic on their route. They do not observe other drivers' bids or other hubs' auctions.
- **Microhubs** observe demand at their own node, driver status broadcasts, and traffic updates. They do **not** observe other hubs' auctions; inter-hub coordination is limited to `InterHubCoordinationProtocol` messages.
- **Regulator** observes aggregate metrics only, via `AuditProtocol`; it does not observe individual trip details unless required for dispute resolution.
- **Environment Agent** observes the full road network and traffic state; it does not observe agent decisions.

---

## 5. Environmental Actions (How Agents Modify the World)

Actions are the ways in which agents interact with the environment. They change the state of entities or consume resources. Actions are separated into **agent actions** and **environment transitions**.

### 5.1 Agent Actions

| Action | Description | Performed By | Effect |
| :--- | :--- | :--- | :--- |
| **Walk to Hub** | Rider physically moves to a microhub. | Rider Agent | Advances rider position; consumes time. |
| **Board Vehicle** | Rider enters the vehicle at pickup. | Rider Agent | Changes rider state to `on_board`. |
| **Alight Vehicle** | Rider exits the vehicle at drop-off. | Rider Agent | Changes rider state to `arrived`. |
| **Move** | Driver traverses a road segment. | Driver Agent | Updates driver location; consumes time and (optionally) fuel. |
| **Pick Up** | Driver collects a rider at a microhub. | Driver Agent | Increments `current_passengers`; triggers `Board Vehicle`. |
| **Drop Off** | Driver delivers a rider to their home cluster. | Driver Agent | Decrements `current_passengers`; triggers `Alight Vehicle`. |
| **Idle** | Driver waits without moving. | Driver Agent | Consumes time; no location change. |
| **Go Online / Offline** | Driver starts or ends a shift. | Driver Agent | Changes availability state. |
| **Reposition** | Driver moves to a staging point. | Driver Agent | Updates location; may consume fuel. |
| **Escrow** | Rider places fare in a holding account. | Rider Agent, Payment Agent | Locks funds; prevents double-spending. |
| **Settle** | Payment Agent releases funds. | Payment Agent | Transfers money to driver; disburses subsidies. |
| **Refund** | Payment Agent returns funds to rider. | Payment Agent | Releases escrow; reverses transaction. |
| **Set Policy** | Regulator sets fare caps, equity zones, subsidies. | Regulator Agent | Modifies environmental policy state. |
| **Allocate Staging Point** | Microhub designates a staging point as active. | Microhub Agent | Modifies environmental staging state. |
| **Instantiate Virtual Hub** | Microhub creates a temporary pickup point. | Microhub Agent | Adds a node to the active graph. |

### 5.2 Environment Transitions

| Transition | Description | Triggered By | Effect |
| :--- | :--- | :--- | :--- |
| **Traffic Update** | Travel times change on road segments. | Environment Agent | Alters edge weights in `W`. |
| **Disruption Event** | Accidents, closures, weather events occur. | Environment Agent | Alters edge weights or removes edges temporarily. |
| **Clock Advance** | Simulation time advances. | Event queue | Updates global clock. |
| **Demand Arrival** | New rider requests appear. | Simulation harness | Adds requests to microhub queues. |

> **Note:** The Environment Agent *triggers* environment transitions; the environment itself remains passive.

---

## 6. Environment-Agent Boundary

It is important to distinguish between the **environment** (passive) and the **Environment Agent** (active).

| Aspect | Environment | Environment Agent |
| :--- | :--- | :--- |
| **Nature** | Passive context. | Active software agent. |
| **Role** | Provides the stage for interactions. | Simulates and broadcasts changes to the stage. |
| **Example** | A road segment with a fixed travel time. | The agent that increases travel time due to simulated traffic. |
| **Example** | A microhub node at coordinates (x, y). | The Microhub Agent that manages auctions at that node. |
| **Example** | A disruption event in the world. | The agent that generates and broadcasts `DisruptionProtocol` messages. |

> **Note:** Staging Points are modelled as **passive entities** managed by microhubs (and updated by the Environment Agent), not as active `StagingPointAgent`s. The optional active-agent variant is documented in the Role Model for extensibility but is not part of the MVP.

---

## 7. Environmental Constraints

These are physical or logical constraints imposed by the environment that agents cannot violate.

| Constraint | Description |
| :--- | :--- |
| **Spatial Continuity** | A driver cannot move from node A to node C without traversing a road segment (or path) connecting them. |
| **Capacity Limit** | A vehicle cannot hold more passengers than its `max_capacity`. |
| **Time Monotonicity** | Time only moves forward; trips cannot be scheduled in the past. |
| **Financial Conservation** | Money cannot be created or destroyed, only transferred (except subsidies from the Regulator). |
| **Road Network Integrity** | Travel time updates must be consistent with the current road network topology. |
| **Edge Weight Positivity** | All travel times and distances are strictly positive. |
| **Graph Connectivity** | Every microhub node is reachable from every other microhub node. |

---

## 8. SPADE Implementation Mapping

| Environmental aspect   | SPADE implementation                                                                                                                                                                               |
| :--------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Microhub graph**     | `networkx.DiGraph` (or equivalent) loaded at startup from a configuration file.                                                                                                                    |
| **Travel time matrix** | `numpy.ndarray` maintained by `EnvironmentAgent`, updated on `TrafficUpdateProtocol`.                                                                                                              |
| **Driver locations**   | Shared state in `EnvironmentAgent` or in a `Directory Facilitator` (DF) attribute.                                                                                                                 |
| **Escrow accounts**    | In-memory `dict` in `PaymentAgent`, optionally persisted to disk for recovery.                                                                                                                     |
| **Wallets**            | In-memory `dict` in `PaymentAgent`.                                                                                                                                                                |
| **Reputation scores**  | In-memory `dict` in `ReputationAgent`.                                                                                                                                                             |
| **Simulation clock**   | Owned by the **Simulation Harness** (e.g., a `simpy.Environment` or a custom event loop). The Harness is the **clock master**: it advances time, schedules events, and starts/stops agents.        |
| **Home clusters**      | Precomputed mapping from home IDs to nearest node, stored in a configuration file.                                                                                                                 |
| **Virtual hubs**       | Dynamically added to `networkx.DiGraph` by `MicrohubAgent`; removed on expiry.                                                                                                                     |
| **Staging points**     | Attributes on `networkx` nodes; managed locally by microhubs.                                                                                                                                      |
| **Environment Agent**  | A `spade.agent.Agent` subclass that observes the clock and **broadcasts** `TrafficUpdateProtocol` and `DisruptionProtocol` messages. It is a **clock observer/broadcaster**, not the clock itself. |
**Simulation Harness vs. Environment Agent.** These are two distinct components and must not be conflated:

- The **Simulation Harness** is the execution driver. It owns the event queue, the global simulation clock, and the orchestration of agent startup and shutdown. In a `simpy`-based setup, the Harness is the `simpy.Environment` instance and the surrounding orchestration code — it is **not** a SPADE agent.
    
- The **Environment Agent** is a regular SPADE agent that runs _inside_ the simulation. It reacts to clock ticks, evaluates environmental state, and emits traffic and disruption updates via FIPA-ACL messages. It does not control time.
    
- **Failure mode to avoid:** embedding the `simpy` event loop inside the Environment Agent. This makes the Environment Agent both the clock master and a message broadcaster, breaks the separation of concerns, and makes it impossible to pause or resume the simulation from outside. Keep the Harness external.
    
- **Rule of thumb:** if a component schedules _when_ other agents run, it belongs in the Harness. If a component sends _messages_ that agents react to, it is an agent.
---

## 9. Summary Checklist

- [x] Environmental properties defined (discrete, dynamic, partially observable)
- [x] Entities defined (Microhubs, Homes, Roads, Staging Points, Vehicles, Virtual Hubs)
- [x] Road network formalised (directed weighted graph, detour formula)
- [x] Hub lifecycle defined (fixed physical, dynamic virtual)
- [x] Resources defined (Seats, Money, Subsidies, Reputation, Fuel, Time)
- [x] Time model defined (event-driven, global clock, tolerance)
- [x] Resource allocation rules defined
- [x] Percepts defined (traffic, disruptions, locations, demand, ETA, fares, subsidies, reputation)
- [x] Observability boundaries defined
- [x] Actions defined (agent actions vs. environment transitions)
- [x] Environment-Agent boundary clarified
- [x] Environmental constraints documented
- [x] SPADE implementation mapping provided

> **Note:** This document completes the **Environmental Model** component of the Gaia Analysis Phase. Together with the **Preliminary Role Model**, **Preliminary Interaction Model**, and **Organizational Rules**, the Analysis Phase is now fully complete.

---

## 10. Revision Summary (Changes from Preliminary Version)

| Change | Rationale |
| :--- | :--- |
| Added Section 1.1: Environmental Properties | Gaia expects discrete/dynamic/observability characterisation. |
| Added Section 2.1: Road Network Formalisation | Graph, weights, home assignment, and detour formula were implicit but essential (GC-06). |
| Added Section 2.2: Hub Lifecycle | Physical vs. virtual hubs, failure handling, coverage. |
| Added Virtual Hub entity | Theme mentioned virtual hubs; now formally modelled. |
| Added Section 3.1: Time Model | Time was listed as a resource but never formalised; needed for all timeouts. |
| Added Section 3.2: Resource Allocation Rules | Resources were listed but not managed. |
| Added missing percepts (pool composition, ETA, fare estimate, subsidy status, shift remaining) | Needed for rider and driver decision-making. |
| Added Section 4.1: Observability Boundaries | Connected to privacy rules (GC-15, SEC-05). |
| Split actions into agent actions and environment transitions | Environment is passive; the previous version conflated actions and transitions. |
| Added missing actions (walk, board, alight, idle, online/offline, set policy, allocate staging, instantiate virtual hub) | Reflected real interactions in the system. |
| Rewrote Section 6 for consistency with Section 1 | Section 1 previously called the Environment Agent "not an agent itself," contradicting Section 6. |
| Added Section 8: SPADE Implementation Mapping | Eased transition from Analysis to Implementation. |
| Added graph positivity and connectivity constraints | Formal graph properties. |
| Reframed Time as an environmental dimension, not a resource | Cleaner modelling. |
| Merged Vehicle + Seats conceptual overlap | Seats are derived from vehicle capacity, not independent. |
| Marked Fuel/Battery as optional for MVP | Avoided committing to a feature without justification. |
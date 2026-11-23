---
tags:
  - MAS
  - Gaia
  - SystemDecomposition
  - SPADE
  - Microtransit
status: Analysis Phase - Pre-requisite
---

# Gaia System Decomposition: Microtransit MAS

Before defining roles and interactions, the system is subdivided into logical sub-organizations. Each sub-organization is a cohesive group of **roles** working towards a shared high-level goal. 

> **Note on Roles vs. Agents:** In the Gaia Analysis Phase, we define **Roles**. Agents are only introduced in the Detailed Design Phase (Agent Model). A single logical agent (e.g., `DriverAgent`) will play multiple roles across different sub-organizations.

---

## 1. Sub-Organization Directory

### Sub-Organization 1: Transit Operations
- **Rationale (Cohesion):** All roles participating in the physical act of matching, pooling, and moving a rider from origin to destination.
- **Roles:** Rider (Role), Driver (Role), Microhub (Role).
- **Core Protocols:** `TripRequestProtocol`, `PoolFormationProtocol`, `MatchAuctionProtocol`, `TripExecutionProtocol`, `RepositionProtocol`, `DriverStatusProtocol`, `ReNegotiationProtocol`, `DriverFailureProtocol`, `CancellationProtocol`.
- **Key Rules:** GC-01, GC-04, GC-05, GC-06, GC-11, GC-13, CC-01 to CC-09.

### Sub-Organization 2: Financial Settlement
- **Rationale (Cohesion):** All roles involved in moving money in response to a trip, ensuring financial conservation and payment finality.
- **Roles:** Rider (Role), Driver (Role), Payment (Role).
- **Core Protocols:** `EscrowProtocol`, `SettlementProtocol`, `RefundProtocol`.
- **Key Rules:** GC-02, GC-03, RI-01, RI-02, RI-06.

### Sub-Organization 3a: Regulation (Prescriptive)
- **Rationale (Cohesion):** Sets rules, caps, and equity standards. It is *prescriptive* — it dictates what agents *must* do.
- **Roles:** Regulator (Role).
- **Core Protocols:** `PolicyProtocol`, `AuditProtocol`.
- **Key Rules:** RI-01 to RI-07, GC-08.

### Sub-Organization 3b: Trust and Reputation (Descriptive)
- **Rationale (Cohesion):** Records observed behaviour and computes trust scores. It is *descriptive* — it records what agents *actually* do.
- **Roles:** Reputation (Role).
- **Core Protocols:** `ReputationProtocol`.
- **Key Rules:** GC-07, ID-07.

### Sub-Organization 4: Environmental Simulation
- **Rationale (Cohesion):** Simulates the physical world (traffic, disruptions) and acts as the time synchronisation master for the road network.
- **Roles:** Environment (Role).
- **Core Protocols:** `TrafficUpdateProtocol`, `DisruptionProtocol`.
- **Key Rules:** GC-12, TS-01 to TS-03.
- **Managed Entities:** Staging Points (passive), Virtual Hubs (lifecycle managed here, but instantiated by Microhubs).

### Sub-Organization 5: Inter-Hub Coordination
- **Rationale (Cohesion):** Coordinates independent microhubs for system resilience and equity compliance.
- **Roles:** Microhub (Role).
- **Core Protocols:** `InterHubCoordinationProtocol`.
- **Key Rules:** FE-07, RI-03.
- **Sub-components:**
  - **Mandatory:** Overflow absorption (FE-07) and Failure recovery (RI-03).
  - **Optional:** Proactive demand forecasting (DemandForecastProtocol).

### Sub-Organization 6: System Infrastructure & Governance (Cross-Cutting)
- **Rationale (Cohesion):** Cross-cutting concerns that apply to all agents but are not domain-specific (Security, Logging, Simulation Time).
- **Roles:** All roles (participate implicitly).
- **Components:**
  - **Simulation Harness (Non-Agent):** The global clock master and event loop (e.g., `simpy`). It is external to the MAS.
  - **Security & Identity:** Handled via Directory Facilitator (DF) registration. (SEC-01 to SEC-05).
  - **Logging & Evaluation:** Handled by the Harness and audited by the Regulator. (LOG-01 to LOG-04).

---

## 2. Role Participation Across Sub-Organizations

Sub-organizations are **functional groupings**, not partitions. A single logical actor (e.g., a human rider) will play different roles in different sub-organizations:
- A **Rider** plays the `Rider Role` in Transit Operations (requesting trips, boarding vehicles) and the `Payer Role` in Financial Settlement (placing escrow).
- A **Driver** plays the `Driver Role` in Transit Operations (bidding, moving) and the `Payee Role` in Financial Settlement (receiving payouts).

---

## 3. Inter-Sub-Organization Interactions

Sub-organizations must interact to achieve the system's goals. The matrix below shows the primary dependencies.

| From / To | Transit Ops | Financial Settle. | Regulation | Trust | Environment | Inter-Hub |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Transit Ops** | - | Triggers settlement (via `trip_completed`) | Reports metrics | Requests ratings | Consumes traffic data | Requests overflow |
| **Financial Settle.** | Confirms payment (`escrow_confirmed`) | - | Reports subsidies | Confirms settlement (ID-07) | - | - |
| **Regulation** | Enforces caps/policies | Sets subsidy rules | - | Audits scores | - | Sets equity targets |
| **Trust** | Provides reputation for auctions | - | Provides audit data | - | - | - |
| **Environment** | Provides traffic/ETAs | - | - | - | - | - |
| **Inter-Hub** | Absorbs overflow/failures | - | Reports equity compliance | - | - | - |

---

## 4. Cross-Reference Matrix (Roles, Protocols, Rules)

| Role | Sub-Organization(s) | Key Protocols | Key Rules |
| :--- | :--- | :--- | :--- |
| **Rider** | Transit Ops, Financial Settlement | `TripRequest`, `PoolFormation`, `Escrow` | GC-05, GC-06, GC-10, SEC-05 |
| **Driver** | Transit Ops, Financial Settlement | `MatchAuction`, `TripExecution`, `Settlement` | GC-01, GC-04, GC-16, CC-01 |
| **Microhub** | Transit Ops, Inter-Hub | `MatchAuction`, `PoolFormation`, `InterHubCoordination` | GC-08, GC-09, FE-01, CC-08 |
| **Payment** | Financial Settlement | `Escrow`, `Settlement`, `Refund` | GC-02, GC-03, RI-06 |
| **Regulator** | Regulation | `Policy`, `Audit` | RI-01 to RI-07, RC-01 |
| **Reputation** | Trust | `Reputation` | GC-07, ID-07 |
| **Environment** | Environmental Simulation | `TrafficUpdate`, `Disruption` | GC-12, TS-01 |

---

## 5. Ontology and Data Contracts

To ensure semantic interoperability between roles (and later, SPADE agents), a shared ontology is defined. Full OWL/RDF is not required; the ontology will be implemented via **Python dataclasses** serialized as **JSON** within FIPA-ACL message content.

### 5.1 Implementation Details
- **Location:** `common/ontology.py`
- **Serialization:** JSON (via `dataclasses.asdict()`)
- **Naming Convention:** PascalCase for dataclasses (e.g., `TripRequest`), snake_case for fields (e.g., `detour_tolerance`).

### 5.2 Domain Concepts (Entities)
- `MicrohubNode`: ID, coverage_radius, capacity.
- `TripRequest`: request_id, origin, destination, time_window, detour_tolerance, max_fare.
- `Policy`: fare_caps, equity_zones, subsidy_rules.

### 5.3 Interaction Concepts (Messages)
- `CFP`: pool_id, constraints, auction_deadline.
- `Bid`: bid_id, price, eta, max_detour, provisional.
- `PoolProposal`: pool_id, shared_route, split_fare, estimated_wait.
- `Escrow`: escrow_id, amount, status.
- `MatchResult`: driver_id, final_fare, pickup_eta.

---

> **Note:** This document completes the **Subdivide System into Sub-organizations** step of the Gaia Analysis Phase. Together with the Environmental Model, Preliminary Role Model, Preliminary Interaction Model, and Organizational Rules, the Analysis Phase is now fully complete.
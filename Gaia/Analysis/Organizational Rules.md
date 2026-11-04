---
tags:
  - MAS
  - Gaia
  - OrganizationalRules
  - SPADE
  - Microtransit
status: Analysis Phase - Complete
---

# Gaia Organizational Rules: Microtransit MAS

This document defines the **Organizational Rules** for the decentralised last-mile microtransit MAS. According to the Gaia methodology, these rules represent the global constraints, dependencies, and invariants that govern the entire system, independent of any single agent's internal behaviour.

These rules must be respected by all agents and will translate directly into assertions, guards, and validation logic in the SPADE implementation.

> **Revision note:** This version resolves redundancy between Global Constraints and Inter-Role Dependencies, adds missing constraints for theme elements (traffic response, repositioning, pool bounds, privacy), expands regulatory and concurrency rules, extends failure handling to system-level failures, and adds sections on Security, Time Synchronisation, Logging, and Rule Conflict Resolution. A Parameter Catalogue has been added for implementation reference.

---

## 1. Organizational Structure

- **Topology:** Decentralised / Federated. There is no central dispatcher. Each Microhub Agent is autonomous and manages its own local market.
- **Hierarchy (rule precedence only):** Regulator Agent > Microhub Agent > Driver Agent. This hierarchy governs rule precedence, not control flow.
- **Agent Discovery:** Agents discover each other via a Directory Facilitator (DF) or a pre-defined graph topology (microhub nodes are known a priori). Drivers register with all microhubs whose coverage area intersects their operating region.
- **Autonomy vs. Coordination:** Microhubs are fully autonomous but may voluntarily coordinate via the `InterHubCoordinationProtocol` for overflow and demand forecasting. Coordination is best-effort, not mandatory.
- **Driver Mobility:** Drivers are not bound to a single microhub. They may serve any hub whose coverage area they are within, subject to concurrency rules (Section 6).
- **Staging Points:** Modelled as passive environmental objects managed locally by microhubs, unless explicitly instantiated as active agents.

---

## 2. Parameter Catalogue

All parameters referenced in this document are defined here. Parameters are either **Global** (fixed system-wide), **Regulated** (set by Regulator Agent), or **Local** (set per Microhub Agent).

| Parameter            | Meaning                                                                                                                | Default        | Scope     |
| :------------------- | :--------------------------------------------------------------------------------------------------------------------- | :------------- | :-------- |
| `T_pool`             | Batching window before pool formation                                                                                  | 180 s          | Local     |
| `T_auction`          | Auction bidding deadline                                                                                               | 30 s           | Local     |
| `T_award`            | Time to award after bid received                                                                                       | 10 s           | Local     |
| `T_provisional`      | Provisional lock expiry                                                                                                | 60 s           | Global    |
| `T_escrow`           | Escrow confirmation timeout                                                                                            | 15 s           | Global    |
| `T_no_show`          | Rider no-show threshold                                                                                                | 120 s          | Regulated |
| `T_reposition`       | Repositioning response timeout                                                                                         | 30 s           | Local     |
| `T_status`           | Driver status broadcast interval                                                                                       | 30 s           | Global    |
| `T_traffic`          | Traffic update interval                                                                                                | 60 s           | Global    |
| `T_hub`              | Inter-hub coordination interval                                                                                        | 300 s          | Global    |
| `T_audit`            | Regulator audit interval                                                                                               | 3600 s         | Regulated |
| `T_settlement_max`   | Maximum queued settlement delay. Also triggers reputation fallback publication with `pending_settlement` flag (ID-07). | 600 s          | Regulated |
| `T_failover`         | Microhub failover absorption time                                                                                      | 300 s          | Global    |
| `T_dispute`          | Dispute escalation deadline                                                                                            | 86400 s        | Regulated |
| `T_subsidy`          | Subsidy disbursement deadline                                                                                          | 300 s          | Regulated |
| `T_report`           | Equity zone reporting interval                                                                                         | 3600 s         | Regulated |
| `T_equity_window`    | Rolling window for equity evaluation                                                                                   | 86400 s        | Regulated |
| `T_traffic_response` | ETA recalculation deadline after traffic update                                                                        | 10 s           | Global    |
| `T_clock`            | Clock synchronisation tolerance                                                                                        | 1 s            | Global    |
| `min_pool_size`      | Minimum riders for a pool                                                                                              | 2              | Local     |
| `max_pool_size`      | Maximum riders for a pool                                                                                              | `max_capacity` | Local     |
| `max_wait_time`      | Maximum rider wait from request to pickup                                                                              | 900 s          | Regulated |
| `max_shift_duration` | Maximum driver shift in rolling window                                                                                 | 8 h            | Regulated |
| `max_no_shows`       | No-shows before temporary suspension                                                                                   | 3              | Regulated |
| `min_service_rate`   | Minimum equity zone service rate                                                                                       | 0.8            | Regulated |
| `T_overflow`         | Deadline to request overflow support                                                                                   | 300 s          | Regulated |

---

## 3. Global Constraints (System-Wide Invariants)

These constraints must **never** be violated by any agent in the system. They are expressed as **state invariants** (conditions that must always hold), not as sequencing rules (which appear in Section 4).

| ID        | Constraint                        | Description                                                                                                                                                                                                                                     |
| :-------- | :-------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GC-01** | **Vehicle Capacity**              | At all times, `current_passengers <= max_capacity` for every Driver Agent.                                                                                                                                                                      |
| **GC-02** | **Escrow-Before-Trip**            | No trip may be in `IN_PROGRESS` state unless a valid escrow hold exists for the rider's fare.                                                                                                                                                   |
| **GC-03** | **Settlement Finality**           | Funds may only be released to a driver after `trip_completed` is confirmed by the Microhub Agent.                                                                                                                                               |
| **GC-04** | **Shift Compliance**              | No driver may accept a trip that exceeds their remaining shift time.                                                                                                                                                                            |
| **GC-05** | **Time Window Compliance**        | No rider may be assigned to a trip that violates their declared time window.                                                                                                                                                                    |
| **GC-06** | **Detour Tolerance**              | No rider may be pooled into a route that exceeds their declared detour tolerance.                                                                                                                                                               |
| **GC-07** | **Reputation Integrity**          | No agent may self-rate. Reputation scores must be bounded and tamper-evident.                                                                                                                                                                   |
| **GC-08** | **Non-Exclusion**                 | Microhubs must not systematically exclude underserved zones from auctions.                                                                                                                                                                      |
| **GC-09** | **Auction Notification**          | Every bidder in an auction must eventually receive `accept-proposal` or `reject-proposal`.                                                                                                                                                      |
| **GC-10** | **Maximum Wait Time**             | No rider may wait more than `max_wait_time` from request submission to pickup, unless they explicitly extend it.                                                                                                                                |
| **GC-11** | **Pool Size Bounds**              | Every pool must satisfy `min_pool_size <= pool_size <= max_pool_size`.                                                                                                                                                                          |
| **GC-12** | **Traffic Update Responsiveness** | All agents must recalculate ETAs within `T_traffic_response` of receiving a `TrafficUpdateProtocol` message.                                                                                                                                    |
| **GC-13** | **Repositioning Safety**          | A driver must not reposition while assigned to an active trip or holding a provisional lock.                                                                                                                                                    |
| **GC-14** | **Driver State Consistency**      | A driver's state (idle, provisional, committed, busy) must be consistent across all hubs that receive `DriverStatusProtocol` updates.                                                                                                           |
| **GC-15** | **Rider Privacy**                 | Rider home locations must not be shared with drivers until a match is confirmed.                                                                                                                                                                |
| **GC-16** | **Total Shift Duration**          | No driver may exceed `max_shift_duration` in a rolling window.                                                                                                                                                                                  |
| **GC-17** | **Award-Lock Timing**             | For every auction, `T_award < T_provisional`, with sufficient margin to account for network latency `δ` between bid submission and receipt. Formally: `T_provisional > T_award + δ_max`, where `δ_max` is the maximum expected message delay.\| |

---

## 4. Inter-Role Dependencies

These rules define the required **sequence of interactions** between roles. They ensure that no role acts on incomplete or invalid information.

### 4.1. Trip Initiation Dependencies

| ID | Dependency | Rule |
| :--- | :--- | :--- |
| **ID-01** | Rider → Microhub → Payment | A Microhub Agent must not accept a trip request (`agree`) unless the Rider Agent has sufficient funds or valid subsidy eligibility. |
| **ID-02** | Rider → Payment → Microhub | A Microhub Agent must not initiate a `MatchAuctionProtocol` until it receives `escrow_confirmed` from the Payment Agent. |
| **ID-09** | Payment → Microhub | If `escrow_confirmed` is not received within `T_escrow`, the Microhub must refuse the rider's request and inform them. |

### 4.2. Auction and Assignment Dependencies

| ID | Dependency | Rule |
| :--- | :--- | :--- |
| **ID-03** | Microhub → Driver | A Microhub Agent must not send `accept-proposal` to a driver unless the driver has submitted a valid `propose` within the auction deadline. |
| **ID-04** | Driver → Microhub | A Driver Agent must not submit a `propose` if it already holds a firm commitment or an active provisional lock. |
| **ID-05** | Microhub → Rider | A Microhub Agent must not inform a Rider Agent of a successful match unless the Driver Agent has confirmed the assignment. |
| **ID-10** | Microhub → Rider | If `MatchAuctionProtocol` fails (no valid bids or timeout), the Microhub must requeue the pool and inform riders before retrying. |

### 4.3. Post-Trip Dependencies

| ID        | Dependency             | Rule                                                                                                                                                                                                                                                                                                                                                                              |
| :-------- | :--------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **ID-06** | Microhub → Payment     | The Payment Agent must not settle a trip until it receives `trip_completed` from the Microhub Agent.                                                                                                                                                                                                                                                                              |
| **ID-07** | Payment → Reputation   | The Reputation Agent may _accept_ ratings before settlement, but must not _publish_ them until either (a) settlement is confirmed by the Payment Agent, or (b) `T_settlement_max` has elapsed since trip completion, in which case the rating is published with a `pending_settlement` flag. Once settlement is eventually confirmed, the flag is removed in a subsequent update. |
| **ID-08** | Driver → Reputation    | A Driver Agent must not rate a Rider Agent until the trip is marked as completed or cancelled by the Microhub Agent.                                                                                                                                                                                                                                                              |
| **ID-11** | Cancellation → Payment | On cancellation, the Microhub must trigger `RefundProtocol` before notifying the rider of cancellation confirmation.                                                                                                                                                                                                                                                              |

---

## 5. Regulatory Invariants

These rules are imposed by the Regulator Agent and override any local Microhub auction rules.

| ID | Invariant | Rule |
| :--- | :--- | :--- |
| **RI-01** | **Fare Cap Supremacy** | No Microhub Agent may finalise a fare that exceeds the current fare cap. Bids exceeding the fare cap must be rejected during evaluation. |
| **RI-02** | **Subsidy Consistency** | Subsidy rules must be applied consistently across all zones unless the Regulator explicitly defines differentiated zones. The Regulator must audit subsidy application via `AuditProtocol` at least every `T_audit`. |
| **RI-03** | **Minimum Service Levels** | Minimum service levels are evaluated over a rolling `T_equity_window`. If a hub's service rate in an equity zone falls below `min_service_rate`, it must request overflow support via `InterHubCoordinationProtocol` within `T_overflow`. |
| **RI-04** | **Policy Acknowledgement** | All Microhub and Driver Agents must acknowledge `PolicyProtocol` updates via `agree` or `refuse`. |
| **RI-05** | **Audit Cooperation** | Microhubs must respond to `AuditProtocol` requests with valid logs. Refusal to cooperate results in a penalty. |
| **RI-06** | **Subsidy Disbursement Timing** | Subsidies must be disbursed within `T_subsidy` of trip settlement. |
| **RI-07** | **Equity Zone Reporting** | Microhubs must report service metrics for equity zones to the Regulator every `T_report`. |

---

## 6. Concurrency and Double-Booking Prevention

Because microhubs operate independently and drivers may receive CFPs from multiple hubs simultaneously, the system enforces a **Driver-Side Provisional Lock** mechanism.

| ID        | Rule                        | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| :-------- | :-------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **CC-01** | **Single Provisional Lock** | A Driver Agent may hold at most one provisional commitment at a time.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| **CC-02** | **Provisional Expiry**      | A provisional commitment expires after `T_provisional` if not confirmed by `accept-proposal`.                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| **CC-03** | **Firm Commitment**         | On receiving `accept-proposal`, the provisional lock becomes a firm commitment. The driver must reject all other CFPs.                                                                                                                                                                                                                                                                                                                                                                                                        |
| **CC-04** | **Lock Release**            | On receiving `reject-proposal` or timeout, the driver releases the provisional lock and may bid on other CFPs.                                                                                                                                                                                                                                                                                                                                                                                                                |
| **CC-05** | **Award Deadline**          | Microhubs must send `accept-proposal` or `reject-proposal` within `T_award` of receiving a bid.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **CC-06** | **No Double-Booking**       | No Driver Agent may be assigned to two conflicting trips at the same time.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| **CC-07** | **Lock Status Broadcast**   | Drivers must broadcast provisional lock status via `DriverStatusProtocol` immediately upon acquiring or releasing a lock.                                                                                                                                                                                                                                                                                                                                                                                                     |
| **CC-08** | **Pre-CFP Status Check**    | Microhubs must check driver status before sending a CFP and must not send CFPs to drivers with an active lock.                                                                                                                                                                                                                                                                                                                                                                                                                |
| **CC-09** | **Locked Driver Response**  | If a driver receives a CFP while holding a lock, it must respond with `refuse(reason: locked)`.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **CC-10** | **ETA Staleness Handling**  | During an auction, if a `TrafficUpdateProtocol` message invalidates a driver's bid ETA, the Microhub Agent must recalculate the ETA using the updated travel time matrix from the driver's last known location to the pickup point. The recalculated ETA is authoritative for bid evaluation. Drivers are not required to re-bid, but may voluntarily submit a revised `propose` before the auction deadline. If the recalculated ETA violates the rider's time window or exceeds `max_wait_time`, the bid must be discarded. |


---

## 7. Failure and Exception Handling Rules

These rules define the system's behaviour when things go wrong.

### 7.1. Trip-Level Failures

| ID | Rule | Description |
| :--- | :--- | :--- |
| **FE-01** | **Auction Timeout** | If no valid bids are received by `auction_deadline`, the Microhub must inform riders of failure and requeue the pool. |
| **FE-02** | **Pool Dissolution** | If fewer than `min_pool_size` riders accept a pool proposal, the Microhub may dissolve the pool and requeue all riders. |
| **FE-03** | **Driver Failure** | If a Driver Agent reports a breakdown mid-trip, the Microhub must trigger `RefundProtocol` and re-auction the trip with remaining riders. |
| **FE-04** | **Re-Negotiation Failure** | If re-negotiation after a disruption fails (either driver or rider rejects), the trip is cancelled and refunded. |
| **FE-05** | **Rider No-Show** | If a rider does not show up within `T_no_show` of the driver's arrival, the driver may report a no-show and the Microhub triggers `CancellationProtocol`. |
| **FE-06** | **Driver Cancellation Penalty** | If a driver cancels after assignment, a penalty is applied via `ReputationProtocol` and the rider receives a full refund. |

### 7.2. System-Level Failures

| ID | Rule | Description |
| :--- | :--- | :--- |
| **FE-07** | **Microhub Failure** | If a Microhub Agent fails, neighbouring hubs must absorb its pending requests via `InterHubCoordinationProtocol` within `T_failover`. |
| **FE-08** | **Payment Agent Failure** | If the Payment Agent is unavailable, trips may proceed but settlement must be queued and completed within `T_settlement_max`. |
| **FE-09** | **Regulator Agent Failure** | If the Regulator Agent is unavailable, the last known policy remains in force until an update is received. |
| **FE-10** | **Network Partition** | If a driver loses contact with a microhub mid-trip, the driver must complete the trip and report completion upon reconnection. |
| **FE-11** | **Dispute Resolution** | Rider-driver disputes must be escalated to the Regulator Agent within `T_dispute`. |
| **FE-12** | **Repeated No-Shows** | Riders with more than `max_no_shows` in a rolling window may be temporarily suspended. |

---

## 8. Security Rules

| ID | Rule | Description |
| :--- | :--- | :--- |
| **SEC-01** | **Authentication** | All agents must authenticate via DF registration before participating in any protocol. |
| **SEC-02** | **Message Traceability** | All messages must be signed or traceable to a registered agent identity. |
| **SEC-03** | **Replay Prevention** | Messages must include nonces or timestamps to prevent replay attacks. |
| **SEC-04** | **Escrow Integrity** | Escrow records must be immutable and auditable. |
| **SEC-05** | **Privacy of Home Locations** | Rider home coordinates must be encrypted in transit and never included in CFPs. |

---

## 9. Time Synchronisation Rules

| ID | Rule | Description |
| :--- | :--- | :--- |
| **TS-01** | **Clock Tolerance** | All agents must synchronise to a common time source with tolerance `T_clock`. |
| **TS-02** | **Authoritative Auction Clock** | Deadlines are evaluated against the microhub's clock, which is authoritative for its auctions. |
| **TS-03** | **Timeout Fairness** | Timeouts must be measured from the message send time on the initiator's clock. |

---

## 10. Logging and Evaluation Rules

| ID | Rule | Description |
| :--- | :--- | :--- |
| **LOG-01** | **Auction Logging** | Every auction must log bids, winner, outcome, and timing. |
| **LOG-02** | **Trip Logging** | Every trip must log wait time, detour, occupancy, fare, and equity zone flags. |
| **LOG-03** | **Audit Availability** | Logs must be available to the Regulator via `AuditProtocol`. |
| **LOG-04** | **Metric Retention** | Logs must be retained for at least `T_equity_window` for equity evaluation. |

---

## 11. Rule Conflict Resolution

| ID | Rule | Description |
| :--- | :--- | :--- |
| **RC-01** | **Regulatory Precedence** | Regulatory invariants override global constraints. |
| **RC-02** | **Global Precedence** | Global constraints override local microhub rules. |
| **RC-03** | **Safety Precedence** | Safety constraints override liveness constraints. |
| **RC-04** | **Privacy Precedence** | Privacy rules override auction transparency rules where they conflict. |
| **RC-05** | **Escalation** | Unresolvable conflicts must be escalated to the Regulator Agent within `T_dispute`. |

---

## 12. Rule Enforcement Mapping (SPADE Implementation)

When implementing in SPADE, these rules will be enforced at three levels:

| Level | Mechanism | Example |
| :--- | :--- | :--- |
| **Agent Level** | Assertions and guards within `Behaviour` methods. | `assert self.current_passengers <= self.max_capacity` before accepting a trip. |
| **Protocol Level** | FSM state guards and transition conditions. | In `MatchAuctionProtocol` FSM, transition to `Evaluating` state only if `received_bids > 0`. |
| **System Level** | Periodic audit by the Regulator Agent. | `AuditProtocol` checks fare logs against fare caps. |

---

## 13. Theme Coverage Checklist

| Theme Element | Where Covered |
| :--- | :--- |
| Time windows | GC-05 |
| Detour tolerance | GC-06 |
| Driver shifts (remaining) | GC-04 |
| Driver shifts (total) | GC-16 |
| Traffic variability | GC-12, TS rules |
| Pooling (capacity) | GC-01 |
| Pooling (size bounds) | GC-11, FE-02 |
| Pricing schemes | RI-01, RI-02, RI-06 |
| Equity / fairness | GC-08, RI-03, RI-07 |
| Repositioning | GC-13 |
| Decentralisation | Section 1 |
| Auction mechanism | GC-09, CC-05, FE-01 |
| Concurrency | CC-01 to CC-09 |
| Failure handling (trip) | FE-01 to FE-06 |
| Failure handling (system) | FE-07 to FE-12 |
| Privacy | GC-15, SEC-05 |
| Reputation | GC-07, ID-07 |
| Security | SEC-01 to SEC-05 |
| Time synchronisation | TS-01 to TS-03 |
| Logging & evaluation | LOG-01 to LOG-04 |
| Rule conflicts | RC-01 to RC-05 |

---

## 14. Summary Checklist

- [x] Organizational structure and hierarchy defined
- [x] Parameter Catalogue defined
- [x] Global Constraints defined (GC-01 to GC-16)
- [x] Inter-Role Dependencies defined (ID-01 to ID-11)
- [x] Regulatory Invariants defined (RI-01 to RI-07)
- [x] Concurrency Rules defined (CC-01 to CC-09)
- [x] Trip-Level Failure Handling defined (FE-01 to FE-06)
- [x] System-Level Failure Handling defined (FE-07 to FE-12)
- [x] Security Rules defined (SEC-01 to SEC-05)
- [x] Time Synchronisation Rules defined (TS-01 to TS-03)
- [x] Logging and Evaluation Rules defined (LOG-01 to LOG-04)
- [x] Rule Conflict Resolution defined (RC-01 to RC-05)
- [x] Enforcement mechanism mapped to SPADE
- [x] Theme coverage verified

> **Note:** This document completes the **Analysis Phase** of the Gaia methodology. The next step is the **Architectural Design Phase**, where the Role Model and Interaction Model are refined into final blueprints, followed by the **Detailed Design Phase** (Agent Model and Services Model).

---

## 15. Revision Summary (Changes from Preliminary Version)

| Change                                                     | Rationale                                                                                                                        |
| :--------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| Removed redundancy between GC and ID                       | GC now contains pure state invariants; ID contains pure sequencing rules.                                                        |
| Added Parameter Catalogue (Section 2)                      | Parameters were referenced but undefined; critical for implementation.                                                           |
| Added GC-10 to GC-16                                       | Covered wait time, pool bounds, traffic response, repositioning safety, driver state consistency, privacy, total shift duration. |
| Added ID-09 to ID-11                                       | Covered escrow timeout, match failure requeue, cancellation-refund ordering.                                                     |
| Clarified ID-07                                            | Resolved settlement-rating deadlock by distinguishing *acceptance* from *publication*.                                           |
| Operationalised RI-03                                      | Added rolling window, service rate threshold, and overflow deadline.                                                             |
| Added RI-06 and RI-07                                      | Covered subsidy timing and equity reporting.                                                                                     |
| Added CC-07 to CC-09                                       | Addressed cross-hub concurrency awareness.                                                                                       |
| Expanded failure handling into trip-level and system-level | Added FE-07 to FE-12 for microhub, payment, regulator, network, dispute, and no-show failures.                                   |
| Added Security Rules (SEC-01 to SEC-05)                    | Authentication, traceability, replay prevention, escrow integrity, privacy.                                                      |
| Added Time Synchronisation Rules (TS-01 to TS-03)          | Clocks, authoritative auction time, timeout fairness.                                                                            |
| Added Logging Rules (LOG-01 to LOG-04)                     | Auction, trip, audit, retention.                                                                                                 |
| Added Rule Conflict Resolution (RC-01 to RC-05)            | Precedence hierarchy and escalation.                                                                                             |
| Expanded theme coverage checklist                          | Now includes security, time, logging, conflicts.                                                                                 |
| Expanded summary checklist                                 | Reflects all new rule categories.                                                                                                |
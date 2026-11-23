This document defines the Interaction Model for the decentralised last-mile microtransit and ride-sharing MAS. It specifies the protocols, initiators, responders, and message exchange patterns required to achieve the system's goals.

The interaction model uses **FIPA-ACL performatives** (e.g., `cfp`, `propose`, `accept-proposal`, `reject-proposal`, `refuse`, `inform`, `request`, `agree`, `query-if`, `confirm`) to ensure compatibility with SPADE agent communication.

## 1. Protocol Catalogue

|Protocol|Purpose|Mandatory?|
|---|---|---|
|`TripRequestProtocol`|Rider submits trip request to a microhub.|Yes|
|`PoolFormationProtocol`|Microhub aggregates compatible riders into a pool.|Yes|
|`MatchAuctionProtocol`|Microhub runs reverse auction with drivers via CFP.|Yes|
|`RepositionProtocol`|Microhub requests driver repositioning to staging points.|Yes|
|`DriverStatusProtocol`|Driver broadcasts location, capacity, and availability.|Yes|
|`PricingProtocol`|Fare splitting, subsidies, and driver payout rules.|Yes|
|`EscrowProtocol`|Rider places fare in escrow before trip.|Yes|
|`SettlementProtocol`|Post-trip payout and settlement.|Yes|
|`RefundProtocol`|Refunds for failed or cancelled trips.|Yes|
|`CancellationProtocol`|Rider or driver cancels a trip.|Yes|
|`ReputationProtocol`|Rating exchange, reputation query, and updates.|Yes|
|`TrafficUpdateProtocol`|Environment broadcasts traffic and travel times.|Yes|
|`DisruptionProtocol`|Environment broadcasts disruptions.|Yes|
|`ReNegotiationProtocol`|Re-negotiation triggered by disruption or failure.|Yes|
|`PolicyProtocol`|Regulator broadcasts fare caps, subsidies, equity rules.|Yes|
|`AuditProtocol`|Regulator audits microhub compliance.|Yes|
|`InterHubCoordinationProtocol`|Microhubs exchange demand forecasts and overflow.|Yes|
|`DriverFailureProtocol`|Driver reports breakdown/accident mid-trip.|Yes|
|`DemandForecastProtocol`|Staging Point broadcasts demand forecasts.|Optional|
|`TripExecutionProtocol`|Manage physical pickup and drop-off.|Yes|

---

## 2. Core Marketplace Protocols

### 2.1. TripRequestProtocol

**Initiator:** Rider Agent  
**Responder:** Microhub Agent  
**Purpose:** Rider submits a trip request to a local microhub.
```mermaid
sequenceDiagram
    participant R as Rider Agent
    participant M as Microhub Agent

    R->>M: request(trip_details, time_window, detour_tolerance, pooling_willingness, max_fare, subsidy_eligibility)
    alt Valid Request
        M-->>R: agree(request_id)
        M->>R: inform(request_queued, expected_batch_time)
    else Invalid Request (no funds, outside service area, malformed)
        M-->>R: refuse(reason)
    end
```
**Notes:**

- `agree` confirms the microhub will process the request; `inform(request_queued)` gives the rider a batching estimate.
    
- Invalid requests include insufficient funds, no valid subsidy, or origin outside hub coverage.

### 2.2. PoolFormationProtocol

**Initiator:** Microhub Agent  
**Responder:** Rider Agent  
**Purpose:** Microhub aggregates compatible riders and proposes a pool.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant R1 as Rider Agent 1
    participant R2 as Rider Agent 2
    participant R3 as Rider Agent 3

    M->>R1: propose(pool_id, shared_route, split_fare, estimated_wait)
    M->>R2: propose(pool_id, shared_route, split_fare, estimated_wait)
    M->>R3: propose(pool_id, shared_route, split_fare, estimated_wait)

    alt Rider Accepts
        R1-->>M: accept-proposal(pool_id)
    else Rider Rejects
        R2-->>M: reject-proposal(pool_id, reason)
    end

    alt Rider Timeout
        note over M: After T_pool seconds, treat as reject
    end

    alt All Responders Accept
        M->>R1: inform(pool_confirmed)
        M->>R2: inform(pool_confirmed)
        M->>R3: inform(pool_confirmed)
    else Partial Acceptance
        note over M: Re-form pool with accepting riders
        note over M: Rejected riders re-enter next batching window
        M->>R1: inform(pool_confirmed, revised_pool)
        M->>R2: inform(pool_rejected, requeued=true)
    else No Acceptances
        note over M: Dissolve pool
        M->>R1: inform(pool_dissolved, requeued=true)
        M->>R2: inform(pool_dissolved, requeued=true)
        M->>R3: inform(pool_dissolved, requeued=true)
    end
```
**Rules:**

- **Partial acceptance:** the microhub re-forms the pool with accepting riders only; rejecting riders are requeued.
    
- **Timeout:** any rider who does not respond within `T_pool` is treated as a rejection.
    
- **Minimum pool size:** if fewer than `min_pool_size` riders accept, the microhub may dissolve the pool and requeue all riders, or proceed with a solo trip (configurable per hub).
  

---

### 2.3. MatchAuctionProtocol (Core Protocol)

**Initiator:** Microhub Agent  
**Responder:** Driver Agent  
**Purpose:** Run a reverse auction to match a pool/trip with a driver.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant D1 as Driver Agent 1
    participant D2 as Driver Agent 2
    participant D3 as Driver Agent 3
    participant R as Rider Agent

    M->>D1: cfp(pool_details, constraints, auction_deadline)
    M->>D2: cfp(pool_details, constraints, auction_deadline)
    M->>D3: cfp(pool_details, constraints, auction_deadline)

    D1-->>M: propose(bid_price, eta, max_detour, capacity, reputation)
    D2-->>M: refuse(reason: shift_ending)
    D3-->>M: propose(bid_price, eta, max_detour, capacity, reputation)

    alt At least one valid bid
        note over M: Evaluate bids (price, wait time, detour, reputation)
        M->>D1: accept-proposal(bid_id)
        M->>D3: reject-proposal(bid_id, reason: outbid)
        M->>R: inform(match_result, driver_details, final_fare)
        M->>D1: inform(trip_assignment, rider_details)
    else No valid bids / timeout
        M->>R: inform(match_failed, reason)
        note over M: Requeue pool or escalate to next batch
    end
```
**Rules:**

- **Timeout:** bids must arrive before `auction_deadline`. Late bids are discarded.
    
- **No valid bids:** microhub informs riders of failure, requeues the pool, and may retry with widened constraints.
    
- **Winner notification:** all non-winning bidders receive `reject-proposal`.
    
- **Rider role:** the rider is _informed_ of the result, not an auction participant.
    

**Auction type options (per microhub):**

- First-price reverse auction (baseline).
    
- Second-price (Vickrey) reverse auction.
    
- Multi-attribute scoring with configurable weights.
    

---

## 3. Support and Logistics Protocols

### 3.1. DriverStatusProtocol

**Initiator:** Driver Agent  
**Responder:** Microhub Agent  
**Purpose:** Driver broadcasts location, capacity, and availability.
```mermaid
sequenceDiagram
    participant D as Driver Agent
    participant M as Microhub Agent

    loop On change or every T_status minutes
        D->>M: inform(location, capacity_available, availability_state, shift_remaining)
    end

    alt State Change (e.g., becomes idle)
        D->>M: inform(availability_state=idle)
    end
```
**Rules:**

- Drivers broadcast on state change (idle → assigned → busy → idle) and at least every `T_status` minutes.
    
- Microhubs use this to maintain a live driver registry for CFP targeting.
    

---

### 3.2. EscrowProtocol

**Initiator:** Rider Agent  
**Responder:** Payment Agent  
**Purpose:** Rider places fare in escrow before trip.
```mermaid
sequenceDiagram
    participant R as Rider Agent
    participant P as Payment Agent
    participant M as Microhub Agent

    R->>P: request(hold_fare_in_escrow, amount, request_id)
    alt Sufficient funds
        P-->>R: agree(escrow_id)
        P->>M: inform(escrow_confirmed, request_id, escrow_id)
    else Insufficient funds
        P-->>R: refuse(reason: insufficient_funds)
        P->>M: inform(escrow_failed, request_id)
    end
```

---

### 3.3. SettlementProtocol

**Initiator:** Microhub Agent  
**Responder:** Payment Agent  
**Purpose:** Post-trip payout and settlement.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant P as Payment Agent
    participant D as Driver Agent
    participant R as Rider Agent

    note over M: Trip completed
    M->>P: inform(trip_completed, pool_id, driver_id, escrow_id)

    P->>P: Release escrow, apply fare splitting
    P->>D: inform(payout, amount)
    alt Rider eligible for subsidy
        P->>R: inform(subsidy_disbursement, amount)
    end
    P->>M: inform(settlement_complete, pool_id)
    P->>R: inform(payment_settled, amount_charged)
```
**Rules:**

- Payment is only released after `trip_completed` is confirmed by the microhub.
    
- Fare splitting follows the declared rule (proportional with pooling discount, Shapley, etc.).
    
- Subsidies are applied per regulator rules.
    

---

### 3.4. RefundProtocol

**Initiator:** Microhub Agent or Rider Agent  
**Responder:** Payment Agent  
**Purpose:** Process refunds for failed or cancelled trips.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant P as Payment Agent
    participant R as Rider Agent

    M->>P: request(process_refund, escrow_id, reason)
    alt Refund approved
        P->>P: Release funds back to Rider
        P-->>M: inform(refund_complete, escrow_id)
        P->>R: inform(refund_issued, amount)
    else Refund denied
        P-->>M: refuse(reason)
    end
```
---

### 3.5. CancellationProtocol

**Initiator:** Rider Agent or Driver Agent  
**Responder:** Microhub Agent  
**Purpose:** Cancel a trip before or after assignment.
```mermaid
sequenceDiagram
    participant R as Rider Agent
    participant M as Microhub Agent
    participant D as Driver Agent
    participant P as Payment Agent

    alt Rider cancels before assignment
        R->>M: request(cancel_trip, request_id)
        M-->>R: agree(cancellation_confirmed)
        M->>P: request(process_refund, escrow_id, reason: rider_cancelled)
    else Rider cancels after assignment
        R->>M: request(cancel_trip, request_id)
        M->>D: inform(cancellation_notice, request_id)
        M-->>R: agree(cancellation_confirmed, fee_applied)
        M->>P: request(process_refund, escrow_id, reason: rider_cancelled_late)
    else Driver cancels after assignment
        D->>M: request(cancel_assignment, request_id, reason)
        M-->>D: agree(cancellation_confirmed, penalty_applied)
        M->>P: request(process_refund, escrow_id, reason: driver_cancelled)
        note over M: Re-auction the pool
    end
```
**Rules:**

- Cancellation before assignment: full refund.
    
- Cancellation after assignment (rider): partial refund, cancellation fee may apply.
    
- Cancellation by driver: full refund to rider; driver penalty applied via `ReputationProtocol`.
    

---

### 3.6. ReputationProtocol

**Initiator:** Rider Agent, Driver Agent, Microhub Agent, Reputation Agent  
**Responder:** Reputation Agent, Microhub Agent, Driver Agent  
**Purpose:** Collect ratings, answer reputation queries, and publish updated scores.
```mermaid
sequenceDiagram
    participant R as Rider Agent
    participant D as Driver Agent
    participant Rep as Reputation Agent
    participant M as Microhub Agent

    R->>Rep: inform(rate_driver, driver_id, score, comments)
    D->>Rep: inform(rate_rider, rider_id, score)

    note over Rep: Updates scores (bounded, min ratings required)

    alt Microhub queries reputation during bid evaluation
        M->>Rep: query-if(driver_reputation, driver_id)
        Rep-->>M: inform(reputation_score, driver_id, score, confidence)
    end

    Rep->>M: inform(reputation_update, driver_id, new_score)
    Rep->>D: inform(reputation_update, new_score)
```
**Rules:**

- Self-rating is forbidden.
    
- Scores are bounded and require a minimum number of ratings before being published.
    
- Microhubs may query reputation during auction evaluation.
    

---

### 3.7. RepositionProtocol

**Initiator:** Microhub Agent  
**Responder:** Driver Agent  
**Purpose:** Incentivize drivers to reposition to staging points for future demand.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant D as Driver Agent

    M->>D: request(reposition_to_staging, staging_point_id, incentive_fee, deadline)
    alt Driver Accepts
        D-->>M: agree(reposition_confirmed)
        D->>D: Move to staging point
        D->>M: inform(arrived_at_staging, staging_point_id)
    else Driver Busy or Rejects
        D-->>M: refuse(reason)
    end
    alt Driver Timeout
        note over M: Treat as refuse after T_reposition
    end
```

**Rules:**

- Repositioning is **optional** but incentivised (bonus, priority, fare guarantee).
    
- Drivers must not reposition while assigned to an active trip.
    
- If a driver does not respond within `T_reposition`, the microhub treats it as a refusal.
    
---

## 4. Environment and Regulation Protocols

### 4.1. TrafficUpdateProtocol

**Initiator:** Environment Agent  
**Responder:** Microhub Agent, Driver Agent  
**Purpose:** Broadcast traffic conditions and travel time estimates.
```mermaid
sequenceDiagram
    participant E as Environment Agent
    participant M as Microhub Agent
    participant D as Driver Agent

    loop Every T_traffic minutes
        E->>M: inform(traffic_update, travel_time_matrix, congestion_levels)
        E->>D: inform(traffic_update, road_conditions, segment_times)
    end
```

---

### 4.2. DisruptionProtocol

**Initiator:** Environment Agent  
**Responder:** Microhub Agent, Driver Agent  
**Purpose:** Broadcast disruptions (accidents, road closures, weather).
```mermaid
sequenceDiagram
    participant E as Environment Agent
    participant M as Microhub Agent
    participant D as Driver Agent

    E->>M: inform(disruption_alert, location, severity, expected_duration)
    E->>D: inform(disruption_alert, location, severity, expected_duration)

    alt Active trip affected
        D->>M: request(re_route, trip_id, disruption_id)
        note over M: Trigger ReNegotiationProtocol
    else No active trip affected
        note over D: Update route cache
    end
```


---

### 4.3. ReNegotiationProtocol

**Initiator:** Microhub Agent  
**Responder:** Driver Agent, Rider Agent  
**Purpose:** Re-negotiate trip parameters after a disruption or failure.
```mermaid
sequenceDiagram
    participant M as Microhub Agent
    participant D as Driver Agent
    participant R as Rider Agent

    note over M: Disruption affects active trip
    M->>D: propose(revised_route, new_eta, revised_fare)
    M->>R: propose(revised_route, new_eta, revised_fare)

    alt Both accept
        D-->>M: accept-proposal(revised_terms)
        R-->>M: accept-proposal(revised_terms)
        M->>D: inform(renegotiation_complete)
        M->>R: inform(renegotiation_complete)
    else Driver rejects
        D-->>M: reject-proposal(reason)
        note over M: Re-auction trip or cancel via CancellationProtocol
    else Rider rejects
        R-->>M: reject-proposal(reason)
        note over M: Cancel trip, trigger RefundProtocol
    end
```


**Rules:**

- Re-negotiation is triggered by `DisruptionProtocol` or `DriverFailureProtocol`.
    
- If re-negotiation fails, the trip is cancelled and refunded.
    

---

### 4.4. DriverFailureProtocol

**Initiator:** Driver Agent  
**Responder:** Microhub Agent  
**Purpose:** Driver reports breakdown, accident, or inability to complete trip.
```mermaid
sequenceDiagram
    participant D as Driver Agent
    participant M as Microhub Agent
    participant R as Rider Agent
    participant P as Payment Agent

    D->>M: inform(driver_failure, trip_id, reason, location)
    M->>R: inform(trip_interrupted, reason, next_steps)
    M->>P: request(process_refund, escrow_id, reason: driver_failure)
    note over M: Re-auction trip with remaining riders
    M->>R: inform(reauction_started)
```


---

### 4.5. PolicyProtocol

**Initiator:** Regulator Agent  
**Responder:** Microhub Agent, Driver Agent  
**Purpose:** Broadcast fare caps, subsidies, and equity rules.
```mermaid
sequenceDiagram
    participant Reg as Regulator Agent
    participant M as Microhub Agent
    participant D as Driver Agent

    Reg->>M: request(acknowledge_policy, policy_update, fare_caps, equity_zones, subsidies)
    Reg->>D: request(acknowledge_policy, policy_update, fare_caps, subsidies)

    alt Acknowledged
        M-->>Reg: agree(policy_acknowledged)
        D-->>Reg: agree(policy_acknowledged)
    else Rejected
        M-->>Reg: refuse(reason)
        D-->>Reg: refuse(reason)
    end
```


**Note:** `request` + `agree`/`refuse` is used instead of `inform` + `agree`, since `agree` is only valid as a response to a `request` or `propose` in FIPA-ACL.

---

### 4.6. AuditProtocol

**Initiator:** Regulator Agent  
**Responder:** Microhub Agent  
**Purpose:** Monitor microhub compliance with regulations.
```mermaid
sequenceDiagram
    participant Reg as Regulator Agent
    participant M as Microhub Agent

    Reg->>M: request(audit_logs, time_period)
    alt Logs available
        M-->>Reg: inform(audit_data, trip_logs, fare_data)
        note over Reg: Verify compliance
        alt Non-compliant
            Reg->>M: inform(penalty_imposed, reason)
        else Compliant
            Reg->>M: inform(audit_passed)
        end
    else Logs unavailable
        M-->>Reg: refuse(reason: logs_unavailable)
        Reg->>M: inform(penalty_imposed, reason: non_cooperation)
    end
```


---

### 4.7. InterHubCoordinationProtocol

**Initiator:** Microhub Agent  
**Responder:** Neighbouring Microhub Agent  
**Purpose:** Exchange demand forecasts, coordinate overflow, and share driver state.
```mermaid
sequenceDiagram
    participant M1 as Microhub Agent 1
    participant M2 as Microhub Agent 2

    loop Every T_hub minutes
        M1->>M2: inform(demand_forecast, expected_riders, time_window)
        M2->>M1: inform(demand_forecast, expected_riders, time_window)
    end

    alt Overflow: M1 has more riders than it can serve
        M1->>M2: request(accept_overflow, pool_details)
        alt M2 accepts
            M2-->>M1: agree(overflow_accepted, pool_id)
        else M2 declines
            M2-->>M1: refuse(reason)
        end
    end
```


**Rules:**

- Hubs remain autonomous; coordination is voluntary.
    
- Overflow requests are best-effort, not obligations.
    
- Driver state sharing is optional and privacy-preserving (aggregate counts only).
    

---

## 5. Auxiliary Protocols

### 5.1. DemandForecastProtocol (Optional)

**Initiator:** Staging Point Agent (if active)  
**Responder:** Driver Agent, Microhub Agent  
**Purpose:** Broadcast local demand forecasts to nearby drivers and microhubs.
```mermaid
sequenceDiagram
    participant S as Staging Point Agent
    participant D as Driver Agent
    participant M as Microhub Agent

    loop Every T_forecast minutes
        S->>D: inform(demand_forecast, expected_riders, coverage_radius)
        S->>M: inform(demand_forecast, expected_riders, coverage_radius)
    end
```


**Note:** If staging points are modelled as passive environmental objects, this protocol is folded into `TrafficUpdateProtocol` and issued by the Environment Agent.

---

### 5.2. TripExecutionProtocol

**Initiator:** Driver Agent  
**Responder:** Rider Agent  
**Purpose:** Manage the physical pickup and drop-off of pooled riders.
```mermaid
sequenceDiagram
    participant D as Driver Agent
    participant R1 as Rider Agent 1
    participant R2 as Rider Agent 2

    D->>R1: inform(arriving_at_pickup, eta)
    R1-->>D: inform(ready_for_pickup)
    D->>R1: inform(picked_up)

    D->>R2: inform(arriving_at_pickup, eta)
    R2-->>D: inform(ready_for_pickup)
    D->>R2: inform(picked_up)

    D->>R1: inform(dropped_off)
    D->>R2: inform(dropped_off)

    alt Rider not ready
        R1-->>D: inform(not_ready, delay_estimate)
        alt Delay exceeds threshold
            D->>M: inform(rider_no_show, rider_id)
            note over M: Trigger CancellationProtocol
        end
    end
```


**Note:** `inform` is used instead of `agree` because `agree` is not a valid response to `inform` in FIPA-ACL.

---

## 6. Concurrency and Double-Booking Prevention

Because microhubs are independent and drivers may receive CFPs from multiple hubs simultaneously, the system must prevent double-booking.

### 6.1. Mechanism: Driver-Side Provisional Lock

A driver may hold **at most one provisional commitment** at a time.
```mermaid
sequenceDiagram
    participant M1 as Microhub Agent 1
    participant M2 as Microhub Agent 2
    participant D as Driver Agent

    M1->>D: cfp(pool_A)
    M2->>D: cfp(pool_B)
    D-->>M1: propose(bid_A, provisional=true)
    D-->>M2: refuse(reason: provisionally_committed)

    alt M1 awards
        M1->>D: accept-proposal(bid_A)
        D->>D: Lock commitment
        M1->>D: inform(trip_assignment)
    else M1 does not award
        M1->>D: reject-proposal(bid_A)
        D->>D: Release provisional commitment
        note over D: May now bid on other CFPs
    end
```


### 6.2. Rules

1. A driver may submit at most one `propose` with `provisional=true` at a time.
    
2. A provisional commitment expires after `T_provisional` if not confirmed.
    
3. On `accept-proposal`, the commitment becomes firm.
    
4. On `reject-proposal`, the commitment is released.
    
5. Microhubs must send `accept-proposal` or `reject-proposal` within `T_award`.
    

### 6.3. Alternative Mechanisms (for experimentation)

|Mechanism|Description|Trade-off|
|---|---|---|
|**Driver-side lock**|Driver holds one provisional bid.|Simple, may under-utilise drivers.|
|**Two-phase commit**|Microhub asks driver to hold, then confirms.|More message overhead.|
|**Optimistic**|Driver bids freely; refuses all but one on award.|Risk of broken commitments.|
|**Reputation penalty**|Driver penalised for breaking commitments.|Requires reputation system maturity.|

---

## 7. Interaction Summary Matrix

|Protocol|Initiator|Responder(s)|Key FIPA-ACL Performatives|
|---|---|---|---|
|`TripRequestProtocol`|Rider|Microhub|`request`, `agree`, `refuse`, `inform`|
|`PoolFormationProtocol`|Microhub|Rider|`propose`, `accept-proposal`, `reject-proposal`, `inform`|
|`MatchAuctionProtocol`|Microhub|Driver|`cfp`, `propose`, `refuse`, `accept-proposal`, `reject-proposal`, `inform`|
|`DriverStatusProtocol`|Driver|Microhub|`inform`|
|`EscrowProtocol`|Rider|Payment|`request`, `agree`, `refuse`, `inform`|
|`SettlementProtocol`|Microhub|Payment|`inform`|
|`RefundProtocol`|Microhub / Rider|Payment|`request`, `agree`, `refuse`, `inform`|
|`CancellationProtocol`|Rider / Driver|Microhub|`request`, `agree`, `refuse`, `inform`|
|`ReputationProtocol`|Rider / Driver / Microhub|Reputation|`inform`, `query-if`|
|`RepositionProtocol`|Microhub|Driver|`request`, `agree`, `refuse`, `inform`|
|`TrafficUpdateProtocol`|Environment|All|`inform`|
|`DisruptionProtocol`|Environment|All|`inform`|
|`ReNegotiationProtocol`|Microhub|Driver, Rider|`propose`, `accept-proposal`, `reject-proposal`, `inform`|
|`DriverFailureProtocol`|Driver|Microhub|`inform`|
|`PolicyProtocol`|Regulator|Microhub, Driver|`request`, `agree`, `refuse`|
|`AuditProtocol`|Regulator|Microhub|`request`, `inform`, `refuse`|
|`InterHubCoordinationProtocol`|Microhub|Microhub|`inform`, `request`, `agree`, `refuse`|
|`DemandForecastProtocol` (optional)|Staging Point|Driver, Microhub|`inform`|
|`TripExecutionProtocol`|Driver|Rider|`inform`|

---

## 8. Mapping to SPADE FSMBehaviour

When moving to the coding phase, these protocols will be implemented as **FSMBehaviour** (Finite State Machine Behaviour) inside SPADE agents.

### 8.1. `MicrohubAgent` — MatchAuctionProtocol FSM

1. **State 1 (Start):** Send `cfp` to all known Driver Agents.
    
2. **State 2 (Collecting Bids):** Wait for `propose` or `refuse`. Set timeout `T_auction`.
    
3. **State 3 (Evaluating):** Evaluate bids with multi-attribute function (price, wait time, detour, reputation).
    
4. **State 4 (Awarding):** Send `accept-proposal` to winner, `reject-proposal` to losers.
    
5. **State 5 (Informing):** Send `inform` to Rider Agents with match result.
    
6. **State 6 (Failure):** If no valid bids, inform riders and requeue pool.
    
7. **State 7 (End):** Reset FSM for next auction.
    

### 8.2. `DriverAgent` — MatchAuctionProtocol FSM

1. **State 1 (Idle):** Wait for `cfp`.
    
2. **State 2 (Evaluating):** Check shift, capacity, reserve price, overlap.
    
3. **State 3 (Bidding):** Send `propose` with `provisional=true` if eligible; otherwise `refuse`.
    
4. **State 4 (Awaiting Award):** Wait for `accept-proposal` or `reject-proposal` until `T_award`.
    
5. **State 5 (Committed):** Lock commitment, await `trip_assignment`.
    
6. **State 6 (Released):** On `reject-proposal` or timeout, release commitment and return to Idle.
    

### 8.3. `RiderAgent` — TripRequestProtocol FSM

1. **State 1 (Idle):** User initiates request.
    
2. **State 2 (Requesting):** Send `request` to microhub.
    
3. **State 3 (Awaiting Pool):** Wait for `propose(pool)`.
    
4. **State 4 (Deciding):** Evaluate pool proposal; send `accept-proposal` or `reject-proposal`.
    
5. **State 5 (Awaiting Match):** Wait for `inform(match_result)`.
    
6. **State 6 (InTrip):** Interact via `TripExecutionProtocol`.
    
7. **State 7 (Rating):** Send rating via `ReputationProtocol`.
    
8. **State 8 (End):** Return to Idle.
    

### 8.4. `PaymentAgent` — EscrowProtocol + SettlementProtocol FSM

1. **State 1 (Idle):** Wait for `request(hold_fare_in_escrow)`.
    
2. **State 2 (Escrow Held):** Confirm with `agree(escrow_id)`, notify microhub.
    
3. **State 3 (Awaiting Completion):** Wait for `inform(trip_completed)`.
    
4. **State 4 (Settling):** Release escrow, split fare, pay driver, disburse subsidy.
    
5. **State 5 (Settled):** Notify all parties; return to Idle.
    
6. **State 6 (Refunding):** On `request(process_refund)`, release funds to rider.
    

---

## 9. Theme Coverage Checklist

|Theme Element|Where Covered|
|---|---|
|Time windows|Rider (input), Microhub (constraint), Driver (bid feasibility)|
|Detour tolerance|Rider (input), Microhub (pool feasibility), Driver (bid constraint)|
|Driver shifts|Driver (safety), Microhub (assignment constraint)|
|Traffic variability|`TrafficUpdateProtocol`, `DisruptionProtocol`, `ReNegotiationProtocol`|
|Pooling|`PoolFormationProtocol`, Rider (willingness), Driver (capacity)|
|Pricing schemes|Driver (reserve/bid), Microhub (rule), `EscrowProtocol`, `SettlementProtocol`|
|Equity/fairness|Regulator (`PolicyProtocol`), Microhub (constraints), metrics|
|Repositioning|`RepositionProtocol`, `DriverStatusProtocol`, Staging Point (coverage)|
|Decentralisation|Independent Microhub agents, `InterHubCoordinationProtocol`, driver autonomy|
|Auction mechanism|`MatchAuctionProtocol`, multi-attribute evaluation|
|Concurrency|Section 6: Driver-side provisional lock|
|Failure handling|Timeouts, `CancellationProtocol`, `DriverFailureProtocol`, `RefundProtocol`|
|Reputation|`ReputationProtocol` with query support|

---

## 10. Open Design Questions

1. **Auction type:** First-price, second-price, or VCG? Multi-attribute or price-only?
    
2. **Fare splitting:** Equal, proportional to solo fare, proportional to distance, or Shapley?
    
3. **Pooling batching window:** How long does a microhub wait before forming a pool?
    
4. **Staging points:** Active agents or passive environmental objects?
    
5. **Inter-hub coordination:** How much should hubs share? Fully independent or federated?
    
6. **Reputation:** Global or per-microhub? How is tampering prevented?
    
7. **Subsidy mechanism:** Direct to rider, to microhub, or to driver?
    
8. **Concurrency mechanism:** Driver-side lock, two-phase commit, or optimistic?
    
9. **Re-negotiation policy:** Who has final say — microhub, driver, or rider?
    
10. **Penalty design:** How are driver cancellations, rider no-shows, and broken commitments penalised?
    

---

## 11. Revision Summary (Changes from Preliminary Version)

|Change|Rationale|
|---|---|
|Added `alt`/`else` blocks for failures and timeouts|Preliminary model showed only happy paths.|
|Handled partial acceptance in `PoolFormationProtocol`|Pool behaviour was undefined when some riders rejected.|
|Removed Rider from `MatchAuctionProtocol` responders|Rider is informed, not an auction participant.|
|Split `PaymentProtocol` into `EscrowProtocol` + `SettlementProtocol`|Cleaner FSM implementation, clearer responsibilities.|
|Added `InterHubCoordinationProtocol`|Role model mentioned inter-hub demand exchange; protocol was missing.|
|Added `ReNegotiationProtocol`|Disruption handling was incomplete.|
|Added `DriverFailureProtocol`|Mid-trip failure was unhandled.|
|Added `CancellationProtocol`|Rider/driver cancellation was unhandled.|
|Added `DriverStatusProtocol`|Role model required location/capacity updates; protocol was missing.|
|Fixed `PolicyProtocol` to use `request` + `agree`/`refuse`|`agree` is invalid as a response to `inform`.|
|Fixed `TripExecutionProtocol` to use `inform` instead of `agree`|Same reason.|
|Added `query-if` sub-protocol to `ReputationProtocol`|Microhubs need reputation during bid evaluation.|
|Marked `DemandForecastProtocol` as optional|Staging Point Agent is optional.|
|Added Section 6: Concurrency and Double-Booking Prevention|Critical for decentralised auctions.|
|Added FSM mappings for Driver, Rider, and Payment agents|Preliminary only had Microhub FSM.|
|Expanded theme coverage checklist|Now includes concurrency, failure handling, reputation.|
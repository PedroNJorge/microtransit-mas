## Overview

This document defines the Gaia role schemas for a **decentralised last-mile microtransit and ride-sharing MAS**. The system is modelled as a graph of microhub nodes, where riders travel from a microhub to their home (off-graph). Microhub agents act as autonomous local mediators, matching riders and drivers through auctions. Drivers are autonomous vehicles that may pool multiple riders and reposition strategically. The system is decentralised: each microhub sets its own auction rules, and drivers retain autonomy over pricing and repositioning.

### Protocol Catalogue

| Protocol | Purpose |
| :--- | :--- |
| `TripRequestProtocol` | Rider submits trip request to a microhub. |
| `PoolFormationProtocol` | Microhub aggregates compatible riders into a pool. |
| `MatchAuctionProtocol` | Microhub runs reverse auction with drivers via CFP. |
| `RepositionProtocol` | Microhub requests driver repositioning to staging points. |
| `PricingProtocol` | Fare splitting, subsidies, and driver payout rules. |
| `PaymentProtocol` | Escrow, settlement, and payout. |
| `ReputationProtocol` | Rating exchange and reputation updates. |
| `TrafficUpdateProtocol` | Environment broadcasts traffic and disruption updates. |
| `PolicyProtocol` | Regulator broadcasts fare caps, subsidies, and equity rules. |

---

## Role Schema: Rider Agent

| Field | Description |
| :--- | :--- |
| **Description** | Represents a user who needs a ride from a microhub node to their home (off-graph destination). |
| **Protocols and Activities** | `TripRequestProtocol`: submit trip request (origin microhub, destination, time window, detour tolerance, pooling willingness, max fare, subsidy eligibility). `PoolFormationProtocol`: receive pool proposal. `MatchAuctionProtocol`: receive match result. `PaymentProtocol`: pay agreed fare. `ReputationProtocol`: rate driver after trip. |
| **Permissions** | Can generate ride requests. Can accept or reject pool/match proposals. Can declare subsidy eligibility. Can rate drivers. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually submit a ride request with all required attributes. Must eventually respond to each pool/match proposal (accept or reject) within a bounded timeout. Must eventually rate the driver after trip completion. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not submit conflicting simultaneous requests. Must not accept a match without sufficient funds or valid subsidy. Must not accept a pool that violates declared detour tolerance or max fare. Must not fail to pay the agreed fare upon successful trip completion. |

---

## Role Schema: Driver Agent

| Field | Description |
| :--- | :--- |
| **Description** | Represents an autonomous vehicle/driver offering transportation services, with capacity for pooling and autonomy over pricing and repositioning. |
| **Protocols and Activities** | `MatchAuctionProtocol`: receive CFP, calculate multi-attribute bid (price, ETA, max detour, capacity), submit bid, receive assignment. `TripExecutionProtocol`: pick up and drop off pooled riders. `RepositionProtocol`: reposition to staging points if incentivised. `PricingProtocol`: set and update reserve price. `ReputationProtocol`: receive rating updates. |
| **Permissions** | Can bid on ride requests. Can set and update reserve price. Can reposition to staging points. Can refuse a CFP. Can accept or reject repositioning requests. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually respond to each CFP with `propose` or `refuse` within a bounded timeout. Must eventually pick up and drop off assigned riders. Must eventually update location and capacity status. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not bid on overlapping requests unless capacity and shift time allow. Must maintain `current_passengers <= max_capacity` at all times. Must not accept trips that violate remaining shift time. Must not abandon an accepted trip. Must not reposition while assigned to an active trip. Must not submit bids below reserve price. |

---

## Role Schema: Microhub Agent

| Field | Description |
| :--- | :--- |
| **Description** | Autonomous mediator at a microhub node that aggregates rider demand, forms pools, matches riders and drivers via auctions, coordinates repositioning, and enforces local rules. |
| **Protocols and Activities** | `TripRequestProtocol`: receive ride requests. `PoolFormationProtocol`: aggregate compatible riders into a pool within a batching window. `MatchAuctionProtocol`: initiate auction (CFP), evaluate multi-attribute bids (price, wait time, detour, reputation), select winner, inform riders and drivers. `RepositionProtocol`: request driver repositioning to staging points. `PricingProtocol`: enforce fare splitting and subsidy rules. `PolicyProtocol`: receive regulator rules. `TrafficUpdateProtocol`: receive traffic updates. Optional: exchange demand forecasts with neighbouring hubs. |
| **Permissions** | Can initiate auctions. Can evaluate bids. Can assign rides. Can set local auction rules and weights. Can request driver repositioning. Can exchange demand forecasts with neighbouring hubs. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually conclude an auction for a received request (either match or fail) within a bounded time. Must eventually notify all bidders of the auction outcome. Must eventually form or dissolve pools within the batching window. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not assign the same driver to conflicting trips. Must ensure assigned driver has sufficient capacity. Must ensure final fare matches declared auction and splitting rules. Must respect regulator fare caps and subsidy rules. Must not systematically exclude underserved zones from auctions. Must not conclude an auction without notifying all bidders of the outcome. |

---

## Role Schema: Regulator Agent

| Field | Description |
| :--- | :--- |
| **Description** | Represents the transit authority or regulator that sets fare caps, subsidies, equity rules, and service standards. |
| **Protocols and Activities** | `PolicyProtocol`: broadcast fare caps, subsidy rules, equity constraints, service standards. `AuditProtocol`: monitor microhub and driver compliance. `SubsidyProtocol`: disburse subsidies to eligible riders or hubs. |
| **Permissions** | Can set and update fare caps. Can define subsidy eligibility. Can define equity zones and minimum service levels. Can audit microhub and driver behaviour. Can impose penalties. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually publish policy updates when conditions change. Must eventually respond to audit requests. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not set fare caps below sustainable driver reserve levels. Must not publish conflicting policies. Must ensure subsidy rules are consistent across zones unless explicitly differentiated. |

---

## Role Schema: Environment Agent

| Field | Description |
| :--- | :--- |
| **Description** | Simulates the physical environment: road network, traffic conditions, travel times, and disruptions. |
| **Protocols and Activities** | `TrafficUpdateProtocol`: broadcast traffic conditions and travel time estimates. `DisruptionProtocol`: broadcast disruptions (accidents, road closures, weather). |
| **Permissions** | Can update traffic state. Can generate disruptions. Can publish travel time matrices. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually publish traffic updates at a defined frequency. Must eventually broadcast disruption alerts when they occur. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not publish contradictory traffic states simultaneously. Must ensure travel time updates are consistent with the current road network state. |

---

## Role Schema: Payment Agent

| Field | Description |
| :--- | :--- |
| **Description** | Handles escrow, fare splitting, driver payout, and subsidy settlement. |
| **Protocols and Activities** | `PaymentProtocol`: hold rider fare in escrow, split fare among pooled riders, pay driver, disburse subsidies. `RefundProtocol`: process refunds for failed or cancelled trips. |
| **Permissions** | Can hold funds in escrow. Can execute payments. Can apply subsidy rules. Can issue refunds. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually settle payment after trip completion. Must eventually process refunds for failed trips. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not release payment before trip completion confirmation. Must not pay drivers below agreed auction price. Must ensure fare splitting follows declared rules. Must ensure subsidy disbursement follows regulator rules. |

---

## Role Schema: Reputation Agent

| Field | Description |
| :--- | :--- |
| **Description** | Tracks ratings, reliability, and complaints for riders and drivers. |
| **Protocols and Activities** | `ReputationProtocol`: collect ratings from riders and drivers, update reputation scores, publish reputation updates. |
| **Permissions** | Can collect ratings. Can update reputation scores. Can publish reputation scores to microhubs and drivers. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually update reputation scores after each trip. Must eventually publish updated scores to relevant parties. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not allow self-rating. Must ensure reputation scores are bounded and tamper-evident. Must not publish reputation scores without sufficient evidence (minimum number of ratings). |

---

## Role Schema: Staging Point Agent (Optional)

| Field | Description |
| :--- | :--- |
| **Description** | Represents a strategic waiting area between microhub nodes that drivers can use to position themselves for future demand. |
| **Protocols and Activities** | `DemandForecastProtocol`: broadcast local demand forecasts to nearby drivers and microhubs. `OccupancyProtocol`: report current driver occupancy. |
| **Permissions** | Can broadcast demand forecasts. Can report occupancy. Can define coverage radius over microhub nodes. |
| **Responsibilities** | |
| &nbsp;&nbsp;&nbsp;&nbsp;**Liveness** | Must eventually broadcast demand forecasts at a defined frequency. Must eventually update occupancy status. |
| &nbsp;&nbsp;&nbsp;&nbsp;**Safety** | Must not broadcast forecasts that exceed available capacity. Must ensure coverage radius is consistent with road network. |

> **Note:** If staging points are modelled as passive environmental objects rather than active agents, this role can be folded into the Environment Agent. In that case, microhub agents manage staging point logic locally.

---

## Role Interaction Summary

| Interaction | Protocol | Initiator | Responder |
| :--- | :--- | :--- | :--- |
| Trip request | `TripRequestProtocol` | Rider | Microhub |
| Pool proposal | `PoolFormationProtocol` | Microhub | Rider |
| Auction CFP | `MatchAuctionProtocol` | Microhub | Driver |
| Bid submission | `MatchAuctionProtocol` | Driver | Microhub |
| Match award | `MatchAuctionProtocol` | Microhub | Rider, Driver |
| Reposition request | `RepositionProtocol` | Microhub | Driver |
| Fare payment | `PaymentProtocol` | Rider | Payment |
| Driver payout | `PaymentProtocol` | Payment | Driver |
| Subsidy disbursement | `PaymentProtocol` | Payment | Rider |
| Rating | `ReputationProtocol` | Rider, Driver | Reputation |
| Reputation update | `ReputationProtocol` | Reputation | Microhub, Driver |
| Traffic update | `TrafficUpdateProtocol` | Environment | All |
| Policy update | `PolicyProtocol` | Regulator | Microhub, Driver |
| Demand forecast | `DemandForecastProtocol` | Staging Point | Driver, Microhub |

---

## Theme Coverage Checklist

| Theme Element | Where Covered |
| :--- | :--- |
| Time windows | Rider (input), Microhub (constraint), Driver (bid feasibility) |
| Detour tolerance | Rider (input), Microhub (pool feasibility), Driver (bid constraint) |
| Driver shifts | Driver (safety), Microhub (assignment constraint) |
| Traffic variability | Environment (updates), Driver (ETA), Microhub (evaluation) |
| Pooling | Rider (willingness), Microhub (formation), Driver (capacity) |
| Pricing schemes | Driver (reserve/bid), Microhub (rule), Payment (settlement) |
| Equity/fairness | Regulator (rules), Microhub (constraints), metrics |
| Repositioning | Driver (optional), Microhub (request), Staging Point (coverage) |
| Decentralisation | Independent Microhub agents, driver autonomy, no central dispatcher |
| Auction mechanism | Microhub (initiate), Driver (bid), multi-attribute evaluation |

---

## Open Design Questions

1. **Concurrent negotiations:** Can a driver hold multiple provisional commitments? How are double-bookings avoided across independent microhubs?
2. **Auction type:** First-price, second-price, or VCG? Multi-attribute or price-only?
3. **Fare splitting:** Equal, proportional to solo fare, proportional to distance, or Shapley?
4. **Pooling batching window:** How long does a microhub wait before forming a pool?
5. **Staging points:** Active agents or passive environmental objects?
6. **Inter-hub coordination:** Do microhubs share demand forecasts, or remain fully independent?
7. **Reputation:** Global or per-microhub? How is tampering prevented?
8. **Subsidy mechanism:** Direct to rider, to microhub, or to driver?
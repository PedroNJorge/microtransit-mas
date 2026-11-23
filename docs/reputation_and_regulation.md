# Reputation gate (ID-07) and the Regulator (Milestone 8)

Code: `src/agents/reputation.py`, `src/agents/regulator.py`, `src/common/policy.py`, plus hooks in Payment, Hub and Driver.
Tests: `tests/integration/test_reputation_flow.py` (done-when), `tests/integration/test_regulator.py`,
`tests/test_reputation.py` (gate, incl. a 25-seed interleaving property), `tests/test_policy_book.py`.

## ID-07: accept early, publish late

    rating --> HELD ----- Payment confirms settlement of the pool -------------> PUBLISHED (settled)
                 |
                 +------- T_settlement_max after the trip completed ----------> PUBLISHED, pending_settlement=True
                                                                                   |
                                          settlement confirmed later -------------+--> same rating, flag removed (new update)

* "Visible" means: in `ReputationScore` messages to the ratee and the hubs, and in `query-if` answers (`refuse(unpublished)` otherwise).
* The published score only counts published ratings; `pending_settlement` is true while any of them is unsettled.
* Settlement confirmation comes from Payment itself (`SettlementComplete` over ReputationProtocol), accepted only from the configured Payment agent.
  Payment may be faster than the hub's trip registry; that order works too.
* The deadline runs from the hub's own completion time (the driver's clock is not trusted) and is enforced by a sweeper.
* ID-08: drivers rate riders only after the hub marked the trip completed (the hub tells the driver). A rating beats the
  registry message at most by a moment; it is parked until the trip is registered.
* Payment's queue can be held shut (`payment.settlement_open = asyncio.Event()`) to simulate a backlog (FE-08); the tests use it.

## PolicyProtocol

`Regulator --request(Policy)--> Hub, Driver, Payment`, answered `agree` or `refuse(reason)` (RI-04).
* Versions increase; a new version starts from the previous one and changes only what is named (no conflicting policies).
* Refuse reasons: `stale_version`, `conflict` (same version, other content), `invalid`, `malformed`, `unknown_regulator`.
* Safety: the Regulator will not publish a cap under `driver_floor` (`DRIVER_FLOOR`, 1.00 EUR); it raises `ValueError`.
* `effective_from` can be in the future; recipients keep a `PolicyBook` and use the policy in force at the time.
* Silent recipients get one re-send after `t_policy_ack`, then show up in `unacknowledged()`.
* **RI-01:** the hub's auction reserve is `min(riders' max fares, fare cap)`. A bid above it is invalid, so no fare above the cap can be awarded;
  a driver whose cost is above the cap refuses (`over_reserve`). The rider's pool proposal states the capped ceiling.
* Payment adopts `subsidy_rules.eligible_discount` of the policy in force at each settlement.

## AuditProtocol

`Regulator --request(AuditRequest)--> Hub --inform(AuditData)--> Regulator --inform(AuditReport) [+ inform(Penalty)]--> Hub`
* The Regulator checks every matched fare against the cap that was in force when it was awarded, using ITS OWN policy record
  (a hub cannot vouch for itself, and a cap published after an award does not make the award a violation).
* `refuse(logs_unavailable)` or silence for `t_audit_response` is non-cooperation (RI-05) and is penalised.
* Every hub is audited every `t_audit` for the period since the last audit (RI-02); `audit(hub)` runs one on demand.
* `AuditReport.zone_service_share` reports matched / requested per destination zone. Acting on it (RI-03/07 overflow) is for the inter-hub milestone.

## Limits
* Penalties are recorded and reported; nothing deducts money yet.
* The cap is per trip (`fare_caps["max"]`); per-km or base-fare formulas in `fare_caps` are carried but not enforced.
* Policies are not sent to riders (they see the cap through the pool proposal).
* Hubs learn reputation by push; the `query-if` path exists on the Reputation side only.

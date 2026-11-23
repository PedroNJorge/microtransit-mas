# Milestone 11: inter-hub coordination

`src/protocols/inter_hub.py`, wired into `MicrohubAgent`. Goal: hubs absorb overflow and recover from failure.
On by default in every scenario (`"inter_hub": false` switches it off).

## Neighbours
The k nearest hubs of a hub (default k = 3) plus every hub that counts it among its k nearest. It is computed from
the graph, so every hub gets the same answer without messages.

## Messages (`InterHubCoordinationProtocol`)
| Message | From -> to | Meaning |
|---|---|---|
| `inform(HubHeartbeat)` | hub -> each neighbour, every `t_heartbeat` (60 s) | alive, idle drivers, queue length, **copy of the requests not yet matched** |
| `request(OverflowRequest)` | hub -> one neighbour | "I cannot find a driver for this pool, please take it" (thread = pool id) |
| `agree` / `refuse(reason)` | neighbour -> hub | the answer |
| `inform(Handover)` | new hub -> rider | "your request is now mine, talk to me" |

## Failure recovery (`t_failover`, default 180 s)
1. Every hub's heartbeat carries its unmatched requests (queued for a pool, pool proposed, waiting for escrow, in auction).
2. A neighbour that hears nothing for `t_failover` declares the hub failed.
3. Each request goes to ONE neighbour: the nearest live neighbour of the failed hub that covers the destination
   (ties by id). All neighbours compute the same answer from the same data, so they need no extra messages.
4. The chosen hub adopts the request as if the rider had sent it, and sends the rider a `Handover`. The rider
   switches hub and goes back to waiting for a pool proposal.
5. Requests already matched are left alone: their driver finishes the trip.

Also, a rider whose hub has not answered a new request within `T_RIDER_REPLY` (300 s) sends it to the next hub
that covers the destination (this covers requests sent to a hub that died before the neighbours learned of it).

## Overflow
A pool that has failed `overflow_after` auctions (default 2) is offered to the neighbour that covers every
destination of the pool and reports the most idle drivers. The neighbour agrees if it has an idle driver and can
serve the requests; it adopts them and informs the riders. The first hub gives the pool up (state `HANDED_OVER`)
and asks Payment to return the riders' escrows. On `refuse` or silence (`t_overflow_reply`, 60 s) it resumes its own auction.

## Payment
A handover makes the rider escrow again with the new hub under the same request id. The ledger accepts that
(`Ledger.hold`): the same rider restarting the same request with another pool or hub gets the old hold back and a
new one. Another rider, or a settled escrow, is still refused as a duplicate.

## Scenario option
```json
"inter_hub": true                                   // default
"inter_hub": false                                  // hubs never coordinate
"inter_hub": {"t_heartbeat": 60, "t_failover": 180, "overflow_after": 2, "k_neighbours": 3, "t_overflow_reply": 60}
```

## Known limits
* A hub that is only silent (not dead) is treated as failed; if it recovers, its requests may be served twice.
* Requests whose destination no live neighbour covers are not taken over; those riders time out as before.
* The rider's old escrow for a dead hub's pool is returned when it escrows again (see Payment).
* Heartbeats copy the pending requests, so message size grows with a hub's backlog.

## Tests
`tests/test_inter_hub.py` (neighbours, takeover rule, overflow target, scenario option) and
`tests/integration/test_hub_failover.py` (kills a hub and checks a neighbour absorbs its request within
`t_failover` + 2 heartbeats and completes the trip; overflow to a neighbour with a driver).

"""
src/protocols/pool_formation.py   PoolFormationProtocol, hub side (Milestone 9)

  Rider --request(TripRequest)--> Hub          pooling_willingness=True -> queued, hub answers
                                               agree("queued") + inform(request_queued:<seconds>)
  ...batch window T_pool closes (first queued request + T_pool, or MAX_BATCH requests waiting)...
  Hub   --propose(PoolProposal)--> each Rider  one proposal per rider of every group the solver built
  Rider --accept-proposal | reject-proposal--> Hub          (silence for T_pool_response = reject)
  all accepted        -> pool confirmed: inform(Pool LOCKED) to every rider, who then escrow (ID-02)
  anyone declines     -> PARTIAL ACCEPTANCE: that pool is dissolved; the decliner is told
                         inform(Pool DISSOLVED); the others are told inform(Pool FORMING) = "back in
                         the queue", and are immediately re-pooled among themselves (new pool id,
                         new proposal). A lone remainder travels solo (`solo_fallback`) or is let go.
  nobody can take a rider (window closed, unreachable) -> inform(Pool DISSOLVED)

Riders with pooling_willingness=False never wait for a batch: they get an immediate solo pool, as in
Milestone 7.  The grouping itself is `common.pooling` (exact: compatibility filter on time windows and
detour tolerance, set-partition DP, drop-off order by enumeration).
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

from spade.behaviour import CyclicBehaviour

from common import clock
from common.messages import make_inform, make_pool_proposal
from common.ontology import Pool, PoolStatus, Protocol, TripRequest
from common.pooling import SOLVERS, Partition

logger = logging.getLogger(__name__)


@dataclass
class QueuedRequest:
    req: TripRequest
    sender: str
    queued_at: float


def window_closes_at(queue: List[QueuedRequest], t_pool: float) -> float:
    return min(q.queued_at for q in queue) + t_pool if queue else float("inf")


def batch_due(queue: List[QueuedRequest], t_pool: float, now: float, max_batch: int,
              slack: float = 0.0) -> bool:
    """The window closes at T_pool after the first request, when max_batch are waiting, or (adaptive)
    when a queued rider's own pickup window ends within `slack` seconds."""
    if not queue:
        return False
    urgent = slack > 0 and any(q.req.time_window_end - now <= slack for q in queue)
    return now >= window_closes_at(queue, t_pool) or len(queue) >= max_batch or urgent


def take_batch(queue: List[QueuedRequest], max_batch: int) -> List[QueuedRequest]:
    """Oldest first; the rest stay queued for the next window."""
    queue.sort(key=lambda q: (q.queued_at, q.req.request_id))
    batch, queue[:] = queue[:max_batch], queue[max_batch:]
    return batch


def plan_batch(hub, entries: List[QueuedRequest], now: float) -> Tuple[Partition, Dict[str, QueuedRequest]]:
    """Run the hub's solver over the entries. Without a road network nobody can be compared, so
    every rider gets a solo pool."""
    by_id = {q.req.request_id: q for q in entries}
    reqs = [q.req for q in entries]
    if hub.network is None:
        return Partition(tuple(), tuple(sorted(by_id))), by_id
    solver = SOLVERS[hub.solver]
    part = solver(hub.network, hub.hub_id, reqs, now, max_size=hub.max_pool_size,
                  detour_weight=hub.detour_weight)
    return part, by_id


async def propose_groups(beh, hub, part: Partition, by_id: Dict[str, QueuedRequest]) -> List:
    """Open a pool per group and send every member its proposal; riders nobody can serve are released."""
    records = []
    for plan in part.groups:
        members = [by_id[i] for i in plan.request_ids]
        rec = hub.open_group(members, plan)
        records.append(rec)
        for q in members:
            await beh.send(make_pool_proposal(q.sender, hub.proposal_for(rec, q.req, plan)))
    for rid in part.unserved:
        q = by_id[rid]
        hub.log(f"request {rid}: no feasible pool (window closed or unreachable)")
        gone = Pool(f"{hub.hub_id}-none-{rid}", hub.hub_id, [q.req], PoolStatus.DISSOLVED, clock.now())
        await beh.send(make_inform(q.sender, Protocol.POOL_FORMATION, gone, gone.pool_id))
    return records


async def run_batch(beh, hub) -> List:
    now = clock.now()
    entries = take_batch(hub.queue, hub.max_batch)
    hub.log(f"batch window closed: pooling {len(entries)} request(s)")
    part, by_id = plan_batch(hub, entries, now)
    if hub.network is None:                                   # no network: everyone alone
        records = []
        for q in entries:
            rec = hub.open_pool(q.req, q.sender)
            records.append(rec)
            await beh.send(make_pool_proposal(q.sender, hub.proposal_for(rec, q.req)))
        return records
    return await propose_groups(beh, hub, part, by_id)


async def handle_decline(beh, hub, rec, declined: List[str]) -> List:
    """Partial acceptance. `declined` = request ids that said no (or stayed silent)."""
    rec.state, rec.pool.status = "DISSOLVED", PoolStatus.DISSOLVED
    by_req = {q.request_id: q for q in rec.pool.requests}
    for rid in declined:
        await beh.send(make_inform(rec.rider_jids[rid], Protocol.POOL_FORMATION, rec.pool, rec.pool.pool_id))
    remaining = [rid for rid in rec.rider_jids if rid not in declined]
    if not remaining:
        return []
    hub.log(f"pool {rec.pool.pool_id}: {declined} declined; re-pooling {remaining}")
    back = Pool(rec.pool.pool_id, hub.hub_id, rec.pool.requests, PoolStatus.FORMING, rec.pool.created_at)
    for rid in remaining:
        await beh.send(make_inform(rec.rider_jids[rid], Protocol.POOL_FORMATION, back, rec.pool.pool_id))
    if len(remaining) == 1 and not hub.solo_fallback:
        gone = Pool(rec.pool.pool_id, hub.hub_id, rec.pool.requests, PoolStatus.DISSOLVED, rec.pool.created_at)
        await beh.send(make_inform(rec.rider_jids[remaining[0]], Protocol.POOL_FORMATION, gone, rec.pool.pool_id))
        return []
    entries = [QueuedRequest(by_req[rid], rec.rider_jids[rid], clock.now()) for rid in remaining]
    part, by_id = plan_batch(hub, entries, clock.now())
    return await propose_groups(beh, hub, part, by_id)


class BatchWindow(CyclicBehaviour):
    """Closes the batch window and forms the pools."""

    async def run(self):
        hub = self.agent
        if batch_due(hub.queue, hub.t_pool, clock.now(), hub.max_batch, hub.window_slack):
            try:
                await run_batch(self, hub)
            except Exception:
                logger.exception("%s: error while forming pools", hub.jid)
        await clock.sim_sleep(1.0)

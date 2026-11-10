"""
src/agents/reputation.py   ReputationAgent (Role: Reputation, Sub-org: Regulatory). Milestones 7-8.

ReputationProtocol
    Rider  --inform(Rating)-------------> Reputation   rate_driver (sender must BE the rater)
    Driver --inform(Rating)-------------> Reputation   rate_rider  (ID-08, see below)
    Hub    --inform(TripCompleted)------> Reputation   trip registry: who rode with whom (trusted hubs only)
    Payment--inform(SettlementComplete)-> Reputation   settlement confirmed (ID-07; trusted Payment only)
    Hub    --query-if(<agent jid>)------> Reputation   -> inform(ReputationScore) | refuse(unpublished)
    Reputation --inform(ReputationScore)-> ratee and every subscribed hub          reputation_update

ID-07: ACCEPT early, PUBLISH late
    A valid rating is stored at once but only becomes visible (to the ratee, the hubs and queries) when
      (a) Payment confirms the settlement of the trip's pool           -> published, settled; or
      (b) T_settlement_max has passed since the trip was completed     -> published with
          `pending_settlement=True`; when the settlement is confirmed later the rating stays and the
          flag is removed in a subsequent update.
    The published score only counts published ratings, and its `pending_settlement` flag says whether
    any of them is still unsettled. Without a payment_jid there is nothing to wait for and ratings
    are published as soon as they are valid.

ID-08: a driver rates a rider only for a trip the hub has marked completed. Both directions use the same
rule here: a rating counts only once the trip is registered; one that arrives earlier is parked
and applied on registration.

A rating counts only if: the sender IS the rater, the trip is registered, and the pair is
(rider of that trip -> its driver) or (its driver -> a rider of that trip). One rating per
(trip, rater, ratee). Score = (prior_weight*prior + sum) / (prior_weight + n) in [1, 5].
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from spade.behaviour import CyclicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_inform, make_message, make_template
from common.ontology import (
    Perf, Protocol, Rating, ReputationScore, SettlementComplete, TripCompleted, parse_payload,
)
from common.settings import (
    MIN_RATINGS, REPUTATION_PRIOR, REPUTATION_PRIOR_WEIGHT, T_REPUTATION_SWEEP, T_SETTLEMENT_MAX,
)
from protocols.payment_common import bare

logger = logging.getLogger(__name__)

HELD, PUBLISHED_PENDING, PUBLISHED_SETTLED = "held", "published_pending", "published_settled"


@dataclass
class Entry:
    rating: Rating
    trip: TripCompleted
    accepted_at: float
    state: str = HELD

    @property
    def published(self) -> bool:
        return self.state != HELD


Updates = List[Tuple[str, ReputationScore]]


class ReputationServer(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        rep = self.agent
        try:
            if msg.get_metadata("performative") == Perf.QUERY_IF:
                await self.answer_query(msg)
                return
            payload = parse_payload(msg, strict=True)
            sender, out = bare(msg.sender), []
            if isinstance(payload, Rating):
                out = rep.on_rating(payload, sender)
            elif isinstance(payload, TripCompleted):
                out = rep.on_trip(payload, sender)
            elif isinstance(payload, SettlementComplete):
                out = rep.on_settlement(payload, sender)
            await rep.publish(self, out)
        except (ValueError, TypeError) as e:
            logger.warning("%s: bad reputation message from %s: %s", rep.jid, msg.sender, e)
        except Exception:
            logger.exception("%s: error in reputation server", rep.jid)

    async def answer_query(self, msg):
        who = bare(msg.body or "")
        score = self.agent.published(who)
        if score is None:
            await self.send(make_message(str(msg.sender), Perf.REFUSE, Protocol.REPUTATION, "unpublished", who))
        else:
            await self.send(make_inform(str(msg.sender), Protocol.REPUTATION, score, who))


class DeadlineSweeper(CyclicBehaviour):
    """ID-07 (b): publish, flagged, the ratings whose T_settlement_max ran out."""

    async def run(self):
        rep = self.agent
        await asyncio.sleep(max(0.05, min(1.0, clock.to_real(rep.sweep_every))))
        try:
            await rep.publish(self, rep.sweep())
        except Exception:
            logger.exception("%s: sweep failed", rep.jid)


class ReputationAgent(BaseAgent):
    role = "reputation"

    def __init__(self, jid: str, password: str, hub_jids: Iterable[str] = (),
                 payment_jid: Optional[str] = None, prior: float = REPUTATION_PRIOR,
                 prior_weight: float = REPUTATION_PRIOR_WEIGHT, min_ratings: int = MIN_RATINGS,
                 t_settlement_max: float = T_SETTLEMENT_MAX, sweep_every: float = T_REPUTATION_SWEEP,
                 **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_jids: List[str] = [bare(j) for j in hub_jids]
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.prior, self.prior_weight, self.min_ratings = prior, prior_weight, min_ratings
        self.t_settlement_max, self.sweep_every = t_settlement_max, sweep_every
        self.trips: Dict[str, TripCompleted] = {}                    # trip_id -> registry entry
        self.settled_pools: Set[str] = set()
        self.entries: Dict[Tuple[str, str, str], Entry] = {}         # (trip, rater, ratee) -> entry
        self.parked: Dict[str, List[Rating]] = defaultdict(list)     # trip_id -> ratings seen early
        self.rejected: List[Tuple[Rating, str]] = []

    async def setup(self):
        await super().setup()
        self.add_behaviour(ReputationServer(), make_template(Protocol.REPUTATION))
        if self.payment_jid:
            self.add_behaviour(DeadlineSweeper())

    # ---- helpers ----------------------------------------------------------------------------
    @staticmethod
    def _t(now: Optional[float]) -> float:
        return now if now is not None else (clock.now() if clock.is_started() else 0.0)

    @property
    def gated(self) -> bool:
        return self.payment_jid is not None

    def _published_for(self, agent_id: str) -> List[Entry]:
        who = bare(agent_id)
        return [e for e in self.entries.values() if bare(e.rating.ratee_id) == who and e.published]

    def held_count(self, agent_id: str) -> int:
        who = bare(agent_id)
        return sum(1 for e in self.entries.values() if bare(e.rating.ratee_id) == who and not e.published)

    # ---- scores -----------------------------------------------------------------------------
    def score_of(self, agent_id: str) -> float:
        xs = [e.rating.score for e in self._published_for(agent_id)]
        return (self.prior_weight * self.prior + sum(xs)) / (self.prior_weight + len(xs))

    def count_of(self, agent_id: str) -> int:
        """Published ratings only."""
        return len(self._published_for(agent_id))

    def published(self, agent_id: str) -> Optional[ReputationScore]:
        es = self._published_for(agent_id)
        if len(es) < self.min_ratings:
            return None
        return ReputationScore(bare(agent_id), round(min(5.0, max(1.0, self.score_of(agent_id))), 4), len(es),
                               self._t(None), any(e.state == PUBLISHED_PENDING for e in es))

    def _announce(self, ratees: Iterable[str]) -> Updates:
        out: Updates = []
        for who in dict.fromkeys(bare(r) for r in ratees):
            score = self.published(who)
            if score is not None:
                out += [(to, score) for to in dict.fromkeys([who, *self.hub_jids])]
        return out

    async def publish(self, beh, updates: Updates) -> None:
        for to, score in updates:
            await beh.send(make_inform(to, Protocol.REPUTATION, score, score.agent_id))

    # ---- ID-07 state changes -----------------------------------------------------------------
    def _resolve(self, e: Entry, now: float) -> bool:
        """Move a HELD entry forward if its conditions are met. True if it became published."""
        if e.state != HELD:
            return False
        if not self.gated or e.trip.pool_id in self.settled_pools:
            e.state = PUBLISHED_SETTLED
        elif now - e.trip.completed_at >= self.t_settlement_max:
            e.state = PUBLISHED_PENDING
        else:
            return False
        return True

    def sweep(self, now: Optional[float] = None) -> Updates:
        now = self._t(now)
        changed = [e.rating.ratee_id for e in list(self.entries.values()) if self._resolve(e, now)]
        return self._announce(changed)

    def on_settlement(self, done: SettlementComplete, sender: str, now: Optional[float] = None) -> Updates:
        if not self.payment_jid or sender != self.payment_jid:
            logger.warning("%s: settlement from %s ignored", self.jid, sender)
            return []
        self.settled_pools.add(done.pool_id)
        changed = []
        for e in self.entries.values():
            if e.trip.pool_id == done.pool_id and e.state in (HELD, PUBLISHED_PENDING):
                e.state = PUBLISHED_SETTLED                  # (a), or the flag of (b) removed
                changed.append(e.rating.ratee_id)
        return self._announce(changed)

    def on_trip(self, trip: TripCompleted, sender: str, now: Optional[float] = None) -> Updates:
        if self.hub_jids and sender not in self.hub_jids:
            logger.warning("%s: trip registry from unknown hub %s ignored", self.jid, sender)
            return []
        self.trips.setdefault(trip.trip_id, trip)
        out: Updates = []
        for r in self.parked.pop(trip.trip_id, []):
            out += self.on_rating(r, bare(r.rater_id), now)
        return out

    def on_rating(self, rating: Rating, sender: str, now: Optional[float] = None) -> Updates:
        """-> [(recipient, ReputationScore)] to publish now (usually empty: the rating is held)."""
        now = self._t(now)
        rater, ratee = bare(rating.rater_id), bare(rating.ratee_id)
        if sender != rater:                                        # nobody rates in someone else's name
            self.rejected.append((rating, "not_authorised"))
            return []
        trip = self.trips.get(rating.trip_id)
        if trip is None:
            self.parked[rating.trip_id].append(rating)
            return []
        driver, riders = bare(trip.driver_id), [bare(r) for r in trip.rider_ids]
        if not ((rater in riders and ratee == driver) or (rater == driver and ratee in riders)):
            self.rejected.append((rating, "not_a_participant"))
            return []
        key = (rating.trip_id, rater, ratee)
        if key in self.entries:
            return []                                              # idempotent
        entry = Entry(rating, trip, now)
        self.entries[key] = entry
        if self._resolve(entry, now):
            return self._announce([ratee])
        logger.info("[reputation] rating of %s held until settlement of %s", ratee, trip.pool_id)
        return []

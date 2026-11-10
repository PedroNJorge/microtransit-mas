"""
src/agents/reputation.py   ReputationAgent (Role: Reputation, Sub-org: Regulatory). Milestone 7 core.

ReputationProtocol, as implemented now
    Rider  --inform(Rating)-------------> Reputation   rate_driver (sender must BE the rater)
    Hub    --inform(TripCompleted)------> Reputation   trip registry: who drove whom (trusted hubs only)
    Hub    --query-if(<driver jid>)-----> Reputation   -> inform(ReputationScore) | refuse(unpublished)
    Reputation --inform(ReputationScore)-> ratee, every subscribed hub          reputation_update

A rating counts only if it matches a registered trip: the rater was a rider of that trip and the
ratee its driver. A rating that arrives before the hub's registry message is parked and applied
when the trip is registered (the rider can be faster than the hub). Self-rating is impossible
(Rating refuses it), one rating per (trip, rater) is kept (repeats are ignored: idempotent).

Score = (prior_weight * prior + sum(scores)) / (prior_weight + n), in [1, 5]: a driver with a
single 5 is not a 5. Published only after `min_ratings` ratings (GC-07).

NOT yet (Milestone 8): the ID-07 gate (accept a rating at once but publish it only after Payment
confirms settlement, or after T_settlement_max with a `pending_settlement` flag), and ratings
of riders by drivers (ID-08). Today a rating is published as soon as it is valid.
"""
import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from spade.behaviour import CyclicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_inform, make_message, make_template
from common.ontology import Perf, Protocol, Rating, ReputationScore, TripCompleted, parse_payload
from common.settings import MIN_RATINGS, REPUTATION_PRIOR, REPUTATION_PRIOR_WEIGHT
from protocols.payment_common import bare

logger = logging.getLogger(__name__)


class ReputationServer(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        rep = self.agent
        try:
            perf = msg.get_metadata("performative")
            if perf == Perf.QUERY_IF:
                await self.answer_query(msg)
                return
            payload = parse_payload(msg, strict=True)
            out = []
            if isinstance(payload, Rating):
                out = rep.on_rating(payload, bare(msg.sender))
            elif isinstance(payload, TripCompleted):
                out = rep.on_trip(payload, bare(msg.sender))
            for to, score in out:
                await self.send(make_inform(to, Protocol.REPUTATION, score, score.agent_id))
        except (ValueError, TypeError) as e:
            logger.warning("%s: bad reputation message from %s: %s", rep.jid, msg.sender, e)
        except Exception:
            logger.exception("%s: error in reputation server", rep.jid)

    async def answer_query(self, msg):
        rep, who = self.agent, bare(msg.body or "")
        score = rep.published(who)
        if score is None:
            await self.send(make_message(str(msg.sender), Perf.REFUSE, Protocol.REPUTATION,
                                         "unpublished", who))
        else:
            await self.send(make_inform(str(msg.sender), Protocol.REPUTATION, score, who))


class ReputationAgent(BaseAgent):
    role = "reputation"

    def __init__(self, jid: str, password: str, hub_jids: Iterable[str] = (),
                 prior: float = REPUTATION_PRIOR, prior_weight: float = REPUTATION_PRIOR_WEIGHT,
                 min_ratings: int = MIN_RATINGS, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_jids: List[str] = [bare(j) for j in hub_jids]
        self.prior, self.prior_weight, self.min_ratings = prior, prior_weight, min_ratings
        self.trips: Dict[str, TripCompleted] = {}                    # trip_id -> registry entry
        self.ratings: Dict[Tuple[str, str], Rating] = {}             # (trip_id, rater) -> rating
        self.parked: Dict[str, List[Rating]] = defaultdict(list)     # trip_id -> ratings seen early
        self.rejected: List[Tuple[Rating, str]] = []
        self._by_ratee: Dict[str, List[int]] = defaultdict(list)

    async def setup(self):
        await super().setup()
        self.add_behaviour(ReputationServer(), make_template(Protocol.REPUTATION))

    # ---- pure-ish logic (unit-testable without XMPP) -------------------------------------
    def score_of(self, agent_id: str) -> float:
        xs = self._by_ratee.get(bare(agent_id), [])
        return (self.prior_weight * self.prior + sum(xs)) / (self.prior_weight + len(xs))

    def count_of(self, agent_id: str) -> int:
        return len(self._by_ratee.get(bare(agent_id), []))

    def published(self, agent_id: str) -> Optional[ReputationScore]:
        n = self.count_of(agent_id)
        if n < self.min_ratings:
            return None
        return ReputationScore(bare(agent_id), round(min(5.0, max(1.0, self.score_of(agent_id))), 4),
                               n, clock.now() if clock.is_started() else 0.0)

    def on_trip(self, trip: TripCompleted, sender: str):
        if self.hub_jids and sender not in self.hub_jids:
            logger.warning("%s: trip registry from unknown hub %s ignored", self.jid, sender)
            return []
        self.trips.setdefault(trip.trip_id, trip)
        out = []
        for r in self.parked.pop(trip.trip_id, []):
            out += self.on_rating(r, bare(r.rater_id))
        return out

    def on_rating(self, rating: Rating, sender: str):
        """-> [(recipient, ReputationScore)] to publish."""
        rater, ratee = bare(rating.rater_id), bare(rating.ratee_id)
        if sender != rater:                                        # nobody rates in someone else's name
            self.rejected.append((rating, "not_authorised"))
            return []
        trip = self.trips.get(rating.trip_id)
        if trip is None:
            self.parked[rating.trip_id].append(rating)
            return []
        if ratee != bare(trip.driver_id) or rater not in [bare(r) for r in trip.rider_ids]:
            self.rejected.append((rating, "not_a_participant"))
            return []
        key = (rating.trip_id, rater)
        if key in self.ratings:
            return []                                              # idempotent
        self.ratings[key] = rating
        self._by_ratee[ratee].append(rating.score)
        score = self.published(ratee)
        if score is None:
            return []
        logger.info("[reputation] %s now %.2f (%d ratings)", ratee, score.score, score.num_ratings)
        return [(to, score) for to in dict.fromkeys([ratee, *self.hub_jids])]

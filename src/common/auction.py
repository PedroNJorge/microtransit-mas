"""
src/common/auction.py
Bid evaluation for the MatchAuctionProtocol. Pure Python (no SPADE, no clock, no randomness)
so it is fast to unit-test and the winner is a deterministic function of the inputs.

The mechanism: a multi-attribute, single-item REVERSE auction in money units
--------------------------------------------------------------------------------------
For a bid i, with price p_i and the attributes the hub itself controls or verifies:

    penalty_i = vot * eta_i + vod * detour_i + vor * (R_max - rep_i)        [EUR]
    cost_i    = p_i + penalty_i                                             [EUR, lower is better]

vot / vod / vor are the rider-side values of waiting time, detour time and reputation, in EUR
per unit (AuctionRules). Because everything is in EUR the score is QUASI-LINEAR in price, which
is exactly what the payment rules below need. (A normalised weighted sum of unitless scores
would rank bids fine but could not be turned into a payment.)

The winner is the bid with the lowest cost_i. Payment to the winner w:

  first_price    pay p_w.                           Simple; drivers can gain by shading.
  second_score   pay min(reserve, p_w + (cost_2 - cost_w)),   cost_2 = best losing cost_i.
                 i.e. the HIGHEST price at which w would still have won ("threshold price").
                 With no competing valid bid, pay the reserve (the riders' max fare).

second_score is the VCG mechanism for one item (a Vickrey / "second-score" auction, Che 1993):
  * Individual rationality: pay >= p_w, because cost_2 >= cost_w. A winner never earns less
    than it bid, so with a truthful bid (p_w = true cost c_w) its profit is >= 0.
  * Dominant-strategy incentive compatibility: the payment does not depend on the winner's own
    price, only on the others. Bidding p_i = c_i is a best reply to ANY bids of the others
    (proof in docs/auction_mechanism.md).
Both properties are also checked numerically in tests/test_auction_eval.py.

What the proof needs (and what the code enforces)
  * Only the price is private information. ETA, detour and reputation are inputs the hub must
    verify itself; this module treats whatever it is given as true. In Milestone 5 the stub
    driver's ETA is still self-reported; Milestone 6 computes it from DriverStatus + the graph.
  * One pool per auction; a driver holds at most one provisional bid (CC-01), so there are no
    cross-auction effects on a driver's cost.
  * Reserve = CFP.max_total_fare is a posted price announced before bidding.

Rounding: prices are handled in cents. A bid is rounded UP to the cent, the reserve DOWN, and
the payment is clamped to [ceil(bid), floor(reserve)], so rounding never breaks IR; it can
change a deviation's gain by at most one cent.
"""
import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from common.ontology import CFP, Bid
from common.settings import (
    AUCTION_PRICING, VALUE_OF_DETOUR, VALUE_OF_REPUTATION, VALUE_OF_TIME)

FIRST_PRICE = "first_price"
SECOND_SCORE = "second_score"
PRICING_RULES = (FIRST_PRICE, SECOND_SCORE)

EPS = 1e-9


class BidReject:
    """Why a bid is not valid (becomes the log line; the driver just gets reject-proposal)."""
    WRONG_POOL = "wrong_pool"
    STALE_ATTEMPT = "stale_attempt"
    BAD_NUMBERS = "bad_numbers"
    OVER_RESERVE = "over_reserve"
    NO_CAPACITY = "no_capacity"
    DETOUR_TOO_LARGE = "detour_too_large"
    TOO_LATE = "too_late"
    DUPLICATE = "duplicate"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class AuctionRules:
    pricing: str = AUCTION_PRICING
    value_of_time: float = VALUE_OF_TIME            # EUR per second of waiting for the pickup
    value_of_detour: float = VALUE_OF_DETOUR        # EUR per second of detour for the riders
    value_of_reputation: float = VALUE_OF_REPUTATION  # EUR per reputation point below the max
    max_reputation: float = 5.0
    default_reputation: float = 3.0                 # used when a bid carries no reputation
    quantize: bool = True                           # payments in whole cents (False: exact maths)

    def __post_init__(self):
        if self.pricing not in PRICING_RULES:
            raise ValueError(f"pricing must be one of {PRICING_RULES}, got {self.pricing!r}")
        for name in ("value_of_time", "value_of_detour", "value_of_reputation"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} must be a finite number >= 0 (got {v!r})")
        if not 1.0 <= self.default_reputation <= self.max_reputation:
            raise ValueError("default_reputation must lie in [1, max_reputation]")


DEFAULT_RULES = AuctionRules()


@dataclass(frozen=True)
class ScoredBid:
    bid: Bid
    reputation: float          # the value actually used (bid.reputation or the default)
    penalty: float             # EUR: value of waiting + detour + reputation shortfall
    cost: float                # price + penalty; the auction minimises this

    @property
    def key(self) -> tuple:
        """Total order used to rank bids: deterministic whatever the arrival order."""
        return (round(self.cost, 9), self.bid.price, self.bid.eta, self.bid.driver_id)


@dataclass
class AuctionResult:
    pool_id: str
    rules: AuctionRules
    reserve: float
    ranking: List[ScoredBid] = field(default_factory=list)          # valid bids, best first
    rejected: List[Tuple[Bid, str]] = field(default_factory=list)   # (bid, BidReject.*)
    price: Optional[float] = None                                   # what the winner is paid

    @property
    def winner(self) -> Optional[ScoredBid]:
        return self.ranking[0] if self.ranking else None

    @property
    def runner_up(self) -> Optional[ScoredBid]:
        return self.ranking[1] if len(self.ranking) > 1 else None

    @property
    def winner_bid(self) -> Optional[Bid]:
        return self.winner.bid if self.winner else None

    def payment_to(self, driver_id: str) -> float:
        """Payment a driver receives from this result (0 if it did not win)."""
        w = self.winner
        return self.price if (w is not None and w.bid.driver_id == driver_id) else 0.0


# ---- helpers -----------------------------------------------------------------------------------

def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def ceil_cents(x: float) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value(rounding=ROUND_CEILING))


def floor_cents(x: float) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value(rounding=ROUND_FLOOR))


def round_cents(x: float) -> int:
    return int((Decimal(str(x)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def reputation_of(bid: Bid, rules: AuctionRules) -> float:
    r = bid.reputation
    if not _finite(r):
        return rules.default_reputation
    return min(max(float(r), 1.0), rules.max_reputation)


def score_bid(bid: Bid, rules: AuctionRules = DEFAULT_RULES) -> ScoredBid:
    rep = reputation_of(bid, rules)
    penalty = (rules.value_of_time * bid.eta + rules.value_of_detour * bid.max_detour
               + rules.value_of_reputation * (rules.max_reputation - rep))
    return ScoredBid(bid, rep, penalty, bid.price + penalty)


def check_bid(cfp: CFP, bid: Bid, now: float, rules: AuctionRules = DEFAULT_RULES) -> Optional[str]:
    """None if the bid is valid for this CFP, else a BidReject reason."""
    if bid.pool_id != cfp.pool_id:
        return BidReject.WRONG_POOL
    if bid.attempt != cfp.attempt:
        return BidReject.STALE_ATTEMPT
    if not (_finite(bid.price) and _finite(bid.eta) and _finite(bid.max_detour)
            and bid.price >= 0 and bid.eta >= 0 and bid.max_detour >= 0):
        return BidReject.BAD_NUMBERS
    over = (ceil_cents(bid.price) > floor_cents(cfp.max_total_fare)) if rules.quantize \
        else bid.price > cfp.max_total_fare + EPS
    if over:
        return BidReject.OVER_RESERVE
    if bid.capacity_available < cfp.passenger_count:
        return BidReject.NO_CAPACITY
    if bid.max_detour > cfp.max_detour + EPS:
        return BidReject.DETOUR_TOO_LARGE
    arrival = now + bid.eta
    if any(arrival > w[1] for w in cfp.rider_time_windows.values() if len(w) == 2):
        return BidReject.TOO_LATE                        # GC-05: never outside a rider's window
    return None


def _payment(rules: AuctionRules, reserve: float, ranking: Sequence[ScoredBid]) -> float:
    w = ranking[0]
    if rules.pricing == FIRST_PRICE:
        raw = w.bid.price
    elif len(ranking) > 1:
        raw = w.bid.price + (ranking[1].cost - w.cost)       # threshold price
    else:
        raw = reserve                                         # no competition: the posted reserve
    raw = min(raw, reserve)
    if not rules.quantize:
        return max(raw, w.bid.price)
    cents_ = min(max(round_cents(raw), ceil_cents(w.bid.price)), floor_cents(reserve))
    return cents_ / 100


def evaluate_bids(cfp: CFP, bids: Iterable[Bid], rules: AuctionRules = DEFAULT_RULES,
                  now: float = 0.0, excluded: Iterable[str] = ()) -> AuctionResult:
    """Validate and rank `bids` and price the winner.

    now       sim time of the evaluation; a bid is late if now + eta passes a rider's window end.
    excluded  driver ids that must not win (e.g. a winner that failed to confirm the award):
              they are ignored completely, also as the competitor that sets the price.
    Deterministic: the result does not depend on the order of `bids`.
    """
    excluded = set(excluded)
    result = AuctionResult(cfp.pool_id, rules, cfp.max_total_fare)
    seen = set()
    valid: List[ScoredBid] = []
    for bid in sorted(bids, key=lambda b: (b.driver_id, b.bid_id)):    # stable duplicate handling
        if bid.driver_id in excluded:
            result.rejected.append((bid, BidReject.EXCLUDED))
        elif bid.driver_id in seen:
            result.rejected.append((bid, BidReject.DUPLICATE))
        else:
            seen.add(bid.driver_id)
            reason = check_bid(cfp, bid, now, rules)
            if reason:
                result.rejected.append((bid, reason))
            else:
                valid.append(score_bid(bid, rules))
    result.ranking = sorted(valid, key=lambda s: s.key)
    if result.ranking:
        result.price = _payment(rules, cfp.max_total_fare, result.ranking)
    return result


def split_fare(final_fare: float, rider_ids: Sequence[str]) -> Dict[str, float]:
    """Equal split in whole cents that sums EXACTLY to final_fare (MatchResult invariant).
    Leftover cents go to the riders first in sorted order, so it is deterministic."""
    riders = sorted(rider_ids)
    if not riders:
        return {}
    total = round_cents(final_fare)
    base, extra = divmod(total, len(riders))
    return {r: (base + (1 if i < extra else 0)) / 100 for i, r in enumerate(riders)}

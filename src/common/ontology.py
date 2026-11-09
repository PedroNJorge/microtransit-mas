"""
common/ontology.py
Shared ontology for the Microtransit MAS (Gaia Detailed Design).

Concepts are Python dataclasses serialised as JSON inside FIPA-ACL message bodies.
The `ontology` metadata field of each message carries the concept name, and
`parse_payload()` uses a registry to rebuild the right class on the receiving side.
"""
import json
import logging
import math
import types
import uuid
from dataclasses import dataclass, asdict, field, is_dataclass
from enum import Enum
from typing import (Any, Dict, List, Optional, Type, TypeVar, Union,
                    get_args, get_origin, get_type_hints)

from spade.message import Message

T = TypeVar("T")
logger = logging.getLogger(__name__)

# ==========================================
# 0. Registry, constants, enums
# ==========================================

ONTOLOGY: Dict[str, type] = {}


def concept(cls):
    """Class decorator: register a dataclass under its class name."""
    ONTOLOGY[cls.__name__] = cls
    return cls


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Protocol:
    """Values for the `protocol` metadata field."""
    TRIP_REQUEST = "TripRequestProtocol"
    POOL_FORMATION = "PoolFormationProtocol"
    MATCH_AUCTION = "MatchAuctionProtocol"          # FIPA contract-net
    TRIP_EXECUTION = "TripExecutionProtocol"
    REPOSITION = "RepositionProtocol"
    DRIVER_STATUS = "DriverStatusProtocol"
    CANCELLATION = "CancellationProtocol"
    DRIVER_FAILURE = "DriverFailureProtocol"
    ESCROW = "EscrowProtocol"
    SETTLEMENT = "SettlementProtocol"
    REFUND = "RefundProtocol"
    REPUTATION = "ReputationProtocol"
    POLICY = "PolicyProtocol"
    AUDIT = "AuditProtocol"
    TRAFFIC_UPDATE = "TrafficUpdateProtocol"
    DISRUPTION = "DisruptionProtocol"
    INTER_HUB = "InterHubCoordinationProtocol"
    DEMAND_FORECAST = "DemandForecastProtocol"
    HEARTBEAT = "HeartbeatProtocol"                 # ping/pong liveness check (every agent)


class Perf:
    """FIPA-ACL performatives used in this system."""
    REQUEST = "request"
    CFP = "cfp"
    PROPOSE = "propose"
    REFUSE = "refuse"
    ACCEPT = "accept-proposal"
    REJECT = "reject-proposal"
    INFORM = "inform"
    CONFIRM = "confirm"
    FAILURE = "failure"      # protocol-level failure ONLY (parse/internal error).
                             # Business failures use REFUSE + reason or INFORM(TripFailed).
    CANCEL = "cancel"        # cancels an in-flight protocol
    QUERY_IF = "query-if"


class EscrowStatus(str, Enum):
    HELD = "HELD"
    SETTLED = "SETTLED"
    REFUNDED = "REFUNDED"


class DriverState(str, Enum):
    """Driver-side state. Transitions (BIDDING->IDLE on reject/T_provisional expiry,
    BIDDING->COMMITTED on accept-proposal) belong to the Agent Model's Driver FSM
    section (CC-01..CC-03); they are not enforced here."""
    IDLE = "IDLE"
    BIDDING = "BIDDING"            # provisional bid outstanding
    COMMITTED = "COMMITTED"        # CC-03
    EN_ROUTE_PICKUP = "EN_ROUTE_PICKUP"
    ON_TRIP = "ON_TRIP"
    REPOSITIONING = "REPOSITIONING"
    OFFLINE = "OFFLINE"


class PoolStatus(str, Enum):
    FORMING = "FORMING"
    LOCKED = "LOCKED"
    AUCTIONING = "AUCTIONING"
    ASSIGNED = "ASSIGNED"
    DISSOLVED = "DISSOLVED"
    COMPLETED = "COMPLETED"


class DisruptionType(str, Enum):
    ACCIDENT = "ACCIDENT"
    ROAD_CLOSURE = "ROAD_CLOSURE"
    WEATHER = "WEATHER"


class FailureReason(str, Enum):
    RIDER_CANCELLED = "RIDER_CANCELLED"
    DRIVER_FAILED = "DRIVER_FAILED"
    NO_MATCH = "NO_MATCH"
    HUB_FAILED = "HUB_FAILED"
    TIMEOUT = "TIMEOUT"
    OUTBID = "OUTBID"          # losing bidder in a successful auction
    NO_BIDS = "NO_BIDS"        # auction closed with zero valid bids


# ==========================================
# 1. Domain Concepts (Entities)
# ==========================================

@concept
@dataclass
class MicrohubNode:
    id: str
    coverage_radius: float
    capacity: int


@concept
@dataclass
class StagingPoint:
    """Passive environmental object (managed by Environment, used by hubs)."""
    id: str
    location: str                       # node id or "x,y"
    capacity: int
    covered_hubs: List[str] = field(default_factory=list)
    occupancy: int = 0


@concept
@dataclass
class TripRequest:
    request_id: str
    rider_id: str
    origin_node: str
    destination_home: str
    time_window_start: float
    time_window_end: float
    detour_tolerance: float             # SECONDS of extra ride time accepted vs a direct trip
    max_fare: float
    pooling_willingness: bool
    subsidy_eligibility: bool
    created_at: float = 0.0

    def __post_init__(self):
        if self.time_window_end < self.time_window_start:
            raise ValueError("time_window_end must be >= time_window_start")
        if self.max_fare < 0 or self.detour_tolerance < 0:
            raise ValueError("max_fare and detour_tolerance must be >= 0")


@concept
@dataclass
class Pool:
    """Locked group of compatible requests (returned by MicrohubAgent.form_pool)."""
    pool_id: str
    hub_id: str
    requests: List[TripRequest]
    status: PoolStatus = PoolStatus.FORMING
    created_at: float = 0.0

    @property
    def size(self) -> int:
        return len(self.requests)


@concept
@dataclass
class Policy:
    policy_id: str
    version: int                        # monotonically increasing; detects conflicts
    effective_from: float
    fare_caps: Dict[str, float]         # e.g. {"base": 2.0, "per_km": 0.8, "max": 25.0}
    equity_zones: List[str]
    subsidy_rules: Dict[str, float]     # e.g. {"eligible_discount": 0.5}
    min_service_level: Dict[str, float] = field(default_factory=dict)  # zone -> min share


@concept
@dataclass
class DriverStatus:
    """Registered with the DF and sent via DriverStatusProtocol."""
    driver_id: str
    location: str
    state: DriverState
    max_capacity: int
    current_passengers: int
    shift_end: float
    reserve_price: float

    def __post_init__(self):
        if self.current_passengers > self.max_capacity:
            raise ValueError("current_passengers exceeds max_capacity")


@concept
@dataclass
class ReputationScore:
    """Published by ReputationAgent via inform(ReputationProtocol)."""
    agent_id: str
    score: float                        # bounded [1, 5]
    num_ratings: int                    # evidence count (publish only above threshold)
    updated_at: float = 0.0

    def __post_init__(self):
        if not 1 <= self.score <= 5:
            raise ValueError("score must be in [1, 5]")
        if self.num_ratings < 0:
            raise ValueError("num_ratings must be >= 0")


# ==========================================
# 2. Interaction Concepts (Messages)
# ==========================================

# --- Pool formation / auction -------------------------------------------

@concept
@dataclass
class PoolProposal:
    """Hub -> each Rider in the pool."""
    pool_id: str
    request_id: str
    shared_route: List[str]
    split_fare: float
    estimated_wait: float
    added_detour: float = 0.0           # seconds of extra ride time for this rider


@concept
@dataclass
class CFP:
    """Hub -> Drivers."""
    pool_id: str
    hub_id: str
    pickup_node: str
    rider_destinations: List[str]
    rider_time_windows: Dict[str, List[float]]   # request_id -> [start, end]
    passenger_count: int
    auction_deadline: float
    max_total_fare: float                        # min(rider max_fares, policy cap)
    max_detour: float = 0.0             # seconds
    attempt: int = 0
    constraints: Dict[str, float] = field(default_factory=dict)


@concept
@dataclass
class Bid:
    bid_id: str
    driver_id: str
    pool_id: str
    price: float
    eta: float
    max_detour: float                   # seconds
    capacity_available: int
    attempt: int = 0                    # auction retry number (FE-01); same pool_id on retry
    provisional: bool = True            # wire-level always True; becomes firm only on
                                        # accept-proposal (driver-side state, CC-03)
    # Filled in by the HUB from ReputationAgent; never trust a driver's own claim.
    reputation: Optional[float] = None

    def __post_init__(self):
        if self.price < 0:
            raise ValueError("price must be >= 0")


@concept
@dataclass
class MatchResult:
    """Hub -> winning Driver and each Rider.

    Fare invariant: final_fare is the agreed auction price (the driver's guaranteed
    minimum payout). rider_fares are PRE-subsidy shares and must sum to final_fare.
    Subsidies are applied by Payment on top (Settlement.subsidy_applied) and never
    reduce the driver's payout.
    """
    pool_id: str
    driver_id: str
    final_fare: float
    pickup_eta: float
    rider_fares: Dict[str, float] = field(default_factory=dict)  # rider_id -> share

    def __post_init__(self):
        if self.rider_fares and not math.isclose(
                sum(self.rider_fares.values()), self.final_fare, abs_tol=0.01):
            raise ValueError("sum(rider_fares) must equal final_fare")


@concept
@dataclass
class AuctionOutcome:
    """Hub -> losing bidders (reject-proposal) and riders on failed auctions."""
    pool_id: str
    awarded: bool
    winner_id: Optional[str] = None
    reason: Optional[FailureReason] = None


# --- Trip execution / failure -------------------------------------------

@concept
@dataclass
class TripCompleted:
    """Driver -> Payment/Hub. Payment finds escrows via its own pool_id -> [escrow_id]
    index, built from EscrowRequest.pool_id (so no escrow_ids field here)."""
    trip_id: str
    pool_id: str
    driver_id: str
    rider_ids: List[str]
    completed_at: float
    distance_km: float = 0.0


# TripExecutionProtocol: per-rider granularity (pooled riders board/alight separately).
# TripCompleted (below) is the single pool-level message that triggers settlement.

@concept
@dataclass
class DriverArriving:
    """Driver -> Rider: inform(arriving_at_pickup, eta)."""
    request_id: str
    pool_id: str
    driver_id: str
    eta: float


@concept
@dataclass
class RiderReady:
    """Rider -> Driver: inform(ready_for_pickup)."""
    request_id: str
    pool_id: str
    ready_at: float


@concept
@dataclass
class RiderBoarded:
    """Driver -> Rider: inform(picked_up)."""
    request_id: str
    pool_id: str
    driver_id: str
    boarded_at: float


@concept
@dataclass
class RiderDroppedOff:
    """Driver -> Rider: inform(dropped_off)."""
    request_id: str
    pool_id: str
    driver_id: str
    dropped_at: float


@concept
@dataclass
class TripFailed:
    """Cancellation, driver failure, no-match, hub failure."""
    pool_id: str
    reason: FailureReason
    failed_agent_id: Optional[str] = None
    escrow_ids: List[str] = field(default_factory=list)


@concept
@dataclass
class RepositionRequest:
    hub_id: str
    driver_id: str
    staging_point_id: str
    incentive: float = 0.0


# --- Financial -------------------------------------------------------------

@concept
@dataclass
class EscrowRequest:
    request_id: str
    rider_id: str
    pool_id: str
    amount: float
    subsidy_eligible: bool = False


@concept
@dataclass
class Escrow:
    """Payment -> Rider AND Hub. status=HELD doubles as `escrow_confirmed` (ID-02)."""
    escrow_id: str
    rider_id: str
    amount: float
    status: EscrowStatus
    pool_id: Optional[str] = None


@concept
@dataclass
class Settlement:
    """driver_payout must be >= MatchResult.final_fare (never pay below auction price)."""
    escrow_id: str
    driver_id: str
    driver_payout: float
    subsidy_applied: float = 0.0
    platform_fee: float = 0.0           # MVP: always 0. If non-zero it is recorded but not
                                        # disbursed (no Platform/Treasury agent yet).


@concept
@dataclass
class Refund:
    escrow_id: str
    rider_id: str
    amount: float
    reason: FailureReason


# --- Reputation --------------------------------------------------------------

@concept
@dataclass
class Rating:
    trip_id: str
    rater_id: str
    ratee_id: str
    score: int
    comments: Optional[str] = None

    def __post_init__(self):
        if not 1 <= self.score <= 5:
            raise ValueError("score must be in [1, 5]")
        if self.rater_id == self.ratee_id:
            raise ValueError("self-rating is not allowed")


# --- Environment -----------------------------------------------------------

@concept
@dataclass
class TrafficUpdate:
    """Base travel times of road edges. Receivers apply it by OVERWRITING the listed edges.

    full_snapshot=True  : edge_weights lists EVERY edge (a receiver may replace its state).
    full_snapshot=False : DELTA. edge_weights lists only edges whose base time changed since
                          the previous update (possibly empty); a receiver that has not yet
                          seen a full snapshot must wait for one (the Environment sends one
                          on first contact and every `full_every` updates).
    Edge keys are the network's wire ids ("a|b"); the network class defines the convention.

    Disruptions are an overlay and independent of these updates:
    effective_time = edge_weights[e] * severity (or blocked if closed), persisting until
    Disruption.end_time regardless of later updates.
    """
    update_id: str
    timestamp: float
    edge_weights: Dict[str, float]      # "nodeA|nodeB" -> travel time (s)
    full_snapshot: bool = True


@concept
@dataclass
class Disruption:
    disruption_id: str
    type: DisruptionType
    affected_edges: List[str]
    start_time: float
    end_time: float
    severity: float = 1.0               # multiplier on edge weight
    closed: bool = False                # True = impassable (avoids inf in JSON)


# --- Regulation ------------------------------------------------------------

@concept
@dataclass
class AuditReport:
    hub_id: str
    period_start: float
    period_end: float
    zone_service_share: Dict[str, float]
    fare_violations: int
    compliant: bool


@concept
@dataclass
class Penalty:
    target_id: str
    reason: str
    amount: float


# --- Heartbeat (every agent answers) --------------------------------------

@concept
@dataclass
class Ping:
    """Anyone -> Agent: request(Ping). thread == ping_id."""
    ping_id: str
    sent_at: float                      # sim time


@concept
@dataclass
class Pong:
    """Agent -> sender: inform(Pong), same thread. Proves the agent is up and its role."""
    ping_id: str
    agent_id: str                       # the agent's JID
    role: str                           # rider, driver, microhub, payment, ...
    ping_sent_at: float                 # echoed from Ping.sent_at
    replied_at: float                   # sim time


# --- Inter-hub -------------------------------------------------------------

@concept
@dataclass
class OverflowRequest:
    from_hub: str
    reason: FailureReason               # e.g. HUB_FAILED or capacity overload
    pending_requests: List[TripRequest]


@concept
@dataclass
class DemandForecast:                   # optional
    hub_id: str
    horizon_start: float
    horizon_end: float
    expected_requests: float


# ==========================================
# 3. Serialization
# ==========================================

def serialize(obj) -> str:
    """Dataclass instance -> JSON string (enums become their values)."""
    return json.dumps(asdict(obj), sort_keys=True)   # deterministic for LOG hashing


def _convert(tp, value, strict: bool = False):
    if tp is Any:
        return value
    origin = get_origin(tp)
    union_types = (Union, getattr(types, "UnionType", Union))
    is_optional = origin in union_types and type(None) in get_args(tp)
    if value is None:
        if is_optional or tp is Any:
            return None
        raise ValueError(f"None passed for non-Optional type {tp}")
    if origin in union_types:
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _convert(inner[0], value, strict)
    if origin in (list, List):
        (item_tp,) = get_args(tp)
        return [_convert(item_tp, v, strict) for v in value]
    if origin in (dict, Dict):
        _, val_tp = get_args(tp)
        return {k: _convert(val_tp, v, strict) for k, v in value.items()}
    if isinstance(tp, type) and is_dataclass(tp):
        return from_dict(tp, value, strict)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if tp in (int, float) and isinstance(value, bool):
        raise ValueError("bool passed for numeric field")
    if tp is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def from_dict(cls: Type[T], data: Dict[str, Any], strict: bool = False) -> T:
    """Recursively rebuild a dataclass. strict=True rejects unknown fields (use in tests);
    strict=False warns and drops them (MVP default; unknown fields usually mean version drift)."""
    hints = get_type_hints(cls)
    unknown = set(data) - set(hints)
    if unknown:
        if strict:
            raise ValueError(f"Unknown fields for {cls.__name__}: {unknown}")
        logger.warning("Dropping unknown fields for %s: %s", cls.__name__, unknown)
    kwargs = {k: _convert(hints[k], v, strict) for k, v in data.items() if k in hints}
    return cls(**kwargs)


def deserialize(json_str: str, cls: Type[T], strict: bool = False) -> T:
    return from_dict(cls, json.loads(json_str), strict)


# ==========================================
# 4. FIPA-ACL helpers (SPADE)
# ==========================================

def create_message(
    to: str,
    performative: str,
    protocol: str,
    ontology_concept: str,
    payload=None,
    thread: Optional[str] = None,
) -> Message:
    """
    Build a SPADE message.
    - thread: conversation id (use pool_id / request_id) so FSMs can correlate
      CFP, bids and award for one auction.
    - ontology_concept: must match a registered concept name (or be "" if no payload).
    """
    msg = Message(to=to)
    msg.set_metadata("performative", performative)
    msg.set_metadata("protocol", protocol)
    msg.set_metadata("ontology", ontology_concept)
    if thread:
        msg.thread = thread
    if payload is not None:
        msg.body = payload if isinstance(payload, str) else serialize(payload)
    return msg


def parse_message(msg: Message):
    """Returns (performative, protocol, ontology_concept, raw_body)."""
    return (msg.get_metadata("performative"),
            msg.get_metadata("protocol"),
            msg.get_metadata("ontology"),
            msg.body)


def parse_payload(msg: Message, strict: bool = False):
    """Decode the body into the dataclass named in the `ontology` metadata."""
    name = msg.get_metadata("ontology")
    if not name or not msg.body:
        return None
    cls = ONTOLOGY.get(name)
    if cls is None:
        raise ValueError(f"Unknown ontology concept: {name}")
    return deserialize(msg.body, cls, strict)

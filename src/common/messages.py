"""
common/messages.py
Thin, typed constructors over ontology.create_message so agents never type
protocol / performative / ontology strings by hand.

Conventions
- The `thread` of every auction message is the pool_id, so FSM behaviours can
  correlate CFP, bids, award and outcome (see make_template).
- The `ontology` metadata is derived from the payload's class name and checked
  against the registry; a payload that is not a registered concept raises.
"""
from typing import Optional

from spade.message import Message
from spade.template import Template

from common.ontology import (
    ONTOLOGY, Perf, Protocol, create_message,
    CFP, Bid, MatchResult, AuctionOutcome, FailureReason, PoolProposal,
)


def concept_name(payload) -> str:
    """Registered ontology name of a payload; '' for no payload / plain text."""
    if payload is None or isinstance(payload, str):
        return ""
    name = type(payload).__name__
    if name not in ONTOLOGY:
        raise ValueError(f"{name} is not a registered ontology concept")
    return name


# ------------------------------------------------------------------
# Generic constructors
# ------------------------------------------------------------------

def make_message(to: str, performative: str, protocol: str, payload=None,
                 thread: Optional[str] = None) -> Message:
    return create_message(to, performative, protocol, concept_name(payload),
                          payload, thread)


def make_inform(to: str, protocol: str, payload, thread: Optional[str] = None) -> Message:
    return make_message(to, Perf.INFORM, protocol, payload, thread)


def make_request(to: str, protocol: str, payload, thread: Optional[str] = None) -> Message:
    return make_message(to, Perf.REQUEST, protocol, payload, thread)


def make_query_if(to: str, protocol: str, payload=None, thread: Optional[str] = None) -> Message:
    return make_message(to, Perf.QUERY_IF, protocol, payload, thread)


def make_reply(msg: Message, performative: str, payload=None,
               protocol: Optional[str] = None) -> Message:
    """Reply to `msg`: addressed to its sender, same thread, same protocol (unless overridden).
    Unlike Message.make_reply(), the body and metadata are NOT copied over."""
    if msg.sender is None or not str(msg.sender):   # SPADE 4 uses an empty JID when unset
        raise ValueError("Cannot reply to a message with no sender")
    return make_message(str(msg.sender), performative,
                        protocol or msg.get_metadata("protocol"),
                        payload, msg.thread)


# ------------------------------------------------------------------
# MatchAuctionProtocol (FIPA contract-net). thread == pool_id
# ------------------------------------------------------------------

def make_cfp(to: str, cfp: CFP) -> Message:
    """Hub -> Driver."""
    return make_message(to, Perf.CFP, Protocol.MATCH_AUCTION, cfp, thread=cfp.pool_id)


def make_propose(to: str, bid: Bid) -> Message:
    """Driver -> Hub."""
    return make_message(to, Perf.PROPOSE, Protocol.MATCH_AUCTION, bid, thread=bid.pool_id)


def make_refuse(to: str, pool_id: str, reason: str = "") -> Message:
    """Driver -> Hub. Body is a plain-text reason (e.g. 'locked', 'no_capacity')."""
    return make_message(to, Perf.REFUSE, Protocol.MATCH_AUCTION, reason or None, thread=pool_id)


def make_accept_proposal(to: str, match: MatchResult) -> Message:
    """Hub -> winning Driver (and Riders, as match notification)."""
    return make_message(to, Perf.ACCEPT, Protocol.MATCH_AUCTION, match, thread=match.pool_id)


def make_reject_proposal(to: str, outcome: AuctionOutcome) -> Message:
    """Hub -> losing bidders (outcome.reason typically OUTBID)."""
    return make_message(to, Perf.REJECT, Protocol.MATCH_AUCTION, outcome, thread=outcome.pool_id)


def make_auction_failed(to: str, pool_id: str,
                        reason: FailureReason = FailureReason.NO_BIDS) -> Message:
    """Hub -> Rider (or bidders) when an auction concludes without an award."""
    outcome = AuctionOutcome(pool_id=pool_id, awarded=False, reason=reason)
    return make_message(to, Perf.INFORM, Protocol.MATCH_AUCTION, outcome, thread=pool_id)


# ------------------------------------------------------------------
# PoolFormationProtocol
# ------------------------------------------------------------------

def make_pool_proposal(to: str, proposal: PoolProposal) -> Message:
    """Hub -> Rider."""
    return make_message(to, Perf.PROPOSE, Protocol.POOL_FORMATION, proposal,
                        thread=proposal.pool_id)


# ------------------------------------------------------------------
# Receiving side: templates for SPADE behaviours
# ------------------------------------------------------------------

def make_template(protocol: Optional[str] = None, performative: Optional[str] = None,
                  ontology: Optional[str] = None, thread: Optional[str] = None) -> Template:
    """Build a spade Template matching on metadata (and optionally thread).
    Only the criteria you pass are enforced."""
    metadata = {}
    if protocol:
        metadata["protocol"] = protocol
    if performative:
        metadata["performative"] = performative
    if ontology:
        metadata["ontology"] = ontology
    return Template(thread=thread, metadata=metadata or None)

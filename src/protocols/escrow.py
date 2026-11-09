"""
src/protocols/escrow.py   EscrowProtocol (Rider -> Payment, Payment -> Rider and Hub)

    Rider  --request(EscrowRequest)-->  Payment            thread = request_id
    Payment --inform(Escrow HELD)--->   Rider              "agree(escrow_id)"
    Payment --inform(Escrow HELD)--->   Hub                "escrow_confirmed"   (ID-02)
    or
    Payment --refuse(reason)-------->   Rider
    Payment --refuse(reason)-------->   Hub                "escrow_failed"      (ID-09)

`refuse` bodies are ledger.Reason values: insufficient_funds, unknown_account, invalid_amount,
duplicate_request (same request_id, different terms), not_authorised (sender is not the rider).
The hub is only told about business failures, never about duplicate or spoofed requests, so
nobody can make a hub drop a legitimate request.

A repeated request (same request_id and terms) is answered with the same Escrow again and
charges nothing, so retries are safe.
"""
import logging
from typing import List

from spade.message import Message

from common.ledger import LedgerError, Reason
from common.messages import make_inform, make_message
from common.ontology import Escrow, EscrowRequest, EscrowStatus, Perf, Protocol
from protocols.base import ServerFSM
from protocols.payment_common import bare, expect, refusal_body

logger = logging.getLogger(__name__)

NOTIFY_HUB_ON = {Reason.INSUFFICIENT_FUNDS, Reason.UNKNOWN_ACCOUNT, Reason.INVALID_AMOUNT}


def handle_escrow(agent, msg: Message) -> List[Message]:
    req = expect(msg, EscrowRequest)
    sender = bare(msg.sender)
    rider = agent.account(req.rider_id)
    hub = agent.account(req.hub_id) if req.hub_id else ""
    thread = req.request_id

    def refuse(to: str, reason: str, detail: str = "") -> Message:
        return make_message(to, Perf.REFUSE, Protocol.ESCROW, refusal_body(reason, detail), thread)

    if sender != rider:                                  # nobody spends someone else's money
        agent.stats["escrow_refused"] += 1
        return [refuse(sender, Reason.NOT_AUTHORISED, "sender is not the rider")]
    try:
        rec, created = agent.ledger.hold(req.request_id, rider, req.pool_id, req.amount,
                                         req.subsidy_eligible, hub)
    except LedgerError as e:
        agent.stats["escrow_refused"] += 1
        out = [refuse(sender, e.reason)]
        if hub and e.reason in NOTIFY_HUB_ON:
            out.append(refuse(hub, e.reason))
        return out
    agent.stats["escrow_held" if created else "escrow_replayed"] += 1
    escrow = Escrow(rec.escrow_id, rider, rec.amount, EscrowStatus.HELD, rec.pool_id)
    out = [make_inform(rider, Protocol.ESCROW, escrow, thread)]
    if hub:
        out.append(make_inform(hub, Protocol.ESCROW, escrow, thread))
    return out


class EscrowFSM(ServerFSM):
    def __init__(self):
        super().__init__(handle_escrow)

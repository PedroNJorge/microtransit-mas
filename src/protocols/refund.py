"""
src/protocols/refund.py   RefundProtocol (Hub -> Payment, Payment -> Rider and Hub)

    Hub     --request(TripFailed)-->  Payment              thread = pool_id
    Payment --inform(Refund)------->  Rider                "refund_issued"
    Payment --inform(Refund)------->  Hub                  "refund_complete", one per escrow
    Payment --refuse("<reason>:<escrow_id>")-->  Hub       one per escrow that cannot be refunded

The request payload is the existing TripFailed concept: pool_id, reason (cancellation, driver
failure, no match, ...) and escrow_ids. An empty escrow_ids list means EVERY escrow of the
pool; each one is judged on its own (see below), so the hub always gets one answer per
escrow. To refund only the no-shows of a partly settled pool, list their escrow_ids.
The reason is copied into each Refund.

Rules (ID-11, FE-01)
- Full refund. Cancellation fees and partial refunds for late rider cancellation are not
  defined in the rules yet; they would be one extra step here.
- Only the hub bound to the escrow may ask. Riders cancel THROUGH the hub (CancellationProtocol,
  ID-11), because a rider who could pull money back mid-trip would ride for free.
- Each escrow is judged on its own: held -> refunded; already refunded -> reported again
  (harmless replay, no money moves); already settled -> refused.
"""
import logging
from typing import List

from spade.message import Message

from common.ledger import LedgerError
from common.messages import make_inform, make_message
from common.ontology import Perf, Protocol, Refund, TripFailed
from protocols.base import ServerFSM
from protocols.payment_common import bare, expect, refusal_body

logger = logging.getLogger(__name__)


def handle_refund(agent, msg: Message) -> List[Message]:
    tf = expect(msg, TripFailed)
    sender = bare(msg.sender)
    thread = tf.pool_id
    try:
        items = agent.ledger.refund(tf.pool_id, tf.escrow_ids, sender)
    except LedgerError as e:
        agent.stats["refund_refused"] += 1
        return [make_message(sender, Perf.REFUSE, Protocol.REFUND,
                             refusal_body(e.reason, tf.pool_id), thread)]
    out: List[Message] = []
    for item in items:
        if not item.ok:
            agent.stats["refund_refused"] += 1
            out.append(make_message(sender, Perf.REFUSE, Protocol.REFUND,
                                    refusal_body(item.reason, item.escrow_id), thread))
            continue
        refund = Refund(item.escrow_id, item.rider, item.amount, tf.reason)
        if item.replayed:
            agent.stats["refund_replayed"] += 1
        else:
            agent.stats["refunds"] += 1
            out.append(make_inform(item.rider, Protocol.REFUND, refund, thread))
        out.append(make_inform(sender, Protocol.REFUND, refund, thread))
    return out


class RefundFSM(ServerFSM):
    def __init__(self):
        super().__init__(handle_refund)

"""
src/protocols/settlement.py   SettlementProtocol (Hub -> Payment, Payment -> Driver, Riders, Hub)

    Hub     --inform(TripCompleted)-->  Payment            thread = pool_id
    Payment --inform(Settlement)----->  Driver             one per rider escrow: "payout"
    Payment --inform(Settlement)----->  each Rider         "payment_settled" + "subsidy_disbursement"
    Payment --inform(SettlementComplete)--> Hub            "settlement_complete"
    or
    Payment --refuse(reason[:detail])-->  Hub

Rules (Organizational Rules GC-03, ID-06, RI-02, RI-06)
- Only the hub bound to the escrow may settle it. A TripCompleted from a driver or anybody
  else is refused (not_authorised) and moves no money.
- The driver is paid exactly TripCompleted.rider_fares (the auction price split). Subsidies
  are an extra rebate to the rider from the subsidy fund; they never reduce the payout.
  The rebate is credited at settlement time, i.e. well within T_subsidy.
- All or nothing: if any rider in the message is invalid, nothing is settled.
- Repeating a TripCompleted moves no money; the hub just gets SettlementComplete again.
- Escrows of riders NOT listed in TripCompleted.rider_ids (no-shows) stay HELD; the hub
  resolves them with a RefundProtocol request, or the expiry sweep returns them.
"""
import logging
from typing import List

from spade.message import Message

from common.ledger import LedgerError, money
from common.messages import make_inform, make_message
from common.ontology import Perf, Protocol, Settlement, SettlementComplete, TripCompleted
from protocols.base import ServerFSM
from protocols.payment_common import bare, expect, refusal_body

logger = logging.getLogger(__name__)


def handle_settlement(agent, msg: Message) -> List[Message]:
    tc = expect(msg, TripCompleted)
    sender = bare(msg.sender)
    thread = tc.pool_id
    driver = agent.account(tc.driver_id)
    riders = [agent.account(r) for r in tc.rider_ids]
    fares = {agent.account(r): f for r, f in tc.rider_fares.items()} or None
    try:
        outcome = agent.ledger.settle(tc.pool_id, driver, riders, fares, sender)
    except LedgerError as e:
        agent.stats["settlement_refused"] += 1
        return [make_message(sender, Perf.REFUSE, Protocol.SETTLEMENT,
                             refusal_body(e.reason, e.detail), thread)]

    complete = SettlementComplete(
        pool_id=tc.pool_id, driver_id=driver, escrow_ids=[l.escrow_id for l in outcome.lines],
        total_paid=outcome.total_paid, total_subsidy=outcome.total_subsidy,
        total_returned=outcome.total_returned)
    out: List[Message] = []
    if outcome.replayed:
        agent.stats["settlement_replayed"] += 1
    else:
        agent.stats["settlements"] += 1
        for line in outcome.lines:
            s = Settlement(line.escrow_id, driver, money(line.payout_c), money(line.subsidy_c),
                           0.0, money(line.excess_c))
            out.append(make_inform(driver, Protocol.SETTLEMENT, s, thread))
            out.append(make_inform(line.rider, Protocol.SETTLEMENT, s, thread))
    out.append(make_inform(sender, Protocol.SETTLEMENT, complete, thread))
    return out


class SettlementFSM(ServerFSM):
    def __init__(self):
        super().__init__(handle_settlement)

"""
src/agents/payment.py
PaymentAgent (Role: Payment, Sub-org: Financial). Milestone 4.

Owns every wallet and escrow account (in memory, see common/ledger.py) and answers three
protocols, each run by its own FSMBehaviour (protocols/):

    EscrowProtocol      rider  -> hold the fare            (protocols/escrow.py)
    SettlementProtocol  hub    -> trip completed: pay      (protocols/settlement.py)
    RefundProtocol      hub    -> trip failed: give back   (protocols/refund.py)

Role Model
- Liveness: every trip completion is eventually settled and every failed trip refunded.
  The hub triggers both; the expiry sweep below returns any escrow that is HELD for longer
  than `hold_max` (e.g. the hub crashed), so no rider's money stays locked forever.
- Safety:   nothing is released before the hub confirms completion (GC-03); the driver is
  paid exactly the auction price; subsidies never reduce that; money is only transferred.

Accounts
- All ids are normalised to full JIDs (`account()`): a bare id like "rider_01" gets this
  agent's domain. Outgoing payloads use JIDs.
- Riders get a wallet with `default_rider_balance` on their first escrow request (a simulation
  endowment, booked as an external inflow), or are refused if that is None. Drivers get an
  empty wallet when first paid. `ledger.open_wallet()` is available for scripted set-ups.

Policy
- `subsidy_discount` starts from the scenario; `set_policy()` takes the Regulator's Policy
  (subsidy_rules["eligible_discount"]) and ignores versions older than the one it has.
  Receiving policies by message arrives with the Regulator milestone.
"""
import logging
from collections import Counter

from spade.behaviour import PeriodicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.ledger import Ledger
from common.messages import make_inform, make_template
from common.ontology import FailureReason, Perf, Policy, Protocol, Refund
from common.settings import SUBSIDY_DISCOUNT, T_ESCROW_HOLD_MAX, T_ESCROW_SWEEP
from protocols.escrow import EscrowFSM
from protocols.refund import RefundFSM
from protocols.settlement import SettlementFSM

logger = logging.getLogger(__name__)


class ExpirySweep(PeriodicBehaviour):
    """Return escrows held longer than `hold_max` to their riders and tell rider and hub."""

    async def run(self):
        agent = self.agent
        for rec, item in agent.ledger.refund_expired(clock.now(), agent.hold_max):
            if not item.ok:
                continue
            agent.stats["refund_expired"] += 1
            refund = Refund(rec.escrow_id, rec.rider, item.amount, FailureReason.TIMEOUT)
            await self.send(make_inform(rec.rider, Protocol.REFUND, refund, rec.pool_id))
            if rec.hub:
                await self.send(make_inform(rec.hub, Protocol.REFUND, refund, rec.pool_id))


class PaymentAgent(BaseAgent):
    role = "payment"

    def __init__(self, jid: str, password: str, default_rider_balance=None,
                 subsidy_budget=None, subsidy_discount: float = SUBSIDY_DISCOUNT,
                 hold_max: float = T_ESCROW_HOLD_MAX, sweep_every: float = T_ESCROW_SWEEP,
                 **kwargs):
        super().__init__(jid, password, **kwargs)
        if hold_max <= 0 or sweep_every <= 0:
            raise ValueError("hold_max and sweep_every must be > 0")
        self.ledger = Ledger(default_rider_balance, subsidy_budget, subsidy_discount,
                             now=clock.now)
        self.hold_max = hold_max
        self.sweep_every = sweep_every
        self.policy_version = -1
        self.stats: Counter = Counter()

    # ---- helpers -----------------------------------------------------------------------
    def account(self, agent_id: str) -> str:
        """Full JID for an agent id (bare ids get this agent's domain)."""
        agent_id = str(agent_id)
        return agent_id if "@" in agent_id else f"{agent_id}@{self.jid.domain}"

    def set_policy(self, policy: Policy) -> bool:
        """Adopt the subsidy rate of a Policy. False if it is older than the current one."""
        if policy.version <= self.policy_version:
            return False
        rate = policy.subsidy_rules.get("eligible_discount")
        if rate is not None:
            self.ledger.set_subsidy_discount(rate)
        self.policy_version = policy.version
        return True

    # ---- lifecycle ---------------------------------------------------------------------
    async def setup(self):
        await super().setup()                                         # answers pings
        self.add_behaviour(EscrowFSM(), make_template(Protocol.ESCROW, Perf.REQUEST))
        self.add_behaviour(SettlementFSM(), make_template(Protocol.SETTLEMENT, Perf.INFORM))
        self.add_behaviour(RefundFSM(), make_template(Protocol.REFUND, Perf.REQUEST))
        self.add_behaviour(ExpirySweep(period=clock.to_real(self.sweep_every)))

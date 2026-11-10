"""
src/agents/regulator.py   RegulatorAgent (Role: Regulator, Sub-org: Regulatory). Milestone 8.

PolicyProtocol  (Regulator -> Hub, Driver, Payment)
    request(Policy) -> agree | refuse(reason)          RI-04
  * Versions only go up; a new version starts from the previous policy and changes what the caller
    names, so two policies never conflict (Safety: no conflicting policies).
  * Safety: a fare cap below the sustainable driver level (`driver_floor`) is refused at publication.
  * A recipient that has not answered after `t_policy_ack` gets the request once more; after that
    it is listed in `unacknowledged()`.
  * effective_from lets a policy start later than it is published (default: at once).

AuditProtocol  (Regulator <-> Hub)                     RI-01, RI-02, RI-05
    request(AuditRequest) -> inform(AuditData) | refuse(logs_unavailable) | (silence)
    verdict: inform(AuditReport) always; plus inform(Penalty) when a fare exceeded the cap in force
    when it was awarded, or when the hub did not cooperate (refusal or silence after t_audit_response).
  The Regulator checks fares against ITS OWN record of the policy history, not against what the hub
  says its cap was. Audits run every `t_audit` for each hub (RI-02) and on demand via `audit()`.
  Equity: AuditReport.zone_service_share (matched / requested per destination zone) is reported;
  acting on min_service_level (RI-03/07 overflow) belongs to the inter-hub milestone.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from spade.behaviour import CyclicBehaviour

from agents.base_agent import BaseAgent
from common import clock
from common.messages import make_inform, make_message, make_request, make_template
from common.ontology import (
    AuditData, AuditReport, AuditRequest, Penalty, Perf, Policy, Protocol, new_id, parse_payload,
)
from common.policy import PolicyBook
from common.settings import (
    DRIVER_BASE_COST, PENALTY_FARE_VIOLATION, PENALTY_NON_COOPERATION, T_AUDIT, T_AUDIT_RESPONSE,
    T_POLICY_ACK,
)
from protocols.payment_common import bare

logger = logging.getLogger(__name__)
EPS = 0.005


@dataclass
class PendingAudit:
    request: AuditRequest
    hub: str
    sent_at: float


@dataclass
class AuditOutcome:
    audit_id: str
    hub: str
    report: Optional[AuditReport]
    violations: List[str] = field(default_factory=list)       # pool ids
    penalty: Optional[Penalty] = None
    cooperated: bool = True


class RegulatorServer(CyclicBehaviour):
    """Policy acknowledgements, audit data/refusals, and the timers (re-send, audit schedule, timeouts)."""

    async def run(self):
        reg = self.agent
        msg = await self.receive(timeout=0.5)
        try:
            if msg is not None:
                out = reg.on_message(msg)
                for m in out:
                    await self.send(m)
            for m in reg.on_tick(clock.now()):
                await self.send(m)
        except Exception:
            logger.exception("%s: regulator step failed", reg.jid)


class RegulatorAgent(BaseAgent):
    role = "regulator"

    def __init__(self, jid: str, password: str, hub_jids: Iterable[str] = (), driver_jids: Iterable[str] = (),
                 payment_jid: Optional[str] = None, t_audit: float = T_AUDIT,
                 t_audit_response: float = T_AUDIT_RESPONSE, t_policy_ack: float = T_POLICY_ACK,
                 driver_floor: float = DRIVER_BASE_COST, auto_audit: bool = True, **kwargs):
        super().__init__(jid, password, **kwargs)
        self.hub_jids = [bare(j) for j in hub_jids]
        self.driver_jids = [bare(j) for j in driver_jids]
        self.payment_jid = bare(payment_jid) if payment_jid else None
        self.t_audit, self.t_audit_response, self.t_policy_ack = t_audit, t_audit_response, t_policy_ack
        self.driver_floor, self.auto_audit = driver_floor, auto_audit
        self.book = PolicyBook()                                   # what was published
        self.acks: Dict[int, Dict[str, str]] = {}                  # version -> recipient -> "agree" | reason
        self._sent: Dict[int, Dict[str, float]] = {}               # version -> recipient -> last sent at
        self._resent: set = set()
        self._recipients: Dict[int, List[str]] = {}
        self.pending: Dict[str, PendingAudit] = {}
        self.outcomes: List[AuditOutcome] = []
        self.penalties: List[Penalty] = []
        self._last_audit_end: Dict[str, float] = {}
        self._next_audit = None
        self._queue: List = []

    async def setup(self):
        await super().setup()
        self.add_behaviour(RegulatorServer(), make_template(None))

    # =========================== PolicyProtocol ===========================
    def publish_policy(self, fare_caps: Optional[Dict[str, float]] = None,
                       subsidy_rules: Optional[Dict[str, float]] = None,
                       equity_zones: Optional[List[str]] = None,
                       min_service_level: Optional[Dict[str, float]] = None,
                       effective_from: Optional[float] = None,
                       recipients: Optional[Iterable[str]] = None) -> Policy:
        """Build the next version (previous values kept unless overridden) and queue it for sending."""
        prev = self.book.latest
        caps = {**(prev.fare_caps if prev else {}), **(fare_caps or {})}
        if "max" in caps and caps["max"] < self.driver_floor:
            raise ValueError(f"fare cap {caps['max']:.2f} is below the sustainable driver level "
                             f"{self.driver_floor:.2f}")
        policy = Policy(
            policy_id=new_id("pol"), version=(prev.version + 1) if prev else 1,
            effective_from=clock.now() if effective_from is None else effective_from, fare_caps=caps,
            equity_zones=list(equity_zones if equity_zones is not None else (prev.equity_zones if prev else [])),
            subsidy_rules={**(prev.subsidy_rules if prev else {}), **(subsidy_rules or {})},
            min_service_level={**(prev.min_service_level if prev else {}), **(min_service_level or {})})
        ack = self.book.offer(policy)
        if not ack.ok:
            raise ValueError(f"invalid policy: {ack.reason}")
        targets = list(dict.fromkeys(bare(r) for r in (recipients if recipients is not None else
                       [*self.hub_jids, *self.driver_jids, *([self.payment_jid] if self.payment_jid else [])])))
        self._recipients[policy.version] = targets
        self.acks[policy.version], self._sent[policy.version] = {}, {}
        for r in targets:
            self._queue.append(self._policy_request(r, policy))
            self._sent[policy.version][r] = clock.now()
        logger.info("[regulator] policy v%d published to %d recipients", policy.version, len(targets))
        return policy

    @staticmethod
    def _policy_request(to: str, policy: Policy):
        return make_request(to, Protocol.POLICY, policy, thread=str(policy.version))

    def unacknowledged(self, version: Optional[int] = None) -> List[str]:
        v = version if version is not None else (self.book.latest.version if self.book.latest else 0)
        return [r for r in self._recipients.get(v, []) if self.acks.get(v, {}).get(r) != "agree"]

    # =========================== AuditProtocol ===========================
    def audit(self, hub_jid: str, period_start: Optional[float] = None, period_end: Optional[float] = None) -> str:
        hub = bare(hub_jid)
        now = clock.now()
        start = self._last_audit_end.get(hub, 0.0) if period_start is None else period_start
        req = AuditRequest(new_id("audit"), start, now if period_end is None else period_end)
        self.pending[req.audit_id] = PendingAudit(req, hub, now)
        self._last_audit_end[hub] = req.period_end
        self._queue.append(make_request(hub, Protocol.AUDIT, req, thread=req.audit_id))
        return req.audit_id

    def verify(self, data: AuditData, hub_jid: Optional[str] = None) -> AuditOutcome:
        """Fares against the cap that was in force when each pool was awarded (own policy record)."""
        bad = []
        for t in data.trips:
            cap = self.book.fare_cap(t.awarded_at)
            if t.matched and cap is not None and t.final_fare > cap + EPS:
                bad.append(t.pool_id)
        total: Dict[str, int] = {}
        served: Dict[str, int] = {}
        for t in data.trips:
            z = t.zone or "unknown"
            total[z] = total.get(z, 0) + 1
            served[z] = served.get(z, 0) + (1 if t.matched else 0)
        share = {z: served[z] / total[z] for z in total}
        report = AuditReport(data.hub_id, data.period_start, data.period_end, share, len(bad), not bad)
        target = hub_jid or data.hub_id                    # the penalty goes to the agent, not to a node id
        pen = (Penalty(target, f"fare_cap_violation x{len(bad)}", PENALTY_FARE_VIOLATION * len(bad))
               if bad else None)
        return AuditOutcome(data.audit_id, target, report, bad, pen)

    def _non_cooperation(self, audit_id: str, hub: str) -> List:
        pen = Penalty(hub, "non_cooperation", PENALTY_NON_COOPERATION)
        self.penalties.append(pen)
        self.outcomes.append(AuditOutcome(audit_id, hub, None, [], pen, cooperated=False))
        return [make_inform(hub, Protocol.AUDIT, pen, thread=audit_id)]

    # =========================== message handling ===========================
    def on_message(self, msg) -> List:
        proto, perf, sender = msg.get_metadata("protocol"), msg.get_metadata("performative"), bare(msg.sender)
        if proto == Protocol.POLICY and perf in (Perf.AGREE, Perf.REFUSE):
            try:
                v = int(msg.thread)
            except (TypeError, ValueError):
                return []
            if sender in self._recipients.get(v, []):
                self.acks[v][sender] = "agree" if perf == Perf.AGREE else (msg.body or "refused")
            return []
        if proto != Protocol.AUDIT:
            return []
        audit_id = msg.thread or ""
        pend = self.pending.get(audit_id)
        if pend is None or sender != pend.hub:                       # unsolicited or from the wrong hub
            return []
        if perf == Perf.REFUSE:
            del self.pending[audit_id]
            return self._non_cooperation(audit_id, pend.hub)
        try:
            data = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, AuditData) or data.audit_id != audit_id:
            return []
        del self.pending[audit_id]
        outcome = self.verify(data, pend.hub)
        self.outcomes.append(outcome)
        out = [make_inform(pend.hub, Protocol.AUDIT, outcome.report, thread=audit_id)]
        if outcome.penalty:
            self.penalties.append(outcome.penalty)
            out.append(make_inform(pend.hub, Protocol.AUDIT, outcome.penalty, thread=audit_id))
        return out

    def on_tick(self, now: float) -> List:
        out, self._queue = list(self._queue), []
        for v, targets in self._recipients.items():                  # RI-04: one re-send to the silent
            policy = self.book.by_version[v]
            for r in targets:
                if (v, r) not in self._resent and r not in self.acks[v] and \
                        now - self._sent[v][r] >= self.t_policy_ack:
                    self._resent.add((v, r))
                    out.append(self._policy_request(r, policy))
        for aid, p in list(self.pending.items()):                    # RI-05: silence is non-cooperation
            if now - p.sent_at >= self.t_audit_response:
                del self.pending[aid]
                out += self._non_cooperation(aid, p.hub)
        if self.auto_audit:                                          # RI-02: every hub every T_audit
            if self._next_audit is None:
                self._next_audit = now + self.t_audit
            elif now >= self._next_audit:
                self._next_audit = now + self.t_audit
                for hub in self.hub_jids:
                    self.audit(hub)
                out += self._queue
                self._queue = []
        return out

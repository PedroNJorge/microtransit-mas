"""
src/agents/brain.py   DriverBrain: the BDI layer of a driver (SPADE-BDI, AgentSpeak plans).

The reactive DriverAgent keeps everything it had (bidding, provisional lock, trips). The brain only
decides what to do while the driver is IDLE: where to wait so that the next call for proposals finds
it close. It is a separate agent with its own JID, talking to its driver over XMPP:

    Driver --inform(BrainPercept)--> Brain     beliefs: state, at_hub, home, eta(Hub,s), pressure(Hub,p)
    Brain  --request(CruiseCommand)--> Driver  action .cruise_to(Hub);  agree | refuse(reason)

Beliefs live in the AgentSpeak belief base (`agents/brains/driver.asl` holds the desires and plans).
`apply_percept` keeps them in step with the latest percept, touching only what changed so that
unchanged beliefs do not re-trigger plans.
"""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Dict, Tuple

import agentspeak as asp
from spade.behaviour import CyclicBehaviour
from spade_bdi.bdi import BDIAgent

from agents.base_agent import PingBehaviour
from common.messages import make_message, make_template
from common.ontology import BidPolicy, BrainPercept, CruiseCommand, Perf, Protocol, parse_payload
from protocols.payment_common import bare

logger = logging.getLogger(__name__)

DEFAULT_ASL = str(Path(__file__).parent / "brains" / "driver.asl")
PERCEPT_TAG = frozenset([asp.Literal("source", (asp.Literal("percept"),))])


def _term(name: str, *args) -> asp.Literal:
    return asp.Literal(name, tuple(asp.Literal(a) if isinstance(a, str) else a for a in args), PERCEPT_TAG)


class PerceptReceiver(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        brain = self.agent
        if bare(msg.sender) != brain.driver_jid:
            return
        try:
            body = parse_payload(msg, strict=True)
        except (ValueError, TypeError):
            return
        perf = msg.get_metadata("performative")
        if isinstance(body, BrainPercept) and perf == Perf.INFORM:
            brain.apply_percept(body)
        elif perf == Perf.REFUSE:
            brain.refusals.append(msg.body or "")
            logger.info("[%s] driver refused the cruise: %s", brain.jid.user, msg.body)
        elif perf == Perf.AGREE:
            brain.agreed += 1


class DriverBrain(BDIAgent):
    role = "driver_brain"

    def __init__(self, jid: str, password: str, driver_jid: str, asl: str = DEFAULT_ASL,
                 cooldown: float = 240.0, bid_policy: bool = False, **kwargs):
        self.bid_policy = bid_policy                  # also own the driver's bid markup (opportunity cost)
        self.markups: list = []                       # markups this brain has set
        self._markup = 0.0
        self.driver_jid = bare(driver_jid)
        self.cooldown = cooldown                      # sim seconds after a move during which no switch is considered
        self.sim_now = 0.0                            # time of the latest percept (sim seconds)
        self.last_move = -1e18
        self.commands: list = []                      # hubs this brain has sent its driver to
        self.refusals: list = []
        self.agreed = 0
        self.percepts_received = 0
        self.pings_answered = 0                       # liveness: the brain answers pings like every agent
        self._beliefs: Dict[Tuple[str, str], tuple] = {}      # (functor, key) -> args currently in the belief base
        super().__init__(jid, password, asl, **kwargs)

    # ---- AgentSpeak action: .cruise_to(Hub) ------------------------------------------------------------
    def add_custom_actions(self, actions):
        @actions.add(".cruise_to", 1)
        def _cruise_to(agent, term, intention):
            hub = str(asp.grounded(term.args[0], intention.scope))
            self.commands.append(hub)
            self.last_move = self.sim_now
            msg = make_message(self.driver_jid, Perf.REQUEST, Protocol.DRIVER_BRAIN,
                               CruiseCommand(self.driver_jid, hub), f"cruise-{uuid.uuid4().hex[:8]}")
            try:
                asyncio.get_running_loop()
            except RuntimeError:                      # no event loop: the brain is being exercised without XMPP
                pass
            else:
                self.submit(self.bdi.send(msg))
            yield

        @actions.add(".set_markup", 1)
        def _set_markup_action(agent, term, intention):
            self._set_markup(float(asp.grounded(term.args[0], intention.scope)))
            yield

    # ---- AgentSpeak action: .set_markup(M) -------------------------------------------------------------
    def _set_markup(self, m: float) -> None:
        m = round(min(1.0, max(0.0, float(m))), 2)
        if abs(m - self._markup) < 0.02:                  # ignore jitter; only real changes are sent
            return
        self._markup = m
        self.markups.append(m)
        msg = make_message(self.driver_jid, Perf.REQUEST, Protocol.DRIVER_BRAIN,
                           BidPolicy(self.driver_jid, m), f"bid-{uuid.uuid4().hex[:8]}")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.submit(self.bdi.send(msg))

    async def setup(self):
        self.add_behaviour(PerceptReceiver(), make_template(Protocol.DRIVER_BRAIN))
        self.add_behaviour(PingBehaviour(), make_template(protocol=Protocol.HEARTBEAT, performative=Perf.REQUEST))
        orig = self.bdi.run

        async def paced():                            # spade_bdi's loop never sleeps; keep 20 brains cheap
            await orig()
            await asyncio.sleep(0.02)
        self.bdi.run = paced

    # ---- belief maintenance ----------------------------------------------------------------------------------
    def _set(self, wanted: Dict[Tuple[str, str], tuple], functor: str) -> None:
        """Make the beliefs of `functor` equal to `wanted` (key -> args); only changes are queued."""
        buf = self.bdi_intention_buffer
        for (f, key), args in list(self._beliefs.items()):
            if f == functor and (f, key) not in wanted:
                buf.append((asp.Trigger.removal, asp.GoalType.belief, _term(f, *args), asp.runtime.Intention()))
                del self._beliefs[(f, key)]
        for (f, key), args in wanted.items():
            old = self._beliefs.get((f, key))
            if old == args:
                continue
            if old is not None:
                buf.append((asp.Trigger.removal, asp.GoalType.belief, _term(f, *old), asp.runtime.Intention()))
            buf.append((asp.Trigger.addition, asp.GoalType.belief, _term(f, *args), asp.runtime.Intention()))
            self._beliefs[(f, key)] = args

    def apply_percept(self, p: BrainPercept) -> None:
        self.percepts_received += 1
        self.sim_now = p.sent_at
        self._set({("cooling", ""): ()} if p.sent_at - self.last_move < self.cooldown else {}, "cooling")
        self._set({("state", ""): (p.state,)}, "state")
        self._set({("at_hub", ""): (p.at_hub,)} if p.at_hub else {}, "at_hub")
        self._set({("home", ""): (p.home,)} if p.home else {}, "home")
        self._set({("bid_policy", ""): ()} if self.bid_policy else {}, "bid_policy")
        self._set({("eta", h): (h, e) for h, e in zip(p.hub_ids, p.etas) if e >= 0}, "eta")
        self._set({("pressure", h): (h, q) for h, q in zip(p.hub_ids, p.pressures)}, "pressure")

"""
src/protocols/base.py
Shared machinery for request/response protocols that a SERVER agent answers (the Payment
agent's Escrow, Settlement and Refund protocols).

    RECEIVE --msg--> HANDLE --> REPLY --> RECEIVE
       ^--timeout--'

- RECEIVE waits (1 s slices, so the behaviour stays killable) for a message that matched
  the template the behaviour was registered with.
- HANDLE runs the protocol's handler, a plain synchronous function
  `handler(agent, msg) -> list[Message]`. All business rules live in the handler and the
  ledger, which keeps them unit-testable. Whatever the handler raises is caught here and
  becomes a protocol-level `failure` reply: SPADE kills a whole FSM when ANY state raises,
  and one malformed message must never switch the Payment agent's protocol off.
- REPLY sends everything the handler returned, then goes back to RECEIVE.

Messages are handled strictly one at a time, in arrival order. That is what makes the
ledger's check-then-act steps safe without locks.
"""
import logging
from typing import Callable, List

from spade.behaviour import FSMBehaviour, State
from spade.message import Message

from common.messages import make_reply
from common.ontology import Perf

logger = logging.getLogger(__name__)

RECEIVE, HANDLE, REPLY = "RECEIVE", "HANDLE", "REPLY"

Handler = Callable[[object, Message], List[Message]]


class _Context:
    """What the three states pass to each other."""
    def __init__(self):
        self.msg = None
        self.outbox: List[Message] = []


class _Receive(State):
    def __init__(self, ctx: _Context):
        super().__init__()
        self.ctx = ctx

    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            self.set_next_state(RECEIVE)
            return
        self.ctx.msg = msg
        self.set_next_state(HANDLE)


class _Handle(State):
    def __init__(self, ctx: _Context, handler: Handler):
        super().__init__()
        self.ctx = ctx
        self.handler = handler

    async def run(self):
        msg = self.ctx.msg
        try:
            self.ctx.outbox = list(self.handler(self.agent, msg))
        except Exception as e:                          # see module docstring
            logger.exception("%s: handler failed on message from %s", self.agent.jid, msg.sender)
            try:
                self.ctx.outbox = [make_reply(msg, Perf.FAILURE, f"{type(e).__name__}: {e}")]
            except Exception:
                self.ctx.outbox = []
        self.set_next_state(REPLY)


class _Reply(State):
    def __init__(self, ctx: _Context):
        super().__init__()
        self.ctx = ctx

    async def run(self):
        outbox, self.ctx.outbox = self.ctx.outbox, []
        for out in outbox:
            await self.send(out)
        self.set_next_state(RECEIVE)


class ServerFSM(FSMBehaviour):
    """RECEIVE -> HANDLE -> REPLY loop around `handler`. Register it with a Template."""

    def __init__(self, handler: Handler):
        self._handler = handler
        super().__init__()

    def setup(self):
        ctx = _Context()
        self.add_state(RECEIVE, _Receive(ctx), initial=True)
        self.add_state(HANDLE, _Handle(ctx, self._handler))
        self.add_state(REPLY, _Reply(ctx))
        self.add_transition(RECEIVE, RECEIVE)
        self.add_transition(RECEIVE, HANDLE)
        self.add_transition(HANDLE, REPLY)
        self.add_transition(REPLY, RECEIVE)

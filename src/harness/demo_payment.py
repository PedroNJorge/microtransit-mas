"""
src/harness/demo_payment.py
A narrated walk through the Payment agent's money flow, for checking it by eye (and for demos).

    python -m harness.demo_payment            # starts its own server + Payment agent; shows balances
    python -m harness.demo_payment --live     # talks to the Payment agent of a RUNNING launcher
                                              # (balances are not readable from outside: messages only)

Acts out a rider, a hub and a driver and prints every message they receive:
  1. success   two riders hold 20.00 and 15.00; the trip costs 12.00 and 9.00; the driver is paid
               21.00, the unused 14.00 goes back, the eligible rider gets a 6.00 subsidy rebate
  2. failure   two riders hold money, the hub reports "no match", everything is refunded
  3. abuse     a driver tries to settle, a rider tries to pull money back, a rider tries to
               spend somebody else's money: all refused, nothing moves
Exit code 0 only if everything behaved as expected.
"""
import argparse
import asyncio
import sys
import uuid
from typing import List, Optional, Sequence

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from agents.payment import PaymentAgent
from common import clock
from common.messages import make_message, make_request
from common.ontology import (
    EscrowRequest, FailureReason, Perf, Protocol, TripCompleted, TripFailed, parse_payload,
)
from harness.active import read_active
from harness.launcher import Harness
from harness.scenario import DEFAULT_SCENARIO, Scenario, ScenarioError


class Actor(Agent):
    """Plays one participant: prints and records everything it receives."""

    def __init__(self, label: str, *a, quiet: bool = False, **kw):
        super().__init__(*a, **kw)
        self.label = label
        self.inbox = []
        self.quiet = quiet

    class Rx(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=0.2)
            if msg is not None:
                self.agent.inbox.append(msg)
                if not self.agent.quiet:
                    print(f"      {self.agent.label:<8}<- {describe(msg)}")

    async def setup(self):
        self.add_behaviour(self.Rx())

    async def tell(self, msg):
        done = asyncio.Event()

        class Tx(OneShotBehaviour):
            async def run(self):
                await self.send(msg)
                done.set()

        self.add_behaviour(Tx())
        await asyncio.wait_for(done.wait(), 5)

    def count(self, protocol=None, performative=None):
        return len([m for m in self.inbox
                    if (protocol is None or m.get_metadata("protocol") == protocol)
                    and (performative is None or m.get_metadata("performative") == performative)])


def describe(msg) -> str:
    perf = msg.get_metadata("performative")
    onto = msg.get_metadata("ontology")
    try:
        body = parse_payload(msg) if onto else msg.body
    except ValueError:
        body = msg.body
    if hasattr(body, "__dataclass_fields__"):
        body = " ".join(f"{k}={getattr(v, 'value', v)}" for k, v in vars(body).items() if v not in ("", [], {}))
    return f"{perf:<12} {onto or 'text':<18} {body}"


async def until(cond, timeout=6.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class Demo:
    def __init__(self, scenario: Scenario, ledger=None):
        self.sc = scenario
        self.ledger = ledger                       # None in --live mode
        n = uuid.uuid4().hex[:5]
        self.n = n
        self.payment = scenario.jid("payment")
        port, pw = scenario.xmpp.port, scenario.xmpp.password
        mk = lambda label, name: Actor(label, scenario.jid(f"{name}_{n}"), pw, port=port)
        self.a, self.b = mk("rider A", "demo_rider_a"), mk("rider B", "demo_rider_b")
        self.hub, self.drv = mk("hub", "demo_hub"), mk("driver", "demo_driver")
        self.failures: List[str] = []

    async def __aenter__(self):
        for actor in (self.a, self.b, self.hub, self.drv):
            await actor.start(auto_register=True)
        return self

    async def __aexit__(self, *exc):
        for actor in (self.a, self.b, self.hub, self.drv):
            await actor.stop()

    # ---- helpers -----------------------------------------------------------------------
    def check(self, ok: bool, what: str) -> None:
        print(f"      {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            self.failures.append(what)

    def balances(self, title: str) -> None:
        if self.ledger is None:
            return
        j = lambda actor: str(actor.jid.bare)
        l = self.ledger
        print(f"      [{title}] rider A {l.balance(j(self.a)):7.2f} | rider B {l.balance(j(self.b)):7.2f}"
              f" | driver {l.balance(j(self.drv)):7.2f} | subsidy paid {l.subsidy_spent:6.2f}"
              f" | books balance: {l.check_conservation()}")

    async def hold(self, actor, amount, pool, eligible=False):
        req = EscrowRequest(f"req-{actor.label[-1]}-{pool}", actor.jid.bare, pool, amount,
                            eligible, self.hub.jid.bare)
        await actor.tell(make_request(self.payment, Protocol.ESCROW, req, thread=req.request_id))

    async def holds(self, pool):
        print("   riders ask Payment to hold money (A is subsidy-eligible):")
        a0, b0, h0 = (self.a.count(Protocol.ESCROW, Perf.INFORM),
                      self.b.count(Protocol.ESCROW, Perf.INFORM),
                      self.hub.count(Protocol.ESCROW, Perf.INFORM))
        await self.hold(self.a, 20.0, pool, eligible=True)
        await self.hold(self.b, 15.0, pool)
        ok = await until(lambda: self.a.count(Protocol.ESCROW, Perf.INFORM) > a0
                         and self.b.count(Protocol.ESCROW, Perf.INFORM) > b0
                         and self.hub.count(Protocol.ESCROW, Perf.INFORM) >= h0 + 2)
        self.check(ok, "riders got their escrow and the hub received escrow_confirmed for both")
        self.balances("after hold")

    async def completed(self, pool, fares, sender=None):
        tc = TripCompleted(f"trip-{pool}", pool, self.drv.jid.bare,
                           [self.a.jid.bare, self.b.jid.bare], clock.now(), 4.2, fares)
        await (sender or self.hub).tell(make_message(
            self.payment, Perf.INFORM, Protocol.SETTLEMENT, tc, thread=pool))

    async def failed(self, pool, sender=None, reason=FailureReason.NO_MATCH):
        tf = TripFailed(pool, reason, self.hub.jid.bare, [])
        await (sender or self.hub).tell(make_request(self.payment, Protocol.REFUND, tf, thread=pool))

    # ---- scenes ----------------------------------------------------------------------------
    async def success(self):
        print("\n1. SUCCESS  hold -> settle -> disbursement")
        pool = f"pool-ok-{self.n}"
        await self.holds(pool)
        print("   the hub reports the trip completed; fares 12.00 and 9.00 against holds of 20.00 and 15.00:")
        await self.completed(pool, {self.a.jid.bare: 12.0, self.b.jid.bare: 9.0})
        ok = await until(lambda: self.hub.count(Protocol.SETTLEMENT, Perf.INFORM) >= 1
                         and self.drv.count(Protocol.SETTLEMENT) >= 2
                         and self.a.count(Protocol.SETTLEMENT) >= 1)
        self.check(ok, "driver got two payouts, riders got their settlements, hub got settlement_complete")
        self.balances("after settle")
        if self.ledger is not None:
            j = lambda actor: str(actor.jid.bare)
            l = self.ledger
            self.check(l.balance(j(self.drv)) == 21.0, "driver was paid exactly 21.00 (12.00 + 9.00)")
            self.check(l.balance(j(self.a)) == 94.0, "rider A: 100 - 20 held + 8 unused + 6 subsidy = 94.00")
            self.check(l.balance(j(self.b)) == 91.0, "rider B: 100 - 15 held + 6 unused = 91.00 (no subsidy)")

    async def failure(self):
        print("\n2. FAILURE  hold -> refund")
        pool = f"pool-fail-{self.n}"
        before = self.drv.count(Protocol.SETTLEMENT)
        await self.holds(pool)
        print("   the hub reports 'no match' for the pool:")
        await self.failed(pool)
        ok = await until(lambda: self.hub.count(Protocol.REFUND, Perf.INFORM) >= 2
                         and self.a.count(Protocol.REFUND, Perf.INFORM) >= 1
                         and self.b.count(Protocol.REFUND, Perf.INFORM) >= 1)
        self.check(ok, "both riders and the hub were told about the refund")
        self.check(self.drv.count(Protocol.SETTLEMENT) == before, "the driver was not paid")
        self.balances("after refund")

    async def abuse(self):
        print("\n3. ABUSE  none of this may move money")
        pool = f"pool-abuse-{self.n}"
        await self.holds(pool)
        print("   the DRIVER claims the trip is completed:")
        await self.completed(pool, {}, sender=self.drv)
        self.check(await until(lambda: self.drv.count(Protocol.SETTLEMENT, Perf.REFUSE) >= 1),
                   "refused (only the hub may confirm completion)")
        print("   rider A asks for a refund while the trip is running:")
        await self.failed(pool, sender=self.a)
        self.check(await until(lambda: self.a.count(Protocol.REFUND, Perf.REFUSE) >= 1),
                   "refused (riders cancel through the hub)")
        print("   rider B asks Payment to hold money in rider A's name:")
        spoof = EscrowRequest(f"req-spoof-{self.n}", self.a.jid.bare, pool, 5.0, False, self.hub.jid.bare)
        await self.b.tell(make_request(self.payment, Protocol.ESCROW, spoof, thread=spoof.request_id))
        self.check(await until(lambda: self.b.count(Protocol.ESCROW, Perf.REFUSE) >= 1),
                   "refused (not your wallet)")
        self.balances("after abuse")
        print("   cleaning up: the hub cancels the pool")
        await self.failed(pool)
        await until(lambda: self.a.count(Protocol.REFUND, Perf.INFORM) >= 2
                    and self.b.count(Protocol.REFUND, Perf.INFORM) >= 2
                    and self.hub.count(Protocol.REFUND, Perf.INFORM) >= 4)
        self.balances("final")
        if self.ledger is not None:
            self.check(self.ledger.check_conservation(), "money conserved: every cent is accounted for")


async def run(scenario: Scenario, live: bool) -> int:
    clock.start(scenario.speed)
    harness: Optional[Harness] = None
    payment: Optional[PaymentAgent] = None
    if not live:
        harness = Harness(scenario)
        await harness.ensure_server()
        payment = PaymentAgent(scenario.jid("payment"), scenario.xmpp.password,
                               port=scenario.xmpp.port,
                               **next(s for s in scenario.roster() if s.role == "payment").kwargs)
        await payment.start(auto_register=True)
    try:
        print(f"Scenario {scenario.name!r}, Payment agent {scenario.jid('payment')} "
              f"({'live system' if live else 'started for this demo'})")
        if live:
            print("   (balances are not visible from outside the Payment agent: messages only)")
        async with Demo(scenario, payment.ledger if payment else None) as demo:
            await demo.success()
            await demo.failure()
            await demo.abuse()
            failures = demo.failures
    finally:
        if payment:
            await payment.stop()
        if harness:
            await harness.stop()
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED"))
    return 0 if not failures else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.demo_payment",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--live", action="store_true", help="use the Payment agent of a running launcher")
    args = ap.parse_args(argv)
    active = read_active()
    path = args.scenario or (active if args.live and active else None) or DEFAULT_SCENARIO
    if args.live and not (args.scenario or active):
        print("no launcher is running: start `python -m harness.launcher` first", file=sys.stderr)
        return 2
    try:
        scenario = Scenario.load(path)
    except ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(scenario, args.live))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

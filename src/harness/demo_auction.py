"""
Narrated Milestone 5 demo: one rider, three stub drivers, Payment escrow, second-score auction.

    uv run python -m harness.demo_auction [--pricing first_price|second_score] [--speed 20]

Starts a throw-away XMPP server, runs the protocol and prints the full trace of the auction
(every state transition, bid, ranking, award, rejection and the rider's notification).
"""
import argparse
import asyncio
import logging
import sys
import uuid

from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.stubs import ScriptedRider, StubDriver
from common import clock
from common.auction import AuctionRules
from common.ontology import TripRequest
from common.settings import VALUE_OF_DETOUR, VALUE_OF_REPUTATION, VALUE_OF_TIME
from harness.launcher import Harness
from harness.scenario import Scenario


async def run(pricing: str, speed: float) -> int:
    h = Harness(Scenario.load())
    await h.ensure_server()
    port, n = h.scenario.xmpp.port, uuid.uuid4().hex[:4]
    clock.start(speed)
    rules = AuctionRules(pricing=pricing, value_of_time=VALUE_OF_TIME,
                         value_of_detour=VALUE_OF_DETOUR, value_of_reputation=VALUE_OF_REPUTATION)
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=50.0)
    specs = [("Ana", 8.0, 150.0), ("Bruno", 9.0, 60.0), ("Carla", 7.5, 600.0)]
    drivers = [StubDriver(f"{name.lower()}_{n}@localhost", "pw", port=port, price=p, eta=e)
               for name, p, e in specs]
    hub = MicrohubAgent(f"hub_{n}@localhost", "pw", port=port, hub_id="hub_demo", rules=rules,
                        driver_jids=[d.jid for d in drivers], payment_jid=pay.jid,
                        t_auction=40.0, t_award=30.0, t_escrow=40.0)
    rj = f"rita_{n}@localhost"
    req = TripRequest("req-1", rj, "hub_demo", "n00003", 0.0, 7200.0, 600.0, 20.0, False, False)
    rider = ScriptedRider(rj, "pw", port=port, hub_jid=hub.jid, payment_jid=pay.jid, request=req,
                          auto_request=False)
    agents = [pay, *drivers, hub, rider]
    try:
        for a in agents:
            await a.start(auto_register=True)
        print(f"\n== {pricing} auction: 1 rider (max fare 20.00) vs drivers "
              + ", ".join(f"{n_} {p:.2f}€/{e:.0f}s" for n_, p, e in specs) + " ==\n")
        rider.send_request()
        await asyncio.wait_for(rider.done.wait(), 30)
        await asyncio.sleep(0.5)
        ctx = hub.finished[-1]
        for e in ctx.trace:
            extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "state", "event"))
            print(f"t={e['t']:7.1f}  {e['state']:<15} {e['event']:<28} {extra}")
        m = rider.last("matched")
        print(f"\nRider informed: driver={m.driver_id} fare={m.final_fare:.2f} "
              f"pickup_eta=t+{m.pickup_eta - ctx.started_at:.0f}s" if m else "\nRider: no match")
        for d in drivers:
            print(f"  {d.jid.user:<14} received: {[p for p, _ in d.inbox]}")
        print(f"  escrow: {[(r.escrow_id, pay.ledger.status(r.escrow_id).value) for r in pay.ledger.pool_escrows(ctx.pool_id)]}")
        return 0
    finally:
        for a in reversed(agents):
            await a.stop()
        await h.stop()
        clock.reset()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pricing", default="second_score", choices=["second_score", "first_price"])
    ap.add_argument("--speed", type=float, default=20.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(a.pricing, a.speed))


if __name__ == "__main__":
    sys.exit(main())

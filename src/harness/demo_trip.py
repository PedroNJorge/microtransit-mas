"""
Narrated Milestone 6 demo: two real drivers compete for one rider, the winner drives the trip.

    uv run python -m harness.demo_trip [--speed 30]

Runs on the small grid network (hub n1_1; one driver waits at the hub, the other three hops away).
Prints the auction trace, each driver's state changes (DriverStatusProtocol), what the rider hears
and the final ledger.
"""
import argparse
import asyncio
import logging
import sys
import uuid

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.stubs import ScriptedRider
from common import clock
from common.graph import build_grid_network
from common.ontology import TripRequest
from harness.launcher import Harness
from harness.scenario import Scenario


async def run(speed: float) -> int:
    h = Harness(Scenario.load())
    await h.ensure_server()
    port, n = h.scenario.xmpp.port, uuid.uuid4().hex[:4]
    clock.start(speed)
    net = build_grid_network()
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=50.0)
    hub = MicrohubAgent(f"hub_{n}@localhost", "pw", port=port, hub_id="n1_1", network=net,
                        payment_jid=pay.jid, t_auction=90.0, t_award=60.0, t_escrow=90.0)
    drivers = [DriverAgent(f"{name}_{n}@localhost", "pw", port=port, start_hub=start, network=net,
                           hub_jids=[hub.jid], payment_jid=pay.jid, t_provisional=300.0)
               for name, start in (("ana", "n1_1"), ("bruno", "n4_1"))]
    hub.driver_jids = [str(d.jid.bare) for d in drivers]
    rj = f"rita_{n}@localhost"
    rider = ScriptedRider(rj, "pw", port=port, hub_jid=hub.jid, payment_jid=pay.jid, auto_request=False,
                          request=TripRequest("req-1", rj, "n1_1", "n2_2", 0.0, 7200.0, 600.0, 20.0, False, False))
    agents = [pay, hub, *drivers, rider]
    try:
        for a in agents:
            await a.start(auto_register=True)
        await asyncio.sleep(1.0)
        print("\n== Ana waits at the hub (n1_1), Bruno is 3 hops away (n4_1); Rita wants n1_1 -> n2_2 ==\n")
        rider.send_request()
        await asyncio.wait_for(rider.trip_done.wait(), 90)
        await asyncio.sleep(1.5)
        ctx = hub.finished[-1]
        for e in ctx.trace:
            if e["event"] in ("auction opens", "bid", "winner", "reject-proposal", "driver confirmed", "rider informed"):
                extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "state", "event"))
                print(f"t={e['t']:7.1f}  {e['event']:<18} {extra}")
        for d in drivers:
            seq = [s.state.value for s in d.status_log]
            dedup = [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]
            print(f"\n{d.jid.user:<12} states: {' -> '.join(dedup)}   (now at {d.location})")
        print("\nRider heard:", " -> ".join(rider.kinds()))
        print(f"Ledger: driver {pay.ledger.balance(str(drivers[0].jid.bare)):.2f}, "
              f"rider {pay.ledger.balance(str(rider.jid.bare)):.2f}, conserved={pay.ledger.check_conservation()}")
        return 0
    finally:
        for a in reversed(agents):
            await a.stop()
        await h.stop()
        clock.reset()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--speed", type=float, default=30.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(a.speed))


if __name__ == "__main__":
    sys.exit(main())

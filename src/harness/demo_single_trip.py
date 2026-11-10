"""
Narrated Milestone 7 demo: one real rider, one real driver, a hub, Payment and Reputation.

    uv run python -m harness.demo_single_trip [--speed 30]

Prints the rider's state changes, the protocol messages it saw, the money and the reputation update.
"""
import argparse
import asyncio
import logging
import sys
import uuid

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import RiderAgent
from common import clock
from common.graph import build_grid_network
from harness.launcher import Harness
from harness.scenario import Scenario


async def run(speed: float) -> int:
    h = Harness(Scenario.load())
    await h.ensure_server()
    port, n = h.scenario.xmpp.port, uuid.uuid4().hex[:4]
    clock.start(speed)
    net = build_grid_network()
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=50.0)
    rep = ReputationAgent(f"reputation_{n}@localhost", "pw", port=port)
    hub = MicrohubAgent(f"hub_{n}@localhost", "pw", port=port, hub_id="n1_1", network=net, payment_jid=pay.jid,
                        reputation_jid=rep.jid, t_auction=90.0, t_award=60.0, t_escrow=90.0)
    rep.hub_jids = [str(hub.jid.bare)]
    drv = DriverAgent(f"ana_{n}@localhost", "pw", port=port, start_hub="n1_1", network=net,
                      hub_jids=[hub.jid], payment_jid=pay.jid, t_provisional=300.0)
    hub.driver_jids = [str(drv.jid.bare)]
    rider = RiderAgent(f"rita_{n}@localhost", "pw", port=port, hub_jids={"n1_1": hub.jid}, network=net,
                       payment_jid=pay.jid, reputation_jid=rep.jid, ready_delay=5.0)
    agents = [pay, rep, hub, drv, rider]
    try:
        for a in agents:
            await a.start(auto_register=True)
        await asyncio.sleep(1.0)
        print("\n== Rita asks for a ride n1_1 -> n2_2 (max fare 20.00); Ana is the only driver ==\n")
        rider.request_trip("n2_2", 20.0)
        await asyncio.wait_for(rider.ended.wait(), 90)
        await asyncio.sleep(1.5)
        t = rider.trips[-1]
        for when, a, b in rider.history:
            print(f"t={when:7.1f}  rider {a:<15} -> {b}")
        print(f"\nProposal : route {t.proposal.shared_route}, fare ceiling {t.proposal.split_fare:.2f}, "
              f"expected wait {t.proposal.estimated_wait:.0f}s")
        print(f"Match    : driver {t.match.driver_id.split('@')[0]}, fare {t.fare:.2f}")
        print(f"Trip     : driver arrived t={t.arrived_at:.0f}, boarded t={t.boarded_at:.0f} "
              f"(walk {rider.ready_delay:.0f}s + boarding dwell {drv.dwell_pickup:.0f}s), "
              f"dropped t={t.dropped_at:.0f}, ride {t.dropped_at - t.boarded_at:.0f}s, rating {t.rating.score}")
        did = str(drv.jid.bare)
        print(f"Money    : driver {pay.ledger.balance(did):.2f}, rider {pay.ledger.balance(str(rider.jid.bare)):.2f}, "
              f"settled={t.settlement is not None}")
        pub = rep.published(did)
        print(f"Reputation: {did.split('@')[0]} = {pub.score:.2f} ({pub.num_ratings} rating); "
              f"hub's copy = {hub.reputations.get(did, float('nan')):.2f}")
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

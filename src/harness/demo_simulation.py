"""
Ride simulation: many riders request trips at random times, a few drivers serve them, the hub pools
riders who can share (Milestone 9), Payment settles, riders rate.

    uv run python -m harness.demo_simulation [--riders 10] [--drivers 3] [--speed 30]
                                             [--span 600] [--t-pool 90] [--seed 1] [--solo-share 0.2]

`--span` is the sim-seconds over which requests arrive. Prints a ride-by-ride table, the pools that
formed, money per driver and reputation. Everything is seeded, so runs are repeatable.
"""
import argparse
import asyncio
import logging
import random
import sys
import uuid
from collections import defaultdict

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import RiderAgent
from common import clock
from common.graph import build_grid_network
from harness.launcher import Harness
from harness.scenario import Scenario

HUB = "n1_1"


def short(jid: str) -> str:
    return jid.split("@")[0].rsplit("_", 1)[0]


async def run(a) -> int:
    rng = random.Random(a.seed)
    h = Harness(Scenario.load())
    await h.ensure_server()
    port, n = h.scenario.xmpp.port, uuid.uuid4().hex[:4]
    clock.start(a.speed)
    net = build_grid_network()
    homes = [x for x in net.nodes_of_type("residential") if x != HUB and net.travel_time(HUB, x) <= net.hubs[HUB].coverage_radius]
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=100.0)
    rep = ReputationAgent(f"reputation_{n}@localhost", "pw", port=port)
    hub = MicrohubAgent(f"hub_{n}@localhost", "pw", port=port, hub_id=HUB, network=net, payment_jid=pay.jid,
                        reputation_jid=rep.jid, t_auction=60.0, t_award=40.0, t_escrow=60.0, t_pool=a.t_pool)
    rep.hub_jids = [str(hub.jid.bare)]
    starts = [HUB] + [rng.choice(list(net.hubs)) for _ in range(a.drivers - 1)]
    drivers = [DriverAgent(f"driver{i + 1}_{n}@localhost", "pw", port=port, start_hub=s, network=net,
                           hub_jids=[hub.jid], payment_jid=pay.jid, t_provisional=300.0,
                           dwell_pickup=10, dwell_dropoff=8) for i, s in enumerate(starts)]
    hub.driver_jids = [str(d.jid.bare) for d in drivers]
    riders = [RiderAgent(f"rider{i + 1}_{n}@localhost", "pw", port=port, hub_jids={HUB: hub.jid}, network=net,
                         payment_jid=pay.jid, reputation_jid=rep.jid, ready_delay=5.0)
              for i in range(a.riders)]
    agents = [pay, rep, hub, *drivers, *riders]
    plan = sorted((rng.uniform(0, a.span), i, rng.choice(homes), rng.choice([0.0, 120.0, 300.0, 600.0]),
                   rng.random() >= a.solo_share) for i in range(a.riders))
    try:
        for ag in agents:
            await ag.start(auto_register=True)
        await asyncio.sleep(1.0)
        print(f"\n== {a.riders} riders, {a.drivers} drivers, hub {HUB}, batch window {a.t_pool:.0f}s, "
              f"requests over {a.span:.0f}s ==\n")
        t0 = clock.now()
        for at, i, dest, tol, pooling in plan:
            await clock.sim_sleep(max(0.0, t0 + at - clock.now()))
            riders[i].request_trip(dest, 25.0, detour_tolerance=tol, pooling_willingness=pooling)
            print(f"t={clock.now():6.0f}  rider{i + 1} requests {HUB} -> {dest}"
                  f"  (detour tol {tol:.0f}s, {'pooling ok' if pooling else 'solo only'})")
        await asyncio.wait_for(asyncio.gather(*(r.ended.wait() for r in riders)), 900)
        await asyncio.sleep(2.0)

        print("\n-- rides --")
        print(f"{'rider':<8}{'dest':<7}{'pool':<15}{'with':<5}{'driver':<9}{'wait':>6}{'ride':>6}{'detour':>8}"
              f"{'fare':>7}{'disc':>6}  result")
        for r in riders:
            t = r.trips[-1]
            rec = hub.pools.get(t.pool_id)
            size = rec.pool.size if rec else 1
            wait = (t.boarded_at - t.requested_at) if t.boarded_at else 0
            ride = (t.dropped_at - t.boarded_at) if t.dropped_at else 0
            det = t.proposal.added_detour if t.proposal else 0
            disc = t.settlement.pooling_discount if t.settlement else 0
            print(f"{short(str(r.jid)):<8}{t.request.destination_home:<7}{t.pool_id.split('-', 1)[-1]:<15}{size:<5}"
                  f"{short(t.driver) if t.match else '-':<9}{wait:>5.0f}s{ride:>5.0f}s{det:>7.0f}s"
                  f"{t.fare:>7.2f}{disc:>6.2f}  {t.outcome}{' ' + t.reason if t.reason else ''}")
        done = [r.trips[-1] for r in riders if r.trips[-1].outcome == "completed"]
        pooled = {t.pool_id for t in done if hub.pools[t.pool_id].pool.size > 1}
        print(f"\n-- summary --\ncompleted {len(done)}/{len(riders)}; {len(pooled)} pooled trip(s) carried "
              f"{sum(hub.pools[p].pool.size for p in pooled)} riders; "
              f"{len({t.pool_id for t in done})} vehicle trips in total")
        earn = defaultdict(float)
        for t in done:
            earn[short(t.driver)] += t.fare
        for d in drivers:
            did = str(d.jid.bare)
            pub = rep.published(did)
            print(f"{short(did):<9} trips {len(d.completed)}  earned {pay.ledger.balance(did):6.2f}  "
                  f"reputation {pub.score:.2f} ({pub.num_ratings} ratings)" if pub else
                  f"{short(did):<9} trips {len(d.completed)}  earned {pay.ledger.balance(did):6.2f}")
        print(f"money conserved: {pay.ledger.check_conservation()}")
        return 0
    finally:
        for ag in reversed(agents):
            await ag.stop()
        await h.stop()
        clock.reset()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--riders", type=int, default=10)
    ap.add_argument("--drivers", type=int, default=3)
    ap.add_argument("--speed", type=float, default=30.0)
    ap.add_argument("--span", type=float, default=600.0, help="sim seconds over which requests arrive")
    ap.add_argument("--t-pool", type=float, default=90.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--solo-share", type=float, default=0.2, help="fraction of riders who refuse pooling")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())

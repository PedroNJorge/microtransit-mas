"""
Ride simulation on a road network with ALL its microhubs (default: the full Porto graph).

Riders request trips at random times to random homes; each rider goes to the nearest hub that
covers its home. Every hub runs its own batch window and pools riders (Milestone 9); all drivers
broadcast their status to all hubs, so hubs compete for them through the provisional lock.
Payment settles and riders rate.

    uv run python -m harness.demo_simulation [--graph config/porto_graph.json | --grid]
        [--riders 24] [--drivers 16] [--speed 60] [--span 1800] [--t-pool 120] [--seed 1]
        [--solo-share 0.2] [--hubs hub_trindade,hub_bolhao,...]

`--span` = sim seconds over which requests arrive. `--grid` uses the small 6x6 test grid.
Prints a ride table, per-hub statistics, driver earnings and reputation. Seeded and repeatable.
"""
import argparse
import asyncio
import logging
import random
import sys
import time
import uuid
from collections import defaultdict

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import RiderAgent
from common import clock
from common.graph import TransitNetwork, build_grid_network
from harness.launcher import Harness
from harness.scenario import PROJECT_ROOT, Scenario


def short(jid: str) -> str:
    return jid.split("@")[0].rsplit("_", 1)[0]


async def run(a) -> int:
    rng = random.Random(a.seed)
    h = Harness(Scenario.load())
    await h.ensure_server()
    port, n = h.scenario.xmpp.port, uuid.uuid4().hex[:4]
    net = build_grid_network() if a.grid else TransitNetwork.from_file(PROJECT_ROOT / a.graph)
    hub_ids = a.hubs.split(",") if a.hubs else sorted(net.hubs)
    unknown = [x for x in hub_ids if x not in net.hubs]
    if unknown:
        raise SystemExit(f"unknown hub(s) {unknown}; the graph has {sorted(net.hubs)}")
    t_build = time.time()
    homes = sorted({x for x in net.nodes_of_type("residential")
                    if any(net.travel_time(hid, x) <= net.hubs[hid].coverage_radius for hid in hub_ids)})
    clock.start(a.speed)
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=100.0)
    rep = ReputationAgent(f"reputation_{n}@localhost", "pw", port=port)
    hubs = {hid: MicrohubAgent(f"{hid}_{n}@localhost", "pw", port=port, hub_id=hid, network=net,
                               payment_jid=pay.jid, reputation_jid=rep.jid, t_auction=60.0, t_award=40.0,
                               t_escrow=60.0, t_pool=a.t_pool) for hid in hub_ids}
    hub_jids = [hb.jid for hb in hubs.values()]
    rep.hub_jids = [str(j.bare) for j in hub_jids]
    starts = [hub_ids[i % len(hub_ids)] for i in range(a.drivers)]            # spread over the hubs
    drivers = [DriverAgent(f"driver{i + 1:02d}_{n}@localhost", "pw", port=port, start_hub=s, network=net,
                           hub_jids=hub_jids, payment_jid=pay.jid, t_provisional=300.0,
                           dwell_pickup=10, dwell_dropoff=8) for i, s in enumerate(starts)]
    for hb in hubs.values():
        hb.driver_jids = [str(d.jid.bare) for d in drivers]
    riders = [RiderAgent(f"rider{i + 1:02d}_{n}@localhost", "pw", port=port,
                         hub_jids={hid: hb.jid for hid, hb in hubs.items()}, network=net,
                         payment_jid=pay.jid, reputation_jid=rep.jid, ready_delay=5.0)
              for i in range(a.riders)]
    agents = [pay, rep, *hubs.values(), *drivers, *riders]
    plan = sorted((rng.uniform(0, a.span), i, rng.choice(homes), rng.choice([0.0, 120.0, 300.0, 600.0]),
                   rng.random() >= a.solo_share) for i in range(a.riders))
    try:
        for ag in agents:
            await ag.start(auto_register=True)
        await asyncio.sleep(2.0)
        print(f"\n== {a.riders} riders, {a.drivers} drivers, {len(hub_ids)} hubs "
              f"({net.g.number_of_nodes()} nodes, {net.g.number_of_edges()} edges), batch window "
              f"{a.t_pool:.0f}s, requests over {a.span:.0f}s, speed x{a.speed:.0f} ==\n")
        t0, sent = clock.now(), []
        for at, i, dest, tol, pooling in plan:
            await clock.sim_sleep(max(0.0, t0 + at - clock.now()))
            req = riders[i].request_trip(dest, 25.0, detour_tolerance=tol, pooling_willingness=pooling)
            if req is None:
                print(f"t={clock.now():6.0f}  rider{i + 1:02d}: no hub covers {dest}")
                continue
            sent.append(i)
            print(f"t={clock.now():6.0f}  rider{i + 1:02d} requests {req.origin_node} -> {dest} "
                  f"(tol {tol:.0f}s, {'pooling ok' if pooling else 'solo only'})")
        await asyncio.wait_for(asyncio.gather(*(riders[i].ended.wait() for i in sent)), 3600 / a.speed + 600)
        await asyncio.sleep(2.0)

        print("\n-- rides --")
        print(f"{'rider':<8}{'hub':<20}{'pool':<13}{'n':<3}{'driver':<10}{'wait':>6}{'ride':>6}{'detour':>8}"
              f"{'fare':>7}{'disc':>6}  result")
        rows = [riders[i].trips[-1] for i in sent]
        for i, t in zip(sent, rows):
            hid = t.request.origin_node
            rec = hubs[hid].pools.get(t.pool_id)
            size = rec.pool.size if rec else 1
            wait = (t.boarded_at - t.requested_at) if t.boarded_at else 0
            ride = (t.dropped_at - t.boarded_at) if t.dropped_at else 0
            det = t.proposal.added_detour if t.proposal else 0
            disc = t.settlement.pooling_discount if t.settlement else 0
            print(f"{short(str(riders[i].jid)):<8}{hid[4:]:<20}{t.pool_id.rsplit('-', 1)[-1]:<13}{size:<3}"
                  f"{short(t.driver) if t.match else '-':<10}{wait:>5.0f}s{ride:>5.0f}s{det:>7.0f}s"
                  f"{t.fare:>7.2f}{disc:>6.2f}  {t.outcome}{' ' + t.reason if t.reason else ''}")

        print("\n-- per hub --")
        print(f"{'hub':<22}{'requests':>9}{'completed':>10}{'pooled trips':>13}{'riders pooled':>14}{'avg wait':>9}")
        for hid in hub_ids:
            mine = [t for t in rows if t.request.origin_node == hid]
            done = [t for t in mine if t.outcome == "completed"]
            pooled = {t.pool_id for t in done if hubs[hid].pools[t.pool_id].pool.size > 1}
            waits = [t.boarded_at - t.requested_at for t in done]
            print(f"{hid[4:]:<22}{len(mine):>9}{len(done):>10}{len(pooled):>13}"
                  f"{sum(hubs[hid].pools[p].pool.size for p in pooled):>14}"
                  f"{(sum(waits) / len(waits) if waits else 0):>8.0f}s")
        done = [t for t in rows if t.outcome == "completed"]
        trips = {(t.request.origin_node, t.pool_id) for t in done}
        print(f"\ncompleted {len(done)}/{len(rows)} ride requests using {len(trips)} vehicle trips")
        print("\n-- drivers --")
        for d in drivers:
            did = str(d.jid.bare)
            pub = rep.published(did)
            print(f"{short(did):<10} trips {len(d.completed):<3} earned {pay.ledger.balance(did):6.2f}  "
                  + (f"reputation {pub.score:.2f} ({pub.num_ratings} ratings)" if pub else "no ratings yet"))
        print(f"\nmoney conserved: {pay.ledger.check_conservation()}")
        return 0
    finally:
        for ag in reversed(agents):
            await ag.stop()
        await h.stop()
        clock.reset()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default="config/porto_graph.json")
    ap.add_argument("--grid", action="store_true", help="use the small 6x6 test grid instead of --graph")
    ap.add_argument("--hubs", default="", help="comma-separated hub ids (default: all hubs of the graph)")
    ap.add_argument("--riders", type=int, default=24)
    ap.add_argument("--drivers", type=int, default=16)
    ap.add_argument("--speed", type=float, default=60.0)
    ap.add_argument("--span", type=float, default=1800.0, help="sim seconds over which requests arrive")
    ap.add_argument("--t-pool", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--solo-share", type=float, default=0.2, help="fraction of riders who refuse pooling")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())

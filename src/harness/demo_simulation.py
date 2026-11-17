"""
Ride simulation on a road network with ALL its microhubs (default: the full Porto graph).

Riders request trips at random times to random homes; each rider goes to the nearest hub that
covers its home. Every hub runs its own batch window and pools riders (Milestone 9); all drivers
broadcast their status to all hubs, so hubs compete for them through the provisional lock.
Payment settles and riders rate.

    uv run python -m harness.demo_simulation [--graph config/porto_graph.json | --grid]
        [--riders 24] [--drivers 16] [--speed 60] [--span 1800] [--t-pool 120] [--seed 1]
        [--solo-share 0.2] [--hubs hub_trindade,hub_bolhao,...]
        [--demand clustered|uniform] [--hub-policy balanced|nearest] [--legacy-market] [--brains] [--breakdown]

`--demand clustered` (default) draws destinations around hotspots and request times from rush peaks,
with detour tolerance proportional to the trip; `--hub-policy balanced` lets riders pick among nearby
covering hubs. `--legacy-market` switches off the market fixes (tariff-based reserve, pickup-radius
driver selection, retry delay, adaptive window) to compare with the earlier behaviour.
`--brains` gives every driver a BDI brain (SPADE-BDI, agents/brains/driver.asl) that decides where to
wait while idle; without it idle drivers stay where their last trip ended. Same seed + and without
`--brains` is the comparison.

`--span` = sim seconds over which requests arrive. `--grid` uses the small 6x6 test grid.
Prints a ride table, per-hub statistics, driver earnings and reputation. Seeded and repeatable.
"""
import argparse
import asyncio
import logging
import random
import sys
import time
from collections import Counter
import uuid
from collections import defaultdict

from agents.driver import DriverAgent
from agents.microhub import MicrohubAgent
from agents.payment import PaymentAgent
from agents.reputation import ReputationAgent
from agents.rider import RiderAgent, balanced_hub_policy
from harness.demand import DemandGenerator
from harness.metrics import PHASES, summarize, wait_breakdown
from common import clock
from common.graph import TransitNetwork, build_grid_network
from common.settings import MAX_PICKUP_ETA, RESERVE_MARGIN, RETRY_DELAY, WINDOW_SLACK
from harness.launcher import Harness
from harness.scenario import PROJECT_ROOT, Scenario


def short(jid: str) -> str:
    return jid.split("@")[0].rsplit("_", 1)[0]


async def run(a) -> dict:
    """Run one simulation; prints the report unless `a.quiet`. Returns the raw results (see `summary`)."""
    say = (lambda *x, **k: None) if getattr(a, "quiet", False) else print
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
    market = {} if a.legacy_market else dict(reserve_margin=RESERVE_MARGIN, max_pickup_eta=MAX_PICKUP_ETA,
                                              retry_delay=RETRY_DELAY, window_slack=WINDOW_SLACK,
                                              max_attempts=4)
    pay = PaymentAgent(f"payment_{n}@localhost", "pw", port=port, default_rider_balance=100.0)
    rep = ReputationAgent(f"reputation_{n}@localhost", "pw", port=port)
    hubs = {hid: MicrohubAgent(f"{hid}_{n}@localhost", "pw", port=port, hub_id=hid, network=net,
                               payment_jid=pay.jid, reputation_jid=rep.jid, t_auction=60.0, t_award=40.0,
                               t_escrow=60.0, t_pool=a.t_pool, broadcast_demand=a.brains, **market) for hid in hub_ids}
    hub_jids = [hb.jid for hb in hubs.values()]
    rep.hub_jids = [str(j.bare) for j in hub_jids]
    starts = [hub_ids[i % len(hub_ids)] for i in range(a.drivers)]            # spread over the hubs
    drivers = [DriverAgent(f"driver{i + 1:02d}_{n}@localhost", "pw", port=port, start_hub=s, network=net,
                           hub_jids=hub_jids, payment_jid=pay.jid, t_provisional=300.0,
                           dwell_pickup=10, dwell_dropoff=8,
                           brain_jid=f"brain{i + 1:02d}_{n}@localhost" if a.brains else None,
                           return_policy=a.return_policy)
               for i, s in enumerate(starts)]
    brains = []
    if a.brains:
        from agents.brain import DriverBrain                      # needs `spade_bdi`
        brains = [DriverBrain(f"brain{i + 1:02d}_{n}@localhost", "pw", driver_jid=str(d.jid.bare), port=port,
                              bid_policy=a.brain_bids)
                  for i, d in enumerate(drivers)]
    for hb in hubs.values():
        hb.driver_jids = [str(d.jid.bare) for d in drivers]
    riders = [RiderAgent(f"rider{i + 1:02d}_{n}@localhost", "pw", port=port,
                         hub_jids={hid: hb.jid for hid, hb in hubs.items()}, network=net,
                         payment_jid=pay.jid, reputation_jid=rep.jid, ready_delay=5.0,
                         hub_policy=balanced_hub_policy(random.Random(a.seed * 1000 + i))
                         if a.hub_policy == "balanced" else None)
              for i in range(a.riders)]
    agents = [pay, rep, *hubs.values(), *brains, *drivers, *riders]     # brains before drivers: percepts need a listener
    plan = DemandGenerator(net, homes, a.seed, solo_share=a.solo_share,
                           clustered=a.demand == "clustered").generate(a.riders, a.span)
    try:
        for ag in agents:
            await asyncio.wait_for(ag.start(auto_register=True), 60)
        await asyncio.sleep(2.0)
        say(f"\n== {a.riders} riders, {a.drivers} drivers, {len(hub_ids)} hubs "
              f"({net.g.number_of_nodes()} nodes, {net.g.number_of_edges()} edges), batch window "
              f"{a.t_pool:.0f}s, requests over {a.span:.0f}s, speed x{a.speed:.0f} ==\n")
        t0, sent = clock.now(), []
        for d in plan:
            await clock.sim_sleep(max(0.0, t0 + d.at - clock.now()))
            r = riders[d.rider]
            hid = r.choose_hub(d.destination)
            direct = net.travel_time(hid, d.destination) if hid else 0.0
            req = r.request_trip(d.destination, 25.0, detour_tolerance=d.tol_frac * direct, hub_id=hid,
                                 pooling_willingness=d.pooling) if hid else None
            if req is None:
                say(f"t={clock.now():6.0f}  rider{d.rider + 1:02d}: no hub covers {d.destination}")
                continue
            sent.append(d.rider)
            say(f"t={clock.now():6.0f}  rider{d.rider + 1:02d} requests {req.origin_node} -> {d.destination} "
                  f"(direct {direct:.0f}s, tol {req.detour_tolerance:.0f}s, "
                  f"{'pooling ok' if d.pooling else 'solo only'})")
        await asyncio.wait_for(asyncio.gather(*(riders[i].ended.wait() for i in sent)), 3600 / a.speed + 600)
        await asyncio.sleep(2.0)

        say("\n-- rides --")
        say(f"{'rider':<8}{'hub':<20}{'pool':<13}{'n':<3}{'driver':<10}{'wait':>6}{'ride':>6}{'detour':>8}"
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
            say(f"{short(str(riders[i].jid)):<8}{hid[4:]:<20}{t.pool_id.rsplit('-', 1)[-1]:<13}{size:<3}"
                  f"{short(t.driver) if t.match else '-':<10}{wait:>5.0f}s{ride:>5.0f}s{det:>7.0f}s"
                  f"{t.fare:>7.2f}{disc:>6.2f}  {t.outcome}{' ' + t.reason if t.reason else ''}")

        say("\n-- per hub --")
        say(f"{'hub':<22}{'requests':>9}{'completed':>10}{'pooled trips':>13}{'riders pooled':>14}{'avg wait':>9}")
        for hid in hub_ids:
            mine = [t for t in rows if t.request.origin_node == hid]
            done = [t for t in mine if t.outcome == "completed"]
            pooled = {t.pool_id for t in done if hubs[hid].pools[t.pool_id].pool.size > 1}
            waits = [t.boarded_at - t.requested_at for t in done]
            say(f"{hid[4:]:<22}{len(mine):>9}{len(done):>10}{len(pooled):>13}"
                  f"{sum(hubs[hid].pools[p].pool.size for p in pooled):>14}"
                  f"{(sum(waits) / len(waits) if waits else 0):>8.0f}s")
        done = [t for t in rows if t.outcome == "completed"]
        trips = {(t.request.origin_node, t.pool_id) for t in done}
        say(f"\ncompleted {len(done)}/{len(rows)} ride requests using {len(trips)} vehicle trips")
        failed = [(i, t) for i, t in zip(sent, rows) if t.outcome != "completed"]
        if failed:
            say("\n-- rides that did not complete --")
            for i, t in failed:
                ctxs = [c for c in hubs[t.request.origin_node].finished if c.pool_id == t.pool_id]
                tries = "; ".join(f"attempt {c.attempt}: {c.outcome}, {len(c.bids)} bid(s), "
                                  f"{len(c.recipients)} called, refusals {dict(Counter(c.refusals.values()))}"
                                  for c in ctxs) or "no auction ran"
                say(f"rider{i + 1:02d} @ {t.request.origin_node[4:]}: {t.outcome} {t.reason} | {tries}")
        ctxs = [c for hb in hubs.values() for c in hb.finished if c.outcome == "matched"]
        if ctxs:
            single = [c for c in ctxs if len(c.bids) == 1]
            paid = [c.match.final_fare / c.cfp.max_total_fare for c in ctxs if c.cfp.max_total_fare]
            say(f"\n-- auction health -- {len(ctxs)} matched auctions; {len(single)} with a single bid "
                  f"(monopoly); mean bids {sum(len(c.bids) for c in ctxs) / len(ctxs):.1f}; "
                  f"price/reserve mean {sum(paid) / len(paid):.2f}, "
                  f"{sum(1 for x in paid if x >= 0.999)} at the full reserve")
            allref = Counter(r for hb in hubs.values() for c in hb.finished for r in c.refusals.values())
            say(f"driver refusals over all attempts: {dict(allref)}")
        waits = sorted(t.boarded_at - t.requested_at for t in done)
        if waits:
            q = lambda f: waits[min(len(waits) - 1, int(f * len(waits)))]
            say(f"\n-- rider wait (request -> boarding) -- mean {sum(waits) / len(waits):.0f}s, "
                  f"median {q(0.5):.0f}s, p90 {q(0.9):.0f}s, max {waits[-1]:.0f}s")
        bd = [(i, t, wait_breakdown(t, hubs[t.request.origin_node].pools[t.pool_id].times, riders[i].history))
              for i, t in zip(sent, rows) if t.outcome == "completed" and t.pool_id in hubs[t.request.origin_node].pools]
        bd = [(i, t, b) for i, t, b in bd if b]
        if bd:
            say("\n-- where the wait goes (sim seconds; request -> boarding) --")
            say(f"{'phase':<10}{'mean':>7}{'median':>8}{'p90':>7}{'share':>8}")
            for k, (m, med, p90, share) in summarize([b for _, _, b in bd]).items():
                say(f"{k:<10}{m:>7.0f}{med:>8.0f}{p90:>7.0f}{share:>7.0%}" if k != "total" else
                      f"{'total':<10}{m:>7.0f}{med:>8.0f}{p90:>7.0f}")
            by_hub = defaultdict(list)
            for _, t, b in bd:
                by_hub[t.request.origin_node[4:]].append(b)
            say(f"\n{'hub':<22}{'n':>3}" + "".join(f"{p:>9}" for p in PHASES) + f"{'total':>8}")
            for hid, rs in sorted(by_hub.items()):
                say(f"{hid:<22}{len(rs):>3}" + "".join(f"{sum(r[p] for r in rs) / len(rs):>9.0f}" for p in PHASES)
                      + f"{sum(r['total'] for r in rs) / len(rs):>8.0f}")
            if a.breakdown:
                say(f"\n{'rider':<8}{'hub':<20}" + "".join(f"{p:>9}" for p in PHASES) + f"{'total':>8}")
                for i, t, b in bd:
                    say(f"{short(str(riders[i].jid)):<8}{t.request.origin_node[4:]:<20}"
                          + "".join(f"{b[p]:>9.0f}" for p in PHASES) + f"{b['total']:>8.0f}")
        if a.brains:
            say(f"-- BDI brains -- {sum(len(d.cruises) for d in drivers)} cruise moves by "
                  f"{sum(1 for d in drivers if d.cruises)}/{len(drivers)} drivers; "
                  f"brain refusals: {dict(Counter(r for b in brains for r in b.refusals))}"
                  + (f"; bid markups set {sum(len(d.markups) for d in drivers)} times" if a.brain_bids else ""))
        say("\n-- drivers --")
        for d in drivers:
            did = str(d.jid.bare)
            pub = rep.published(did)
            say(f"{short(did):<10} trips {len(d.completed):<3} cruises {len(d.cruises):<3} "
                  f"earned {pay.ledger.balance(did):6.2f}  "
                  + (f"reputation {pub.score:.2f} ({pub.num_ratings} ratings)" if pub else "no ratings yet"))
        conserved = pay.ledger.check_conservation()
        say(f"\nmoney conserved: {conserved}")
        return dict(
            requested=len(rows), completed=len(done), vehicle_trips=len(trips), conserved=conserved,
            waits=[t.boarded_at - t.requested_at for t in done],
            breakdowns=[b for _, _, b in bd],
            pooled_riders=sum(hubs[t.request.origin_node].pools[t.pool_id].pool.size > 1 for t in done),
            cruises=sum(len(d.cruises) for d in drivers), returns=sum(len(d.returns) for d in drivers),
            markup_mean=(sum(d.bid_markup for d in drivers) / len(drivers)), markup_changes=sum(len(d.markups) for d in drivers),
            fares=[t.fare for t in done],
            matched=len(ctxs), single_bid=sum(1 for c in ctxs if len(c.bids) == 1),
            price_over_reserve=[c.match.final_fare / c.cfp.max_total_fare for c in ctxs if c.cfp.max_total_fare])
    finally:
        # Agents stop one after the other, and the drivers' last status messages reach hubs whose
        # behaviours are already gone; SPADE logs each as a warning. Expected at shutdown, so mute it.
        logging.getLogger("spade.Agent").setLevel(logging.ERROR)
        for ag in reversed(agents):
            try:
                await asyncio.wait_for(ag.stop(), 10)           # a stuck agent must not block the next run
            except Exception as e:
                logging.getLogger(__name__).error("%s did not stop cleanly: %r", ag.jid, e)
        try:
            await asyncio.wait_for(h.stop(), 20)
        except Exception:
            pass
        clock.reset()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], add_help=False)
    ap.add_argument("--graph", default="config/porto_graph.json")
    ap.add_argument("--grid", action="store_true", help="use the small 6x6 test grid instead of --graph")
    ap.add_argument("--hubs", default="", help="comma-separated hub ids (default: all hubs of the graph)")
    ap.add_argument("--riders", type=int, default=24)
    ap.add_argument("--drivers", type=int, default=16)
    ap.add_argument("--speed", type=float, default=60.0)
    ap.add_argument("--span", type=float, default=1800.0, help="sim seconds over which requests arrive")
    ap.add_argument("--t-pool", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--demand", choices=["clustered", "uniform"], default="clustered")
    ap.add_argument("--hub-policy", choices=["balanced", "nearest"], default="balanced")
    ap.add_argument("--breakdown", action="store_true", help="also print the wait breakdown of every ride")
    ap.add_argument("--brains", action="store_true", help="give every driver a BDI brain (needs spade_bdi)")
    ap.add_argument("--return-policy", choices=["none", "start_hub", "nearest_hub"], default="none",
                    help="fixed baseline: idle drivers drive back to their start / the nearest hub after a trip")
    ap.add_argument("--brain-bids", action="store_true",
                    help="the brain also sets each driver's bid markup (opportunity cost); needs --brains")
    ap.add_argument("--legacy-market", action="store_true", help="switch off the market fixes")
    ap.add_argument("--solo-share", type=float, default=0.2, help="fraction of riders who refuse pooling")
    return ap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(parents=[build_parser()], description=__doc__.split("\n\n")[0])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())

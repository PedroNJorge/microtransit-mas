import asyncio
import logging
import copy
import random
import signal
import sys
from pathlib import Path

# Garante que importa a pasta src corretamente
sys.path.insert(0, str(Path(__file__).parent / "src"))

from agents.rider import IDLE, RiderAgent
from common import clock
from common.graph import parse_edge_key
from agents.driver import DriverAgent
from common.ws_server import broadcast_event, start_ws_server
from harness.launcher import Harness
from harness.scenario import Scenario


LOG_PATH = Path("viz_demo.log")


def vprint(*args):
    """print() that ALSO appends to viz_demo.log, so the demo's own lines survive the agents' message flood."""
    text = " ".join(str(a) for a in args)
    print(text, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def get_node_latlon(network, node_id):
    """Extrai (lat, lon) diretamente do grafo do NetworkX."""
    if not node_id:
        return None, None
    candidates = [node_id, f"hub_{node_id}"]
    for cid in candidates:
        if cid in network.g.nodes:
            ndata = network.g.nodes[cid]
            lat = ndata.get("lat") or ndata.get("y")
            lon = ndata.get("lon") or ndata.get("x")
            if lat is not None and lon is not None:
                return float(lat), float(lon)
    return None, None


class RoadWatcher:
    """Mirrors the CLOSED roads of the network (scenario 1's `block_road` event) to the map.

    Sends  ROAD_BLOCKED {id, segments: [[[lat, lon], [lat, lon]], ...], start, end}  when a closure starts
    (and again every few seconds, so a browser that connects late still sees it) and
    ROAD_CLEARED {id}  when it ends. Each street is sent once even if both directions are closed.
    """

    def __init__(self, network):
        self.network = network
        self.sent = {}                                   # disruption id -> payload

    def current(self, now):
        out = {}
        for d in self.network.active_disruptions(now):
            if not d.closed:                             # slow traffic is not a blocked street
                continue
            segments, seen = [], set()
            for key in d.affected_edges:
                a, b = parse_edge_key(key)
                if frozenset((a, b)) in seen:
                    continue
                seen.add(frozenset((a, b)))
                (la, lo), (lb, lob) = get_node_latlon(self.network, a), get_node_latlon(self.network, b)
                if None not in (la, lo, lb, lob):
                    segments.append([[la, lo], [lb, lob]])
            out[d.disruption_id] = {"id": d.disruption_id, "segments": segments,
                                    "start": d.start_time, "end": d.end_time}
        return out

    async def sync(self, now, resend=False):
        cur = self.current(now)
        for did, payload in cur.items():
            if resend or did not in self.sent:
                if did not in self.sent and not payload["segments"]:
                    vprint(f"[VIZ-DEMO] AVISO: {did!r} não tem coordenadas lat/lon nos nós do grafo -> "
                          f"nada para desenhar a vermelho.")
                if did not in self.sent:
                    vprint(f"[VIZ-DEMO] t={now:.0f}s: street closure {did!r} -> map "
                          f"({len(payload['segments'])} segments, until t={payload['end']:.0f}s)")
                await broadcast_event("ROAD_BLOCKED", payload)
        for did in set(self.sent) - set(cur):
            vprint(f"[VIZ-DEMO] t={now:.0f}s: street closure {did!r} cleared")
            await broadcast_event("ROAD_CLEARED", {"id": did})
        self.sent = cur


class TrafficWatcher:
    """Mirrors heavy traffic (scenario 2: slower, still open roads) to the map, to be drawn in ORANGE.

    TRAFFIC_LEVEL   {id, severity, scope: "all"}                      every street is slower
                    {id, severity, scope: "edges", edges: ["a|b"..]}  only these streets are
    TRAFFIC_CLEARED {id}                                              that slowdown is over
    The scenario's `profile` (e.g. x1.5 -> x3 -> x1.5) is a chain of consecutive ids, so the level on the
    map follows it. Closed roads are not traffic (they are RoadWatcher's, in red).
    """

    def __init__(self, network):
        self.network = network
        self.sent = {}

    def current(self, now):
        out, n_all = {}, self.network.g.number_of_edges()
        for d in self.network.active_disruptions(now):
            if d.closed:
                continue
            payload = {"id": d.disruption_id, "severity": d.severity, "start": d.start_time, "end": d.end_time}
            if len(set(d.affected_edges)) >= n_all:
                payload["scope"] = "all"
            else:
                payload.update(scope="edges", edges=sorted(set(d.affected_edges)))
            out[d.disruption_id] = payload
        return out

    async def sync(self, now, resend=False):
        cur = self.current(now)
        for did, payload in cur.items():
            if resend or did not in self.sent:
                if did not in self.sent:
                    vprint(f"[VIZ-DEMO] t={now:.0f}s: traffic x{payload['severity']:g} ({payload['scope']}) -> map")
                await broadcast_event("TRAFFIC_LEVEL", payload)
        for did in set(self.sent) - set(cur):
            await broadcast_event("TRAFFIC_CLEARED", {"id": did})
        self.sent = cur


WAITING_STATES = ("REQUESTING", "AWAITING_POOL", "DECIDING", "AWAITING_MATCH")
ACTIVE_STATES = ("DRIVER_EN_ROUTE", "IN_TRIP")


RECENT_WINDOW = 180.0       # sim seconds: "requests received recently"
GOOD_OUTCOMES = ("completed",)


class HubLoadWatcher:
    """How busy each hub is right now: HUB_LOAD {hub_id, recent, waiting, active, failed, drivers}.

    recent  = trip requests the hub received in the last RECENT_WINDOW sim seconds (a burst shows up here,
              even if the hub clears the queue in seconds, because requests are answered fast)
    waiting = requested but no driver assigned yet
    active  = driver on the way, or the ride itself
    failed  = recent requests that ended WITHOUT a trip (unmatched, timed out...), i.e. riders left unserved
    drivers = drivers standing at the hub's node right now (not offline)
    Sent when a hub's numbers change, and every few seconds for browsers that connect late.
    """

    def __init__(self, riders, drivers=(), hub_ids=()):
        self.riders, self.drivers = riders, list(drivers)
        self.hub_ids = set(hub_ids)          # drivers are only counted when standing AT a hub
        self.last = {}

    async def sync(self, now, resend=False):
        load = {}

        def bump(hub, k):
            v = list(load.get(hub, (0, 0, 0, 0, 0)))
            v[k] += 1
            load[hub] = tuple(v)

        for r in self.riders:
            trips = list(getattr(r, "trips", []))
            if r.trip is not None:
                trips.append(r.trip)
            for t in trips:
                if not getattr(t, "hub_jid", None) or now - t.requested_at > RECENT_WINDOW:
                    continue
                hub = t.hub_jid.split("@")[0]
                bump(hub, 0)
                if t.outcome and t.outcome not in GOOD_OUTCOMES:
                    bump(hub, 3)
            t = r.trip
            if t is not None and getattr(t, "hub_jid", None):
                hub = t.hub_jid.split("@")[0]
                if r.state in WAITING_STATES:
                    bump(hub, 1)
                elif r.state in ACTIVE_STATES:
                    bump(hub, 2)
        for d in self.drivers:
            if d.location in self.hub_ids and getattr(d.state, "value", d.state) != "OFFLINE":
                bump(d.location, 4)
        zero = (0, 0, 0, 0, 0)
        for hub in set(load) | set(self.last):
            cur = load.get(hub, zero)
            if resend or cur != self.last.get(hub, zero):
                await broadcast_event("HUB_LOAD", dict(zip(("hub_id", "recent", "waiting", "active", "failed", "drivers"),
                                                          (hub,) + cur)))
        self.last = load


def microhubs(h):
    return [a for a in h.agents.values() if getattr(a, "role", None) == "microhub"]


class RepositionWatcher:
    """Drivers being moved to where demand is expected (scenario 5).

    REPOSITION      {id, driver, from_node, to_node, hub, incentive, state: "asking"|"moving"}
    REPOSITION_DONE {id, outcome}
    One id per hub-to-driver request (RepositionProtocol thread). "asking" = driver has not answered yet.
    """

    def __init__(self, harness):
        self.h = harness
        self.active = {}

    async def sync(self, now, resend=False):
        cur = {}
        for hub in microhubs(self.h):
            for thread, ctx in list(getattr(hub, "repositions", {}).items()):
                p = ctx.plan
                cur[thread] = {"id": thread, "driver": p.driver_jid.split("@")[0], "from_node": p.from_node,
                               "to_node": p.to_node, "hub": hub.hub_id, "incentive": round(p.incentive, 2),
                               "state": "moving" if ctx.agreed_at else "asking"}
        for rid, payload in cur.items():
            if resend or self.active.get(rid) != payload:
                if rid not in self.active:
                    vprint(f"[VIZ-DEMO] t={now:.0f}s: reposition {payload['driver']} {payload['from_node']} -> "
                           f"{payload['to_node']} (incentive {payload['incentive']}) -> map")
                await broadcast_event("REPOSITION", payload)
        for rid in set(self.active) - set(cur):
            outcome = next((c.outcome for hub in microhubs(self.h) for c in getattr(hub, "reposition_log", [])
                            if c.thread == rid), "")
            vprint(f"[VIZ-DEMO] t={now:.0f}s: reposition {rid} finished: {outcome or '?'}")
            await broadcast_event("REPOSITION_DONE", {"id": rid, "outcome": outcome})
        self.active = cur


class ForecastWatcher:
    """Demand forecasts the hubs hold (scenario 5): FORECAST {hub_id, expected, start, end} while it is valid,
    FORECAST_CLEARED {hub_id} when it expires."""

    def __init__(self, harness):
        self.h = harness
        self.sent = {}

    async def sync(self, now, resend=False):
        cur = {}
        for hub in microhubs(self.h):
            for fid, fc in getattr(hub, "demand_forecasts", {}).items():
                if fc.horizon_end > now and (fid not in cur or fc.horizon_start > cur[fid]["start"]):
                    cur[fid] = {"hub_id": fid, "expected": fc.expected_requests,
                                "start": fc.horizon_start, "end": fc.horizon_end}
        for fid, payload in cur.items():
            if resend or self.sent.get(fid) != payload:
                if fid not in self.sent:
                    vprint(f"[VIZ-DEMO] t={now:.0f}s: forecast {payload['expected']:g} requests at {fid} "
                           f"until t={payload['end']:.0f}s -> map")
                await broadcast_event("FORECAST", payload)
        for fid in set(self.sent) - set(cur):
            await broadcast_event("FORECAST_CLEARED", {"hub_id": fid})
        self.sent = cur


class HubWatcher:
    """Tells the map when a microhub agent is no longer running (scenario `kill_hub`).

    Sends  HUB_DOWN {hub_id}  once when the hub stops, and HUB_UP {hub_id}  if it ever runs again.
    """

    def __init__(self, harness):
        self.h = harness
        self.down = set()

    async def sync(self, now, resend=False):
        for a in self.h.agents.values():
            if getattr(a, "role", None) != "microhub":
                continue
            runner = getattr(self.h, "runner", None)
            alive = a.is_alive() and a.hub_id not in getattr(runner, "killed", ())
            if not alive and (a.hub_id not in self.down or resend):
                if a.hub_id not in self.down:
                    vprint(f"[VIZ-DEMO] t={now:.0f}s: hub {a.hub_id!r} is down -> map")
                self.down.add(a.hub_id)
                await broadcast_event("HUB_DOWN", {"hub_id": a.hub_id})
            elif alive and a.hub_id in self.down:
                self.down.discard(a.hub_id)
                await broadcast_event("HUB_UP", {"hub_id": a.hub_id})


async def broadcast_rider(network, r: RiderAgent):
    """Envia o estado e coordenadas atuais de um passageiro para o WebSocket."""
    t = r.trip
    hub_node = t.hub_jid.split("@")[0] if (t and t.hub_jid) else None
    dest_node = getattr(t.request, "destination_home", None) if t else None
    lat, lon = get_node_latlon(network, hub_node)

    await broadcast_event("RIDER_UPDATE", {
        "id": r.jid.user,
        "state": r.state,
        "node": hub_node,
        "destination": dest_node,
        "lat": lat,
        "lon": lon,
    })


def expand_riders_in_scenario(scenario: Scenario, target_count: int = 32):
    """Adiciona passageiros extra em memória para a demo visual (sem alterar o JSON)."""
    if not hasattr(scenario, "riders") or not scenario.riders:
        return
    base_riders = list(scenario.riders)
    while len(scenario.riders) < target_count:
        idx = len(scenario.riders) + 1
        template = copy.deepcopy(base_riders[(idx - 1) % len(base_riders)])
        if isinstance(template, dict):
            old_jid = template.get("jid", "rider_1@localhost")
            domain = old_jid.split("@")[1] if "@" in old_jid else "localhost"
            template["jid"] = f"rider_{idx}@{domain}"
            if "rider_id" in template:
                template["rider_id"] = f"rider_{idx}"
        elif hasattr(template, "jid"):
            domain = template.jid.split("@")[1] if "@" in template.jid else "localhost"
            template.jid = f"rider_{idx}@{domain}"
            if hasattr(template, "rider_id"):
                template.rider_id = f"rider_{idx}"
        scenario.riders.append(template)


def ensure_scenario_runner(h):
    """Make sure the scenario's EVENTS (street closures, hub kills, forecasts) really are played.

    The events are played by harness.runner.ScenarioRunner. Normally Harness.start() starts it, but if your
    launcher.py does not (older version), nothing would ever happen and the map would stay empty. So:
    say what the scenario contains, and start the runner here if nobody did.
    Returns the runner THIS function started (to stop it later), else None.
    """
    sc = h.scenario
    events = getattr(sc, "events", None)
    if events is None:
        vprint("[VIZ-DEMO] AVISO: o teu scenario.py não conhece a secção 'events' -> nenhum corte de rua / "
              "falha de hub vai acontecer. Aplica patches/src_harness_scenario.py.patch.")
        return None
    vprint(f"[VIZ-DEMO] eventos do cenário: " + (", ".join(f"{e.id}@t={e.at:.0f}s ({e.type})" for e in events) or "nenhum"))
    if getattr(sc, "reposition", None) is not None:
        hubs_off = [a.hub_id for a in microhubs(h) if getattr(a, "reposition_rules", None) is None]
        if hubs_off:
            vprint("[VIZ-DEMO] AVISO: o cenário pede reposição mas os hubs não a têm ligada (launcher.py antigo) "
                   "-> ninguém vai ser reposicionado. Aplica patches/src_harness_launcher.py.patch "
                   "(e src_agents_microhub.py.patch).")
    if getattr(h, "runner", None) is not None:
        vprint("[VIZ-DEMO] o launcher já arrancou o ScenarioRunner.")
        return None
    from harness.runner import ScenarioRunner
    h.runner = ScenarioRunner(h)
    h.runner.start()
    vprint("[VIZ-DEMO] o launcher NÃO arrancou o ScenarioRunner (launcher.py antigo): arrancado aqui.")
    return h.runner


async def demand_and_viz_loop(h: Harness, stop_event: asyncio.Event):
    """Mantém até 32 passageiros ativos em simultâneo pelos vários hubs do mapa."""
    await asyncio.sleep(2.5)  # dá tempo aos agentes para terminarem o setup

    riders = [a for a in h.agents.values() if isinstance(a, RiderAgent)]
    drivers = [a for a in h.agents.values() if isinstance(a, DriverAgent)]
    all_nodes = list(h.network.g.nodes)
    all_hubs = list(riders[0].hubs.keys()) if riders else []

    vprint(f"[VIZ-DEMO] Ativo com {len(drivers)} motoristas e {len(riders)} passageiros em simultâneo.")

    auto_demand = not getattr(h.scenario, "demand", None)
    vprint("[VIZ-DEMO] procura: " + ("aleatória (o cenário não define 'demand')" if auto_demand
                                     else "a do cenário (esta demo não pede viagens por conta própria)"))
    shown_events = 0
    roads = RoadWatcher(h.network)
    hubs_watch = HubWatcher(h)
    hub_load = HubLoadWatcher(riders, drivers, [a.hub_id for a in microhubs(h)])
    repo_watch = RepositionWatcher(h)
    forecast_watch = ForecastWatcher(h)
    traffic = TrafficWatcher(h.network)
    tick = 0
    while not stop_event.is_set():
        tick += 1
        # Each watcher has its OWN try/except: a failure in one must never stop the others (or the demo).
        try:     # events as the runner fires them (so the terminal shows what the map should show)
            runner = getattr(h, "runner", None)
            while runner is not None and shown_events < len(runner.summary.events):
                t_ev, text_ev = runner.summary.events[shown_events]
                vprint(f"[VIZ-DEMO] evento t={t_ev:.0f}s: {text_ev}")
                shown_events += 1
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO a ler eventos: {e!r}")
        try:     # 0. blocked streets (red on the map)
            await roads.sync(clock.now(), resend=(tick % 5 == 0))
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO nas ruas cortadas: {e!r}")
        try:     # 0a. heavy traffic (orange streets on the map)
            await traffic.sync(clock.now(), resend=(tick % 5 == 0))
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO no tráfego: {e!r}")
        try:     # 0c. queue length per hub (badge on the map)
            await hub_load.sync(clock.now(), resend=(tick % 5 == 0))
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO na carga dos hubs: {e!r}")
        try:     # 0d. drivers being repositioned + forecasts (scenario 5)
            await repo_watch.sync(clock.now(), resend=(tick % 5 == 0))
            await forecast_watch.sync(clock.now(), resend=(tick % 5 == 0))
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO na reposição/previsões: {e!r}")
        try:     # 0b. hubs that were killed (black on the map)
            await hubs_watch.sync(clock.now(), resend=(tick % 5 == 0))
        except Exception as e:
            vprint(f"[VIZ-DEMO] ERRO nos hubs parados: {e!r}")

        try:
            # 1. Atualiza o estado atual de todos os riders no mapa
            for r in riders:
                await broadcast_rider(h.network, r)

            # 2. Pede viagem imediatamente para TODOS os passageiros que estejam livres (IDLE)
            #    ... mas só se o cenário NÃO tiver a sua própria procura ("demand"): senão esta demo
            #    gastava os passageiros todos logo no início e a procura do cenário (p.ex. o pico num hub) nunca se via.
            idle_riders = [r for r in riders if r.state == IDLE] if auto_demand else []
            for r in idle_riders:
                chosen_hub = random.choice(all_hubs) if all_hubs else None
                random.shuffle(all_nodes)
                for dest in all_nodes[:40]:
                    if dest == chosen_hub:
                        continue
                    req = r.request_trip(
                        destination=dest,
                        max_fare=80.0,
                        detour_tolerance=1800.0,
                        hub_id=chosen_hub,
                        pooling_willingness=True,
                    )
                    if req is not None:
                        await broadcast_rider(h.network, r)
                        break

        except Exception as e:
            vprint(f"[VIZ-DEMO] Aviso no ciclo de procura: {e}")

        await asyncio.sleep(1.0)


async def main():
    LOG_PATH.write_text("", encoding="utf-8")              # fresh log for this run
    logging.getLogger("spade").setLevel(logging.ERROR)     # hide the "No behaviour matched" warnings
    scenario_path = sys.argv[1] if len(sys.argv) > 1 else "config/scenarios/01_random_disturbance.json"
    scenario = Scenario.load(scenario_path)
    expand_riders_in_scenario(scenario, target_count=32)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    async with Harness(scenario) as h:
        ws_server = await start_ws_server()
        own_runner = ensure_scenario_runner(h)
        print(f"Cenário {scenario.name!r} iniciado. Abre o porto_graph_map.html no browser. Ctrl-C para parar.")
        task = asyncio.create_task(demand_and_viz_loop(h, stop_event))
        await stop_event.wait()
        task.cancel()
        if own_runner is not None:
            await own_runner.stop()
            h.runner = None


if __name__ == "__main__":
    asyncio.run(main())
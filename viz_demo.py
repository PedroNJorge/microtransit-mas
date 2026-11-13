import asyncio
import copy
import random
import signal
import sys
from pathlib import Path

# Garante que importa a pasta src corretamente
sys.path.insert(0, str(Path(__file__).parent / "src"))

from agents.rider import IDLE, RiderAgent
from agents.driver import DriverAgent
from common.ws_server import broadcast_event
from harness.launcher import Harness
from harness.scenario import Scenario


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


async def demand_and_viz_loop(h: Harness, stop_event: asyncio.Event):
    """Mantém até 32 passageiros ativos em simultâneo pelos vários hubs do mapa."""
    await asyncio.sleep(2.5)  # dá tempo aos agentes para terminarem o setup

    riders = [a for a in h.agents.values() if isinstance(a, RiderAgent)]
    drivers = [a for a in h.agents.values() if isinstance(a, DriverAgent)]
    all_nodes = list(h.network.g.nodes)
    all_hubs = list(riders[0].hubs.keys()) if riders else []

    print(f"[VIZ-DEMO] Ativo com {len(drivers)} motoristas e {len(riders)} passageiros em simultâneo.")

    while not stop_event.is_set():
        try:
            # 1. Atualiza o estado atual de todos os riders no mapa
            for r in riders:
                await broadcast_rider(h.network, r)

            # 2. Pede viagem imediatamente para TODOS os passageiros que estejam livres (IDLE)
            idle_riders = [r for r in riders if r.state == IDLE]
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
            print(f"[VIZ-DEMO] Aviso no ciclo de procura: {e}")

        await asyncio.sleep(1.0)


async def main():
    scenario_path = sys.argv[1] if len(sys.argv) > 1 else "config/scenarios/full_8hubs.json"
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
        print(f"Cenário {scenario.name!r} iniciado. Abre o porto_graph_map.html no browser. Ctrl-C para parar.")
        task = asyncio.create_task(demand_and_viz_loop(h, stop_event))
        await stop_event.wait()
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
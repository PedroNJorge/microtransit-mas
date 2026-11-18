import asyncio
import json

import websockets

# Browsers connected to the map
CONNECTED_CLIENTS: set = set()

# Last known state of things that last for a while, replayed to every browser that connects
# (so a map opened or refreshed mid-simulation still shows the blocked streets).
# kind -> {key -> payload}; filled by broadcast_event.
_STATE: dict = {"ROAD_BLOCKED": {}, "HUB_DOWN": {}, "TRAFFIC_LEVEL": {}, "FORECAST": {}, "REPOSITION": {}, "METRICS": {}}


def _snapshot_messages() -> list:
    return [json.dumps({"type": t, "data": d}) for t, items in _STATE.items() for d in items.values()]


async def _ws_handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    try:
        for msg in _snapshot_messages():              # catch the new browser up
            await websocket.send(msg)
        await websocket.wait_closed()
    finally:
        CONNECTED_CLIENTS.discard(websocket)


_SERVER = None


async def start_ws_server(host: str = "127.0.0.1", port: int = 8765):
    """Starts the WebSocket server in the background, inside the SPADE event loop.

    Safe to call more than once in the same process (the second call returns the running server),
    so it does not matter whether the launcher or the demo script starts it first.
    """
    global _SERVER
    if _SERVER is not None:
        return _SERVER
    try:
        _SERVER = await websockets.serve(_ws_handler, host, port)
    except OSError as e:
        raise OSError(f"{e}\n-> port {port} is held by ANOTHER process (not this one). In WSL run "
                      f"`ss -ltnp | grep {port}`; on Windows (PowerShell) run `netstat -ano | findstr {port}`.") from e
    print(f"🗺️ Servidor de Visualização WebSocket ativo em ws://{host}:{port}")
    return _SERVER


async def broadcast_event(event_type: str, data: dict):
    """Sends a real-time update to the HTML map."""
    # remember closures even when nobody is connected yet
    if event_type == "ROAD_BLOCKED":
        _STATE.setdefault("ROAD_BLOCKED", {})[data["id"]] = data
    elif event_type == "ROAD_CLEARED":
        _STATE.setdefault("ROAD_BLOCKED", {}).pop(data["id"], None)
    elif event_type == "TRAFFIC_LEVEL":
        _STATE.setdefault("TRAFFIC_LEVEL", {})[data["id"]] = data
    elif event_type == "TRAFFIC_CLEARED":
        _STATE.setdefault("TRAFFIC_LEVEL", {}).pop(data["id"], None)
    elif event_type == "FORECAST":
        _STATE.setdefault("FORECAST", {})[data["hub_id"]] = data
    elif event_type == "FORECAST_CLEARED":
        _STATE.setdefault("FORECAST", {}).pop(data["hub_id"], None)
    elif event_type == "REPOSITION":
        _STATE.setdefault("REPOSITION", {})[data["id"]] = data
    elif event_type == "REPOSITION_DONE":
        _STATE.setdefault("REPOSITION", {}).pop(data["id"], None)
    elif event_type == "METRICS":                       # the whole history, so a viewer opened mid-run draws full charts
        _STATE.setdefault("METRICS", {})[str(data.get("t"))] = data
    elif event_type == "HUB_DOWN":
        _STATE.setdefault("HUB_DOWN", {})[data["hub_id"]] = data
    elif event_type == "HUB_UP":
        _STATE.setdefault("HUB_DOWN", {}).pop(data["hub_id"], None)

    if not CONNECTED_CLIENTS:
        return
    message = json.dumps({"type": event_type, "data": data})
    # send to every open browser without blocking the simulation
    await asyncio.gather(*[c.send(message) for c in list(CONNECTED_CLIENTS)], return_exceptions=True)
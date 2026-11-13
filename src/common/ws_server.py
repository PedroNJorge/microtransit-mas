import asyncio
import json
import websockets
from typing import Set

# Guarda os browsers ligados ao mapa
CONNECTED_CLIENTS: Set[websockets.WebSocketServerProtocol] = set()

async def _ws_handler(websocket):
    CONNECTED_CLIENTS.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        CONNECTED_CLIENTS.remove(websocket)

async def start_ws_server(host: str = "127.0.0.1", port: int = 8765):
    """Arranca o servidor WebSocket em background dentro do loop do SPADE."""
    server = await websockets.serve(_ws_handler, host, port)
    print(f"🗺️ Servidor de Visualização WebSocket ativo em ws://{host}:{port}")
    return server

async def broadcast_event(event_type: str, data: dict):
    """Envia uma atualização em tempo real para o mapa HTML."""
    if not CONNECTED_CLIENTS:
        return
    
    message = json.dumps({
        "type": event_type,
        "data": data
    })
    
    # Envia a todos os browsers abertos sem bloquear a simulação
    await asyncio.gather(
        *[client.send(message) for client in CONNECTED_CLIENTS],
        return_exceptions=True
    )
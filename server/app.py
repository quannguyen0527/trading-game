"""
FastAPI server: players connect over a WebSocket and trade in real time.

Connect:  ws://localhost:8000/ws/{room_id}?name=alice
Send JSON messages:
  {"type": "order", "side": "buy" | "sell", "qty": 5, "price": 10050}   # price in cents; omit for market sell
  {"type": "cancel", "order_id": 3}
Receive JSON messages:
  {"type": "state", "public": {...}, "you": {...}}   # after every change
  {"type": "error", "message": "..."}                 # only to the player who caused it

The server runs on a single asyncio event loop, and no handler awaits while it is
changing a room, so two players' orders can never interleave half-way through.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .game import OrderRejected, Room

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Trading Game")
rooms: Dict[str, Room] = {}
# room id -> player name -> that player's open WebSocket
connections: Dict[str, Dict[str, WebSocket]] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws/{room_id}")
async def play(websocket: WebSocket, room_id: str, name: str = "") -> None:
    await websocket.accept()

    if not NAME_PATTERN.match(name) or not NAME_PATTERN.match(room_id):
        await _close_with_error(websocket, "names: 1-20 letters, numbers, - or _")
        return
    room_conns = connections.setdefault(room_id, {})
    if name in room_conns:
        await _close_with_error(websocket, f"'{name}' is already playing in this room")
        return

    room = rooms.setdefault(room_id, Room(room_id))
    room.join(name)
    room_conns[name] = websocket
    await _broadcast(room)

    try:
        while True:
            message = await websocket.receive_json()
            try:
                _handle(room, name, message)
            except OrderRejected as e:
                await websocket.send_json({"type": "error", "message": str(e)})
                continue
            await _broadcast(room)
    except (WebSocketDisconnect, ValueError):
        # ValueError: the client sent something that isn't JSON
        pass
    finally:
        room_conns.pop(name, None)
        # The account stays in the room so the player can reconnect with the same name.


def _handle(room: Room, name: str, message: object) -> None:
    if not isinstance(message, dict):
        raise OrderRejected("message must be a JSON object")
    kind = message.get("type")
    if kind == "order":
        room.place_order(name, message.get("side"), message.get("qty"), message.get("price"))
    elif kind == "cancel":
        room.cancel(name, message.get("order_id"))
    else:
        raise OrderRejected(f"unknown message type: {kind!r}")


async def _broadcast(room: Room) -> None:
    public = room.public_state()
    for player, ws in list(connections.get(room.id, {}).items()):
        try:
            await ws.send_json({"type": "state", "public": public, "you": room.private_state(player)})
        except Exception:
            # The socket died mid-send; its own handler will clean it up.
            pass


async def _close_with_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "message": message})
    await websocket.close()

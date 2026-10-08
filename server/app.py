"""
FastAPI server: players connect over a WebSocket and trade in real time.

Connect:  ws://localhost:8000/ws/{room_id}?name=alice
Send JSON messages:
  {"type": "start"}                                                     # start a new round
  {"type": "order", "side": "buy" | "sell", "qty": 5, "price": 10050}   # price in cents; omit for market sell
  {"type": "cancel", "order_id": 3}
  {"type": "coach"}                                                     # ask the AI coach to review your last round
Receive JSON messages:
  {"type": "state", "public": {...}, "you": {...}}   # after every change
  {"type": "error", "message": "..."}                 # only to the player who caused it
  {"type": "coach", "round": 2, "text": "..."}        # the coach's review (or "error": "..."), only to you

The server runs on a single asyncio event loop, and no handler awaits while it is
changing a room, so two players' orders can never interleave half-way through.
Each running round also has a clock task on the same loop that calls room.tick()
once a second, so news and the end of the round arrive without anyone acting.

All state lives in this process's memory, so the server must run as a single
process. Limits on rooms, players and message rate keep one visitor from
using up that memory or CPU, and empty rooms are deleted.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .bots import add_bots
from .coach import CoachUnavailable, coach_from_env
from .game import OrderRejected, Phase, Room

TICK_SECONDS = 1.0
MAX_ROOMS = 50
MAX_PLAYERS_PER_ROOM = 20           # humans; bots don't count
MAX_MESSAGES_PER_SECOND = 10        # per connection
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Trading Game")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
rooms: Dict[str, Room] = {}
# room id -> player name -> that player's open WebSocket
connections: Dict[str, Dict[str, WebSocket]] = {}
# room id -> the task that ticks that room's clock while a round runs
clock_tasks: Dict[str, asyncio.Task] = {}
coach = coach_from_env()
# Coach requests in flight. asyncio only keeps weak references to tasks, so we hold them here.
background_tasks: Set[asyncio.Task] = set()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Used by the hosting platform to check the server is up."""
    return {"status": "ok", "rooms": len(rooms)}


@app.websocket("/ws/{room_id}")
async def play(websocket: WebSocket, room_id: str, name: str = "") -> None:
    await websocket.accept()

    if not NAME_PATTERN.match(name) or not NAME_PATTERN.match(room_id):
        await _close_with_error(websocket, "names: 1-20 letters, numbers, - or _")
        return
    if name in connections.get(room_id, {}):
        await _close_with_error(websocket, f"'{name}' is already playing in this room")
        return

    room = rooms.get(room_id)
    if room is None:
        if len(rooms) >= MAX_ROOMS:
            await _close_with_error(websocket, "the server is full; try again later")
            return
        room = rooms[room_id] = Room(room_id)
        add_bots(room)
    if name not in room.accounts and len(room.accounts) - len(room.bots) >= MAX_PLAYERS_PER_ROOM:
        await _close_with_error(websocket, "this room is full; try another room name")
        return
    room.join(name)
    room_conns = connections.setdefault(room_id, {})
    room_conns[name] = websocket
    await _broadcast(room)

    window_start, count = time.monotonic(), 0
    try:
        while True:
            message = await websocket.receive_json()
            # Fixed-window rate limit: at most MAX_MESSAGES_PER_SECOND each second.
            now = time.monotonic()
            if now - window_start >= 1:
                window_start, count = now, 0
            count += 1
            if count > MAX_MESSAGES_PER_SECOND:
                if count == MAX_MESSAGES_PER_SECOND + 1:
                    await websocket.send_json({"type": "error", "message": "slow down: too many orders"})
                continue
            if isinstance(message, dict) and message.get("type") == "coach":
                # Runs in the background so this player's other messages aren't held up.
                task = asyncio.create_task(_send_review(websocket, room, name))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
                continue
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
        # The account stays in the room so the player can reconnect with the same name,
        # unless everyone has left; then the room is deleted.
        _delete_if_empty(room)


def _handle(room: Room, name: str, message: object) -> None:
    if not isinstance(message, dict):
        raise OrderRejected("message must be a JSON object")
    kind = message.get("type")
    if kind == "order":
        room.place_order(name, message.get("side"), message.get("qty"), message.get("price"))
    elif kind == "cancel":
        room.cancel(name, message.get("order_id"))
    elif kind == "start":
        room.start()
        task = clock_tasks.get(room.id)
        # If last round's task hasn't exited yet, it keeps running and serves this round.
        if task is None or task.done():
            clock_tasks[room.id] = asyncio.create_task(_run_clock(room))
    else:
        raise OrderRejected(f"unknown message type: {kind!r}")


async def _run_clock(room: Room) -> None:
    while room.phase == Phase.RUNNING:
        await asyncio.sleep(TICK_SECONDS)
        if room.tick():
            await _broadcast(room)
    _delete_if_empty(room)


def _delete_if_empty(room: Room) -> None:
    """Forget a room nobody is connected to, once no round is running in it."""
    if connections.get(room.id) or room.phase == Phase.RUNNING:
        return  # a running round is cleaned up by its clock task when it ends
    if rooms.get(room.id) is room:
        del rooms[room.id]
        connections.pop(room.id, None)
        clock_tasks.pop(room.id, None)


async def _send_review(websocket: WebSocket, room: Room, name: str) -> None:
    round_number = room.round_number
    try:
        text = await coach.review(room, name)
        reply = {"type": "coach", "round": round_number, "text": text}
    except CoachUnavailable as e:
        reply = {"type": "coach", "round": round_number, "error": str(e)}
    try:
        await websocket.send_json(reply)
    except Exception:
        pass  # the player left while the coach was writing


async def _broadcast(room: Room) -> None:
    public = room.public_state()
    public["coach_enabled"] = coach.enabled
    for player, ws in list(connections.get(room.id, {}).items()):
        try:
            await ws.send_json({"type": "state", "public": public, "you": room.private_state(player)})
        except Exception:
            # The socket died mid-send; its own handler will clean it up.
            pass


async def _close_with_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "message": message})
    await websocket.close()

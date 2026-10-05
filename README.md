# Real-Time Multiplayer Trading Game

A multiplayer stock-trading game built on a from-scratch **limit order book matching engine**. Players join a room, start with $10,000 and 100 shares, and trade against each other in real time over WebSockets.

**Tech:** Python 3.13 · FastAPI · WebSockets · vanilla JavaScript · unittest

## Features

- **Matching engine** with price-time priority, the same rule real exchanges use: the best price trades first, and ties go to the earliest order.
- **Limit and market orders**, partial fills, cancels, and aggregated order-book depth.
- **Pre-trade risk checks:** cash and shares committed to open orders are reserved, so players can't double-spend.
- **Real-time updates:** every player sees the order book, trades, and leaderboard update instantly.
- **19 unit and integration tests**, including two simulated players trading over live WebSocket connections.

## How the matching engine works

```
           ASKS (sellers, min-heap)          best ask = lowest price to buy at
           $101.00   x 5
           $100.50   x 12   <-- best ask
  spread ─────────────────
           $100.00   x 8    <-- best bid
           $ 99.50   x 20
           BIDS (buyers, max-heap)           best bid = highest price to sell at
```

- Bids live in a **max-heap** and asks in a **min-heap**, so the best price on each side can be found in O(1) and added or removed in O(log n).
- An incoming order trades against the opposite side while the prices cross (bid ≥ ask). Each trade executes at the **resting order's price**.
- **Lazy cancellation:** cancelling only marks the order. It is discarded when it reaches the top of its heap, so cancel is O(1) instead of an O(n) heap search.
- **Prices are integer cents**, avoiding floating-point errors (in floating point, `0.1 + 0.2 != 0.3`).

## Design decisions

- **The engine, the game rules and the web server are separate layers.** `engine/` is pure data structures with no I/O, `server/game.py` holds the game rules and accounting, and `server/app.py` is only networking. Each layer can be tested on its own.
- **No locks needed:** the server runs on a single asyncio event loop, and room updates are synchronous, so two players' orders can never interleave mid-update.
- **Market buys are disabled for now:** without a price, an order's cost can't be checked against buying power up front. Planned fix: reserve cash against the current best ask.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server.app:app --reload
```

Open http://localhost:8000 in two browser tabs, join the same room with different names, and trade.

Run the tests:

```bash
.venv/bin/python -m unittest -v
```

## Project structure

```
engine/order_book.py   matching engine (heaps, price-time priority)
server/game.py         accounts, risk checks, settlement, leaderboard
server/app.py          FastAPI + WebSocket server
static/index.html      browser client
tests/                 engine, game-rule and WebSocket tests
```

## Roadmap

- [ ] Timed rounds and random news events that move the stock's value
- [ ] Bot traders so the market is active when you play alone
- [ ] Polished UI with a live price chart
- [ ] Port the matching engine to C++ (pybind11) and benchmark the speedup
- [ ] Deploy online

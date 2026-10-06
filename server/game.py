"""
Game rules for one trading room: player accounts, pre-trade risk checks and settlement.

Every player starts with the same cash and shares. Before an order reaches the
order book we check the player can afford it:
- Buying power = cash minus money already promised to open buy orders.
- Sellable shares = shares minus shares already promised to open sell orders.
This stops players from spending the same dollar (or share) twice.

Each round lasts ROUND_SECONDS. The stock has a hidden "fair value" that random
news headlines push up or down. When the round ends, open orders are cancelled and
every share is settled at the fair value, so players win by reading the news and
trading before everyone else does, not by luck of the last trade price.

All money is in integer cents. The clock and random generator can be passed in,
so tests can control time and news instead of waiting for them.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional

from engine import OrderBook, Side, Trade

STARTING_CASH = 10_000_00  # $10,000.00
STARTING_SHARES = 100
STARTING_PRICE = 100_00  # $100.00, used for net worth before any trade
MAX_QTY = 10_000
MAX_RECENT_TRADES = 50
ROUND_SECONDS = 180
NEWS_EVERY = (15, 30)  # seconds between headlines, picked at random in this range
MIN_FAIR_VALUE = 1_00

# (headline, % change to fair value). The stock is Bayou Energy (BYOU).
NEWS = [
    ("Bayou Energy discovers major new offshore oil field", 15),
    ("OPEC announces production cut; oil prices jump", 10),
    ("Bayou Energy beats quarterly earnings estimates", 8),
    ("Natural gas prices hit a five-year high", 6),
    ("Government approves new Gulf drilling permits", 5),
    ("Analyst upgrades Bayou Energy to Buy", 4),
    ("Analyst downgrades Bayou Energy to Sell", -4),
    ("New carbon tax proposed in Congress", -5),
    ("Mild winter forecast cuts heating demand", -5),
    ("CEO of Bayou Energy resigns unexpectedly", -6),
    ("Bayou Energy misses quarterly earnings estimates", -8),
    ("Pipeline leak shuts down Bayou Energy's main facility", -10),
    ("Hurricane forecast to hit Gulf Coast refineries", -12),
]


class Phase(str, Enum):
    WAITING = "waiting"    # no round played yet
    RUNNING = "running"
    FINISHED = "finished"  # round over; fair value revealed


class OrderRejected(Exception):
    """Raised when an order breaks a game rule. The message is shown to the player."""


@dataclass
class Account:
    name: str
    cash: int = STARTING_CASH
    shares: int = STARTING_SHARES


@dataclass(frozen=True)
class NewsItem:
    headline: str
    change_pct: int
    second: int  # seconds into the round


class Room:
    def __init__(
        self,
        room_id: str,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.id = room_id
        self.clock = clock
        self.rng = rng or random.Random()
        self.accounts: Dict[str, Account] = {}
        self.phase = Phase.WAITING
        self._reset_market()

    # ---------- rounds ----------

    def start(self) -> None:
        """Start a new round: everyone gets fresh cash and shares."""
        if self.phase == Phase.RUNNING:
            raise OrderRejected("a round is already running")
        self._reset_market()
        for account in self.accounts.values():
            account.cash, account.shares = STARTING_CASH, STARTING_SHARES
        self.phase = Phase.RUNNING
        self.started_at = self.clock()
        self.ends_at = self.started_at + ROUND_SECONDS
        self._schedule_news(self.started_at)

    def tick(self) -> bool:
        """Release any news that is due and end the round on time.

        Called about once a second by the server. Returns True if anything changed.
        """
        if self.phase != Phase.RUNNING:
            return False
        now = self.clock()
        changed = False
        while self.next_news_at <= min(now, self.ends_at):
            self._publish_news(self.next_news_at)
            self._schedule_news(self.next_news_at)
            changed = True
        if now >= self.ends_at:
            self._finish()
            changed = True
        return changed

    def seconds_left(self) -> int:
        if self.phase != Phase.RUNNING:
            return 0
        return max(0, math.ceil(self.ends_at - self.clock()))

    # ---------- players ----------

    def join(self, name: str) -> Account:
        """Add a player, or return their existing account if they rejoin."""
        if name not in self.accounts:
            self.accounts[name] = Account(name)
        return self.accounts[name]

    # ---------- orders ----------

    def place_order(self, name: str, side: str, qty: int, price: Optional[int]) -> List[Trade]:
        """Validate, submit and settle an order. Returns the trades it caused."""
        account = self._account(name)
        if self.phase != Phase.RUNNING:
            raise OrderRejected("trading is closed; start a round first")
        try:
            side = Side(side)
        except ValueError:
            raise OrderRejected("side must be 'buy' or 'sell'")
        if type(qty) is not int or not 1 <= qty <= MAX_QTY:
            raise OrderRejected(f"quantity must be a whole number from 1 to {MAX_QTY}")
        if price is not None and (type(price) is not int or price <= 0):
            raise OrderRejected("price must be a positive number of cents")
        if price is None and side == Side.BUY:
            # A market buy has no price, so we can't check it against buying power.
            raise OrderRejected("market buys are not supported yet; set a limit price")

        if side == Side.BUY and qty * price > self.buying_power(name):
            raise OrderRejected("not enough cash for this order")
        if side == Side.SELL and qty > self.sellable_shares(name):
            raise OrderRejected("not enough shares for this order")

        _, trades = self.book.submit(account.name, side, qty, price)
        for trade in trades:
            self._settle(trade)
        return trades

    def cancel(self, name: str, order_id: int) -> None:
        if not any(o.id == order_id for o in self.book.open_orders(name)):
            raise OrderRejected("no open order with that id")
        self.book.cancel(order_id)

    # ---------- account math ----------

    def buying_power(self, name: str) -> int:
        reserved = sum(
            o.remaining * o.price for o in self.book.open_orders(name) if o.side == Side.BUY
        )
        return self._account(name).cash - reserved

    def sellable_shares(self, name: str) -> int:
        reserved = sum(o.remaining for o in self.book.open_orders(name) if o.side == Side.SELL)
        return self._account(name).shares - reserved

    def mark_price(self) -> int:
        """Price used to value shares: the last trade, or the fair value once revealed."""
        return self.fair_value if self.phase == Phase.FINISHED else self.last_price

    def net_worth(self, name: str) -> int:
        account = self._account(name)
        return account.cash + account.shares * self.mark_price()

    # ---------- state sent to clients ----------

    def public_state(self) -> dict:
        depth = self.book.depth(levels=10)
        return {
            "room": self.id,
            "phase": self.phase.value,
            "seconds_left": self.seconds_left(),
            "last_price": self.last_price,
            # Secret until the round ends; revealing it early would give the game away.
            "fair_value": self.fair_value if self.phase == Phase.FINISHED else None,
            "news": [
                {"headline": n.headline, "second": n.second,
                 "change_pct": n.change_pct if self.phase == Phase.FINISHED else None}
                for n in self.news
            ],
            "book": {
                "bids": [{"price": p, "qty": q} for p, q in depth["bids"]],
                "asks": [{"price": p, "qty": q} for p, q in depth["asks"]],
            },
            "trades": [
                {"buyer": t.buyer, "seller": t.seller, "price": t.price, "qty": t.qty}
                for t in self.trades[-20:]
            ],
            "leaderboard": sorted(
                ({"name": n, "net_worth": self.net_worth(n)} for n in self.accounts),
                key=lambda row: row["net_worth"],
                reverse=True,
            ),
        }

    def private_state(self, name: str) -> dict:
        account = self._account(name)
        return {
            "name": name,
            "cash": account.cash,
            "shares": account.shares,
            "buying_power": self.buying_power(name),
            "sellable_shares": self.sellable_shares(name),
            "net_worth": self.net_worth(name),
            "open_orders": [
                {"id": o.id, "side": o.side.value, "price": o.price, "remaining": o.remaining}
                for o in self.book.open_orders(name)
            ],
        }

    # ---------- internals ----------

    def _reset_market(self) -> None:
        self.book = OrderBook()
        self.trades: List[Trade] = []
        self.news: List[NewsItem] = []
        self.last_price = STARTING_PRICE
        self.fair_value = STARTING_PRICE

    def _schedule_news(self, after: float) -> None:
        self.next_news_at = after + self.rng.uniform(*NEWS_EVERY)

    def _publish_news(self, at: float) -> None:
        headline, pct = self.rng.choice(NEWS)
        self.fair_value = max(MIN_FAIR_VALUE, round(self.fair_value * (100 + pct) / 100))
        self.news.append(NewsItem(headline, pct, int(at - self.started_at)))

    def _finish(self) -> None:
        for name in self.accounts:
            for order in self.book.open_orders(name):
                self.book.cancel(order.id)
        self.phase = Phase.FINISHED

    def _account(self, name: str) -> Account:
        if name not in self.accounts:
            raise OrderRejected("join the room first")
        return self.accounts[name]

    def _settle(self, trade: Trade) -> None:
        cost = trade.price * trade.qty
        buyer, seller = self.accounts[trade.buyer], self.accounts[trade.seller]
        buyer.cash -= cost
        buyer.shares += trade.qty
        seller.cash += cost
        seller.shares -= trade.qty
        self.last_price = trade.price
        self.trades.append(trade)
        del self.trades[:-MAX_RECENT_TRADES]

"""
Game rules for one trading room: player accounts, pre-trade risk checks and settlement.

Every player starts with the same cash and shares. Before an order reaches the
order book we check the player can afford it:
- Buying power = cash minus money already promised to open buy orders.
- Sellable shares = shares minus shares already promised to open sell orders.
This stops players from spending the same dollar (or share) twice.

All money is in integer cents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from engine import OrderBook, Side, Trade

STARTING_CASH = 10_000_00  # $10,000.00
STARTING_SHARES = 100
STARTING_PRICE = 100_00  # $100.00, used for net worth before any trade
MAX_QTY = 10_000
MAX_RECENT_TRADES = 50


class OrderRejected(Exception):
    """Raised when an order breaks a game rule. The message is shown to the player."""


@dataclass
class Account:
    name: str
    cash: int = STARTING_CASH
    shares: int = STARTING_SHARES


class Room:
    def __init__(self, room_id: str) -> None:
        self.id = room_id
        self.book = OrderBook()
        self.accounts: Dict[str, Account] = {}
        self.trades: List[Trade] = []
        self.last_price = STARTING_PRICE

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

    def net_worth(self, name: str) -> int:
        account = self._account(name)
        return account.cash + account.shares * self.last_price

    # ---------- state sent to clients ----------

    def public_state(self) -> dict:
        depth = self.book.depth(levels=10)
        return {
            "room": self.id,
            "last_price": self.last_price,
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

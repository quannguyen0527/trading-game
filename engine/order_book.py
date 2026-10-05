"""
Limit order book with price-time priority matching.

How it works:
- Buy orders ("bids") wait in a max-heap: the highest price is matched first.
- Sell orders ("asks") wait in a min-heap: the lowest price is matched first.
- Ties at the same price go to whoever arrived first (lower sequence number).
- A trade happens when the best bid price >= the best ask price.
  The trade executes at the price of the order that was already resting in the book.

Prices are integers (cents) to avoid floating-point rounding errors.

Cancellation is "lazy": a cancelled order is only marked as cancelled, and gets
skipped when it reaches the top of its heap. This makes cancel O(1) instead of
O(n) for searching and removing it from the heap.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    id: int
    player: str
    side: Side
    price: Optional[int]  # cents; None means a market order
    qty: int
    seq: int  # arrival order, used for time priority
    remaining: int = field(init=False)
    cancelled: bool = False

    def __post_init__(self) -> None:
        self.remaining = self.qty

    @property
    def is_active(self) -> bool:
        return not self.cancelled and self.remaining > 0


@dataclass(frozen=True)
class Trade:
    buyer: str
    seller: str
    price: int
    qty: int
    buy_order_id: int
    sell_order_id: int


class OrderBook:
    def __init__(self) -> None:
        # Heap entries are (sort_key, seq, order). seq breaks ties by time.
        self._bids: List[Tuple[int, int, Order]] = []  # key = -price (max-heap)
        self._asks: List[Tuple[int, int, Order]] = []  # key = price  (min-heap)
        self._orders: Dict[int, Order] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count()

    # ---------- public API ----------

    def submit(
        self, player: str, side: Side, qty: int, price: Optional[int] = None
    ) -> Tuple[Order, List[Trade]]:
        """Submit a limit order (with price) or market order (price=None).

        Returns the order and any trades it caused. A market order never rests
        in the book: whatever cannot be filled immediately is cancelled.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        if price is not None and price <= 0:
            raise ValueError("price must be positive")

        order = Order(next(self._ids), player, Side(side), price, qty, next(self._seq))
        self._orders[order.id] = order
        trades = self._match(order)

        if order.remaining > 0:
            if price is None:
                order.cancelled = True  # unfilled part of a market order
            else:
                self._rest(order)
        return order, trades

    def cancel(self, order_id: int) -> bool:
        """Cancel an open order. Returns False if it is unknown or already done."""
        order = self._orders.get(order_id)
        if order is None or not order.is_active:
            return False
        order.cancelled = True
        return True

    def best_bid(self) -> Optional[int]:
        top = self._peek(self._bids)
        return top.price if top else None

    def best_ask(self) -> Optional[int]:
        top = self._peek(self._asks)
        return top.price if top else None

    def depth(self, levels: int = 5) -> Dict[str, List[Tuple[int, int]]]:
        """Total quantity at each of the best `levels` prices, per side.

        Returns {"bids": [(price, qty), ...] high->low, "asks": [...] low->high}.
        """
        return {
            "bids": self._aggregate(self._bids, levels, reverse=True),
            "asks": self._aggregate(self._asks, levels, reverse=False),
        }

    def open_orders(self, player: str) -> List[Order]:
        return [o for o in self._orders.values() if o.player == player and o.is_active]

    # ---------- internals ----------

    def _match(self, incoming: Order) -> List[Trade]:
        trades: List[Trade] = []
        book = self._asks if incoming.side == Side.BUY else self._bids

        while incoming.remaining > 0:
            resting = self._peek(book)
            if resting is None or not self._crosses(incoming, resting):
                break

            qty = min(incoming.remaining, resting.remaining)
            incoming.remaining -= qty
            resting.remaining -= qty

            buy, sell = (incoming, resting) if incoming.side == Side.BUY else (resting, incoming)
            trades.append(Trade(buy.player, sell.player, resting.price, qty, buy.id, sell.id))

        return trades

    @staticmethod
    def _crosses(incoming: Order, resting: Order) -> bool:
        if incoming.price is None:  # market orders take any price
            return True
        if incoming.side == Side.BUY:
            return incoming.price >= resting.price
        return incoming.price <= resting.price

    def _rest(self, order: Order) -> None:
        if order.side == Side.BUY:
            heapq.heappush(self._bids, (-order.price, order.seq, order))
        else:
            heapq.heappush(self._asks, (order.price, order.seq, order))

    @staticmethod
    def _peek(heap: List[Tuple[int, int, Order]]) -> Optional[Order]:
        """Return the best active order, discarding filled/cancelled ones on top."""
        while heap and not heap[0][2].is_active:
            heapq.heappop(heap)
        return heap[0][2] if heap else None

    @staticmethod
    def _aggregate(heap, levels: int, reverse: bool) -> List[Tuple[int, int]]:
        totals: Dict[int, int] = {}
        for _, _, order in heap:
            if order.is_active:
                totals[order.price] = totals.get(order.price, 0) + order.remaining
        prices = sorted(totals, reverse=reverse)[:levels]
        return [(p, totals[p]) for p in prices]

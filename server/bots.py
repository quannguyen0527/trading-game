"""
Bot traders, so the market has life even with one human player.

Each bot is a normal account in the room and places orders through
Room.place_order, so it passes the same risk checks as a human. The room calls
bot.act() on every clock tick while a round is running.

- MarketMaker: always quotes a bid and an ask around the last trade price, so
  there is someone to trade with. It skews its quotes against its inventory to
  avoid piling up a big position. It ignores the news, so its quotes go stale
  right after a headline.
- NewsTrader: reads each headline and trades toward its own (noisy) estimate of
  fair value, but only a few seconds after the news. That delay is the human's
  window to trade against the market maker's stale quotes first.
- NoiseTrader: places small random orders near the last price, so the tape
  never looks dead.

Bot names contain a space and brackets, which human names can't, so a player
can never log in as a bot.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING, Optional

from .game import STARTING_PRICE, STARTING_SHARES, OrderRejected

if TYPE_CHECKING:
    from .game import Room


class Bot:
    name = "[bot]"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.reset(now=0.0)

    def reset(self, now: float) -> None:
        """Called when a new round starts."""

    def act(self, room: Room, now: float) -> bool:
        """Look at the market and maybe trade. Returns True if anything changed."""
        raise NotImplementedError

    # ---------- helpers ----------

    def _cancel_all(self, room: Room) -> bool:
        orders = room.book.open_orders(self.name)
        for order in orders:
            room.cancel(self.name, order.id)
        return bool(orders)

    def _order(self, room: Room, side: str, qty: int, price: int) -> bool:
        """Place a limit order, shrinking it to what the bot can afford."""
        price = max(1, price)
        if side == "buy":
            qty = min(qty, room.buying_power(self.name) // price)
        else:
            qty = min(qty, room.sellable_shares(self.name))
        if qty <= 0:
            return False
        try:
            room.place_order(self.name, side, qty, price)
        except OrderRejected:
            return False
        return True


class MarketMaker(Bot):
    name = "[bot] maker"
    REFRESH_SECONDS = 2.0
    QUOTE_QTY = 10
    HALF_SPREAD_PCT = 0.5
    MIN_HALF_SPREAD = 5     # cents
    SKEW_PER_SHARE = 2      # cents the quotes move per share of inventory

    def reset(self, now: float) -> None:
        self.next_quote_at = now

    def act(self, room: Room, now: float) -> bool:
        if now < self.next_quote_at:
            return False
        self.next_quote_at = now + self.REFRESH_SECONDS
        self._cancel_all(room)

        # Holding extra shares -> quote lower to sell them off, and vice versa.
        inventory = room.accounts[self.name].shares - STARTING_SHARES
        mid = room.last_price - inventory * self.SKEW_PER_SHARE
        half = max(self.MIN_HALF_SPREAD, round(mid * self.HALF_SPREAD_PCT / 100))
        self._order(room, "buy", self.QUOTE_QTY, mid - half)
        self._order(room, "sell", self.QUOTE_QTY, mid + half)
        return True


class NewsTrader(Bot):
    name = "[bot] news"
    REACTION_SECONDS = (4.0, 8.0)  # head start humans get after each headline
    READ_ACCURACY = (0.6, 1.2)     # how much of a headline's real impact it guesses
    EDGE_PCT = 1.0                 # only trades if the price is this far from its estimate
    TRADE_QTY = 10

    def reset(self, now: float) -> None:
        self.estimate = STARTING_PRICE
        self.news_seen = 0
        self.react_at: Optional[float] = None

    def act(self, room: Room, now: float) -> bool:
        changed = self._cancel_all(room)  # never leave stale orders resting

        if self.react_at is None and self.news_seen < len(room.news):
            self.react_at = now + self.rng.uniform(*self.REACTION_SECONDS)
        if self.react_at is not None and now >= self.react_at:
            for item in room.news[self.news_seen:]:
                guess = item.change_pct * self.rng.uniform(*self.READ_ACCURACY)
                self.estimate = round(self.estimate * (100 + guess) / 100)
            self.news_seen = len(room.news)
            self.react_at = None

        cheap = round(self.estimate * (100 - self.EDGE_PCT) / 100)
        rich = round(self.estimate * (100 + self.EDGE_PCT) / 100)
        best_ask, best_bid = room.book.best_ask(), room.book.best_bid()
        if best_ask is not None and best_ask <= cheap:
            changed |= self._order(room, "buy", self.TRADE_QTY, cheap)
        elif best_bid is not None and best_bid >= rich:
            changed |= self._order(room, "sell", self.TRADE_QTY, rich)
        return changed


class NoiseTrader(Bot):
    name = "[bot] noise"
    EVERY_SECONDS = (2.0, 6.0)
    MAX_QTY = 5
    PRICE_JITTER_PCT = 1.0

    def reset(self, now: float) -> None:
        self.next_trade_at = now + self.rng.uniform(*self.EVERY_SECONDS)

    def act(self, room: Room, now: float) -> bool:
        if now < self.next_trade_at:
            return False
        self.next_trade_at = now + self.rng.uniform(*self.EVERY_SECONDS)
        self._cancel_all(room)
        side = self.rng.choice(["buy", "sell"])
        jitter = self.rng.uniform(-self.PRICE_JITTER_PCT, self.PRICE_JITTER_PCT)
        price = round(room.last_price * (100 + jitter) / 100)
        self._order(room, side, self.rng.randint(1, self.MAX_QTY), price)
        return True


def add_bots(room: Room) -> None:
    """Seat one of each bot in the room. Their randomness comes from the room's."""
    for cls in (MarketMaker, NewsTrader, NoiseTrader):
        bot = cls(random.Random(room.rng.random()))
        room.join(bot.name)
        room.bots.append(bot)

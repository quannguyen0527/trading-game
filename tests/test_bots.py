import random
import unittest

from server.app import NAME_PATTERN
from server.bots import MarketMaker, NewsTrader, NoiseTrader, add_bots
from server.game import ROUND_SECONDS, STARTING_PRICE, STARTING_SHARES, NewsItem, Room


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def room_with(*bot_classes, seed=1):
    """A started room holding only the given bots, plus a human called alice."""
    clock = FakeClock()
    room = Room("test", clock=clock, rng=random.Random(seed))
    bots = []
    for cls in bot_classes:
        bot = cls(random.Random(seed))
        room.join(bot.name)
        room.bots.append(bot)
        bots.append(bot)
    room.join("alice")
    room.start()
    return room, clock, bots


class TestBots(unittest.TestCase):
    def test_humans_cannot_use_bot_names(self):
        for cls in (MarketMaker, NewsTrader, NoiseTrader):
            self.assertIsNone(NAME_PATTERN.match(cls.name))

    def test_market_maker_quotes_both_sides_around_last_price(self):
        room, clock, (maker,) = room_with(MarketMaker)
        self.assertTrue(maker.act(room, clock.now))
        bid, ask = room.book.best_bid(), room.book.best_ask()
        self.assertLess(bid, STARTING_PRICE)
        self.assertGreater(ask, STARTING_PRICE)

    def test_market_maker_quotes_lower_when_holding_extra_shares(self):
        room, clock, (maker,) = room_with(MarketMaker)
        maker.act(room, clock.now)
        flat_bid = room.book.best_bid()

        room.accounts[maker.name].shares = STARTING_SHARES + 20
        clock.now += MarketMaker.REFRESH_SECONDS
        maker.act(room, clock.now)
        self.assertLess(room.book.best_bid(), flat_bid)

    def test_news_trader_waits_before_reacting(self):
        room, clock, (news,) = room_with(NewsTrader)
        room.place_order("alice", "sell", 10, STARTING_PRICE)  # fairly priced, for now
        room.news.append(NewsItem("Great news", 15, 0))

        news.act(room, clock.now)
        self.assertEqual(room.trades, [], "reacted with no delay")

        clock.now += NewsTrader.REACTION_SECONDS[1]
        news.act(room, clock.now)
        self.assertEqual(room.trades[-1].buyer, news.name)
        self.assertGreater(news.estimate, STARTING_PRICE)

    def test_bots_never_overspend(self):
        room, clock, (news,) = room_with(NewsTrader)
        room.accounts[news.name].cash = 0
        room.place_order("alice", "sell", 10, 50_00)  # a bargain it can't afford
        self.assertFalse(news.act(room, clock.now))
        self.assertEqual(room.trades, [])

    def test_full_round_with_bots_conserves_cash_and_shares(self):
        room = Room("test", clock=FakeClock(), rng=random.Random(3))
        add_bots(room)
        room.start()
        totals = lambda: (
            sum(a.cash for a in room.accounts.values()),
            sum(a.shares for a in room.accounts.values()),
        )
        before = totals()
        for second in range(1, ROUND_SECONDS + 1):
            room.clock.now = room.started_at + second
            room.tick()
        self.assertGreater(len(room.trades), 0)
        self.assertEqual(totals(), before)  # trading only moves money between players

    def test_bots_reset_each_round(self):
        room, clock, (news,) = room_with(NewsTrader)
        news.estimate, news.news_seen = 1, 5
        clock.now += ROUND_SECONDS
        room.tick()
        room.start()
        self.assertEqual((news.estimate, news.news_seen), (STARTING_PRICE, 0))


if __name__ == "__main__":
    unittest.main()

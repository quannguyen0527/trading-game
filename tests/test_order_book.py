import unittest

from engine import OrderBook, Side


class TestOrderBook(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook()

    def test_non_crossing_orders_rest(self):
        _, trades = self.book.submit("alice", Side.BUY, 10, price=100)
        _, trades2 = self.book.submit("bob", Side.SELL, 10, price=105)
        self.assertEqual(trades + trades2, [])
        self.assertEqual(self.book.best_bid(), 100)
        self.assertEqual(self.book.best_ask(), 105)

    def test_full_match_at_resting_price(self):
        self.book.submit("alice", Side.SELL, 10, price=100)
        _, trades = self.book.submit("bob", Side.BUY, 10, price=110)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t.buyer, t.seller, t.price, t.qty), ("bob", "alice", 100, 10))
        self.assertIsNone(self.book.best_ask())
        self.assertIsNone(self.book.best_bid())

    def test_partial_fill_leaves_remainder_resting(self):
        self.book.submit("alice", Side.SELL, 4, price=100)
        order, trades = self.book.submit("bob", Side.BUY, 10, price=100)
        self.assertEqual(sum(t.qty for t in trades), 4)
        self.assertEqual(order.remaining, 6)
        self.assertEqual(self.book.best_bid(), 100)

    def test_walks_multiple_price_levels_best_first(self):
        self.book.submit("a", Side.SELL, 5, price=102)
        self.book.submit("b", Side.SELL, 5, price=100)
        self.book.submit("c", Side.SELL, 5, price=101)
        _, trades = self.book.submit("buyer", Side.BUY, 12, price=105)
        self.assertEqual([(t.price, t.qty) for t in trades], [(100, 5), (101, 5), (102, 2)])

    def test_time_priority_at_same_price(self):
        self.book.submit("first", Side.SELL, 5, price=100)
        self.book.submit("second", Side.SELL, 5, price=100)
        _, trades = self.book.submit("buyer", Side.BUY, 5, price=100)
        self.assertEqual(trades[0].seller, "first")

    def test_market_order_fills_and_never_rests(self):
        self.book.submit("alice", Side.SELL, 3, price=100)
        order, trades = self.book.submit("bob", Side.BUY, 10)  # market
        self.assertEqual(sum(t.qty for t in trades), 3)
        self.assertTrue(order.cancelled)
        self.assertIsNone(self.book.best_bid())

    def test_cancel_removes_order_from_matching(self):
        order, _ = self.book.submit("alice", Side.SELL, 5, price=100)
        self.assertTrue(self.book.cancel(order.id))
        self.assertFalse(self.book.cancel(order.id))  # already cancelled
        _, trades = self.book.submit("bob", Side.BUY, 5, price=100)
        self.assertEqual(trades, [])

    def test_depth_aggregates_by_price(self):
        self.book.submit("a", Side.BUY, 5, price=99)
        self.book.submit("b", Side.BUY, 3, price=99)
        self.book.submit("c", Side.BUY, 2, price=98)
        self.book.submit("d", Side.SELL, 4, price=101)
        self.assertEqual(
            self.book.depth(),
            {"bids": [(99, 8), (98, 2)], "asks": [(101, 4)]},
        )

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            self.book.submit("a", Side.BUY, 0, price=100)
        with self.assertRaises(ValueError):
            self.book.submit("a", Side.BUY, 1, price=-5)


if __name__ == "__main__":
    unittest.main()

import random
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from server import app as app_module
from server import game as game_module
from server.game import (
    NEWS_EVERY, ROUND_SECONDS, STARTING_CASH, STARTING_PRICE, STARTING_SHARES,
    OrderRejected, Phase, Room,
)


class FakeClock:
    """A clock the test moves by hand, so a 3-minute round takes no real time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestRoom(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.room = Room("test", clock=self.clock, rng=random.Random(42))
        self.room.join("alice")
        self.room.join("bob")
        self.room.start()

    def test_trade_moves_cash_and_shares(self):
        self.room.place_order("alice", "sell", 10, 100_00)
        self.room.place_order("bob", "buy", 10, 100_00)
        alice, bob = self.room.accounts["alice"], self.room.accounts["bob"]
        self.assertEqual(alice.cash, STARTING_CASH + 1000_00)
        self.assertEqual(alice.shares, STARTING_SHARES - 10)
        self.assertEqual(bob.cash, STARTING_CASH - 1000_00)
        self.assertEqual(bob.shares, STARTING_SHARES + 10)
        self.assertEqual(self.room.last_price, 100_00)

    def test_cannot_spend_same_cash_twice(self):
        # $10,000 cash: one $6,000 buy fits, a second one does not.
        self.room.place_order("bob", "buy", 60, 100_00)
        with self.assertRaises(OrderRejected):
            self.room.place_order("bob", "buy", 60, 100_00)

    def test_cannot_sell_more_shares_than_owned(self):
        self.room.place_order("alice", "sell", STARTING_SHARES, 200_00)
        with self.assertRaises(OrderRejected):
            self.room.place_order("alice", "sell", 1, 200_00)

    def test_cancel_frees_reserved_cash(self):
        self.room.place_order("bob", "buy", 100, 100_00)
        self.assertEqual(self.room.buying_power("bob"), 0)
        order_id = self.room.private_state("bob")["open_orders"][0]["id"]
        self.room.cancel("bob", order_id)
        self.assertEqual(self.room.buying_power("bob"), STARTING_CASH)

    def test_cannot_cancel_someone_elses_order(self):
        self.room.place_order("alice", "sell", 1, 100_00)
        order_id = self.room.private_state("alice")["open_orders"][0]["id"]
        with self.assertRaises(OrderRejected):
            self.room.cancel("bob", order_id)

    def test_rejects_bad_input(self):
        for side, qty, price in [
            ("hold", 1, 100), ("buy", 0, 100), ("buy", True, 100),
            ("buy", 1.5, 100), ("buy", 1, -1), ("buy", 1, None),
        ]:
            with self.subTest(side=side, qty=qty, price=price):
                with self.assertRaises(OrderRejected):
                    self.room.place_order("alice", side, qty, price)

    def test_market_sell_with_no_buyers_is_rejected(self):
        with self.assertRaises(OrderRejected):
            self.room.place_order("alice", "sell", 1, None)

    def test_market_sell_fills_against_best_bid(self):
        self.room.place_order("bob", "buy", 5, 99_00)
        trades = self.room.place_order("alice", "sell", 5, None)
        self.assertEqual([(t.price, t.qty) for t in trades], [(99_00, 5)])

    def test_leaderboard_sorted_by_net_worth(self):
        self.room.join("carol")
        self.room.place_order("alice", "sell", 10, 150_00)
        self.room.place_order("bob", "buy", 10, 150_00)   # bob buys high at $150
        self.room.place_order("carol", "sell", 1, 50_00)
        self.room.place_order("alice", "buy", 1, 50_00)   # price crashes to $50
        # alice sold high: $11,450 + 91 * $50 = $16,000
        # carol held:      $10,050 + 99 * $50 = $15,000
        # bob bought high:  $8,500 + 110 * $50 = $14,000
        names = [row["name"] for row in self.room.public_state()["leaderboard"]]
        self.assertEqual(names, ["alice", "carol", "bob"])


class TestRounds(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.room = Room("test", clock=self.clock, rng=random.Random(7))
        self.room.join("alice")
        self.room.join("bob")

    def test_no_trading_before_round_starts(self):
        with self.assertRaises(OrderRejected):
            self.room.place_order("alice", "sell", 1, 100_00)

    def test_news_arrives_and_moves_fair_value(self):
        self.room.start()
        self.assertFalse(self.room.tick())  # nothing due yet
        self.clock.now += NEWS_EVERY[1]     # the first headline is due by now
        self.assertTrue(self.room.tick())
        self.assertGreaterEqual(len(self.room.news), 1)
        expected = STARTING_PRICE
        for item in self.room.news:
            expected = round(expected * (100 + item.change_pct) / 100)
        self.assertEqual(self.room.fair_value, expected)

    def test_fair_value_and_news_impact_hidden_until_round_ends(self):
        self.room.start()
        self.clock.now += NEWS_EVERY[1]
        self.room.tick()
        state = self.room.public_state()
        self.assertIsNone(state["fair_value"])
        self.assertIsNone(state["news"][0]["change_pct"])

        self.clock.now += ROUND_SECONDS
        self.room.tick()
        state = self.room.public_state()
        self.assertEqual(state["fair_value"], self.room.fair_value)
        self.assertIsNotNone(state["news"][0]["change_pct"])

    def test_round_end_cancels_orders_and_settles_at_fair_value(self):
        self.room.start()
        self.room.place_order("alice", "sell", 10, 120_00)
        self.clock.now += ROUND_SECONDS
        self.assertTrue(self.room.tick())

        self.assertEqual(self.room.phase, Phase.FINISHED)
        self.assertEqual(self.room.seconds_left(), 0)
        self.assertEqual(self.room.private_state("alice")["open_orders"], [])
        self.assertEqual(
            self.room.net_worth("alice"),
            STARTING_CASH + STARTING_SHARES * self.room.fair_value,
        )
        with self.assertRaises(OrderRejected):
            self.room.place_order("bob", "buy", 1, 100_00)

    def test_no_news_after_round_ends(self):
        self.room.start()
        self.clock.now += ROUND_SECONDS * 10  # server was asleep a long time
        self.room.tick()
        self.assertTrue(all(n.second <= ROUND_SECONDS for n in self.room.news))

    def test_new_round_resets_accounts(self):
        self.room.start()
        self.room.place_order("alice", "sell", 10, 100_00)
        self.room.place_order("bob", "buy", 10, 100_00)
        self.clock.now += ROUND_SECONDS
        self.room.tick()

        self.room.start()
        alice = self.room.accounts["alice"]
        self.assertEqual((alice.cash, alice.shares), (STARTING_CASH, STARTING_SHARES))
        self.assertEqual(self.room.news, [])
        self.assertEqual(self.room.fair_value, STARTING_PRICE)

    def test_price_history_has_one_point_per_second(self):
        self.room.start()
        for _ in range(5):
            self.clock.now += 1
            self.room.tick()
            self.room.tick()  # a second tick in the same second adds nothing
        seconds = [s for s, _ in self.room.public_state()["price_history"]]
        self.assertEqual(seconds, [0, 1, 2, 3, 4, 5])

    def test_fair_value_history_hidden_until_round_ends(self):
        self.room.start()
        self.clock.now += NEWS_EVERY[1]
        self.room.tick()
        self.assertIsNone(self.room.public_state()["fair_history"])

        self.clock.now += ROUND_SECONDS
        self.room.tick()
        state = self.room.public_state()
        self.assertEqual(state["fair_history"][-1][1], state["fair_value"])
        self.assertEqual(state["price_history"][-1][0], ROUND_SECONDS)

    def test_cannot_start_twice(self):
        self.room.start()
        with self.assertRaises(OrderRejected):
            self.room.start()


class TestWebSocket(unittest.TestCase):
    def setUp(self):
        app_module.rooms.clear()
        app_module.connections.clear()
        app_module.clock_tasks.clear()
        # One shared event loop for every connection, like the real server.
        self.client = self.enterContext(TestClient(app_module.app))

    def test_two_players_trade_and_both_see_it(self):
        with self.client.websocket_connect("/ws/r1?name=alice") as alice, \
             self.client.websocket_connect("/ws/r1?name=bob") as bob:
            alice.receive_json()  # alice joined
            alice.receive_json()  # bob joined
            bob.receive_json()
            alice.send_json({"type": "start"})
            self.assertEqual(alice.receive_json()["public"]["phase"], "running")
            bob.receive_json()

            alice.send_json({"type": "order", "side": "sell", "qty": 5, "price": 101_00})
            alice.receive_json(); bob.receive_json()
            bob.send_json({"type": "order", "side": "buy", "qty": 5, "price": 101_00})

            state = alice.receive_json()
            self.assertEqual(state["public"]["last_price"], 101_00)
            self.assertEqual(state["you"]["shares"], STARTING_SHARES - 5)
            self.assertEqual(bob.receive_json()["you"]["shares"], STARTING_SHARES + 5)

    def test_rejected_order_returns_error_only_to_sender(self):
        with self.client.websocket_connect("/ws/r2?name=alice") as alice:
            alice.receive_json()
            alice.send_json({"type": "start"})
            alice.receive_json()
            alice.send_json({"type": "order", "side": "sell", "qty": 999, "price": 1})
            self.assertEqual(alice.receive_json()["type"], "error")

    def test_clock_ends_round_and_pushes_result(self):
        # Shrink the round so the real clock task finishes it within a fraction of a second.
        with mock.patch.object(game_module, "ROUND_SECONDS", 0.05), \
             mock.patch.object(app_module, "TICK_SECONDS", 0.01), \
             self.client.websocket_connect("/ws/r4?name=alice") as alice:
            alice.receive_json()
            alice.send_json({"type": "start"})
            self.assertEqual(alice.receive_json()["public"]["phase"], "running")
            final = alice.receive_json()["public"]
            while final["phase"] == "running":  # skip updates caused by bot trades
                final = alice.receive_json()["public"]
            self.assertEqual(final["phase"], "finished")
            self.assertEqual(final["fair_value"], STARTING_PRICE)

    def test_health_check(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_room_is_deleted_when_everyone_leaves(self):
        with self.client.websocket_connect("/ws/r5?name=alice") as alice:
            alice.receive_json()
            self.assertIn("r5", app_module.rooms)
        self.assertNotIn("r5", app_module.rooms)

    def test_server_refuses_new_rooms_when_full(self):
        with mock.patch.object(app_module, "MAX_ROOMS", 1), \
             self.client.websocket_connect("/ws/r6?name=alice") as alice:
            alice.receive_json()
            with self.client.websocket_connect("/ws/r7?name=bob") as bob:
                self.assertIn("full", bob.receive_json()["message"])
            self.assertNotIn("r7", app_module.rooms)

    def test_room_refuses_players_when_full(self):
        with mock.patch.object(app_module, "MAX_PLAYERS_PER_ROOM", 1), \
             self.client.websocket_connect("/ws/r8?name=alice") as alice:
            alice.receive_json()
            with self.client.websocket_connect("/ws/r8?name=bob") as bob:
                self.assertIn("full", bob.receive_json()["message"])

    def test_rate_limit(self):
        with mock.patch.object(app_module, "MAX_MESSAGES_PER_SECOND", 2), \
             self.client.websocket_connect("/ws/r9?name=alice") as alice:
            alice.receive_json()
            for _ in range(3):
                alice.send_json({"type": "cancel", "order_id": 999})
            messages = [alice.receive_json()["message"] for _ in range(3)]
            self.assertEqual(messages[:2], ["no open order with that id"] * 2)
            self.assertIn("slow down", messages[2])

    def test_duplicate_name_is_refused(self):
        with self.client.websocket_connect("/ws/r3?name=alice") as first:
            first.receive_json()
            with self.client.websocket_connect("/ws/r3?name=alice") as second:
                msg = second.receive_json()
                self.assertEqual(msg["type"], "error")


if __name__ == "__main__":
    unittest.main()

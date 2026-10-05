import unittest

from fastapi.testclient import TestClient

from server import app as app_module
from server.game import STARTING_CASH, STARTING_SHARES, OrderRejected, Room


class TestRoom(unittest.TestCase):
    def setUp(self):
        self.room = Room("test")
        self.room.join("alice")
        self.room.join("bob")

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


class TestWebSocket(unittest.TestCase):
    def setUp(self):
        app_module.rooms.clear()
        app_module.connections.clear()
        self.client = TestClient(app_module.app)

    def test_two_players_trade_and_both_see_it(self):
        with self.client.websocket_connect("/ws/r1?name=alice") as alice, \
             self.client.websocket_connect("/ws/r1?name=bob") as bob:
            alice.receive_json()  # alice joined
            alice.receive_json()  # bob joined
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
            alice.send_json({"type": "order", "side": "sell", "qty": 999, "price": 1})
            self.assertEqual(alice.receive_json()["type"], "error")

    def test_duplicate_name_is_refused(self):
        with self.client.websocket_connect("/ws/r3?name=alice") as first:
            first.receive_json()
            with self.client.websocket_connect("/ws/r3?name=alice") as second:
                msg = second.receive_json()
                self.assertEqual(msg["type"], "error")


if __name__ == "__main__":
    unittest.main()

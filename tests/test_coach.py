import random
import unittest
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
from fastapi.testclient import TestClient

from server import app as app_module
from server.coach import COACH_MODEL, Coach, CoachUnavailable, round_report
from server.game import ROUND_SECONDS, NewsItem, Room


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records each request, returns a canned reply."""

    def __init__(self, text="Nice work.", stop_reason="end_turn", error=None):
        self.calls = []
        self.text, self.stop_reason, self.error = text, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=self.text)],
        )


def finished_round():
    """alice buys right after good news, then the round ends."""
    clock = FakeClock()
    room = Room("test", clock=clock, rng=random.Random(1))
    room.join("alice")
    room.join("bob")
    room.start()
    clock.now = 12
    room.news.append(NewsItem("Bayou Energy discovers major new offshore oil field", 15, 12))
    room.fair_value = 115_00
    room.fair_history.append((12, 115_00))
    clock.now = 14
    room.place_order("bob", "sell", 10, 101_00)
    room.place_order("alice", "buy", 10, 101_00)
    room.next_news_at = float("inf")  # no more random headlines before the bell
    clock.now = ROUND_SECONDS
    room.tick()
    return room, clock


class TestRoundReport(unittest.TestCase):
    def test_report_has_the_facts_the_coach_needs(self):
        room, _ = finished_round()
        report = round_report(room, "alice")
        self.assertIn("0:12  Bayou Energy discovers major new offshore oil field  +15%  -> $115.00", report)
        self.assertIn("0:14  BOUGHT 10 @ $101.00 from/to another player; fair value then $115.00", report)
        self.assertIn("rank 1 of 2", report)
        self.assertIn("Final fair value: $115.00", report)

    def test_report_with_no_trades(self):
        room, _ = finished_round()
        room.join("carol")
        self.assertIn("(no trades)", round_report(room, "carol"))


class TestCoach(unittest.IsolatedAsyncioTestCase):
    async def test_review_sends_report_and_returns_text(self):
        room, _ = finished_round()
        claude = FakeClaude(text="You bought before the news bot. Great.")
        text = await Coach(claude).review(room, "alice")
        self.assertEqual(text, "You bought before the news bot. Great.")
        call = claude.calls[0]
        self.assertEqual(call["model"], COACH_MODEL)
        self.assertEqual(call["fallbacks"], "default")
        self.assertIn("BOUGHT 10 @ $101.00", call["messages"][0]["content"])

    async def test_same_round_is_only_paid_for_once(self):
        room, _ = finished_round()
        claude = FakeClaude()
        coach = Coach(claude)
        first, second = coach.review(room, "alice"), coach.review(room, "alice")
        self.assertEqual(await first, await second)
        self.assertEqual(len(claude.calls), 1)

    async def test_no_review_until_round_ends(self):
        room, _ = finished_round()
        room.start()
        with self.assertRaises(CoachUnavailable):
            Coach(FakeClaude()).review(room, "alice")

    async def test_disabled_without_client(self):
        room, _ = finished_round()
        coach = Coach(None)
        self.assertFalse(coach.enabled)
        with self.assertRaises(CoachUnavailable):
            coach.review(room, "alice")

    async def test_hourly_limit(self):
        room, _ = finished_round()
        clock = FakeClock()
        coach = Coach(FakeClaude(), clock=clock, max_per_hour=1)
        await coach.review(room, "alice")
        with self.assertRaises(CoachUnavailable):
            coach.review(room, "bob")
        clock.now += 3601  # an hour later the budget is back
        await coach.review(room, "bob")

    async def test_refusal_becomes_a_friendly_error(self):
        room, _ = finished_round()
        with self.assertRaises(CoachUnavailable):
            await Coach(FakeClaude(stop_reason="refusal")).review(room, "alice")

    async def test_api_failure_can_be_retried(self):
        room, _ = finished_round()
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        claude = FakeClaude(error=anthropic.APIConnectionError(request=request))
        coach = Coach(claude)
        with self.assertRaises(CoachUnavailable), self.assertLogs("server.coach", "ERROR"):
            await coach.review(room, "alice")
        claude.error = None
        self.assertEqual(await coach.review(room, "alice"), "Nice work.")
        self.assertEqual(len(claude.calls), 2)


class TestCoachOverWebSocket(unittest.TestCase):
    def setUp(self):
        app_module.rooms.clear()
        app_module.connections.clear()
        app_module.clock_tasks.clear()
        self.claude = FakeClaude(text="Good round.")
        patcher = mock.patch.object(app_module, "coach", Coach(self.claude))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app_module.app))

    def test_coach_reviews_finished_round(self):
        with mock.patch("server.game.ROUND_SECONDS", 0.05), \
             mock.patch.object(app_module, "TICK_SECONDS", 0.01), \
             self.client.websocket_connect("/ws/c1?name=alice") as alice:
            self.assertTrue(alice.receive_json()["public"]["coach_enabled"])
            alice.send_json({"type": "coach"})
            self.assertIn("error", alice.receive_json())  # round hasn't been played yet

            alice.send_json({"type": "start"})
            msg = alice.receive_json()
            while msg["public"]["phase"] != "finished":
                msg = alice.receive_json()
            alice.send_json({"type": "coach"})
            reply = alice.receive_json()
            self.assertEqual(reply, {"type": "coach", "round": 1, "text": "Good round."})


if __name__ == "__main__":
    unittest.main()

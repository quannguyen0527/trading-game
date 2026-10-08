"""
AI trading coach: after a round, Claude reviews one player's trades and writes a
short, personal debrief.

The model never sees the live game. We first build a plain-text report of facts
from the round (every trade with its time, the hidden fair value at that moment,
the headlines, the final standings), so the feedback is grounded in what really
happened and can quote real numbers. That report is built synchronously, before
any await, so a new round starting mid-request can't change it.

Cost controls, since the demo is public:
- one review per player per round (cached; repeat clicks reuse it)
- at most COACH_MAX_PER_HOUR reviews across the whole server
- disabled entirely when no ANTHROPIC_API_KEY is set
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from typing import TYPE_CHECKING, Callable, Deque, Dict, Optional, Tuple

import anthropic

from .game import STARTING_NET_WORTH, Phase

if TYPE_CHECKING:
    from .game import Room

log = logging.getLogger(__name__)

COACH_MODEL = os.environ.get("COACH_MODEL", "claude-opus-5-5")
# Small on purpose: one busy hour shouldn't use up a month's API budget. This count
# lives in memory and resets when the server restarts, so the hard ceiling is the
# monthly spend limit set in the Anthropic Console.
COACH_MAX_PER_HOUR = int(os.environ.get("COACH_MAX_PER_HOUR", "10"))
MAX_FILLS_IN_REPORT = 80

SYSTEM_PROMPT = """\
You are the trading coach in BYOU, a multiplayer stock-trading game that teaches how markets react to news.

How the game works: each round lasts 3 minutes. Every player starts with $10,000 and 100 shares of Bayou Energy (BYOU), starting at $100. The stock has a hidden fair value; news headlines arrive every 15-30 seconds and move it by a set percentage. When the round ends, every share is valued at the final fair value. Players trade with each other and with three bots: a market maker that always quotes a bid and an ask but ignores the news, a news trader that reacts to headlines 4-8 seconds late with an imperfect estimate, and a noise trader that places small random orders. So the edge comes from reading a headline and trading against the market maker's stale quotes before the news bot does.

You will get a factual report of one player's round. Write them a short debrief, speaking to them directly:
- what they did well, if anything
- their biggest missed opportunity or mistake, citing the time and the numbers from the report
- one concrete thing to try next round

Where it fits naturally, name and briefly explain one real trading concept the round illustrates (for example the bid-ask spread, adverse selection, position sizing, or how fast prices absorb news). If they made no trades, encourage them and explain the single best trade the news offered.

Keep it under 150 words, plain text, no headings or bold. Up to three short "- " bullet points are fine. Use only numbers that appear in the report; don't invent prices or times."""


class CoachUnavailable(Exception):
    """The review couldn't be produced. The message is shown to the player."""


def _money(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def _mmss(second: int) -> str:
    return f"{second // 60}:{second % 60:02d}"


def _who(name: str, me: str) -> str:
    return "you" if name == me else name if name.startswith("[bot]") else "another player"


def round_report(room: Room, name: str) -> str:
    """A factual summary of one player's finished round, used as the model's input."""
    fair_points = room.fair_history  # (second, fair value), first entry at 0:00

    def fair_at(second: int) -> int:
        value = fair_points[0][1]
        for s, v in fair_points:
            if s <= second:
                value = v
        return value

    leaderboard = room.public_state()["leaderboard"]
    rank = next(i for i, row in enumerate(leaderboard, 1) if row["name"] == name)
    account = room.accounts[name]
    pnl = room.net_worth(name) - STARTING_NET_WORTH

    lines = [
        f"Player: {name}",
        f"Final fair value: {_money(room.fair_value)} (started at {_money(fair_points[0][1])}). "
        f"Last trade price at the bell: {_money(room.last_price)}.",
        f"Result: rank {rank} of {len(leaderboard)}, net worth {_money(room.net_worth(name))} "
        f"({'+' if pnl >= 0 else ''}{_money(pnl)} vs the {_money(STARTING_NET_WORTH)} start). "
        f"Ended with {_money(account.cash)} cash and {account.shares} shares.",
        "",
        "Headlines (time, headline, effect on fair value, fair value after):",
    ]
    for item in room.news:
        lines.append(
            f"  {_mmss(item.second)}  {item.headline}  {item.change_pct:+d}%  -> {_money(fair_at(item.second))}"
        )
    if not room.news:
        lines.append("  (none)")

    mine = [(s, t) for s, t in room.fills if name in (t.buyer, t.seller)]
    lines += ["", f"Your trades ({len(mine)} total; fair value shown is what each share was really worth at that moment):"]
    for second, t in mine[:MAX_FILLS_IN_REPORT]:
        bought = t.buyer == name
        other = t.seller if bought else t.buyer
        lines.append(
            f"  {_mmss(second)}  {'BOUGHT' if bought else 'SOLD'} {t.qty} @ {_money(t.price)} "
            f"from/to {_who(other, name)}; fair value then {_money(fair_at(second))}"
        )
    if len(mine) > MAX_FILLS_IN_REPORT:
        lines.append(f"  ... {len(mine) - MAX_FILLS_IN_REPORT} more trades not shown")
    if not mine:
        lines.append("  (no trades)")

    lines += ["", "Final standings:"]
    for i, row in enumerate(leaderboard, 1):
        row_pnl = row["net_worth"] - STARTING_NET_WORTH
        lines.append(f"  {i}. {_who(row['name'], name)}: {_money(row['net_worth'])} ({'+' if row_pnl >= 0 else ''}{_money(row_pnl)})")
    return "\n".join(lines)


class Coach:
    def __init__(
        self,
        client: Optional[anthropic.AsyncAnthropic],
        clock: Callable[[], float] = time.monotonic,
        max_per_hour: int = COACH_MAX_PER_HOUR,
    ) -> None:
        self.client = client
        self.clock = clock
        self.max_per_hour = max_per_hour
        # (room id, round number, player) -> the review, finished or in progress
        self._reviews: Dict[Tuple[str, int, str], asyncio.Task] = {}
        self._recent_calls: Deque[float] = collections.deque()

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def review(self, room: Room, name: str) -> asyncio.Task:
        """Start (or reuse) the review of `name`'s last round. Await the result for the text."""
        if not self.enabled:
            raise CoachUnavailable("the AI coach isn't set up on this server")
        if room.phase != Phase.FINISHED:
            raise CoachUnavailable("the coach reviews your trades once the round is over")

        key = (room.id, room.round_number, name)
        task = self._reviews.get(key)
        if task is not None and not (task.done() and task.exception()):
            return task  # already reviewed, or still in progress: don't pay twice

        self._check_rate_limit()
        report = round_report(room, name)  # before any await: the round can't change under us
        task = asyncio.create_task(self._ask_claude(report))
        self._reviews[key] = task
        self._forget_old_reviews()
        return task

    def _check_rate_limit(self) -> None:
        now = self.clock()
        while self._recent_calls and now - self._recent_calls[0] > 3600:
            self._recent_calls.popleft()
        if len(self._recent_calls) >= self.max_per_hour:
            raise CoachUnavailable("the coach has hit its hourly limit; try again later")
        self._recent_calls.append(now)

    def _forget_old_reviews(self) -> None:
        # Keep memory bounded on a long-running server: drop the oldest finished reviews.
        while len(self._reviews) > 1000:
            oldest = next(iter(self._reviews))
            self._reviews.pop(oldest)

    async def _ask_claude(self, report: str) -> str:
        try:
            response = await self.client.beta.messages.create(
                model=COACH_MODEL,
                max_tokens=16000,
                output_config={"effort": "low"},  # a short debrief doesn't need deep reasoning
                # If the model declines, the API retries on a fallback model automatically.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"<round_report>\n{report}\n</round_report>"}],
            )
        except anthropic.RateLimitError:
            log.warning("coach: rate limited by the API")
            raise CoachUnavailable("the coach is busy right now; try again in a minute")
        except anthropic.APIStatusError as e:
            log.error("coach: API error %s (request %s)", e.status_code, e.request_id)
            raise CoachUnavailable("the coach couldn't review this round; try again")
        except anthropic.APIConnectionError:
            log.error("coach: couldn't reach the API")
            raise CoachUnavailable("the coach couldn't be reached; try again")

        if response.stop_reason == "refusal":
            raise CoachUnavailable("the coach couldn't review this round")
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise CoachUnavailable("the coach had nothing to say this time; try again")
        return text


def coach_from_env() -> Coach:
    """A coach that calls Claude if ANTHROPIC_API_KEY is set, otherwise a disabled one."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return Coach(client=None)
    return Coach(client=anthropic.AsyncAnthropic(timeout=60.0))

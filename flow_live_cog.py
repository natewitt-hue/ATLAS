"""
flow_live_cog.py — ATLAS FLOW Live Engagement System
─────────────────────────────────────────────────────
Manages #flow-live channel: pulse dashboard, highlight broadcasts,
session recaps. Consumes events from flow_events.py.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from dataclasses import dataclass as dc
from enum import Enum, auto
from typing import Optional

try:
    import discord
    from discord.ext import commands, tasks
except ImportError:
    discord = None  # type: ignore
    commands = None  # type: ignore
    tasks = None  # type: ignore

try:
    from flow_events import GameResultEvent, SportsbookEvent, PredictionEvent, flow_bus
except ImportError:
    flow_bus = None  # Soft fallback — cog degrades gracefully if flow_events missing

log = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT = 300  # 5 minutes


@dataclass
class PlayerSession:
    discord_id: int
    guild_id: int
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    total_games: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    net_profit: int = 0
    biggest_win: int = 0
    biggest_loss: int = 0
    current_streak: int = 0     # positive=wins, negative=losses
    best_streak: int = 0
    games_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    events: list = field(default_factory=list)

    def record(self, event: "GameResultEvent") -> None:
        self.last_activity = time.time()
        self.total_games += 1
        self.games_by_type[event.game_type] += 1
        self.events.append(event)

        profit = event.net_profit
        self.net_profit += profit

        if event.outcome == "win":
            self.wins += 1
            if profit > self.biggest_win:
                self.biggest_win = profit
            self.current_streak = max(self.current_streak, 0) + 1
        elif event.outcome == "loss":
            self.losses += 1
            if profit < self.biggest_loss:
                self.biggest_loss = profit
            self.current_streak = min(self.current_streak, 0) - 1
        else:
            self.pushes += 1

        if self.current_streak > self.best_streak:
            self.best_streak = self.current_streak


class SessionTracker:
    def __init__(self, idle_timeout: int = SESSION_IDLE_TIMEOUT):
        self._idle_timeout = idle_timeout
        # key: (discord_id, guild_id)
        self._active: dict[tuple[int, int], PlayerSession] = {}

    def record(self, event: "GameResultEvent") -> PlayerSession:
        key = (event.discord_id, event.guild_id)
        session = self._active.get(key)
        if session is None:
            session = PlayerSession(
                discord_id=event.discord_id,
                guild_id=event.guild_id,
            )
            self._active[key] = session
        session.record(event)
        return session

    def get_active(self, discord_id: int, guild_id: int) -> Optional[PlayerSession]:
        return self._active.get((discord_id, guild_id))

    def collect_expired(self) -> list[PlayerSession]:
        now = time.time()
        expired = []
        to_remove = []
        for key, session in self._active.items():
            if now - session.last_activity > self._idle_timeout:
                expired.append(session)
                to_remove.append(key)
        for key in to_remove:
            del self._active[key]
        return expired

    def get_all_active(self, guild_id: int) -> list[PlayerSession]:
        return [s for (_, gid), s in self._active.items() if gid == guild_id]


# ── Highlight Detection ──────────────────────────────────────────────────────

class HighlightType(Enum):
    INSTANT = auto()    # Post immediately as individual card
    SESSION = auto()    # Batch into session recap


@dc
class Highlight:
    highlight_type: HighlightType
    reason: str
    event: object       # GameResultEvent, SportsbookEvent, or PredictionEvent


# ── Thresholds (aggressive) ──
INSTANT_THRESHOLDS = {
    "jackpot": True,                # any jackpot hit
    "pvp_flip": True,               # any PvP coinflip result
    "last_man_standing": True,      # crash LMS
    "parlay": True,                 # any parlay hit (sportsbook)
    "prediction_resolution": True,  # any market resolution
}
SESSION_THRESHOLDS = {
    "min_multiplier": 2.0,         # win 2x+ → session highlight
    "min_loss": 300,               # loss $300+ → session highlight
    "min_streak": 3,               # 3+ win streak → session highlight
    "crash_min_cashout": 3.0,      # crash cashout 3x+ → session highlight
}


class HighlightDetector:
    def check(self, event: "GameResultEvent",
              session: Optional["PlayerSession"]) -> Optional[Highlight]:
        # ── Instant: jackpot ──
        if event.extra.get("jackpot"):
            return Highlight(HighlightType.INSTANT, "Jackpot hit!", event)

        # ── Instant: PvP flip ──
        if event.game_type == "coinflip_pvp":
            return Highlight(HighlightType.INSTANT, "PvP flip result", event)

        # ── Instant: crash last man standing ──
        if event.extra.get("last_man_standing"):
            return Highlight(HighlightType.INSTANT, "Last Man Standing", event)

        # ── Session: crash cashout (MUST come before generic multiplier — both match crash 3.5x) ──
        if (event.game_type == "crash" and event.outcome == "win"
                and event.multiplier >= SESSION_THRESHOLDS["crash_min_cashout"]):
            return Highlight(HighlightType.SESSION, f"Crash {event.multiplier}x cashout", event)

        # ── Session: big multiplier win (generic, all games) ──
        if event.outcome == "win" and event.multiplier >= SESSION_THRESHOLDS["min_multiplier"]:
            return Highlight(HighlightType.SESSION, f"{event.multiplier}x win", event)

        # ── Session: big loss ──
        if event.outcome == "loss" and event.wager >= SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Lost ${event.wager}", event)

        # ── Session: streak milestone ──
        if session and session.current_streak >= SESSION_THRESHOLDS["min_streak"]:
            return Highlight(HighlightType.SESSION, f"{session.current_streak}-win streak", event)

        return None

    def check_sportsbook(self, event: "SportsbookEvent") -> Optional[Highlight]:
        # Instant: any parlay
        if event.bet_type == "parlay":
            return Highlight(HighlightType.INSTANT, "Parlay hit!", event)
        # Session: big sportsbook loss
        if event.amount <= -SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Lost ${abs(event.amount)} on {event.bet_type}", event)
        # Session: big sportsbook win
        if event.amount >= SESSION_THRESHOLDS["min_loss"]:
            return Highlight(HighlightType.SESSION, f"Won ${event.amount} on {event.bet_type}", event)
        return None

    def check_prediction(self, event: "PredictionEvent") -> Optional[Highlight]:
        return Highlight(HighlightType.INSTANT,
                         f'"{event.market_title}" resolved {event.resolution}', event)

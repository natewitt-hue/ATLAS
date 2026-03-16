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
from typing import Optional

try:
    import discord
    from discord.ext import commands, tasks
except ImportError:
    discord = None  # type: ignore
    commands = None  # type: ignore
    tasks = None  # type: ignore

try:
    from flow_events import GameResultEvent, flow_bus
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

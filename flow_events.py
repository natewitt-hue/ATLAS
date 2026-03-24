"""
flow_events.py — ATLAS FLOW Event Bus
──────────────────────────────────────
Lightweight async event bus for game results.
Game cogs emit events; flow_live_cog consumes them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

@dataclass
class GameResultEvent:
    discord_id: int
    guild_id: int
    game_type: str          # "blackjack", "slots", "crash", "coinflip", "coinflip_pvp"
    wager: int
    outcome: str            # "win", "loss", "push"
    payout: int
    multiplier: float
    new_balance: int
    txn_id: Optional[int] = None
    extra: dict = field(default_factory=dict)  # game-specific metadata

    @property
    def net_profit(self) -> int:
        return self.payout - self.wager

@dataclass
class SportsbookEvent:
    discord_id: int
    guild_id: int
    source: str             # "TSL_BET", "REAL_BET"
    bet_type: str           # "spread", "ml", "ou", "parlay"
    amount: int             # signed: positive=won, negative=lost
    balance_after: int
    description: str
    bet_id: Optional[int] = None

@dataclass
class PredictionEvent:
    guild_id: int
    market_title: str
    resolution: str         # "YES", "NO"
    total_payout: int
    winners: int            # number of winners paid out

class FlowEventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def clear(self) -> None:
        """Remove all handlers. Used in tests and cog_unload."""
        self._handlers.clear()

    async def emit(self, event_type: str, event) -> None:
        for handler in self._handlers.get(event_type, []):
            try:
                await handler(event)
            except Exception:
                log.exception("Error in flow event handler for %s", event_type)

# Wagering settlement event — emitted by ingestor cogs when a game/market finalises
# payload shape: {"event_id": str, "source": "TSL" | "REAL" | "POLY"}
EVENT_FINALIZED = "event_finalized"

# Singleton — imported by game cogs and flow_live_cog
flow_bus = FlowEventBus()

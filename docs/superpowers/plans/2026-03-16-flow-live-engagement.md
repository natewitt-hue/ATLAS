# FLOW Live Engagement System — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the noisy #ledger firehose with a curated #flow-live channel featuring a live-updating pulse dashboard, instant highlight broadcasts for exceptional moments, and session recap cards — all in V6 FLOW visual style via Playwright.

**Architecture:** New `flow_live_cog.py` orchestrates activity tracking, highlight detection, and the pulse dashboard. It hooks into existing game result flows via a thin event bus (`flow_events.py`). Each game cog emits events after completing a game; `flow_live_cog` consumes them to update the pulse, detect highlights, and batch session recaps. The existing `ledger_poster.py` is modified to emit text-only messages instead of PNG cards. The approved pulse dashboard mockup is in `.superpowers/brainstorm/940-1773685185/APPROVED-pulse-dashboard.html`.

**Tech Stack:** Python 3.14, discord.py 2.3+, Playwright (existing singleton via `card_renderer.py`), SQLite (`flow_economy.db`), existing V6 HTML renderer pattern.

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `flow_events.py` | Lightweight event bus for game results | **Create** |
| `flow_live_cog.py` | Pulse dashboard, highlight detection, session tracking, #flow-live orchestration | **Create** |
| `casino/renderer/pulse_renderer.py` | Playwright HTML→PNG for pulse dashboard card | **Create** |
| `casino/renderer/highlight_renderer.py` | Playwright HTML→PNG for highlight broadcast cards | **Create** |
| `casino/renderer/session_recap_renderer.py` | Playwright HTML→PNG for session recap cards | **Create** |
| `ledger_poster.py` | Switch from PNG cards to text-only audit lines | **Modify** |
| `casino/casino.py` | Emit events after `post_to_ledger()` | **Modify** |
| `flow_sportsbook.py` | Emit events after bet grading | **Modify** |
| `polymarket_cog.py` | Emit events after market resolution | **Modify** |
| `setup_cog.py` | Add `flow_live` to `REQUIRED_CHANNELS` | **Modify** |
| `bot.py` | Add `flow_live_cog` to load order, bump version | **Modify** |
| `boss_cog.py` | Add FLOW admin buttons to commissioner control room | **Modify** |
| `tests/test_flow_events.py` | Unit tests for event bus | **Create** |
| `tests/test_highlight_detection.py` | Unit tests for highlight threshold logic | **Create** |
| `tests/test_session_tracker.py` | Unit tests for session batching | **Create** |
| `tests/test_pulse_data.py` | Unit tests for pulse data aggregation | **Create** |

---

## Chunk 1: Event Bus & Session Tracker (Foundation)

### Task 1: Create the Event Bus

**Files:**
- Create: `flow_events.py`
- Create: `tests/test_flow_events.py`

- [ ] **Step 1: Write failing tests for event bus**

```python
# tests/test_flow_events.py
import asyncio
import pytest
from flow_events import FlowEventBus, GameResultEvent

@pytest.fixture
def bus():
    return FlowEventBus()

@pytest.mark.asyncio
async def test_subscribe_and_emit(bus):
    received = []
    async def handler(event):
        received.append(event)
    bus.subscribe("game_result", handler)
    event = GameResultEvent(
        discord_id=123, guild_id=456, game_type="blackjack",
        wager=100, outcome="win", payout=200, multiplier=2.0,
        new_balance=1200, txn_id=1
    )
    await bus.emit("game_result", event)
    assert len(received) == 1
    assert received[0].game_type == "blackjack"

@pytest.mark.asyncio
async def test_multiple_subscribers(bus):
    count = {"a": 0, "b": 0}
    async def handler_a(event): count["a"] += 1
    async def handler_b(event): count["b"] += 1
    bus.subscribe("game_result", handler_a)
    bus.subscribe("game_result", handler_b)
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="slots",
        wager=50, outcome="loss", payout=0, multiplier=0.0,
        new_balance=950, txn_id=2
    )
    await bus.emit("game_result", event)
    assert count["a"] == 1
    assert count["b"] == 1

@pytest.mark.asyncio
async def test_unrelated_event_not_received(bus):
    received = []
    async def handler(event): received.append(event)
    bus.subscribe("other_event", handler)
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="slots",
        wager=50, outcome="loss", payout=0, multiplier=0.0,
        new_balance=950, txn_id=3
    )
    await bus.emit("game_result", event)
    assert len(received) == 0

def test_game_result_event_net_profit():
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="blackjack",
        wager=100, outcome="win", payout=200, multiplier=2.0,
        new_balance=1200, txn_id=4
    )
    assert event.net_profit == 100  # payout - wager

def test_game_result_event_net_profit_loss():
    event = GameResultEvent(
        discord_id=1, guild_id=1, game_type="blackjack",
        wager=100, outcome="loss", payout=0, multiplier=0.0,
        new_balance=900, txn_id=5
    )
    assert event.net_profit == -100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_flow_events.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'flow_events'`

- [ ] **Step 3: Implement the event bus**

```python
# flow_events.py
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

# Singleton — imported by game cogs and flow_live_cog
flow_bus = FlowEventBus()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_flow_events.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add flow_events.py tests/test_flow_events.py
git commit -m "feat: add FLOW event bus for game result routing"
```

---

### Task 2: Create the Session Tracker

**Files:**
- Create: `flow_live_cog.py` (SessionTracker + PlayerSession classes)
- Create: `tests/test_session_tracker.py`

- [ ] **Step 1: Write failing tests for session tracker**

```python
# tests/test_session_tracker.py
import asyncio
import pytest
import time
from flow_events import GameResultEvent
from flow_live_cog import SessionTracker

@pytest.fixture
def tracker():
    return SessionTracker(idle_timeout=5)  # 5 seconds for testing

def _make_event(discord_id=123, guild_id=456, game_type="blackjack",
                outcome="win", wager=100, payout=200, multiplier=2.0):
    return GameResultEvent(
        discord_id=discord_id, guild_id=guild_id, game_type=game_type,
        wager=wager, outcome=outcome, payout=payout, multiplier=multiplier,
        new_balance=1000, txn_id=None
    )

def test_record_creates_session(tracker):
    event = _make_event()
    tracker.record(event)
    session = tracker.get_active(123, 456)
    assert session is not None
    assert session.total_games == 1
    assert session.net_profit == 100

def test_record_accumulates(tracker):
    tracker.record(_make_event(outcome="win", wager=100, payout=200, multiplier=2.0))
    tracker.record(_make_event(outcome="loss", wager=100, payout=0, multiplier=0.0))
    tracker.record(_make_event(outcome="win", wager=100, payout=300, multiplier=3.0))
    session = tracker.get_active(123, 456)
    assert session.total_games == 3
    assert session.wins == 2
    assert session.losses == 1
    assert session.net_profit == 300  # (200-100) + (0-100) + (300-100)

def test_streak_tracking(tracker):
    tracker.record(_make_event(outcome="win"))
    tracker.record(_make_event(outcome="win"))
    tracker.record(_make_event(outcome="win"))
    session = tracker.get_active(123, 456)
    assert session.current_streak == 3
    assert session.best_streak == 3

def test_streak_resets_on_loss(tracker):
    tracker.record(_make_event(outcome="win"))
    tracker.record(_make_event(outcome="win"))
    tracker.record(_make_event(outcome="loss", payout=0, multiplier=0.0))
    session = tracker.get_active(123, 456)
    assert session.current_streak == -1
    assert session.best_streak == 2

def test_biggest_win_tracked(tracker):
    tracker.record(_make_event(wager=100, payout=200, multiplier=2.0))
    tracker.record(_make_event(wager=100, payout=500, multiplier=5.0))
    tracker.record(_make_event(wager=100, payout=150, multiplier=1.5))
    session = tracker.get_active(123, 456)
    assert session.biggest_win == 400  # 500 - 100

def test_expired_sessions(tracker):
    tracker.record(_make_event())
    # Manually expire
    session = tracker.get_active(123, 456)
    session.last_activity = time.time() - 10  # 10s ago, timeout is 5s
    expired = tracker.collect_expired()
    assert len(expired) == 1
    assert expired[0].discord_id == 123
    assert tracker.get_active(123, 456) is None

def test_separate_guilds(tracker):
    tracker.record(_make_event(guild_id=1))
    tracker.record(_make_event(guild_id=2))
    assert tracker.get_active(123, 1) is not None
    assert tracker.get_active(123, 2) is not None
    assert tracker.get_active(123, 1) is not tracker.get_active(123, 2)

def test_game_type_breakdown(tracker):
    tracker.record(_make_event(game_type="blackjack"))
    tracker.record(_make_event(game_type="blackjack"))
    tracker.record(_make_event(game_type="slots"))
    session = tracker.get_active(123, 456)
    assert session.games_by_type["blackjack"] == 2
    assert session.games_by_type["slots"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_session_tracker.py -v`
Expected: FAIL — `ImportError: cannot import name 'SessionTracker' from 'flow_live_cog'`

- [ ] **Step 3: Create flow_live_cog.py with SessionTracker**

```python
# flow_live_cog.py (initial — SessionTracker only, cog shell)
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

import discord
from discord.ext import commands, tasks

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
    events: list[GameResultEvent] = field(default_factory=list)

    def record(self, event: GameResultEvent) -> None:
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

    def record(self, event: GameResultEvent) -> PlayerSession:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session_tracker.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add flow_live_cog.py tests/test_session_tracker.py
git commit -m "feat: add session tracker for FLOW live engagement"
```

---

### Task 3: Create the Highlight Detector

**Files:**
- Create: `tests/test_highlight_detection.py`
- Modify: `flow_live_cog.py` (add HighlightDetector class)

- [ ] **Step 1: Write failing tests for highlight detection**

```python
# tests/test_highlight_detection.py
import pytest
from flow_events import GameResultEvent, SportsbookEvent, PredictionEvent
from flow_live_cog import HighlightDetector, HighlightType, PlayerSession

def _game_event(game_type="blackjack", outcome="win", wager=100,
                payout=200, multiplier=2.0, extra=None):
    return GameResultEvent(
        discord_id=123, guild_id=456, game_type=game_type,
        wager=wager, outcome=outcome, payout=payout,
        multiplier=multiplier, new_balance=1000, txn_id=1,
        extra=extra or {}
    )

@pytest.fixture
def detector():
    return HighlightDetector()

# ── Instant highlights (post immediately) ──

def test_jackpot_is_instant(detector):
    event = _game_event(game_type="slots", multiplier=50.0, payout=5000, wager=100)
    event.extra = {"jackpot": True}
    result = detector.check(event, session=None)
    assert result is not None
    assert result.highlight_type == HighlightType.INSTANT
    assert "jackpot" in result.reason.lower()

def test_pvp_flip_is_instant(detector):
    event = _game_event(game_type="coinflip_pvp", payout=500, wager=250)
    result = detector.check(event, session=None)
    assert result is not None
    assert result.highlight_type == HighlightType.INSTANT

def test_crash_last_man_standing_is_instant(detector):
    event = _game_event(game_type="crash", multiplier=8.0, payout=800, wager=100)
    event.extra = {"last_man_standing": True}
    result = detector.check(event, session=None)
    assert result is not None
    assert result.highlight_type == HighlightType.INSTANT

# ── Session-batched highlights (included in recap) ──

def test_2x_win_is_session(detector):
    event = _game_event(multiplier=2.5, payout=250, wager=100)
    result = detector.check(event, session=None)
    assert result is not None
    assert result.highlight_type == HighlightType.SESSION

def test_big_loss_is_session(detector):
    event = _game_event(outcome="loss", wager=500, payout=0, multiplier=0.0)
    result = detector.check(event, session=None)
    assert result is not None
    assert result.highlight_type == HighlightType.SESSION

def test_streak_3_is_session(detector):
    session = PlayerSession(discord_id=123, guild_id=456)
    session.current_streak = 3
    session.best_streak = 3
    event = _game_event()
    result = detector.check(event, session=session)
    assert result is not None
    assert result.highlight_type == HighlightType.SESSION

# ── Below threshold ──

def test_small_win_no_highlight(detector):
    event = _game_event(multiplier=1.5, payout=150, wager=100)
    result = detector.check(event, session=None)
    assert result is None

def test_small_loss_no_highlight(detector):
    event = _game_event(outcome="loss", wager=100, payout=0, multiplier=0.0)
    result = detector.check(event, session=None)
    assert result is None

def test_push_no_highlight(detector):
    event = _game_event(outcome="push", payout=100, multiplier=1.0)
    result = detector.check(event, session=None)
    assert result is None

# ── Sportsbook events ──

def test_sportsbook_parlay_is_instant(detector):
    event = SportsbookEvent(
        discord_id=123, guild_id=456, source="TSL_BET",
        bet_type="parlay", amount=1000, balance_after=2000,
        description="3-leg parlay +420", bet_id=1
    )
    result = detector.check_sportsbook(event)
    assert result is not None
    assert result.highlight_type == HighlightType.INSTANT

def test_sportsbook_big_loss_is_session(detector):
    event = SportsbookEvent(
        discord_id=123, guild_id=456, source="TSL_BET",
        bet_type="spread", amount=-500, balance_after=500,
        description="Week 14 spread Cowboys", bet_id=2
    )
    result = detector.check_sportsbook(event)
    assert result is not None

# ── Prediction events ──

def test_prediction_resolution_is_instant(detector):
    event = PredictionEvent(
        guild_id=456, market_title="MVP goes to RB",
        resolution="YES", total_payout=4100, winners=6
    )
    result = detector.check_prediction(event)
    assert result is not None
    assert result.highlight_type == HighlightType.INSTANT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_highlight_detection.py -v`
Expected: FAIL — `ImportError: cannot import name 'HighlightDetector'`

- [ ] **Step 3: Implement HighlightDetector in flow_live_cog.py**

Add the following to `flow_live_cog.py` after the `SessionTracker` class:

```python
from enum import Enum, auto
from dataclasses import dataclass as dc

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
    def check(self, event: GameResultEvent,
              session: Optional[PlayerSession]) -> Optional[Highlight]:
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

    def check_sportsbook(self, event: SportsbookEvent) -> Optional[Highlight]:
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

    def check_prediction(self, event: PredictionEvent) -> Optional[Highlight]:
        return Highlight(HighlightType.INSTANT,
                         f'"{event.market_title}" resolved {event.resolution}', event)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_highlight_detection.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add flow_live_cog.py tests/test_highlight_detection.py
git commit -m "feat: add highlight detector with aggressive thresholds"
```

---

## Chunk 2: Renderers (Pulse, Highlights, Session Recaps)

### Task 4: Create the Pulse Dashboard Renderer

**Files:**
- Create: `casino/renderer/pulse_renderer.py`
- Reference: `.superpowers/brainstorm/940-1773685185/APPROVED-pulse-dashboard.html`

- [ ] **Step 1: Write failing test for pulse renderer**

```python
# tests/test_pulse_data.py
import pytest
from casino.renderer.pulse_renderer import build_pulse_data, PulseData

def test_empty_pulse_data():
    data = build_pulse_data(
        active_bj=0, bj_players=[], bj_streak_player=None, bj_streak_count=0,
        slots_spins_today=0, slots_top_player=None, slots_top_amount=0, slots_top_mult=0,
        sb_week=14, sb_bets=0, sb_volume=0, sb_hot_player=None, sb_hot_desc="",
        pred_open=0, pred_hot_title="", pred_yes_pct=0, pred_no_pct=0, pred_volume=0,
        jackpot_amount=0, jackpot_last_player=None, jackpot_last_amount=0, jackpot_last_ago="never",
        highlights=[],
    )
    assert isinstance(data, PulseData)
    assert data.jackpot_amount == 0

def test_pulse_data_populated():
    data = build_pulse_data(
        active_bj=3, bj_players=["MikeD", "JRock", "Phantom"],
        bj_streak_player="MikeD", bj_streak_count=7,
        slots_spins_today=48, slots_top_player="JRock",
        slots_top_amount=2450, slots_top_mult=25,
        sb_week=14, sb_bets=42, sb_volume=18400,
        sb_hot_player="Phantom", sb_hot_desc="riding a 4-leg parlay (+820)",
        pred_open=3, pred_hot_title="MVP goes to an RB this season",
        pred_yes_pct=72, pred_no_pct=28, pred_volume=4100,
        jackpot_amount=8247, jackpot_last_player="Phantom",
        jackpot_last_amount=4100, jackpot_last_ago="3 days ago",
        highlights=[],
    )
    assert data.active_bj == 3
    assert data.jackpot_amount == 8247
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pulse_data.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement pulse_renderer.py**

Create `casino/renderer/pulse_renderer.py`. This file:
1. Defines `PulseData` dataclass with all fields from the approved mockup
2. Defines `build_pulse_data()` factory function
3. Defines `render_pulse_card(data: PulseData) -> bytes` which builds the HTML from the approved mockup template and renders via Playwright

The HTML template should be extracted directly from `.superpowers/brainstorm/940-1773685185/APPROVED-pulse-dashboard.html`, converting hardcoded values to f-string interpolations from `PulseData` fields.

Key implementation notes:
- Use `_font_face_css()` from `casino_html_renderer.py` for Outfit + JetBrains Mono
- Use `_get_browser()` from `card_renderer.py` for Playwright singleton
- Card width: 700px (matches all V6 cards)
- Progressive jackpot: Outfit font at 48px/800 weight with gold gradient
- Game card left borders: BJ=#4ADE80, Slots=#60A5FA, SB=#D4AF37, Predictions=#F472B6
- Game labels: 15px/700 white (#e8e0d0)
- Icon pills: 24x24px with colored backgrounds
- Highlight feed: CSS grid `26px 1fr 84px 48px`, loss rows with red tint background
- ATLAS-voiced loss descriptions via `get_persona()` from `echo_loader.py`
- All secondary text: #b0a890 / #c0b8a8 (warm tinted, mobile-safe)
- Footer: #a8a090 at 11px

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pulse_data.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add casino/renderer/pulse_renderer.py tests/test_pulse_data.py
git commit -m "feat: add pulse dashboard Playwright renderer (V6 style)"
```

---

### Task 5: Create the Highlight Broadcast Renderer

**Files:**
- Create: `casino/renderer/highlight_renderer.py`

- [ ] **Step 1: Create highlight_renderer.py**

This renderer produces individual V6-styled PNG cards for instant highlights:
- Jackpot hits (gold themed, big number)
- PvP flip results (matchup card with winner/loser)
- Crash Last Man Standing (multiplier showcase)
- Prediction market resolutions (market title + YES/NO result)
- Sportsbook parlay hits (legs + total payout)

Each card follows the same V6 pattern:
- 700px wide, `#111111` bg, noise texture, gold border
- 5px status bar: green for wins, red for losses, gold for jackpots
- Header with game icon pill + game name
- Body with event-specific content
- ATLAS-voiced one-liner (via `get_persona()`) for flavor text
- Footer with player name + timestamp

Implementation: Create a function per highlight type:
```python
async def render_jackpot_card(player: str, amount: int, multiplier: float) -> bytes
async def render_pvp_card(winner: str, loser: str, amount: int) -> bytes
async def render_crash_lms_card(player: str, multiplier: float, payout: int) -> bytes
async def render_prediction_card(title: str, resolution: str, winners: int, payout: int) -> bytes
async def render_parlay_card(player: str, legs: int, odds: str, payout: int) -> bytes
```

- [ ] **Step 2: Commit**

```bash
git add casino/renderer/highlight_renderer.py
git commit -m "feat: add highlight broadcast card renderer"
```

---

### Task 6: Create the Session Recap Renderer

**Files:**
- Create: `casino/renderer/session_recap_renderer.py`

- [ ] **Step 1: Create session_recap_renderer.py**

Produces a V6-styled PNG card summarizing a player's session:
- Header: player name + session duration
- Stats row: games played | wins | losses | net P&L
- Game breakdown: per-game-type stats with icon pills
- Highlight moments: top 3 notable events from the session
- Streak badge (if applicable)
- ATLAS commentary line (playful roast if net negative, hype if net positive)

```python
async def render_session_recap(session: PlayerSession, display_name: str) -> bytes
```

Uses same V6 CSS base, 700px card, Playwright singleton.

- [ ] **Step 2: Commit**

```bash
git add casino/renderer/session_recap_renderer.py
git commit -m "feat: add session recap card renderer"
```

---

## Chunk 3: Integration (Wire Up Events, Cog, Channel Routing)

### Task 7: Add `flow_live` to Channel Routing

**Files:**
- Modify: `setup_cog.py:39-63` (add to REQUIRED_CHANNELS)

- [ ] **Step 1: Add flow_live to REQUIRED_CHANNELS**

In `setup_cog.py`, add to the `REQUIRED_CHANNELS` list (line 63, after the last entry):
```python
    # ── ATLAS — Flow (live engagement feed) ──
    ("flow_live",          "flow-live",           "ATLAS — Flow",   True,  False),
```

Note: `REQUIRED_CHANNELS` is a `list[tuple[str, str, str, bool, bool]]` — format is `(config_key, display_name, category_name, read_only_for_members, admin_only)`. The channel is read-only so members can't post in it.

- [ ] **Step 2: Commit**

```bash
git add setup_cog.py
git commit -m "feat: add flow_live to REQUIRED_CHANNELS"
```

---

### Task 8: Emit Events from Casino Games

**Files:**
- Modify: `casino/casino.py` (add event emission in `post_to_ledger()`)

- [ ] **Step 1: Add `extra` parameter and event emission to `post_to_ledger`**

The current `post_to_ledger()` signature in `casino/casino.py:44-55` has NO `extra` parameter. Add it as an optional kwarg and emit the event after the ledger post:

```python
# casino/casino.py — modify the existing signature at line 44:
async def post_to_ledger(
    bot: commands.Bot,
    guild_id: int,
    discord_id: int,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: int | None = None,
    extra: dict | None = None,        # ← NEW: game-specific metadata
) -> None:
    """Post a casino game result slip to #ledger via ledger_poster."""
    try:
        from ledger_poster import post_casino_result
        await post_casino_result(
            bot, guild_id, discord_id, game_type,
            wager, outcome, payout, multiplier, new_balance, txn_id
        )
    except Exception:
        log.exception("Failed to post to ledger")

    # Emit FLOW event for live engagement system
    try:
        from flow_events import GameResultEvent, flow_bus
        event = GameResultEvent(
            discord_id=discord_id, guild_id=guild_id, game_type=game_type,
            wager=wager, outcome=outcome, payout=payout, multiplier=multiplier,
            new_balance=new_balance, txn_id=txn_id, extra=extra or {}
        )
        await flow_bus.emit("game_result", event)
    except Exception:
        log.exception("Failed to emit FLOW event")
```

Because `extra` is a new kwarg with a default of `None`, all existing callers continue to work unchanged — no signature breakage.

- [ ] **Step 2: Update crash.py to pass LMS metadata**

In `casino/games/crash.py`, when calling `post_to_ledger()` for the last man standing, add:
```python
extra={"last_man_standing": True}
```

- [ ] **Step 3: Update slots.py to pass jackpot metadata**

In `casino/games/slots.py`, when a jackpot-tier hit occurs, add:
```python
extra={"jackpot": True}
```

- [ ] **Step 4: Commit**

```bash
git add casino/casino.py casino/games/crash.py casino/games/slots.py
git commit -m "feat: emit FLOW events from casino games"
```

---

### Task 9: Emit Events from Sportsbook

**Files:**
- Modify: `flow_sportsbook.py` (add event emission in grading flow)

- [ ] **Step 1: Add event emission after bet grading**

**IMPORTANT:** `_grade_sync()` runs inside `loop.run_in_executor()` (sync context — cannot `await`). The pattern is:
1. `_grade_sync()` collects event data into a plain list during grading
2. `_grade_bets_impl()` (async, runs after executor returns) iterates the list and emits events

In `flow_sportsbook.py`:

```python
from flow_events import SportsbookEvent, flow_bus

# Inside _grade_sync() — collect event dicts (NOT dataclasses, to stay sync-safe):
# Add a list to accumulate events:
pending_events = []

# After each bet is graded inside _grade_sync(), append:
pending_events.append({
    "discord_id": discord_id, "guild_id": guild_id,
    "source": "TSL_BET", "bet_type": bet_type,
    "amount": profit, "balance_after": new_balance,
    "description": description, "bet_id": bet_id
})

# Return pending_events as part of the _grade_sync() return value
# (modify return to be a tuple: (results, pending_events))

# Inside _grade_bets_impl(), AFTER the executor returns:
results, pending_events = await loop.run_in_executor(None, _grade_sync)

# Now in async context, emit all collected events:
for ev_data in pending_events:
    sb_event = SportsbookEvent(**ev_data)
    await flow_bus.emit("sportsbook_result", sb_event)
```

This avoids the async/sync boundary problem: sync code collects dicts, async code emits events.

**IMPORTANT:** There are TWO grading paths in the sportsbook:
- `_run_autograde()` (line ~921) — async function that uses `run_in_executor` with `_grade_sync()`. Use the collect-then-emit pattern above.
- `_grade_bets_impl()` (line ~2036) — async method that runs sync DB calls directly (no executor). Since it's already async, emit events directly with `await flow_bus.emit(...)` inline after each bet is processed.

Both paths must emit events or manual grading via `/boss` will silently skip the live feed.

- [ ] **Step 2: Commit**

```bash
git add flow_sportsbook.py
git commit -m "feat: emit FLOW events from sportsbook grading"
```

---

### Task 10: Emit Events from Prediction Markets

**Files:**
- Modify: `polymarket_cog.py` (add event emission on market resolution)

- [ ] **Step 1: Add event emission after market resolution**

In `polymarket_cog.py`, after market resolution card is posted, emit a `PredictionEvent`:

```python
from flow_events import PredictionEvent, flow_bus

pred_event = PredictionEvent(
    guild_id=guild_id, market_title=market_title,
    resolution=resolution, total_payout=total_payout,
    winners=winners_count
)
await flow_bus.emit("prediction_result", pred_event)
```

- [ ] **Step 2: Commit**

```bash
git add polymarket_cog.py
git commit -m "feat: emit FLOW events from prediction market resolution"
```

---

### Task 11: Slim Down #ledger

**Files:**
- Modify: `ledger_poster.py`

- [ ] **Step 1: Replace PNG cards with text-only audit lines**

In `ledger_poster.py`, modify `post_casino_result()` and `post_transaction()`:

Replace the Playwright render + embed + file attachment pattern with a simple text message:

```python
# Instead of rendering PNG and posting embed+file:
line = f"`{timestamp}` | **{display_name}** | {game_label} | {outcome_emoji} {outcome} | Wager: ${wager:,} | Payout: ${payout:,} | Balance: ${new_balance:,}"
await channel.send(line)
```

This preserves the audit trail in #ledger but eliminates the PNG rendering overhead and visual noise.

- [ ] **Step 2: Commit**

```bash
git add ledger_poster.py
git commit -m "refactor: slim #ledger to text-only audit trail"
```

---

### Task 12: Wire Up the Flow Live Cog

**Files:**
- Modify: `flow_live_cog.py` (add the Cog class with event handlers and background tasks)

- [ ] **Step 1: Add the Cog class to flow_live_cog.py**

Add `FlowLiveCog(commands.Cog)` with:

1. **`__init__`**: Initialize `SessionTracker`, `HighlightDetector`, subscribe to `flow_bus` events
2. **`on_game_result` handler**: Record in session tracker, check for highlights, post instant highlights, queue session highlights
3. **`on_sportsbook_result` handler**: Check for highlights, post instants
4. **`on_prediction_result` handler**: Post instant highlight card
5. **`pulse_update_loop` (60s task)**: Aggregate pulse data from session tracker + DB queries, render pulse card, edit-in-place the pinned message in #flow-live
6. **`session_reaper_loop` (30s task)**: Collect expired sessions, render session recap cards, post to #flow-live
7. **`_get_flow_live_channel(guild_id)`**: Resolve #flow-live channel via `setup_cog.get_channel_id("flow_live", guild_id)`
8. **`_ensure_pulse_message(channel)`**: Find or create the pinned pulse message to edit

Key implementation:
```python
class FlowLiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions = SessionTracker()
        self.detector = HighlightDetector()
        self._pulse_message_ids: dict[int, int] = {}  # guild_id → message_id

        flow_bus.subscribe("game_result", self._on_game_result)
        flow_bus.subscribe("sportsbook_result", self._on_sportsbook_result)
        flow_bus.subscribe("prediction_result", self._on_prediction_result)

    async def cog_load(self):
        # Restore persisted pulse message IDs from DB
        self._load_pulse_message_ids()
        self.pulse_loop.start()
        self.session_reaper.start()

    async def cog_unload(self):
        self.pulse_loop.cancel()
        self.session_reaper.cancel()
        flow_bus.unsubscribe("game_result", self._on_game_result)
        flow_bus.unsubscribe("sportsbook_result", self._on_sportsbook_result)
        flow_bus.unsubscribe("prediction_result", self._on_prediction_result)

    def _ensure_state_table(self):
        """Create flow_live_state table if it doesn't exist.
        Separate from server_config (which has config_key/channel_id/guild_id schema
        specifically for channel routing)."""
        try:
            import sqlite3
            conn = sqlite3.connect("flow_economy.db")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS flow_live_state (
                    guild_id    INTEGER PRIMARY KEY,
                    pulse_msg_id INTEGER NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception:
            log.exception("Failed to create flow_live_state table")

    def _load_pulse_message_ids(self):
        """Load pulse message IDs from flow_live_state in flow_economy.db."""
        try:
            import sqlite3
            self._ensure_state_table()
            conn = sqlite3.connect("flow_economy.db")
            rows = conn.execute(
                "SELECT guild_id, pulse_msg_id FROM flow_live_state"
            ).fetchall()
            conn.close()
            for guild_id, msg_id in rows:
                self._pulse_message_ids[guild_id] = msg_id
        except Exception:
            log.exception("Failed to load pulse message IDs")

    def _save_pulse_message_id(self, guild_id: int, message_id: int):
        """Persist pulse message ID to DB so it survives restarts."""
        try:
            import sqlite3
            conn = sqlite3.connect("flow_economy.db")
            conn.execute(
                "INSERT OR REPLACE INTO flow_live_state (guild_id, pulse_msg_id) VALUES (?, ?)",
                (guild_id, message_id)
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("Failed to save pulse message ID")

    @tasks.loop(seconds=60)
    async def pulse_loop(self):
        for guild in self.bot.guilds:
            await self._update_pulse(guild)

    @pulse_loop.before_loop
    async def before_pulse_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def session_reaper(self):
        expired = self.sessions.collect_expired()
        for session in expired:
            await self._post_session_recap(session)

    @session_reaper.before_loop
    async def before_session_reaper(self):
        await self.bot.wait_until_ready()

    # ── Core methods ──

    async def _update_pulse(self, guild: discord.Guild):
        """Aggregate live data and edit-in-place the pulse dashboard message."""
        channel = await self._get_flow_live_channel(guild.id)
        if not channel:
            return
        # 1. Query pulse data: active sessions, jackpot amount, sportsbook stats,
        #    prediction market stats, recent highlights (last 6)
        # 2. Call build_pulse_data() with aggregated values
        # 3. Call render_pulse_card(data) → bytes (Playwright HTML→PNG)
        # 4. Get or create the pinned pulse message:
        msg_id = self._pulse_message_ids.get(guild.id)
        # 5. If msg_id exists, try to edit the message with new image
        #    If edit fails (message deleted), create new and persist ID
        # 6. If no msg_id, send new message, pin it, persist ID via _save_pulse_message_id()

    async def _post_session_recap(self, session: PlayerSession):
        """Render and post a session recap card when a player goes idle."""
        channel = await self._get_flow_live_channel(session.guild_id)
        if not channel or session.total_games < 2:
            return  # Don't recap single-game sessions
        # 1. Resolve display name from discord_id
        # 2. Call render_session_recap(session, display_name) → bytes
        # 3. Post as a new message in #flow-live

    async def _post_instant_highlight(self, highlight, guild_id: int):
        """Render and post an instant highlight card."""
        channel = await self._get_flow_live_channel(guild_id)
        if not channel:
            return
        # 1. Determine highlight type and call appropriate renderer
        #    (render_jackpot_card, render_pvp_card, render_crash_lms_card, etc.)
        # 2. Post as a new message in #flow-live

    async def _get_flow_live_channel(self, guild_id: int):
        """Resolve #flow-live channel via setup_cog."""
        try:
            from setup_cog import get_channel_id
            ch_id = get_channel_id("flow_live", guild_id)
            if ch_id:
                return self.bot.get_channel(ch_id)
        except Exception:
            pass
        return None

    # ── _impl methods for boss_cog delegation ──

    async def _update_pulse_impl(self, guild: discord.Guild):
        """Force refresh pulse dashboard. Called from boss_cog."""
        await self._update_pulse(guild)

    async def _test_highlight_impl(self, guild: discord.Guild, channel):
        """Post a test highlight card. Called from boss_cog."""
        # Create a fake GameResultEvent and render a highlight card

    async def _session_dump_impl(self, guild: discord.Guild) -> str:
        """Return active sessions as formatted text. Called from boss_cog."""
        sessions = self.sessions.get_all_active(guild.id)
        if not sessions:
            return "No active sessions."
        lines = []
        for s in sessions:
            lines.append(f"<@{s.discord_id}> — {s.total_games} games, net ${s.net_profit:+,}")
        return "\n".join(lines)
```

- [ ] **Step 2: Commit**

```bash
git add flow_live_cog.py
git commit -m "feat: wire up FlowLiveCog with event handlers and background tasks"
```

---

### Task 13: Register the Cog and Bump Version

**Files:**
- Modify: `bot.py` (add to load order, bump ATLAS_VERSION)
- Modify: `boss_cog.py` (add FLOW admin buttons to commissioner control room)

- [ ] **Step 1: Add flow_live_cog to bot.py load order**

Add after `economy_cog` (position 12), before `boss_cog`:
```python
"flow_live_cog",  # FLOW live engagement system
```

Bump `ATLAS_VERSION` to `2.9.0`.

- [ ] **Step 2: Add FLOW admin commands to boss_cog.py**

In `boss_cog.py`, add a FLOW section to the commissioner control room with buttons that delegate to `_impl` methods on `FlowLiveCog`:
- **Force Pulse Refresh** — calls `flow_live_cog._update_pulse_impl(guild)`
- **Test Highlight** — calls `flow_live_cog._test_highlight_impl(guild, channel)`
- **Session Dump** — calls `flow_live_cog._session_dump_impl(guild)`

Follow the existing `boss_cog` delegation pattern: buttons in the View call `_impl` methods on the target cog.

- [ ] **Step 3: Commit**

```bash
git add bot.py boss_cog.py
git commit -m "feat: register FlowLiveCog, add FLOW admin controls to boss_cog (v2.9.0)"
```

---

## Chunk 4: End-to-End Verification

### Task 14: Integration Testing

- [ ] **Step 1: Run all unit tests**

Run: `python -m pytest tests/test_flow_events.py tests/test_session_tracker.py tests/test_highlight_detection.py tests/test_pulse_data.py -v`
Expected: All tests pass

- [ ] **Step 2: Manual smoke test**

1. Start the bot locally
2. Verify `flow_live_cog` loads without errors in bot logs
3. Open the `/boss` commissioner control room → click **Force Pulse Refresh** — verify pulse card renders and posts to #flow-live
4. Play a blackjack hand — verify:
   - Game result is private (ephemeral to player)
   - #ledger gets a text-only audit line (no PNG)
   - Session tracker records the game
5. Play 3+ winning hands — verify:
   - Streak is tracked
   - Session recap posts to #flow-live when session expires (5 min idle)
6. Hit a jackpot or PvP flip — verify instant highlight card posts to #flow-live
7. Grade sportsbook bets — verify parlay highlights post instantly

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration test fixes for FLOW live system"
```

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
    assert session.net_profit == 200  # (200-100) + (0-100) + (300-100)

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

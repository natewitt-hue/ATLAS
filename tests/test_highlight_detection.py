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

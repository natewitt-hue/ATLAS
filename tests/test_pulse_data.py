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

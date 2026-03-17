"""
test_all_renders.py — ATLAS render verification script.
Tests every card-rendering function with minimal valid sample data,
saves PNGs to test_renders/, and reports latency + size.

Usage:
    python test_all_renders.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Add project root to path so local modules resolve
sys.path.insert(0, os.path.dirname(__file__))


async def main() -> None:
    # ── Init pool ────────────────────────────────────────────────────────────
    from atlas_html_engine import init_pool, drain_pool
    await init_pool()

    os.makedirs("test_renders", exist_ok=True)
    results: list[tuple[str, str, int, float]] = []

    async def test_card(name: str, coro) -> None:
        t0 = time.perf_counter()
        try:
            png = await coro
            elapsed = (time.perf_counter() - t0) * 1000
            path = f"test_renders/{name}.png"
            Path(path).write_bytes(png)
            results.append((name, "OK", len(png), elapsed))
            print(f"  OK  {name}: {len(png):,} bytes, {elapsed:.0f}ms")
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            results.append((name, "FAIL", 0, elapsed))
            print(f"  FAIL {name}: {e}")

    # ── Group B — Casino games ───────────────────────────────────────────────
    print("\n=== Group B: Casino games ===")

    from casino.renderer.casino_html_renderer import (
        render_blackjack_card,
        render_slots_card,
        render_crash_card,
        render_coinflip_card,
        render_scratch_card_v6,
    )

    await test_card("blackjack", render_blackjack_card(
        dealer_hand=[("10", "♠"), ("K", "♥")],
        player_hand=[("A", "♠"), ("9", "♦")],
        dealer_score=20,
        player_score=20,
        hide_dealer=False,
        status="Push!",
        wager=100,
        payout=100,
        balance=1000,
        player_name="TestUser",
    ))

    await test_card("slots", render_slots_card(
        reels=["seven", "seven", "seven"],
        revealed=3,
        wager=50,
        payout=500,
        balance=1500,
        result_msg="JACKPOT!",
        player_name="TestUser",
    ))

    await test_card("crash", render_crash_card(
        current_mult=3.5,
        crashed=True,
        cashed_out=False,
        cashout_mult=None,
        history=[1.2, 2.5, 3.5],
        players_in=4,
        total_wagered=400,
        wager=100,
        payout=0,
        balance=900,
        player_name="TestUser",
        players=["TestUser", "Player2"],
    ))

    await test_card("coinflip", render_coinflip_card(
        result="heads",
        player_pick="heads",
        wager=200,
        payout=400,
        balance=1200,
        player_name="TestUser",
    ))

    await test_card("scratch", render_scratch_card_v6(
        tiles=[7, 7, 7],
        revealed=3,
        is_match=True,
        player_name="TestUser",
        total=300,
        balance=1300,
    ))

    # ── Group C — Highlights ─────────────────────────────────────────────────
    print("\n=== Group C: Highlights ===")

    from casino.renderer.highlight_renderer import (
        render_jackpot_card,
        render_pvp_card,
        render_crash_lms_card,
        render_prediction_card,
        render_parlay_card,
    )

    await test_card("jackpot", render_jackpot_card(
        player="TestUser",
        amount=50000,
        multiplier=50.0,
        commentary="What a hit. Unreal.",
    ))

    await test_card("pvp", render_pvp_card(
        winner="TestUser",
        loser="Opponent",
        amount=500,
        commentary="Heads wins again.",
    ))

    await test_card("crash_lms", render_crash_lms_card(
        player="TestUser",
        multiplier=12.5,
        payout=1250,
        commentary="Last one standing.",
    ))

    await test_card("prediction_highlight", render_prediction_card(
        title="Will the Eagles win Super Bowl 96?",
        resolution="YES",
        winners=3,
        payout=1500,
        commentary="Called it.",
    ))

    await test_card("parlay", render_parlay_card(
        player="TestUser",
        legs=4,
        odds="+650",
        payout=7500,
        commentary="Four for four. Clean sweep.",
    ))

    # ── Group D — Flow Live ───────────────────────────────────────────────────
    print("\n=== Group D: Flow Live ===")

    from casino.renderer.session_recap_renderer import render_session_recap
    from casino.renderer.pulse_renderer import render_pulse_card, build_pulse_data, PulseData, HighlightRow
    from flow_live_cog import PlayerSession

    import time as _time
    now = _time.time()
    session = PlayerSession(discord_id=123456789, guild_id=987654321)
    session.started_at = now - 1800   # 30 minutes ago
    session.last_activity = now
    session.total_games = 10
    session.wins = 6
    session.losses = 3
    session.pushes = 1
    session.net_profit = 750
    session.biggest_win = 400
    session.biggest_loss = -200
    session.current_streak = 2
    session.best_streak = 4
    session.games_by_type = {"blackjack": 5, "slots": 3, "coinflip": 2}

    await test_card("session_recap", render_session_recap(
        session=session,
        display_name="TestUser",
        commentary="Solid session. Don't push your luck.",
    ))

    pulse_data = build_pulse_data(
        active_bj=2,
        bj_players=["TestUser", "Player2"],
        bj_streak_player="TestUser",
        bj_streak_count=4,
        slots_spins_today=37,
        slots_top_player="Player3",
        slots_top_amount=1200,
        slots_top_mult=12,
        sb_week=8,
        sb_bets=15,
        sb_volume=7500,
        sb_hot_player="Player4",
        sb_hot_desc="Eagles ML +120",
        pred_open=5,
        pred_hot_title="Will the Chiefs go 18-0?",
        pred_yes_pct=62,
        pred_no_pct=38,
        pred_volume=3200,
        jackpot_amount=48000,
        jackpot_last_player="TestUser",
        jackpot_last_amount=50000,
        jackpot_last_ago="2h",
        highlights=[
            HighlightRow(
                icon="&#128293;",
                description_html="<span style='color:#FBBF24'>TestUser</span> crashed at 12.5x",
                amount_html="+$1,250",
                time_ago="5m",
                is_loss=False,
            ),
            HighlightRow(
                icon="&#127920;",
                description_html="<span style='color:#F87171'>Player2</span> bust on slots",
                amount_html="-$200",
                time_ago="12m",
                is_loss=True,
            ),
        ],
    )

    await test_card("pulse", render_pulse_card(pulse_data))

    # ── Group E — Predictions ─────────────────────────────────────────────────
    print("\n=== Group E: Predictions ===")

    from casino.renderer.prediction_html_renderer import (
        render_market_list_card,
        render_market_detail_card,
        render_bet_confirmation_card,
        render_portfolio_card,
        render_resolution_card,
    )

    sample_markets = [
        {"title": "Will the Eagles win the Super Bowl?", "category": "🏈 Sports", "yes_price": 0.62, "no_price": 0.38},
        {"title": "Will ATLAS reach v3.0 by Week 10?", "category": "⚙️ Tech", "yes_price": 0.45, "no_price": 0.55},
        {"title": "Most passing yards Week 8?", "category": "🏈 Sports", "yes_price": 0.30, "no_price": 0.70},
    ]

    await test_card("market_list", render_market_list_card(
        markets=sample_markets,
        page=1,
        total_pages=3,
        filter_label="All Categories",
    ))

    await test_card("market_detail", render_market_detail_card(
        title="Will the Eagles win the Super Bowl?",
        category="🏈 Sports",
        yes_price=0.62,
        no_price=0.38,
        volume=4500.0,
        liquidity=1200.0,
        end_date="2026-02-08",
        user_position="YES",
        user_contracts=5,
        user_cost=310,
    ))

    await test_card("bet_confirmation", render_bet_confirmation_card(
        market_title="Will the Eagles win the Super Bowl?",
        side="YES",
        price=0.62,
        quantity=5,
        cost=310,
        potential_payout=500,
        balance=2500,
        player_name="TestUser",
        txn_id="TXN-0042",
    ))

    await test_card("portfolio", render_portfolio_card(
        positions=[
            {"title": "Eagles win Super Bowl", "side": "YES", "qty": 5, "cost": 310, "payout": 500, "buy_price": 0.62},
            {"title": "ATLAS reaches v3.0", "side": "NO", "qty": 3, "cost": 165, "payout": 300, "buy_price": 0.55},
        ],
        player_name="TestUser",
        total_invested=475,
        total_potential=800,
        balance=2025,
    ))

    await test_card("resolution", render_resolution_card(
        market_title="Will the Eagles win the Super Bowl?",
        result="YES",
        winners=[
            {"name": "TestUser", "qty": 5, "payout": 500, "profit": 190},
            {"name": "Player2", "qty": 3, "payout": 300, "profit": 114},
        ],
        total_won=800,
        total_lost=200,
        total_voided=0,
    ))

    # ── Group F — Trade + Ledger ──────────────────────────────────────────────
    print("\n=== Group F: Trade + Ledger ===")

    from card_renderer import render_trade_card
    from casino.renderer.ledger_renderer import render_ledger_card

    trade_data = {
        "status": "approved",
        "band": "GREEN",
        "team_a_name": "Philadelphia Eagles",
        "team_b_name": "Kansas City Chiefs",
        "team_a_owner": "NateW",
        "team_b_owner": "ChiefsFan",
        "players_a": [
            {"name": "A.J. Brown", "position": "WR", "ovr": 92, "age": 27, "dev": "Superstar"},
        ],
        "players_b": [
            {"name": "Travis Kelce", "position": "TE", "ovr": 95, "age": 34, "dev": "X-Factor"},
        ],
        "picks_a": [],
        "picks_b": [],
        "side_a_value": 340,
        "side_b_value": 380,
        "delta_pct": 11.8,
        "ovr_delta": 3,
        "notes": ["Eagles acquire elite TE for stretch run."],
    }

    await test_card("trade_card", render_trade_card(trade_data))

    await test_card("ledger", render_ledger_card(
        player_name="TestUser",
        game_type="blackjack",
        wager=100,
        outcome="win",
        payout=200,
        multiplier=2.0,
        new_balance=1200,
        txn_id=9999,
    ))

    # ── Drain pool + summary ──────────────────────────────────────────────────
    await drain_pool()

    ok = sum(1 for r in results if r[1] == "OK")
    print(f"\n{'=' * 55}")
    print(f"Results: {ok}/{len(results)} passed")
    if ok < len(results):
        print("Failed cards:")
        for name, status, _, _ in results:
            if status == "FAIL":
                print(f"  FAILED: {name}")

    print(f"\nLatency summary (OK cards only):")
    ok_results = [(n, ms) for n, s, _, ms in results if s == "OK"]
    if ok_results:
        ok_results.sort(key=lambda x: x[1], reverse=True)
        for name, ms in ok_results:
            print(f"  {ms:>6.0f}ms  {name}")


if __name__ == "__main__":
    asyncio.run(main())

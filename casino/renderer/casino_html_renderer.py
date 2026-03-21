"""
casino_html_renderer.py — ATLAS FLOW Casino · Unified HTML Card Renderer
─────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for all casino game cards.
V6 design language: dark bg, gold accents, Outfit + JetBrains Mono,
noise texture, glass-morphism cells.

Uses the unified render engine from atlas_html_engine.py.

Usage:
    from casino.renderer.casino_html_renderer import render_blackjack_card
    png_bytes = await render_blackjack_card(data)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math
from typing import Optional

from atlas_style_tokens import Tokens
from atlas_html_engine import (
    render_card,
    wrap_card,
    esc,
    card_img_src,
    card_back_src,
    slot_icon_src,
    icon_pill,
    SLOT_ICON_CONFIG,
    build_header_html,
    build_data_grid_html,
    build_footer_html,
    build_streak_badge_html,
    build_near_miss_html,
    build_jackpot_footer_html,
)


# ══════════════════════════════════════════════════════════════════════════════
#  BLACKJACK RENDERER
# ══════════════════════════════════════════════════════════════════════════════

# Suit code mapping from unicode to file prefix
_SUIT_TO_CODE = {"♠": "S", "♥": "H", "♦": "D", "♣": "C"}


def _build_blackjack_html(
    dealer_hand: list[tuple[str, str]],
    player_hand: list[tuple[str, str]],
    dealer_score: int | str,
    player_score: int | str,
    hide_dealer: bool = True,
    status: str = "",
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> str:
    """Build the full blackjack card HTML."""

    # Determine outcome for status bar and badge
    outcome = "active"
    badge_text = "IN PLAY"
    if status:
        s = status.lower()
        if "blackjack" in s:
            outcome = "blackjack"
            badge_text = "BLACKJACK"
        elif "win" in s or "bust" in s and "dealer" in s.lower():
            outcome = "win"
            badge_text = "WIN"
        elif "bust" in s or "loss" in s:
            outcome = "loss"
            badge_text = "LOSS"
        elif "push" in s:
            outcome = "push"
            badge_text = "PUSH"

    # Build card images
    def _card_html(hand, hide_second=False):
        cards_html = ""
        for i, (value, suit) in enumerate(hand):
            if hide_second and i == 1:
                src = card_back_src()
            else:
                code = _SUIT_TO_CODE.get(suit, suit) or "S"
                src = card_img_src(code, value)

            # Seeded rotation for visual variety
            import hashlib
            seed = int(hashlib.md5(f"{value}{suit}{i}".encode()).hexdigest()[:8], 16)
            angle = (seed % 5) - 2  # -2 to +2 degrees

            cards_html += f"""
            <div class="playing-card" style="transform: rotate({angle}deg);">
              <img src="{src}" alt="{esc(value)}{esc(suit)}">
            </div>"""
        return cards_html

    dealer_cards = _card_html(dealer_hand, hide_second=hide_dealer)
    player_cards = _card_html(player_hand)

    # Score display
    dealer_score_display = "?" if hide_dealer else str(dealer_score)

    # Result banner (between hands, only when game is over)
    result_banner = ""
    if status and outcome != "active":
        banner_color = {
            "blackjack": "var(--gold)",
            "win": "var(--win)",
            "loss": "var(--loss)",
            "push": "var(--push)",
        }.get(outcome, "var(--text-primary)")
        result_banner = f"""
        <div class="result-banner" style="color: {banner_color};">
          {esc(status)}
        </div>"""

    # Near-miss overrides loss outcome to amber
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"
        badge_text = "SO CLOSE"

    header = build_header_html(icon_pill("blackjack", "♠"), "BLACKJACK", [player_name], status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)
    data_grid = build_data_grid_html(wager, payout, balance) if status else ""
    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = """
    <style>
    /* Blackjack-specific styles */
    .bj-section {
      padding: var(--space-lg) 20px var(--space-sm);
    }
    .bj-label {
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 14px;
      color: var(--gold);
      letter-spacing: 2px;
      text-transform: uppercase;
      margin-bottom: var(--space-xs);
    }
    .bj-score {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.1);
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: 16px;
      color: var(--text-primary);
      margin-left: 10px;
      vertical-align: middle;
    }
    .bj-cards {
      display: flex;
      gap: var(--space-xs);
      padding: 10px 20px;
      justify-content: center;
    }
    .playing-card {
      flex-shrink: 0;
      filter: drop-shadow(0 var(--space-xs) var(--space-sm) rgba(0,0,0,0.5));
    }
    .playing-card img {
      width: 100px;
      height: auto;
      border-radius: 6px;
    }
    .result-banner {
      text-align: center;
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: 28px;
      padding: var(--space-md) 0;
      letter-spacing: 1px;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <!-- Dealer -->
    <div class="bj-section">
      <span class="bj-label">DEALER</span>
      <span class="bj-score">{esc(dealer_score_display)}</span>
    </div>
    <div class="bj-cards">{dealer_cards}</div>

    {result_banner}
    {near_miss_banner}

    <!-- Player -->
    <div class="bj-section">
      <span class="bj-label">PLAYER</span>
      <span class="bj-score">{esc(str(player_score))}</span>
    </div>
    <div class="bj-cards">{player_cards}</div>

    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class)


async def render_blackjack_card(
    dealer_hand: list[tuple[str, str]],
    player_hand: list[tuple[str, str]],
    dealer_score: int | str,
    player_score: int | str,
    hide_dealer: bool = True,
    status: str = "",
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> bytes:
    """Render a blackjack card to PNG bytes."""
    html = _build_blackjack_html(
        dealer_hand, player_hand, dealer_score, player_score,
        hide_dealer, status, wager, payout, balance, player_name, txn_id,
        streak_info, near_miss_msg, jackpot_info,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  SLOTS RENDERER
# ══════════════════════════════════════════════════════════════════════════════

# Fallback emoji for when slot icon PNGs aren't available
_SLOT_EMOJI_FALLBACK = {
    "shield": "🛡️", "crown": "👑", "trophy": "🏆",
    "wild": "✦", "football": "🏈", "star": "⭐", "coin": "🪙",
}

_SLOT_TIER_COLORS = {
    "jackpot": Tokens.GOLD_LIGHT,
    "legend": Tokens.GOLD,
    "epic": Tokens.PURPLE,
    "wild": "#F5E6C8",
    "rare": Tokens.BLUE_LIGHT,
    "common": Tokens.TEXT_PRIMARY,
    "base": Tokens.TEXT_MUTED,
}


def _build_slots_html(
    reels: list[str],
    revealed: int = 3,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    result_msg: str = "",
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> str:
    """Build the full slots card HTML."""

    is_triple = revealed == 3 and len(set(reels)) == 1
    is_win = payout > 0

    if is_triple and reels[0] == "shield":
        outcome = "jackpot"
        badge_text = "JACKPOT!"
    elif is_win:
        outcome = "win"
        badge_text = "WIN"
    elif revealed == 3 and not is_win:
        outcome = "loss"
        badge_text = "LOSS"
    else:
        outcome = "active"
        badge_text = "SPINNING..."

    # Near-miss overrides loss to amber
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"
        badge_text = "SO CLOSE"

    header = build_header_html(icon_pill("slots", "🎰"), "SLOTS", [player_name], status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Build reel windows
    reels_html = ""
    for i in range(3):
        is_revealed = i < revealed
        symbol = reels[i] if i < len(reels) else "coin"
        cfg = SLOT_ICON_CONFIG.get(symbol, {})
        tier = cfg.get("tier", "base")
        tier_color = _SLOT_TIER_COLORS.get(tier, "#888")

        border_style = f"border: 2px solid {tier_color};" if is_revealed else "border: 2px solid #333;"
        glow = f"box-shadow: 0 0 20px {tier_color}40;" if is_revealed and tier in ("jackpot", "legend") else ""

        if is_triple and is_revealed:
            glow = f"box-shadow: 0 0 25px {tier_color}60;"

        icon_src = slot_icon_src(symbol)
        if is_revealed:
            if icon_src:
                inner = f'<img src="{icon_src}" class="slot-icon-img">'
            else:
                emoji = _SLOT_EMOJI_FALLBACK.get(symbol, "?")
                inner = f'<span class="slot-emoji">{emoji}</span>'
        else:
            inner = '<span class="slot-unknown">?</span>'

        reels_html += f"""
        <div class="reel-window" style="{border_style} {glow}">
          {inner}
        </div>"""

    # Payline
    payline = '<div class="payline"></div>' if revealed == 3 else ""

    # Result message
    result_html = ""
    if result_msg and revealed == 3:
        result_color = "#FFDA50" if is_triple else "var(--win)" if is_win else "var(--text-muted)"
        result_html = f'<div class="slots-result" style="color: {result_color};">{esc(result_msg)}</div>'

    data_grid = build_data_grid_html(wager, payout, balance) if revealed == 3 else ""
    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = """
    <style>
    .reels-container {
      display: flex;
      justify-content: center;
      gap: var(--space-lg);
      padding: var(--space-xl) 20px;
      position: relative;
    }
    .reel-window {
      width: 150px;
      height: 150px;
      border-radius: 12px;
      background: rgba(0,0,0,0.4);
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
    }
    .slot-icon-img {
      width: 100px;
      height: 100px;
      object-fit: contain;
    }
    .slot-emoji {
      font-size: 64px;
    }
    .slot-unknown {
      font-family: var(--font-mono), monospace;
      font-size: 48px;
      font-weight: 800;
      color: #333;
    }
    .payline {
      position: absolute;
      left: 20px;
      right: 20px;
      top: 50%;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--gold) 10%, var(--gold) 90%, transparent);
      pointer-events: none;
    }
    .slots-result {
      text-align: center;
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-lg);
      padding: 0 20px var(--space-md);
      letter-spacing: 0.5px;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="reels-container">
      {reels_html}
      {payline}
    </div>

    {result_html}
    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_grid}
    {"<div class='gold-divider'></div>" if data_grid else ""}
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class)


async def render_slots_card(
    reels: list[str],
    revealed: int = 3,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    result_msg: str = "",
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> bytes:
    """Render a slots card to PNG bytes."""
    html = _build_slots_html(
        reels, revealed, wager, payout, balance, result_msg, player_name, txn_id,
        streak_info, near_miss_msg, jackpot_info,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  CRASH RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def _build_crash_html(
    current_mult: float,
    crashed: bool = False,
    cashed_out: bool = False,
    cashout_mult: Optional[float] = None,
    history: Optional[list[float]] = None,
    players_in: int = 0,
    total_wagered: int = 0,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    players: Optional[list[str]] = None,
    txn_id: Optional[str] = None,
    is_live: bool = False,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> str:
    """Build the full crash card HTML with rocket launch theme."""

    if cashed_out:
        outcome = "win"
        badge_text = f"CASHED {cashout_mult or current_mult:.2f}x"
    elif crashed:
        outcome = "loss"
        badge_text = f"CRASHED {current_mult:.2f}x"
    else:
        outcome = "active"
        badge_text = "CLIMBING..."

    # Near-miss overrides loss to amber
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"

    display_players = players or [player_name]
    header = build_header_html(icon_pill("crash", "🚀"), "CRASH", display_players, status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Altitude gauge
    gauge_mult = cashout_mult if (cashed_out and cashout_mult is not None) else current_mult
    max_gauge = 25.0
    fill_pct = min(100.0, max(0.0, math.log(max(gauge_mult, 1.001)) / math.log(max_gauge) * 100))

    if cashed_out:
        fill_gradient = f"linear-gradient(to top, {Tokens.WIN}, {Tokens.WIN_DARK})"
    elif crashed:
        fill_gradient = f"linear-gradient(to top, {Tokens.LOSS}, {Tokens.LOSS_DARK})"
    else:
        fill_gradient = f"linear-gradient(to top, {Tokens.WIN}, {Tokens.GOLD}, {Tokens.LOSS})"

    gauge_markers = ""
    for mult_val in [1, 2, 5, 10, 25]:
        pos = math.log(max(mult_val, 1.001)) / math.log(max_gauge) * 100
        gauge_markers += f"""
        <div class="gauge-marker" style="bottom: {pos}%;">
          <div class="gauge-tick"></div>
          <span class="gauge-label">{mult_val}x</span>
        </div>"""

    # Cashout/crash dot on gauge
    gauge_dot = ""
    if cashed_out:
        gauge_dot = f'<div class="gauge-dot win" style="bottom: {fill_pct}%;"></div>'
    elif crashed:
        gauge_dot = f'<div class="gauge-dot loss" style="bottom: {fill_pct}%;"></div>'

    # Rocket area
    if crashed:
        rocket_html = """
        <div class="explosion">
          <div class="explosion-inner"></div>
        </div>"""
    else:
        rocket_html = '<div class="rocket">🚀</div>'

    # Multiplier display
    if crashed:
        mult_color = "var(--loss)"
        mult_sub = "CRASHED"
    elif cashed_out:
        mult_color = "var(--win)"
        mult_sub = "CASHED OUT"
    else:
        mult_color = "var(--gold)"
        mult_sub = "Climbing..."

    profit_pill = ""
    if cashed_out and payout > wager:
        profit = payout - wager
        profit_pill = f'<div class="profit-pill">+${profit:,}</div>'

    # Recent history pills
    history_html = ""
    if history:
        pills = ""
        for i, h in enumerate(history[-6:]):
            is_current = (i == len(history[-6:]) - 1) and is_live
            if h < 2.0:
                pill_color = Tokens.LOSS_LIGHT
            elif h < 10.0:
                pill_color = Tokens.WIN
            else:
                pill_color = Tokens.GOLD_LIGHT
            glow_class = "current-pill" if is_current else ""
            pills += f'<span class="history-pill {glow_class}" style="color: {pill_color};">{h:.2f}x</span>'
        history_html = f'<div class="history-row">{pills}</div>'

    # Data section: live round shows players+wagered, finished shows grid
    if is_live:
        data_section = f"""
        <div class="live-info">
          <span class="live-stat">👥 {players_in} Players</span>
          <span class="live-stat">💰 ${total_wagered:,} Wagered</span>
        </div>"""
    else:
        data_section = build_data_grid_html(wager, payout, balance)

    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = """
    <style>
    .crash-content {
      display: flex;
      align-items: stretch;
      padding: 20px;
      gap: 20px;
      min-height: 280px;
    }

    /* Altitude gauge */
    .gauge-container {
      width: 80px;
      display: flex;
      flex-direction: column;
      align-items: center;
      position: relative;
    }
    .gauge-track {
      width: var(--space-sm);
      flex: 1;
      background: rgba(255,255,255,0.06);
      border-radius: var(--border-radius-sm);
      position: relative;
      overflow: visible;
    }
    .gauge-fill {
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      border-radius: var(--border-radius-sm);
      transition: height 0.3s ease;
    }
    .gauge-marker {
      position: absolute;
      left: 20px;
      display: flex;
      align-items: center;
      gap: 6px;
      transform: translateY(50%);
    }
    .gauge-tick {
      width: 10px;
      height: 2px;
      background: var(--gold);
    }
    .gauge-label {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-sm);
      color: var(--gold);
      white-space: nowrap;
    }
    .gauge-dot {
      position: absolute;
      left: 50%;
      transform: translate(-50%, 50%);
      width: 10px;
      height: 10px;
      border-radius: 50%;
      z-index: 5;
    }
    .gauge-dot.win {
      background: var(--win);
      box-shadow: 0 0 10px var(--win);
    }
    .gauge-dot.loss {
      background: var(--loss);
      box-shadow: 0 0 10px var(--loss);
    }

    /* Rocket area */
    .rocket-area {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .rocket {
      font-size: var(--font-display-size);
      transform: rotate(-15deg);
      filter: drop-shadow(0 0 var(--space-md) rgba(212,175,55,0.5));
    }
    .explosion {
      width: 180px;
      height: 180px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,200,0,0.6) 0%, rgba(255,120,0,0.4) 40%, rgba(255,50,0,0.2) 70%, transparent 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      filter: blur(3px);
    }
    .explosion-inner {
      width: 80px;
      height: 80px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,255,200,0.8) 0%, rgba(255,160,0,0.6) 50%, transparent 100%);
    }

    /* Multiplier display */
    .mult-display {
      width: 200px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: var(--space-xs);
    }
    .mult-value {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: 64px;
      line-height: 1;
      letter-spacing: -2px;
    }
    .mult-sub {
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: 2px;
      text-transform: uppercase;
      opacity: 0.7;
    }
    .profit-pill {
      margin-top: var(--space-sm);
      padding: var(--space-xs) 14px;
      border-radius: 20px;
      background: rgba(74,222,128,0.12);
      border: 1px solid rgba(74,222,128,0.3);
      color: var(--win);
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: var(--font-sm);
    }

    /* History */
    .history-row {
      display: flex;
      justify-content: center;
      gap: var(--space-sm);
      padding: var(--space-sm) 20px var(--space-md);
      flex-wrap: wrap;
    }
    .history-pill {
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: var(--font-sm);
      padding: 3px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
    }
    .history-pill.current-pill {
      color: #fff !important;
      border: 1px solid currentColor;
      box-shadow: 0 0 var(--space-sm) currentColor;
    }

    /* Live info */
    .live-info {
      display: flex;
      justify-content: center;
      gap: var(--space-xl);
      padding: var(--space-md) 20px;
    }
    .live-stat {
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: 14px;
      color: var(--text-muted);
    }
    </style>"""

    mult_display_val = cashout_mult if cashed_out else current_mult

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="crash-content">
      <!-- Altitude gauge -->
      <div class="gauge-container">
        <div class="gauge-track">
          <div class="gauge-fill" style="height: {fill_pct}%; background: {fill_gradient};"></div>
          {gauge_dot}
          {gauge_markers}
        </div>
      </div>

      <!-- Rocket -->
      <div class="rocket-area">
        {rocket_html}
      </div>

      <!-- Multiplier -->
      <div class="mult-display">
        <div class="mult-value" style="color: {mult_color};">{mult_display_val:.2f}x</div>
        <div class="mult-sub" style="color: {mult_color};">{mult_sub}</div>
        {profit_pill}
      </div>
    </div>

    {history_html}
    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_section}
    <div class="gold-divider"></div>
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class)


async def render_crash_card(
    current_mult: float,
    crashed: bool = False,
    cashed_out: bool = False,
    cashout_mult: Optional[float] = None,
    history: Optional[list[float]] = None,
    players_in: int = 0,
    total_wagered: int = 0,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    players: Optional[list[str]] = None,
    txn_id: Optional[str] = None,
    is_live: bool = False,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> bytes:
    """Render a crash card to PNG bytes."""
    html = _build_crash_html(
        current_mult, crashed, cashed_out, cashout_mult, history,
        players_in, total_wagered, wager, payout, balance,
        player_name, players, txn_id, is_live,
        streak_info, near_miss_msg, jackpot_info,
    )
    return await render_card(html)


# ══════════════════════════════════════════════════════════════════════════════
#  COIN FLIP RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def _build_coinflip_html(
    result: str,  # "heads" or "tails"
    player_pick: str,  # "heads" or "tails"
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    # PvP fields
    is_pvp: bool = False,
    opponent_name: Optional[str] = None,
    opponent_pick: Optional[str] = None,
    # Momentum fields
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> str:
    """Build the full coin flip card HTML."""

    won = result == player_pick

    if is_pvp:
        if won:
            outcome = "win"
            badge_text = f"{player_name.upper()} WINS"
        else:
            outcome = "loss"
            badge_text = f"{(opponent_name or 'Opponent').upper()} WINS"
        display_players = [player_name, opponent_name or "Opponent"]
    else:
        if won:
            outcome = "win"
            badge_text = "WIN"
        else:
            outcome = "loss"
            badge_text = "LOSS"
        display_players = [player_name]

    # Near-miss overrides loss to amber (edge tease)
    status_class = outcome
    if near_miss_msg and outcome == "loss":
        status_class = "near_miss"

    header = build_header_html(icon_pill("coinflip", "🪙"), "COIN FLIP", display_players, status_class, badge_text, txn_id)
    streak_badge = build_streak_badge_html(streak_info)
    near_miss_banner = build_near_miss_html(near_miss_msg)

    # Coin visual
    is_heads = result == "heads"
    coin_letter = "H" if is_heads else "T"
    coin_gradient = "linear-gradient(135deg, #D4AF37, #FFDA50, #B8942D)" if is_heads else "linear-gradient(135deg, #C0C0C0, #E8E8E8, #A0A0A0)"
    coin_rim = "var(--gold)" if is_heads else "#C0C0C0"

    # Player pick indicator
    pick_icon = "✓" if won else "✗"
    pick_color = "var(--win)" if won else "var(--loss)"

    # PvP layout
    pvp_html = ""
    if is_pvp:
        p1_won = won
        p2_won = not won
        p1_color = "var(--win)" if p1_won else "var(--text-muted)"
        p2_color = "var(--win)" if p2_won else "var(--text-muted)"
        p1_opacity = "1" if p1_won else "0.5"
        p2_opacity = "1" if p2_won else "0.5"
        pvp_html = f"""
        <div class="pvp-row">
          <div class="pvp-player" style="color: {p1_color}; opacity: {p1_opacity};">
            <div class="pvp-name">{esc(player_name)}</div>
            <div class="pvp-pick">{esc(player_pick.upper())}</div>
          </div>
          <div class="pvp-vs">VS</div>
          <div class="pvp-player" style="color: {p2_color}; opacity: {p2_opacity};">
            <div class="pvp-name">{esc(opponent_name or 'Opponent')}</div>
            <div class="pvp-pick">{esc((opponent_pick or '').upper())}</div>
          </div>
        </div>"""
    else:
        pvp_html = f"""
        <div class="pick-display">
          <span style="color: {pick_color}; font-size: 20px;">{pick_icon}</span>
          <span class="pick-text">You picked <strong>{esc(player_pick.upper())}</strong></span>
        </div>"""

    data_grid = build_data_grid_html(wager, payout, balance)
    footer = build_footer_html(balance)
    jackpot_footer = build_jackpot_footer_html(jackpot_info)

    game_css = f"""
    <style>
    .coin-area {{
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: var(--space-xl) 20px var(--space-lg);
    }}
    .coin {{
      width: 140px;
      height: 140px;
      border-radius: 50%;
      background: {coin_gradient};
      display: flex;
      align-items: center;
      justify-content: center;
      border: 4px solid {coin_rim};
      box-shadow: 0 var(--space-sm) var(--space-xl) rgba(0,0,0,0.4), inset 0 2px var(--space-xs) rgba(255,255,255,0.3);
      transform: perspective(400px) rotateX(10deg);
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: var(--font-display-size);
      color: rgba(0,0,0,0.25);
    }}
    .coin-result {{
      text-align: center;
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: 22px;
      color: var(--text-primary);
      margin-top: var(--space-md);
      letter-spacing: 2px;
    }}
    .pick-display {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--space-sm);
      margin-top: var(--space-sm);
    }}
    .pick-text {{
      font-family: var(--font-display), sans-serif;
      font-size: 14px;
      color: var(--text-muted);
    }}
    .pick-text strong {{
      color: var(--text-primary);
    }}

    /* PvP */
    .pvp-row {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--space-xl);
      margin-top: var(--space-md);
    }}
    .pvp-player {{
      text-align: center;
    }}
    .pvp-name {{
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 16px;
    }}
    .pvp-pick {{
      font-family: var(--font-mono), monospace;
      font-weight: 700;
      font-size: 12px;
      margin-top: 2px;
      opacity: 0.7;
    }}
    .pvp-vs {{
      font-family: var(--font-display), sans-serif;
      font-weight: 800;
      font-size: 14px;
      color: var(--text-dim);
      letter-spacing: 2px;
    }}
    </style>"""

    content = f"""
    {game_css}
    {header}
    {streak_badge}
    <div class="gold-divider"></div>

    <div class="coin-area">
      <div class="coin">{coin_letter}</div>
      <div class="coin-result">{result.upper()}</div>
      {pvp_html}
    </div>

    {near_miss_banner}

    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}
    {jackpot_footer}"""

    return wrap_card(content, status_class)


async def render_coinflip_card(
    result: str,
    player_pick: str,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    player_name: str = "Player",
    txn_id: Optional[str] = None,
    is_pvp: bool = False,
    opponent_name: Optional[str] = None,
    opponent_pick: Optional[str] = None,
    streak_info: dict | None = None,
    near_miss_msg: str | None = None,
    jackpot_info: dict | None = None,
) -> bytes:
    """Render a coin flip card to PNG bytes."""
    html = _build_coinflip_html(
        result, player_pick, wager, payout, balance, player_name, txn_id,
        is_pvp, opponent_name, opponent_pick,
        streak_info, near_miss_msg, jackpot_info,
    )
    return await render_card(html)


# ═════════════════════════════════════════════════════════════════════════════
#  SCRATCH CARD
# ═════════════════════════════════════════════════════════════════════════════

def _build_scratch_html(
    tiles: list[int],
    revealed: int = 0,
    is_match: bool = False,
    player_name: str = "Player",
    total: int = 0,
    balance: int = 0,
) -> str:
    """Build scratch card HTML with V6 design. 3 tiles, sequential reveal."""

    # Determine outcome state
    all_revealed = revealed >= 3
    if all_revealed and is_match:
        status_class = "jackpot"
        outcome = "jackpot"
        badge_text = "TRIPLE MATCH!"
    elif all_revealed:
        status_class = "win"
        outcome = "win"
        badge_text = f"+${total:,}"
    else:
        status_class = "push"
        outcome = "active"
        badge_text = f"{3 - revealed} LEFT"

    header = build_header_html(
        icon=icon_pill("scratch", "\U0001f3ab"),
        title="DAILY SCRATCH",
        players=[player_name],
        outcome=outcome,
        badge_text=badge_text,
    )

    # Build tile HTML
    tiles_html = ""
    for i in range(3):
        if i < revealed:
            # Revealed tile
            value = tiles[i]
            glow = "glow" if (is_match and all_revealed) else ""
            tiles_html += f"""
            <div class="scratch-tile revealed {glow}">
              <div class="tile-value">${value:,}</div>
            </div>"""
        else:
            # Unrevealed tile
            tiles_html += """
            <div class="scratch-tile hidden">
              <div class="tile-mystery">?</div>
            </div>"""

    # Result message
    if all_revealed and is_match:
        result_html = f"""
        <div class="scratch-result match">
          \U0001f3c6 TRIPLE MATCH — ${tiles[0]:,} × 3 = +${total:,}!
        </div>"""
    elif all_revealed:
        result_html = f"""
        <div class="scratch-result normal">
          \u2705 You won ${total:,}!
        </div>"""
    else:
        remaining = 3 - revealed
        result_html = f"""
        <div class="scratch-result pending">
          Tap Scratch! to reveal ({remaining} tile{"s" if remaining > 1 else ""} left)
        </div>"""

    footer = build_footer_html(balance)

    game_css = """<style>
    .scratch-area {
      display: flex;
      justify-content: center;
      gap: 14px;
      padding: 20px var(--space-xl) 10px;
    }
    .scratch-tile {
      width: 160px;
      height: 120px;
      border-radius: 12px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
    }
    .scratch-tile.hidden {
      background: rgba(140,115,36,0.12);
      border: 2px solid var(--gold-dim);
      box-shadow: inset 0 2px var(--space-sm) rgba(0,0,0,0.5), inset 0 -1px 2px rgba(255,255,255,0.03);
      /* Cross-hatch pattern */
      background-image:
        repeating-linear-gradient(
          45deg,
          transparent,
          transparent var(--space-sm),
          rgba(212,175,55,0.06) var(--space-sm),
          rgba(212,175,55,0.06) 9px
        ),
        repeating-linear-gradient(
          -45deg,
          transparent,
          transparent var(--space-sm),
          rgba(212,175,55,0.06) var(--space-sm),
          rgba(212,175,55,0.06) 9px
        );
    }
    .tile-mystery {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: var(--font-hero);
      color: var(--gold-dim);
      opacity: 0.5;
    }
    .scratch-tile.revealed {
      background: rgba(212,175,55,0.08);
      border: 2px solid var(--gold);
      box-shadow: inset 0 2px 6px rgba(0,0,0,0.45), inset 0 -1px 3px rgba(255,255,255,0.04);
    }
    /* Scratched grain texture on revealed tiles */
    .scratch-tile.revealed::before {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 10px;
      opacity: 0.07;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 128 128' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='g'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='1.2' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
      pointer-events: none;
    }
    .scratch-tile.revealed.glow {
      border-color: var(--gold-light);
      box-shadow: 0 0 20px rgba(212,175,55,0.35), inset 0 2px var(--space-sm) rgba(0,0,0,0.4), inset 0 -1px 3px rgba(255,218,80,0.06);
    }
    .tile-value {
      font-family: var(--font-mono), monospace;
      font-weight: 800;
      font-size: 32px;
      color: var(--text-primary);
      line-height: 1;
    }
    .scratch-tile.glow .tile-value {
      color: var(--gold-light);
    }
    .tile-label {
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: var(--font-xs);
      color: var(--gold-dim);
      letter-spacing: 2px;
      margin-top: 6px;
    }
    .scratch-result {
      text-align: center;
      padding: var(--space-sm) 20px 14px;
      font-family: var(--font-display), sans-serif;
      font-weight: 700;
      font-size: 14px;
    }
    .scratch-result.match {
      color: var(--gold-light);
      font-size: 16px;
    }
    .scratch-result.normal {
      color: var(--win);
    }
    .scratch-result.pending {
      color: var(--text-muted);
      font-weight: 600;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    <div class="gold-divider"></div>

    <div class="scratch-area">
      {tiles_html}
    </div>

    {result_html}

    <div class="gold-divider"></div>
    {footer}"""

    return wrap_card(content, status_class)


async def render_scratch_card_v6(
    tiles: list[int],
    revealed: int = 0,
    is_match: bool = False,
    player_name: str = "Player",
    total: int = 0,
    balance: int = 0,
) -> bytes:
    """Render a scratch card to PNG bytes using V6 Playwright renderer."""
    html = _build_scratch_html(tiles, revealed, is_match, player_name, total, balance)
    return await render_card(html)

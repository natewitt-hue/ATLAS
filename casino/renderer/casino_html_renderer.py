"""
casino_html_renderer.py — ATLAS FLOW Casino · Unified HTML Card Renderer
─────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for all casino game cards.
V6 design language: dark bg, gold accents, Outfit + JetBrains Mono,
noise texture, glass-morphism cells.

Reuses the browser singleton from card_renderer.py (_get_browser).

Usage:
    from casino.renderer.casino_html_renderer import render_blackjack_card
    png_bytes = await render_blackjack_card(data)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import base64
import html as html_mod
import math
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────

_DIR = Path(__file__).parent
_CARDS_DIR = _DIR / "cards"
_SLOT_ICONS_DIR = _DIR / "slot_icons"
_FONTS_DIR = Path(__file__).parent.parent.parent / "fonts"

# ── Base64 font loader ────────────────────────────────────────────────────────

_FONT_CACHE: dict[str, str] = {}


def _load_font_b64(name: str) -> str:
    if name not in _FONT_CACHE:
        path = _FONTS_DIR / name
        if path.exists():
            _FONT_CACHE[name] = base64.b64encode(path.read_bytes()).decode()
        else:
            _FONT_CACHE[name] = ""
    return _FONT_CACHE[name]


def _font_face_css() -> str:
    fonts = [
        ("Outfit", "Outfit-Regular.ttf", 400),
        ("Outfit", "Outfit-SemiBold.ttf", 600),
        ("Outfit", "Outfit-Bold.ttf", 700),
        ("Outfit", "Outfit-ExtraBold.ttf", 800),
        ("JetBrains Mono", "JetBrainsMono-Regular.ttf", 400),
        ("JetBrains Mono", "JetBrainsMono-Bold.ttf", 700),
        ("JetBrains Mono", "JetBrainsMono-ExtraBold.ttf", 800),
    ]
    css = ""
    for family, filename, weight in fonts:
        b64 = _load_font_b64(filename)
        if b64:
            css += f"""
@font-face {{
  font-family: '{family}';
  src: url('data:font/ttf;base64,{b64}') format('truetype');
  font-weight: {weight};
  font-style: normal;
  font-display: block;
}}"""
    return css


# ── Base64 card asset loader ─────────────────────────────────────────────────

_CARD_B64_CACHE: dict[str, str] = {}


def _card_b64(filename: str) -> str:
    if filename not in _CARD_B64_CACHE:
        path = _CARDS_DIR / filename
        if path.exists():
            _CARD_B64_CACHE[filename] = base64.b64encode(path.read_bytes()).decode()
        else:
            _CARD_B64_CACHE[filename] = ""
    return _CARD_B64_CACHE[filename]


def card_img_src(suit_code: str, value: str) -> str:
    """Return base64 data URI for a card face. suit_code: S/H/D/C, value: A/2-10/J/Q/K"""
    b64 = _card_b64(f"{suit_code}{value}.png")
    return f"data:image/png;base64,{b64}" if b64 else ""


def card_back_src() -> str:
    b64 = _card_b64("back.png")
    return f"data:image/png;base64,{b64}" if b64 else ""


# ── Base64 slot icon loader ──────────────────────────────────────────────────

_SLOT_ICON_B64_CACHE: dict[str, str] = {}

SLOT_ICON_CONFIG = {
    "shield":   {"file": "shield.png",   "label": "TSL Shield",  "mult": 50, "tier": "jackpot", "weight": 2},
    "crown":    {"file": "crown.png",    "label": "Crown",       "mult": 20, "tier": "legend",  "weight": 5},
    "trophy":   {"file": "trophy.png",   "label": "Trophy",      "mult": 10, "tier": "epic",    "weight": 8},
    "football": {"file": "football.png", "label": "Football",    "mult": 5,  "tier": "rare",    "weight": 15},
    "star":     {"file": "star.png",     "label": "Star",        "mult": 3,  "tier": "common",  "weight": 20},
    "coin":     {"file": "coin.png",     "label": "Coin",        "mult": 2,  "tier": "base",    "weight": 30},
}


def _slot_icon_b64(symbol: str) -> str:
    if symbol not in _SLOT_ICON_B64_CACHE:
        cfg = SLOT_ICON_CONFIG.get(symbol)
        if cfg:
            path = _SLOT_ICONS_DIR / cfg["file"]
            if path.exists():
                _SLOT_ICON_B64_CACHE[symbol] = base64.b64encode(path.read_bytes()).decode()
            else:
                _SLOT_ICON_B64_CACHE[symbol] = ""
        else:
            _SLOT_ICON_B64_CACHE[symbol] = ""
    return _SLOT_ICON_B64_CACHE[symbol]


def slot_icon_src(symbol: str) -> str:
    b64 = _slot_icon_b64(symbol)
    return f"data:image/png;base64,{b64}" if b64 else ""


# ── HTML escape helper ───────────────────────────────────────────────────────

def _esc(text) -> str:
    return html_mod.escape(str(text))


# ── Shared V6 CSS ────────────────────────────────────────────────────────────

def _base_css() -> str:
    return _font_face_css() + """

:root {
  --bg: #111111;
  --gold: #D4AF37;
  --gold-light: #FFDA50;
  --gold-dim: #8C7324;
  --win: #4ADE80;
  --loss: #F87171;
  --push: #FBBF24;
  --text-primary: #e8e0d0;
  --text-sub: #aaa;
  --text-muted: #888;
  --text-dim: #555;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background: transparent;
  font-family: 'Outfit', sans-serif;
  color: #fff;
  padding: 0;
}

.card {
  width: 700px;
  border-radius: 14px;
  overflow: hidden;
  position: relative;
  background: var(--bg);
  border: 1px solid rgba(212,175,55,0.18);
}

/* Noise texture overlay */
.card::before {
  content: '';
  position: absolute;
  inset: 0;
  opacity: 0.035;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: 1;
}

.card > * { position: relative; z-index: 2; }

/* ── Status bar (5px top edge) ── */
.status-bar { height: 5px; width: 100%; }
.status-bar.win      { background: linear-gradient(90deg, #4ADE80, #22C55E, #4ADE80); }
.status-bar.loss     { background: linear-gradient(90deg, #F87171, #EF4444, #F87171); }
.status-bar.push     { background: linear-gradient(90deg, #FBBF24, #D97706, #FBBF24); }
.status-bar.jackpot  { background: linear-gradient(90deg, #D4AF37, #FFDA50, #D4AF37); }
.status-bar.blackjack{ background: linear-gradient(90deg, #D4AF37, #FFDA50, #D4AF37); }

/* ── Header ── */
.header {
  display: flex;
  align-items: center;
  padding: 14px 20px 10px;
}
.header-left {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex: 1;
}
.game-icon-pill {
  width: 32px; height: 32px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  background: rgba(212,175,55,0.15);
  color: var(--gold);
  flex-shrink: 0;
}
.game-title-group {
  display: flex;
  flex-direction: column;
}
.game-title {
  font-family: 'Outfit', sans-serif;
  font-weight: 800;
  font-size: 20px;
  color: var(--text-primary);
  letter-spacing: 1.5px;
  line-height: 1.2;
}
.game-subtitle {
  font-family: 'Outfit', sans-serif;
  font-weight: 600;
  font-size: 11px;
  color: var(--text-sub);
  letter-spacing: 0.5px;
}
.txn-id {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 600;
  font-size: 9px;
  color: var(--text-dim);
  margin-top: 2px;
}

/* Center username */
.header-center {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
}
.username-badge {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 15px;
  color: var(--text-primary);
  white-space: nowrap;
  background: rgba(255,255,255,0.05);
  border-radius: 8px;
  padding: 4px 12px;
}

/* Right badge */
.header-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex: 1;
}
.result-badge {
  padding: 5px 14px;
  border-radius: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.5px;
  white-space: nowrap;
}
.result-badge.win     { background: rgba(74,222,128,0.12); border: 1px solid rgba(74,222,128,0.35); color: #4ADE80; }
.result-badge.loss    { background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.35); color: #F87171; }
.result-badge.push    { background: rgba(251,191,36,0.12); border: 1px solid rgba(251,191,36,0.35); color: #FBBF24; }
.result-badge.jackpot { background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); color: #FFDA50; }
.result-badge.blackjack { background: rgba(212,175,55,0.15); border: 1px solid rgba(212,175,55,0.4); color: #FFDA50; }
.result-badge.active  { background: rgba(212,175,55,0.1); border: 1px solid rgba(212,175,55,0.3); color: #D4AF37; }

/* ── Gold divider ── */
.gold-divider {
  height: 1px;
  margin: 0 20px;
  background: linear-gradient(90deg, transparent, rgba(212,175,55,0.3) 15%, rgba(212,175,55,0.3) 85%, transparent);
}

/* ── Data grid (4 cells) ── */
.data-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  padding: 12px 20px;
}
.data-cell {
  background: rgba(255,255,255,0.03);
  border-radius: 8px;
  padding: 10px 12px;
  text-align: center;
  border-top: 1px solid rgba(255,255,255,0.06);
  border-left: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(0,0,0,0.3);
  border-right: 1px solid rgba(0,0,0,0.2);
}
.data-label {
  font-family: 'Outfit', sans-serif;
  font-weight: 700;
  font-size: 12px;
  color: var(--gold-dim);
  letter-spacing: 1.5px;
  margin-bottom: 4px;
  text-transform: uppercase;
}
.data-value {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 18px;
  color: var(--text-primary);
}
.data-value.green { color: var(--win); }
.data-value.red   { color: var(--loss); }
.data-value.amber { color: var(--push); }
.data-value.gold  { color: var(--gold); }

/* ── Footer ── */
.footer {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 10px 20px 14px;
}
.footer-balance {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 800;
  font-size: 20px;
}
.footer-balance .label {
  color: var(--gold);
}
.footer-balance .amount {
  color: var(--text-primary);
}
"""


# ── Shared HTML builders ─────────────────────────────────────────────────────

def _build_header_html(
    icon: str,
    title: str,
    players: list[str],
    outcome: str,
    badge_text: str,
    txn_id: Optional[str] = None,
    subtitle: str = "FLOW Casino",
) -> str:
    """Build the shared card header: icon+title (left), username(s) (center), badge (right)."""
    txn_html = f'<div class="txn-id">TXN #{_esc(txn_id)}</div>' if txn_id else ""

    # Build badge-style player list
    if players:
        player_badges = " ".join(
            f'<span class="username-badge">{_esc(p)}</span>'
            for p in players
        )
        players_html = player_badges
    else:
        players_html = ""

    return f"""
    <div class="header">
      <div class="header-left">
        <div class="game-icon-pill">{icon}</div>
        <div class="game-title-group">
          <div class="game-title">{_esc(title)}</div>
          <div class="game-subtitle">{_esc(subtitle)}</div>
          {txn_html}
        </div>
      </div>
      <div class="header-center">
        {players_html}
      </div>
      <div class="header-right">
        <div class="result-badge {_esc(outcome)}">{_esc(badge_text)}</div>
      </div>
    </div>"""


def _build_data_grid_html(
    wager: int,
    payout: int,
    balance: int,
) -> str:
    """Build the 4-cell data grid: Wager, Payout, P&L, Balance."""
    pl = payout - wager
    pl_color = "green" if pl > 0 else "red" if pl < 0 else "amber"
    pl_str = f"+${pl:,}" if pl > 0 else f"-${abs(pl):,}" if pl < 0 else "$0"
    payout_color = "green" if payout > wager else "red" if payout < wager else ""

    return f"""
    <div class="data-grid">
      <div class="data-cell">
        <div class="data-label">Wager</div>
        <div class="data-value">${wager:,}</div>
      </div>
      <div class="data-cell">
        <div class="data-label">Payout</div>
        <div class="data-value {payout_color}">${payout:,}</div>
      </div>
      <div class="data-cell">
        <div class="data-label">P&amp;L</div>
        <div class="data-value {pl_color}">{pl_str}</div>
      </div>
      <div class="data-cell">
        <div class="data-label">Balance</div>
        <div class="data-value">${balance:,}</div>
      </div>
    </div>"""


def _build_footer_html(balance: int) -> str:
    """Build the centered footer: Balance: $X,XXX."""
    return f"""
    <div class="footer">
      <div class="footer-balance">
        <span class="label">Balance:</span>
        <span class="amount"> ${balance:,}</span>
      </div>
    </div>"""


def _wrap_card(status_class: str, content: str) -> str:
    """Wrap game content in the full card HTML with CSS."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>{_base_css()}</style>
</head>
<body>
<div class="card">
  <div class="status-bar {_esc(status_class)}"></div>
  {content}
</div>
</body>
</html>"""


# ── Render helper (Playwright HTML → PNG) ────────────────────────────────────

async def _render_card_html(html: str, width: int = 720) -> bytes:
    """Render an HTML string to PNG bytes using the shared Playwright browser."""
    from card_renderer import _get_browser

    browser = await _get_browser()
    page = await browser.new_page(viewport={"width": width, "height": 1200})
    try:
        await page.set_content(html, wait_until="networkidle")

        card = await page.query_selector(".card")
        clip = None
        if card:
            box = await card.bounding_box()
            if box:
                await page.set_viewport_size({
                    "width": width,
                    "height": int(box["height"]) + 4,
                })
                clip = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                }

        return await page.screenshot(clip=clip, type="png")
    finally:
        await page.close()


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
              <img src="{src}" alt="{_esc(value)}{_esc(suit)}">
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
          {_esc(status)}
        </div>"""

    header = _build_header_html("♠", "BLACKJACK", [player_name], outcome, badge_text, txn_id)
    data_grid = _build_data_grid_html(wager, payout, balance) if status else ""
    footer = _build_footer_html(balance)

    game_css = """
    <style>
    /* Blackjack-specific styles */
    .bj-section {
      padding: 16px 20px 8px;
    }
    .bj-label {
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      font-size: 14px;
      color: var(--gold);
      letter-spacing: 2px;
      text-transform: uppercase;
      margin-bottom: 4px;
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
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 16px;
      color: var(--text-primary);
      margin-left: 10px;
      vertical-align: middle;
    }
    .bj-cards {
      display: flex;
      gap: 4px;
      padding: 10px 20px;
      justify-content: center;
    }
    .playing-card {
      flex-shrink: 0;
      filter: drop-shadow(0 4px 8px rgba(0,0,0,0.5));
    }
    .playing-card img {
      width: 100px;
      height: auto;
      border-radius: 6px;
    }
    .result-banner {
      text-align: center;
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 28px;
      padding: 12px 0;
      letter-spacing: 1px;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    <div class="gold-divider"></div>

    <!-- Dealer -->
    <div class="bj-section">
      <span class="bj-label">DEALER</span>
      <span class="bj-score">{_esc(dealer_score_display)}</span>
    </div>
    <div class="bj-cards">{dealer_cards}</div>

    {result_banner}

    <!-- Player -->
    <div class="bj-section">
      <span class="bj-label">PLAYER</span>
      <span class="bj-score">{_esc(str(player_score))}</span>
    </div>
    <div class="bj-cards">{player_cards}</div>

    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}"""

    return _wrap_card(outcome, content)


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
) -> bytes:
    """Render a blackjack card to PNG bytes."""
    html = _build_blackjack_html(
        dealer_hand, player_hand, dealer_score, player_score,
        hide_dealer, status, wager, payout, balance, player_name, txn_id,
    )
    return await _render_card_html(html)


# ══════════════════════════════════════════════════════════════════════════════
#  SLOTS RENDERER
# ══════════════════════════════════════════════════════════════════════════════

# Fallback emoji for when slot icon PNGs aren't available
_SLOT_EMOJI_FALLBACK = {
    "shield": "🛡️", "crown": "👑", "trophy": "🏆",
    "football": "🏈", "star": "⭐", "coin": "🪙",
}

_SLOT_TIER_COLORS = {
    "jackpot": "#FFDA50",
    "legend": "#D4AF37",
    "epic": "#C084FC",
    "rare": "#60A5FA",
    "common": "#e8e0d0",
    "base": "#888",
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

    header = _build_header_html("🎰", "SLOTS", [player_name], outcome, badge_text, txn_id)

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
        result_html = f'<div class="slots-result" style="color: {result_color};">{_esc(result_msg)}</div>'

    data_grid = _build_data_grid_html(wager, payout, balance) if revealed == 3 else ""
    footer = _build_footer_html(balance)

    game_css = """
    <style>
    .reels-container {
      display: flex;
      justify-content: center;
      gap: 16px;
      padding: 24px 20px;
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
      font-family: 'JetBrains Mono', monospace;
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
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 18px;
      padding: 0 20px 12px;
      letter-spacing: 0.5px;
    }
    </style>"""

    content = f"""
    {game_css}
    {header}
    <div class="gold-divider"></div>

    <div class="reels-container">
      {reels_html}
      {payline}
    </div>

    {result_html}

    <div class="gold-divider"></div>
    {data_grid}
    {"<div class='gold-divider'></div>" if data_grid else ""}
    {footer}"""

    return _wrap_card(outcome, content)


async def render_slots_card(
    reels: list[str],
    revealed: int = 3,
    wager: int = 0,
    payout: int = 0,
    balance: int = 0,
    result_msg: str = "",
    player_name: str = "Player",
    txn_id: Optional[str] = None,
) -> bytes:
    """Render a slots card to PNG bytes."""
    html = _build_slots_html(
        reels, revealed, wager, payout, balance, result_msg, player_name, txn_id,
    )
    return await _render_card_html(html)


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

    display_players = players or [player_name]
    header = _build_header_html("🚀", "CRASH", display_players, outcome, badge_text, txn_id)

    # Altitude gauge
    gauge_mult = cashout_mult if (cashed_out and cashout_mult is not None) else current_mult
    max_gauge = 25.0
    fill_pct = min(100.0, max(0.0, math.log(max(gauge_mult, 1.001)) / math.log(max_gauge) * 100))

    if cashed_out:
        fill_gradient = "linear-gradient(to top, #4ADE80, #22C55E)"
    elif crashed:
        fill_gradient = "linear-gradient(to top, #F87171, #EF4444)"
    else:
        fill_gradient = "linear-gradient(to top, #4ADE80, #D4AF37, #F87171)"

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
                pill_color = "#FCA5A5"
            elif h < 10.0:
                pill_color = "#4ADE80"
            else:
                pill_color = "#FFDA50"
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
        data_section = _build_data_grid_html(wager, payout, balance)

    footer = _build_footer_html(balance)

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
      width: 8px;
      flex: 1;
      background: rgba(255,255,255,0.06);
      border-radius: 4px;
      position: relative;
      overflow: visible;
    }
    .gauge-fill {
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      border-radius: 4px;
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
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 13px;
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
      font-size: 56px;
      transform: rotate(-15deg);
      filter: drop-shadow(0 0 12px rgba(212,175,55,0.5));
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
      gap: 4px;
    }
    .mult-value {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 64px;
      line-height: 1;
      letter-spacing: -2px;
    }
    .mult-sub {
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: 2px;
      text-transform: uppercase;
      opacity: 0.7;
    }
    .profit-pill {
      margin-top: 8px;
      padding: 4px 14px;
      border-radius: 20px;
      background: rgba(74,222,128,0.12);
      border: 1px solid rgba(74,222,128,0.3);
      color: var(--win);
      font-family: 'JetBrains Mono', monospace;
      font-weight: 700;
      font-size: 13px;
    }

    /* History */
    .history-row {
      display: flex;
      justify-content: center;
      gap: 8px;
      padding: 8px 20px 12px;
      flex-wrap: wrap;
    }
    .history-pill {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 700;
      font-size: 13px;
      padding: 3px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
    }
    .history-pill.current-pill {
      color: #fff !important;
      border: 1px solid currentColor;
      box-shadow: 0 0 8px currentColor;
    }

    /* Live info */
    .live-info {
      display: flex;
      justify-content: center;
      gap: 24px;
      padding: 12px 20px;
    }
    .live-stat {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 700;
      font-size: 14px;
      color: var(--text-muted);
    }
    </style>"""

    mult_display_val = cashout_mult if cashed_out else current_mult

    content = f"""
    {game_css}
    {header}
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

    <div class="gold-divider"></div>
    {data_section}
    <div class="gold-divider"></div>
    {footer}"""

    return _wrap_card(outcome, content)


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
) -> bytes:
    """Render a crash card to PNG bytes."""
    html = _build_crash_html(
        current_mult, crashed, cashed_out, cashout_mult, history,
        players_in, total_wagered, wager, payout, balance,
        player_name, players, txn_id, is_live,
    )
    return await _render_card_html(html)


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

    header = _build_header_html("🪙", "COIN FLIP", display_players, outcome, badge_text, txn_id)

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
            <div class="pvp-name">{_esc(player_name)}</div>
            <div class="pvp-pick">{_esc(player_pick.upper())}</div>
          </div>
          <div class="pvp-vs">VS</div>
          <div class="pvp-player" style="color: {p2_color}; opacity: {p2_opacity};">
            <div class="pvp-name">{_esc(opponent_name or 'Opponent')}</div>
            <div class="pvp-pick">{_esc((opponent_pick or '').upper())}</div>
          </div>
        </div>"""
    else:
        pvp_html = f"""
        <div class="pick-display">
          <span style="color: {pick_color}; font-size: 20px;">{pick_icon}</span>
          <span class="pick-text">You picked <strong>{_esc(player_pick.upper())}</strong></span>
        </div>"""

    data_grid = _build_data_grid_html(wager, payout, balance)
    footer = _build_footer_html(balance)

    game_css = f"""
    <style>
    .coin-area {{
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 24px 20px 16px;
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
      box-shadow: 0 8px 24px rgba(0,0,0,0.4), inset 0 2px 4px rgba(255,255,255,0.3);
      transform: perspective(400px) rotateX(10deg);
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 56px;
      color: rgba(0,0,0,0.25);
    }}
    .coin-result {{
      text-align: center;
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 22px;
      color: var(--text-primary);
      margin-top: 12px;
      letter-spacing: 2px;
    }}
    .pick-display {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      margin-top: 8px;
    }}
    .pick-text {{
      font-family: 'Outfit', sans-serif;
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
      gap: 24px;
      margin-top: 12px;
    }}
    .pvp-player {{
      text-align: center;
    }}
    .pvp-name {{
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      font-size: 16px;
    }}
    .pvp-pick {{
      font-family: 'JetBrains Mono', monospace;
      font-weight: 700;
      font-size: 12px;
      margin-top: 2px;
      opacity: 0.7;
    }}
    .pvp-vs {{
      font-family: 'Outfit', sans-serif;
      font-weight: 800;
      font-size: 14px;
      color: var(--text-dim);
      letter-spacing: 2px;
    }}
    </style>"""

    content = f"""
    {game_css}
    {header}
    <div class="gold-divider"></div>

    <div class="coin-area">
      <div class="coin">{coin_letter}</div>
      <div class="coin-result">{result.upper()}</div>
      {pvp_html}
    </div>

    <div class="gold-divider"></div>
    {data_grid}
    <div class="gold-divider"></div>
    {footer}"""

    return _wrap_card(outcome, content)


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
) -> bytes:
    """Render a coin flip card to PNG bytes."""
    html = _build_coinflip_html(
        result, player_pick, wager, payout, balance, player_name, txn_id,
        is_pvp, opponent_name, opponent_pick,
    )
    return await _render_card_html(html)


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

    header = _build_header_html(
        icon="\U0001f3ab",  # 🎫
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

    footer = _build_footer_html(balance)

    game_css = """<style>
    .scratch-area {
      display: flex;
      justify-content: center;
      gap: 14px;
      padding: 20px 24px 10px;
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
      box-shadow: inset 0 2px 8px rgba(0,0,0,0.5), inset 0 -1px 2px rgba(255,255,255,0.03);
      /* Cross-hatch pattern */
      background-image:
        repeating-linear-gradient(
          45deg,
          transparent,
          transparent 8px,
          rgba(212,175,55,0.06) 8px,
          rgba(212,175,55,0.06) 9px
        ),
        repeating-linear-gradient(
          -45deg,
          transparent,
          transparent 8px,
          rgba(212,175,55,0.06) 8px,
          rgba(212,175,55,0.06) 9px
        );
    }
    .tile-mystery {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 42px;
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
      box-shadow: 0 0 20px rgba(212,175,55,0.35), inset 0 2px 8px rgba(0,0,0,0.4), inset 0 -1px 3px rgba(255,218,80,0.06);
    }
    .tile-value {
      font-family: 'JetBrains Mono', monospace;
      font-weight: 800;
      font-size: 32px;
      color: var(--text-primary);
      line-height: 1;
    }
    .scratch-tile.glow .tile-value {
      color: var(--gold-light);
    }
    .tile-label {
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      font-size: 11px;
      color: var(--gold-dim);
      letter-spacing: 2px;
      margin-top: 6px;
    }
    .scratch-result {
      text-align: center;
      padding: 8px 20px 14px;
      font-family: 'Outfit', sans-serif;
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

    return _wrap_card(status_class, content)


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
    return await _render_card_html(html)

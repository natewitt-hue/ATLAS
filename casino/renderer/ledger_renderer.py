"""
ledger_renderer.py — ATLAS Universal Ledger Slip Renderer (V2)
─────────────────────────────────────────────────────────────────────────────
Playwright HTML → PNG renderer for the #ledger channel.
Supports casino game results (4-col) and general transactions (3-col).
Reuses the browser singleton from card_renderer.py.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from atlas_html_engine import render_card as _engine_render_card, esc, _font_face_css

# ── Game metadata ─────────────────────────────────────────────────────────────
GAME_INFO = {
    "blackjack":    {"label": "BLACKJACK",   "icon": "\u2663"},
    "slots":        {"label": "SLOTS",       "icon": "\u2B50"},
    "crash":        {"label": "CRASH",       "icon": "\u25B2"},
    "coinflip":     {"label": "COIN FLIP",   "icon": "\u25CF"},
    "coinflip_pvp": {"label": "PVP FLIP",    "icon": "\u2694"},
    "scratch":      {"label": "SCRATCH",     "icon": "\u2605"},
}

# ── Source metadata (non-casino) ──────────────────────────────────────────────
SOURCE_INFO = {
    "CASINO":     {"label": "CASINO",      "icon": "\u2B50", "css_class": "casino"},
    "TSL_BET":    {"label": "SPORTSBOOK",  "icon": "SB",     "css_class": "sportsbook"},
    "ADMIN":      {"label": "ADMIN",       "icon": "\u26A1", "css_class": "admin"},
    "STIPEND":    {"label": "STIPEND",     "icon": "\uD83D\uDCB0", "css_class": "stipend"},
    "PREDICTION": {"label": "PREDICTION",  "icon": "\uD83D\uDCCA", "css_class": "prediction"},
    "REAL_BET":   {"label": "REAL BET",    "icon": "RB",     "css_class": "sportsbook"},
}

BIG_THRESHOLD = 500

# ── Shared CSS ────────────────────────────────────────────────────────────────

def _css() -> str:
    return _font_face_css() + """
* { margin: 0; padding: 0; box-sizing: border-box; }"""


_CSS_BODY = """\
body {
  background: transparent;
  font-family: var(--font-display), sans-serif;
  display: flex;
  justify-content: center;
  padding: 0;
}
.card {
  width: 700px;
  border-radius: var(--space-md);
  overflow: hidden;
  position: relative;
  background: #111111;
  border: 1px solid rgba(212,175,55,0.25);
}
.card::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at 10% 10%, rgba(212,175,55,0.06) 0%, transparent 50%),
    radial-gradient(ellipse at 90% 90%, rgba(212,175,55,0.04) 0%, transparent 50%);
  pointer-events: none;
  z-index: 1;
}
.card::after {
  content: '';
  position: absolute;
  inset: 0;
  opacity: 0.035;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: 2;
}
.card > * { position: relative; z-index: 3; }

/* Status bar */
.status-bar { height: 5px; width: 100%; }
.status-bar.win    { background: linear-gradient(90deg, #22C55E, #16A34A, #22C55E); }
.status-bar.loss   { background: linear-gradient(90deg, #EF4444, #DC2626, #EF4444); }
.status-bar.push   { background: linear-gradient(90deg, #F59E0B, #D97706, #F59E0B); }
.status-bar.credit { background: linear-gradient(90deg, #22C55E, #16A34A, #22C55E); }
.status-bar.debit  { background: linear-gradient(90deg, #EF4444, #DC2626, #EF4444); }
.status-bar.neutral{ background: linear-gradient(90deg, #D4AF37, #B8962E, #D4AF37); }

/* Header */
.header {
  display: flex; align-items: center;
  justify-content: space-between;
  padding: 14px 20px 10px;
}
.header-left { display: flex; align-items: center; gap: 10px; }
.source-icon {
  width: 26px; height: 26px; border-radius: 5px;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700;
}
.source-icon.casino     { background: rgba(212,175,55,0.2); color: #D4AF37; }
.source-icon.sportsbook { background: rgba(26,115,232,0.2); color: #5B9CF5; }
.source-icon.admin      { background: rgba(239,68,68,0.2); color: #EF4444; }
.source-icon.stipend    { background: rgba(34,197,94,0.2); color: #22C55E; }
.source-icon.prediction { background: rgba(168,85,247,0.2); color: #A855F7; }

.game-label {
  font-weight: 700; font-size: var(--font-lg); color: #e8e0d0; letter-spacing: 1.5px;
}

/* Badge */
.badge {
  padding: var(--space-xs) var(--space-md); border-radius: 6px;
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: var(--font-sm); letter-spacing: 0.5px;
}
.badge.win    { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35); color: #4ADE80; }
.badge.loss   { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: #F87171; }
.badge.push   { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: #FBBF24; }
.badge.credit { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35); color: #4ADE80; }
.badge.debit  { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: #F87171; }

/* Divider */
.divider {
  height: 1px; margin: 0 20px;
  background: linear-gradient(90deg, transparent, rgba(212,175,55,0.35), transparent);
}

/* Data grids */
.data-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: var(--space-sm); padding: var(--space-md) 20px;
}
.data-grid-3 {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: var(--space-sm); padding: var(--space-md) 20px;
}
.data-cell {
  background: rgba(255,255,255,0.03); border-radius: var(--border-radius); padding: 10px var(--space-md);
  border-top: 1px solid rgba(255,255,255,0.06);
  border-left: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(0,0,0,0.3);
  border-right: 1px solid rgba(0,0,0,0.2);
}
.data-label {
  font-weight: 600; font-size: 10px; color: #8C7324;
  letter-spacing: 1.5px; margin-bottom: var(--space-xs); text-transform: uppercase;
}
.data-value {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 16px; color: #e8e0d0;
}
.data-value.green { color: #4ADE80; }
.data-value.red   { color: #F87171; }
.data-value.amber { color: #FBBF24; }

/* Footer */
.footer {
  display: flex; justify-content: space-between; align-items: center;
  padding: var(--space-sm) 20px var(--space-md);
}
.footer-left { display: flex; align-items: center; gap: var(--space-lg); }
.footer-balance {
  font-family: var(--font-mono), monospace;
  font-weight: 700; font-size: 12px; color: #DCDCE6;
}
.footer-txn {
  font-family: var(--font-mono), monospace;
  font-weight: 400; font-size: 10px; color: #555;
}
.footer-time {
  font-family: var(--font-mono), monospace;
  font-weight: 400; font-size: var(--font-xs); color: #8C7324;
}

/* Highlight bar */
.highlight-bar {
  margin: 0 20px var(--space-sm); padding: 6px 14px; border-radius: 6px;
  font-weight: 700; font-size: 12px; letter-spacing: 1px; text-align: center;
}
.highlight-bar.win  { background: rgba(34,197,94,0.08); color: #4ADE80; border: 1px solid rgba(34,197,94,0.15); }
.highlight-bar.loss { background: rgba(239,68,68,0.08); color: #F87171; border: 1px solid rgba(239,68,68,0.15); }
.highlight-bar.push { background: rgba(245,158,11,0.08); color: #FBBF24; border: 1px solid rgba(245,158,11,0.15); }

/* Description row */
.desc-row { padding: 6px 20px 2px; font-size: var(--font-sm); color: #aaa; }
"""


def _esc(text: str) -> str:
    """HTML-escape user-controlled text."""
    return esc(text)


def _pl_color(value: int) -> str:
    if value > 0:
        return "green"
    elif value < 0:
        return "red"
    return "amber"


def _amount_color(value: int) -> str:
    if value > 0:
        return "green"
    elif value < 0:
        return "red"
    return ""


def _format_amount(value: int) -> str:
    if value > 0:
        return f"+{value:,}"
    return f"{value:,}"


def _build_casino_html(
    player_name: str,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: Optional[int] = None,
) -> str:
    """Build HTML for a casino game ledger slip (4-column)."""
    game = GAME_INFO.get(game_type, {"label": game_type.upper(), "icon": "\u2B22"})
    pl = payout - wager
    is_big = wager >= BIG_THRESHOLD

    mult_str = (
        f"{multiplier:.1f}x" if multiplier != int(multiplier)
        else f"{int(multiplier)}x"
    )
    time_str = datetime.now(timezone.utc).strftime("%I:%M %p")
    txn_str = f"TXN #{txn_id}" if txn_id else ""

    # Highlight bar for big wagers
    highlight_html = ""
    if is_big:
        if outcome == "win":
            hl_text = f"MASSIVE WIN \u2014 {payout:,} BUCKS PAYOUT"
        elif outcome == "loss":
            hl_text = f"HIGH ROLLER LOSS \u2014 {wager:,} BUCKS GONE"
        else:
            hl_text = f"HIGH STAKES PUSH \u2014 {wager:,} BUCKS RETURNED"
        highlight_html = f'<div class="highlight-bar {outcome}">{_esc(hl_text)}</div>'

    payout_color = "green" if payout > 0 and outcome == "win" else ""

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>{_css()}{_CSS_BODY}</style></head>
<body>
<div class="card">
  <div class="status-bar {outcome}"></div>
  <div class="header">
    <div class="header-left">
      <div class="source-icon casino">{_esc(game["icon"])}</div>
      <span class="game-label">{_esc(game["label"])}</span>
    </div>
    <div class="badge {outcome}">{_esc(outcome.upper())} {_esc(mult_str)}</div>
  </div>
  <div class="divider"></div>
  <div class="data-grid">
    <div class="data-cell">
      <div class="data-label">Player</div>
      <div class="data-value">{_esc(player_name)}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Wager</div>
      <div class="data-value">{wager:,}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Payout</div>
      <div class="data-value {payout_color}">{payout:,}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">P&amp;L</div>
      <div class="data-value {_pl_color(pl)}">{_format_amount(pl)}</div>
    </div>
  </div>
  {highlight_html}
  <div class="divider"></div>
  <div class="footer">
    <div class="footer-left">
      <span class="footer-balance">BAL: {new_balance:,}</span>
      <span class="footer-txn">{_esc(txn_str)}</span>
    </div>
    <span class="footer-time">{_esc(time_str)}</span>
  </div>
</div>
</body></html>"""


def _build_transaction_html(
    source: str,
    player_name: str,
    amount: int,
    balance_after: int,
    description: str = "",
    txn_id: Optional[int] = None,
) -> str:
    """Build HTML for a non-casino transaction slip (3-column + description)."""
    info = SOURCE_INFO.get(source, SOURCE_INFO.get("ADMIN"))
    assert info is not None

    status_class = "credit" if amount > 0 else "debit" if amount < 0 else "neutral"
    badge_class = "credit" if amount >= 0 else "debit"
    time_str = datetime.now(timezone.utc).strftime("%I:%M %p")
    txn_str = f"TXN #{txn_id}" if txn_id else ""

    # Amount label varies by source
    amount_label = "Amount"
    if source == "PREDICTION":
        amount_label = "Cost" if amount < 0 else "Payout"

    desc_html = ""
    if description:
        desc_html = f'<div class="desc-row">{_esc(description)}</div>'

    return f"""\
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>{_css()}{_CSS_BODY}</style></head>
<body>
<div class="card">
  <div class="status-bar {status_class}"></div>
  <div class="header">
    <div class="header-left">
      <div class="source-icon {info['css_class']}">{_esc(info["icon"])}</div>
      <span class="game-label">{_esc(info["label"])}</span>
    </div>
    <div class="badge {badge_class}">{_format_amount(amount)}</div>
  </div>
  <div class="divider"></div>
  {desc_html}
  <div class="data-grid-3">
    <div class="data-cell">
      <div class="data-label">Player</div>
      <div class="data-value">{_esc(player_name)}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">{_esc(amount_label)}</div>
      <div class="data-value {_amount_color(amount)}">{_format_amount(amount)}</div>
    </div>
    <div class="data-cell">
      <div class="data-label">Balance</div>
      <div class="data-value">{balance_after:,}</div>
    </div>
  </div>
  <div class="divider"></div>
  <div class="footer">
    <div class="footer-left">
      <span class="footer-balance">BAL: {balance_after:,}</span>
      <span class="footer-txn">{_esc(txn_str)}</span>
    </div>
    <span class="footer-time">{_esc(time_str)}</span>
  </div>
</div>
</body></html>"""


# ── Rendering ─────────────────────────────────────────────────────────────────

async def _render_html_to_png(html_content: str) -> bytes:
    """Render HTML string to PNG bytes via unified engine."""
    return await _engine_render_card(html_content, width=720)


async def render_ledger_card(
    player_name: str,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: Optional[int] = None,
) -> bytes:
    """
    Render a casino game ledger slip as a PNG.
    Returns PNG bytes ready for discord.File().
    """
    html_content = _build_casino_html(
        player_name, game_type, wager, outcome,
        payout, multiplier, new_balance, txn_id,
    )
    return await _render_html_to_png(html_content)


async def render_transaction_slip(
    source: str,
    player_name: str,
    amount: int,
    balance_after: int,
    description: str = "",
    txn_id: Optional[int] = None,
) -> bytes:
    """
    Render a general transaction slip as a PNG.
    For non-casino sources: sportsbook, admin, stipend, prediction.
    Returns PNG bytes ready for discord.File().
    """
    html_content = _build_transaction_html(
        source, player_name, amount, balance_after, description, txn_id,
    )
    return await _render_html_to_png(html_content)

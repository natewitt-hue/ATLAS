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

from atlas_html_engine import render_card as _engine_render_card, wrap_card, esc

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

# ── Ledger-specific CSS (layered on top of engine shared CSS) ─────────────────

_LEDGER_CSS = """\
<style>
  /* Ledger card overrides */
  .card {
    width: var(--card-width);
    border-radius: var(--space-md);
    border: 1px solid rgba(212,175,55,0.25);
  }

  /* Gold radial glow overlay */
  .ledger-glow {
    position: absolute;
    inset: 0;
    background:
      radial-gradient(ellipse at 10% 10%, rgba(212,175,55,0.06) 0%, transparent 50%),
      radial-gradient(ellipse at 90% 90%, rgba(212,175,55,0.04) 0%, transparent 50%);
    pointer-events: none;
    z-index: 1;
  }

  /* Extra status bar classes for ledger */
  .status-bar.credit { background: linear-gradient(90deg, var(--win-dark), var(--win-dark), var(--win-dark)); }
  .status-bar.debit  { background: linear-gradient(90deg, var(--loss-dark), var(--loss-dark), var(--loss-dark)); }
  .status-bar.neutral{ background: linear-gradient(90deg, var(--gold), var(--gold-dim), var(--gold)); }

  /* Header */
  .ledger-header {
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
  .source-icon.casino     { background: rgba(212,175,55,0.2); color: var(--gold); }
  .source-icon.sportsbook { background: rgba(26,115,232,0.2); color: var(--blue-light); }
  .source-icon.admin      { background: rgba(239,68,68,0.2); color: var(--loss-dark); }
  .source-icon.stipend    { background: rgba(34,197,94,0.2); color: var(--win-dark); }
  .source-icon.prediction { background: rgba(168,85,247,0.2); color: var(--purple); }

  .game-label {
    font-weight: 700; font-size: var(--font-lg); color: var(--text-primary); letter-spacing: 1.5px;
  }

  /* Badge */
  .badge {
    padding: var(--space-xs) var(--space-md); border-radius: 6px;
    font-family: var(--font-mono), monospace;
    font-weight: 700; font-size: var(--font-sm); letter-spacing: 0.5px;
  }
  .badge.win    { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35); color: var(--win); }
  .badge.loss   { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: var(--loss); }
  .badge.push   { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: var(--push); }
  .badge.credit { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35); color: var(--win); }
  .badge.debit  { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: var(--loss); }

  /* Divider */
  .ledger-divider {
    height: 1px; margin: 0 20px;
    background: linear-gradient(90deg, transparent, rgba(212,175,55,0.35), transparent);
  }

  /* Data grids */
  .ledger-grid {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: var(--space-sm); padding: var(--space-md) 20px;
  }
  .ledger-grid-3 {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: var(--space-sm); padding: var(--space-md) 20px;
  }
  .ledger-cell {
    background: rgba(255,255,255,0.03); border-radius: var(--border-radius); padding: 10px var(--space-md);
    border-top: 1px solid rgba(255,255,255,0.06);
    border-left: 1px solid rgba(255,255,255,0.04);
    border-bottom: 1px solid rgba(0,0,0,0.3);
    border-right: 1px solid rgba(0,0,0,0.2);
  }
  .ledger-label {
    font-weight: 600; font-size: 10px; color: var(--gold-dim);
    letter-spacing: 1.5px; margin-bottom: var(--space-xs); text-transform: uppercase;
  }
  .ledger-value {
    font-family: var(--font-mono), monospace;
    font-weight: 700; font-size: 16px; color: var(--text-primary);
  }
  .ledger-value.green { color: var(--win); }
  .ledger-value.red   { color: var(--loss); }
  .ledger-value.amber { color: var(--push); }

  /* Footer */
  .ledger-footer {
    display: flex; justify-content: space-between; align-items: center;
    padding: var(--space-sm) 20px var(--space-md);
  }
  .footer-left { display: flex; align-items: center; gap: var(--space-lg); }
  .footer-balance {
    font-family: var(--font-mono), monospace;
    font-weight: 700; font-size: 12px; color: var(--text-light);
  }
  .footer-txn {
    font-family: var(--font-mono), monospace;
    font-weight: 400; font-size: 10px; color: var(--text-dim);
  }
  .footer-time {
    font-family: var(--font-mono), monospace;
    font-weight: 400; font-size: var(--font-xs); color: var(--gold-dim);
  }

  /* Highlight bar */
  .highlight-bar {
    margin: 0 20px var(--space-sm); padding: 6px 14px; border-radius: 6px;
    font-weight: 700; font-size: 12px; letter-spacing: 1px; text-align: center;
  }
  .highlight-bar.win  { background: rgba(34,197,94,0.08); color: var(--win); border: 1px solid rgba(34,197,94,0.15); }
  .highlight-bar.loss { background: rgba(239,68,68,0.08); color: var(--loss); border: 1px solid rgba(239,68,68,0.15); }
  .highlight-bar.push { background: rgba(245,158,11,0.08); color: var(--push); border: 1px solid rgba(245,158,11,0.15); }

  /* Description row */
  .desc-row { padding: 6px 20px 2px; font-size: var(--font-sm); color: var(--text-sub); }
</style>
"""


# Use esc() directly from atlas_html_engine — kept as alias for grep-ability
_esc = esc


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


def _commentary_html(commentary: str) -> str:
    """Build an optional ATLAS voice commentary block (gold left-border, italic)."""
    if not commentary:
        return ""
    return (
        f'<div style="margin:var(--space-md) 0 0;padding:10px 14px;'
        f'border-radius:var(--border-radius);background:rgba(255,255,255,0.03);'
        f'border-left:3px solid rgba(212,175,55,0.5);">'
        f'<div style="font-size:12px;font-style:italic;color:var(--text-warm);'
        f'line-height:1.45;">{_esc(commentary)}</div>'
        f'</div>'
    )


def _build_casino_html(
    player_name: str,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: Optional[int] = None,
    theme_id: str | None = None,
    commentary: str = "",
) -> str:
    """Build HTML for a casino game ledger slip (4-column)."""
    game = GAME_INFO.get(game_type, {"label": game_type.upper(), "icon": "\u2B22"})
    pl = payout - wager
    is_big = wager >= BIG_THRESHOLD

    mult_str = (
        f"{multiplier:.1f}x" if multiplier != int(multiplier)
        else f"{int(multiplier)}x"
    )
    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
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

    body = f"""{_LEDGER_CSS}
  <div class="ledger-glow"></div>
  <div class="ledger-header">
    <div class="header-left">
      <div class="source-icon casino">{_esc(game["icon"])}</div>
      <span class="game-label">{_esc(game["label"])}</span>
    </div>
    <div class="badge {outcome}">{_esc(outcome.upper())} {_esc(mult_str)}</div>
  </div>
  <div class="ledger-divider"></div>
  <div class="ledger-grid">
    <div class="ledger-cell">
      <div class="ledger-label">Player</div>
      <div class="ledger-value">{_esc(player_name)}</div>
    </div>
    <div class="ledger-cell">
      <div class="ledger-label">Wager</div>
      <div class="ledger-value">{wager:,}</div>
    </div>
    <div class="ledger-cell">
      <div class="ledger-label">Payout</div>
      <div class="ledger-value {payout_color}">{payout:,}</div>
    </div>
    <div class="ledger-cell">
      <div class="ledger-label">P&amp;L</div>
      <div class="ledger-value {_pl_color(pl)}">{_format_amount(pl)}</div>
    </div>
  </div>
  {highlight_html}
  {_commentary_html(commentary)}
  <div class="ledger-divider"></div>
  <div class="ledger-footer">
    <div class="footer-left">
      <span class="footer-balance">BAL: {new_balance:,}</span>
      <span class="footer-txn">{_esc(txn_str)}</span>
    </div>
    <span class="footer-time">{_esc(time_str)}</span>
  </div>"""

    return wrap_card(body, outcome, theme_id=theme_id)


def _build_transaction_html(
    source: str,
    player_name: str,
    amount: int,
    balance_after: int,
    description: str = "",
    txn_id: Optional[int] = None,
    theme_id: str | None = None,
    commentary: str = "",
) -> str:
    """Build HTML for a non-casino transaction slip (3-column + description)."""
    info = SOURCE_INFO.get(source, SOURCE_INFO.get("ADMIN"))
    if info is None:
        raise ValueError(f"Unknown ledger source: {source!r}")

    status_class = "credit" if amount > 0 else "debit" if amount < 0 else "neutral"
    badge_class = "credit" if amount >= 0 else "debit"
    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    txn_str = f"TXN #{txn_id}" if txn_id else ""

    # Amount label varies by source
    amount_label = "Amount"
    if source == "PREDICTION":
        amount_label = "Cost" if amount < 0 else "Payout"

    desc_html = ""
    if description:
        desc_html = f'<div class="desc-row">{_esc(description)}</div>'

    body = f"""{_LEDGER_CSS}
  <div class="ledger-glow"></div>
  <div class="ledger-header">
    <div class="header-left">
      <div class="source-icon {info['css_class']}">{_esc(info["icon"])}</div>
      <span class="game-label">{_esc(info["label"])}</span>
    </div>
    <div class="badge {badge_class}">{_format_amount(amount)}</div>
  </div>
  <div class="ledger-divider"></div>
  {desc_html}
  <div class="ledger-grid-3">
    <div class="ledger-cell">
      <div class="ledger-label">Player</div>
      <div class="ledger-value">{_esc(player_name)}</div>
    </div>
    <div class="ledger-cell">
      <div class="ledger-label">{_esc(amount_label)}</div>
      <div class="ledger-value {_amount_color(amount)}">{_format_amount(amount)}</div>
    </div>
    <div class="ledger-cell">
      <div class="ledger-label">Balance</div>
      <div class="ledger-value">{balance_after:,}</div>
    </div>
  </div>
  {_commentary_html(commentary)}
  <div class="ledger-divider"></div>
  <div class="ledger-footer">
    <div class="footer-left">
      <span class="footer-balance">BAL: {balance_after:,}</span>
      <span class="footer-txn">{_esc(txn_str)}</span>
    </div>
    <span class="footer-time">{_esc(time_str)}</span>
  </div>"""

    return wrap_card(body, status_class, theme_id=theme_id)


# ── Rendering ─────────────────────────────────────────────────────────────────

async def _render_html_to_png(html_content: str) -> bytes:
    """Render HTML string to PNG bytes via unified engine."""
    return await _engine_render_card(html_content)


async def render_ledger_card(
    player_name: str,
    game_type: str,
    wager: int,
    outcome: str,
    payout: int,
    multiplier: float,
    new_balance: int,
    txn_id: Optional[int] = None,
    theme_id: str | None = None,
    commentary: str = "",
) -> bytes:
    """
    Render a casino game ledger slip as a PNG.
    Returns PNG bytes ready for discord.File().
    """
    html_content = _build_casino_html(
        player_name, game_type, wager, outcome,
        payout, multiplier, new_balance, txn_id,
        theme_id=theme_id, commentary=commentary,
    )
    return await _render_html_to_png(html_content)


async def render_transaction_slip(
    source: str,
    player_name: str,
    amount: int,
    balance_after: int,
    description: str = "",
    txn_id: Optional[int] = None,
    theme_id: str | None = None,
    commentary: str = "",
) -> bytes:
    """
    Render a general transaction slip as a PNG.
    For non-casino sources: sportsbook, admin, stipend, prediction.
    Returns PNG bytes ready for discord.File().
    """
    html_content = _build_transaction_html(
        source, player_name, amount, balance_after, description, txn_id,
        theme_id=theme_id, commentary=commentary,
    )
    return await _render_html_to_png(html_content)

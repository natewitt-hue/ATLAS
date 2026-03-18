"""
sportsbook_cards.py — Sportsbook Card Builders
═══════════════════════════════════════════════════════════════════════════════
Uses the ATLAS HTML engine to build the main sportsbook hub card and the
stats/profile card. These functions query the sportsbook DB and return
PNG bytes ready for discord.py.

Integration:
    from sportsbook_cards import build_sportsbook_card, build_stats_card, card_to_file

    png = await build_sportsbook_card(user_id=interaction.user.id)
    file = card_to_file(png)
═══════════════════════════════════════════════════════════════════════════════
"""

import io
import sqlite3

import discord

from atlas_html_engine import render_card, wrap_card, esc
from card_data import (
    DB_PATH, STARTING_BALANCE, _STATUS_MAP,
    _get_balance, _get_season_start_balance, _get_weekly_delta,
    _get_sparkline_data, _get_lifetime_record, _get_total_wagered,
    _get_leaderboard_rank, _determine_status, _sparkline_svg,
)


# ═════════════════════════════════════════════════════════════════════════════
#  DATA QUERIES (sportsbook-specific)
# ═════════════════════════════════════════════════════════════════════════════

def _get_last_n_results(user_id: int, n: int = 5) -> tuple[list, str]:
    """Get last N bet results as (list_of_bool_or_none, record_string).
    True=Win, False=Loss, None=Push."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """SELECT status FROM bets_table
               WHERE discord_id = ? AND status IN ('Won', 'Lost', 'Push')
               AND parlay_id IS NULL
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, n)
        ).fetchall()
    results = []
    w, l, p = 0, 0, 0
    for (status,) in reversed(rows):  # Reverse to show oldest→newest
        if status == 'Won':
            results.append(True)
            w += 1
        elif status == 'Lost':
            results.append(False)
            l += 1
        else:
            results.append(None)
            p += 1
    record = f"{w}-{l}-{p}" if p else f"{w}-{l}"
    return results, record


def _get_open_bets(user_id: int) -> tuple[int, int, int]:
    """Get (count, total_wagered, potential_payout) for pending bets."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """SELECT wager_amount, odds FROM bets_table
               WHERE discord_id = ? AND status = 'Pending'
               AND parlay_id IS NULL""",
            (user_id,)
        ).fetchall()
        # Also include parlays
        parlay_rows = con.execute(
            """SELECT wager_amount, combined_odds FROM parlays_table
               WHERE discord_id = ? AND status = 'Pending'""",
            (user_id,)
        ).fetchall()

    count = len(rows) + len(parlay_rows)
    wagered = sum(r[0] for r in rows) + sum(r[0] for r in parlay_rows)

    # Calculate potential payout
    payout = 0
    for wager, odds in rows:
        if odds == 0:
            payout += wager  # push-equivalent: return wager if odds are zero
        elif odds > 0:
            payout += wager + int(wager * odds / 100)
        else:
            payout += wager + int(wager * 100 / abs(odds))
    for wager, odds in parlay_rows:
        if odds == 0:
            payout += wager  # push-equivalent: return wager if odds are zero
        elif odds > 0:
            payout += wager + int(wager * odds / 100)
        else:
            payout += wager + int(wager * 100 / abs(odds))

    return count, wagered, payout


def _get_total_won(user_id: int) -> int:
    """Sum of all payouts received."""
    with sqlite3.connect(DB_PATH) as con:
        # Calculate from winning bets
        rows = con.execute(
            """SELECT wager_amount, odds FROM bets_table
               WHERE discord_id = ? AND status = 'Won' AND parlay_id IS NULL""",
            (user_id,)
        ).fetchall()
        parlay_rows = con.execute(
            """SELECT wager_amount, combined_odds FROM parlays_table
               WHERE discord_id = ? AND status = 'Won'""",
            (user_id,)
        ).fetchall()

    total = 0
    for wager, odds in rows:
        if odds > 0:
            total += int(wager * odds / 100)
        else:
            total += int(wager * 100 / abs(odds))
    for wager, odds in parlay_rows:
        if odds > 0:
            total += int(wager * odds / 100)
        else:
            total += int(wager * 100 / abs(odds))
    return total


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED CSS FOR SPORTSBOOK CARDS
# ═════════════════════════════════════════════════════════════════════════════

_SPORTSBOOK_CSS = """\
.hero-section { padding: 20px; text-align: center; }
.hero-label { font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; }
.hero-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-display-size); color: var(--text-primary); }
.hero-delta { font-family: var(--font-mono); font-weight: 600; font-size: var(--font-sm); margin-top: 4px; }
.hero-delta.positive { color: var(--win); }
.hero-delta.negative { color: var(--loss); }

.sparkline-section { padding: 0 20px 12px; text-align: center; }
.sparkline-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; }

.ticker-section { padding: 4px 20px 12px; text-align: center; }
.ticker-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; margin-bottom: 6px; }
.ticker-dots { display: flex; justify-content: center; gap: 6px; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.dot.win { background: var(--win); }
.dot.loss { background: var(--loss); }
.dot.push { background: var(--push); }

.info-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.info-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 12px; }
.info-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.info-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-xl); color: var(--text-primary); margin-top: 4px; }
.info-value.green { color: var(--win); }
.info-sub { font-family: var(--font-display); font-weight: 600; font-size: 11px; color: var(--text-muted); margin-top: 2px; }

.sport-footer { display: flex; justify-content: center; gap: 8px; padding: 8px 20px 14px; }
.sport-pill { font-family: var(--font-display); font-weight: 700; font-size: 10px; letter-spacing: 1px; padding: 4px 12px; border-radius: 12px; color: var(--text-dim); background: rgba(255,255,255,0.03); }
.sport-pill.active { color: var(--gold); background: rgba(212,175,55,0.12); border: 1px solid rgba(212,175,55,0.25); }

.stat-grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.stat-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.stat-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 4px; }
.stat-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-lg); color: var(--text-primary); }
.stat-value.green { color: var(--win); }
.stat-value.red { color: var(--loss); }
.stat-value.gold { color: var(--gold); }
"""

# ═════════════════════════════════════════════════════════════════════════════
#  WIN/LOSS TICKER HTML HELPER
# ═════════════════════════════════════════════════════════════════════════════

def _ticker_html(results: list, record: str, label_prefix: str = "LAST 5") -> str:
    """Build win/loss ticker dots HTML."""
    if not results:
        return ""
    dots = []
    for r in results:
        if r is True:
            dots.append('<span class="dot win"></span>')
        elif r is False:
            dots.append('<span class="dot loss"></span>')
        else:
            dots.append('<span class="dot push"></span>')
    return f"""<div class="ticker-section">
  <div class="ticker-label">{esc(label_prefix)} \u00b7 {esc(record)}</div>
  <div class="ticker-dots">
    {''.join(dots)}
  </div>
</div>"""


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN SPORTSBOOK CARD
# ═════════════════════════════════════════════════════════════════════════════

async def build_sportsbook_card(user_id: int) -> bytes:
    """
    Build the main sportsbook hub card for a user.
    Returns PNG bytes.
    """
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    spark_data = _get_sparkline_data(user_id, days=7)
    results, record = _get_last_n_results(user_id, n=5)
    open_count, wagered, payout = _get_open_bets(user_id)
    status = _determine_status(user_id)

    # ── Delta string ──────────────────────────────────────────────────────
    delta_str = f"+${delta:,}" if delta >= 0 else f"-${abs(delta):,}"
    delta_class = "positive" if delta >= 0 else "negative"

    # ── Sparkline SVG ─────────────────────────────────────────────────────
    sparkline_html = _sparkline_svg(spark_data)

    # ── Ticker HTML ───────────────────────────────────────────────────────
    ticker = _ticker_html(results, record, "LAST 5")

    # ── Status bar CSS class ──────────────────────────────────────────────
    status_class = _STATUS_MAP.get(status, "win")

    # ── Sport pills ───────────────────────────────────────────────────────
    sports = ["TSL", "NFL", "NBA", "MLB", "NHL"]
    sport_pills = ""
    for s in sports:
        cls = "sport-pill active" if s == "TSL" else "sport-pill"
        sport_pills += f'<span class="{cls}">{esc(s)}</span>'

    # ── Build HTML ────────────────────────────────────────────────────────
    body = f"""<style>{_SPORTSBOOK_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">\U0001f3c6</div>
    <div class="game-title-group">
      <div class="game-title">ATLAS SPORTSBOOK</div>
      <div class="game-subtitle">GLOBAL WAGERING</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Hero Balance -->
<div class="hero-section">
  <div class="hero-label">YOUR BALANCE</div>
  <div class="hero-value">${balance:,}</div>
  <div class="hero-delta {delta_class}">{esc(delta_str)} this week</div>
</div>

<!-- Sparkline -->
<div class="sparkline-section">
  <div class="sparkline-label">7-DAY</div>
  {sparkline_html}
</div>

<div class="gold-divider"></div>

<!-- Win/Loss Ticker -->
{ticker}

<div class="gold-divider"></div>

<!-- Info Panel -->
<div class="info-panel">
  <div class="info-cell">
    <div class="info-label">OPEN BETS</div>
    <div class="info-value">{open_count}</div>
    <div class="info-sub">${wagered:,} wagered</div>
  </div>
  <div class="info-cell">
    <div class="info-label">POTENTIAL PAYOUT</div>
    <div class="info-value green">${payout:,}</div>
    <div class="info-sub">if all bets hit</div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Sport Footer -->
<div class="sport-footer">
  {sport_pills}
</div>
"""

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  STATS / PROFILE CARD
# ═════════════════════════════════════════════════════════════════════════════

async def build_stats_card(user_id: int) -> bytes:
    """
    Build the detailed bettor stats card for a user.
    Returns PNG bytes.
    """
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    spark_data = _get_sparkline_data(user_id, days=30)
    results, record = _get_last_n_results(user_id, n=10)
    wins, losses, pushes = _get_lifetime_record(user_id)
    total_wagered = _get_total_wagered(user_id)
    total_won = _get_total_won(user_id)
    rank, total_users = _get_leaderboard_rank(user_id)
    open_count, wagered, payout = _get_open_bets(user_id)
    status = _determine_status(user_id)

    total_bets = wins + losses + pushes
    win_rate = (wins / total_bets * 100) if total_bets > 0 else 0
    roi = ((balance - STARTING_BALANCE) / STARTING_BALANCE * 100) if STARTING_BALANCE > 0 else 0

    # ── Delta string ──────────────────────────────────────────────────────
    delta_str = f"+${delta:,}" if delta >= 0 else f"-${abs(delta):,}"
    delta_class = "positive" if delta >= 0 else "negative"

    # ── Win rate color ────────────────────────────────────────────────────
    wr_class = "green" if win_rate >= 50 else "red" if total_bets > 0 else ""

    # ── ROI color ─────────────────────────────────────────────────────────
    roi_class = "green" if roi >= 0 else "red"

    # ── Leaderboard color ─────────────────────────────────────────────────
    lb_class = "gold" if rank <= 10 else ""

    # ── Sparkline SVG ─────────────────────────────────────────────────────
    sparkline_html = _sparkline_svg(spark_data)

    # ── Ticker HTML ───────────────────────────────────────────────────────
    ticker = _ticker_html(results, record, "LAST 10")

    # ── Status bar CSS class ──────────────────────────────────────────────
    status_class = _STATUS_MAP.get(status, "win")

    # ── Open bets section (conditional) ───────────────────────────────────
    open_bets_html = ""
    if open_count > 0:
        open_bets_html = f"""
<div class="gold-divider"></div>

<!-- Open Bets Info -->
<div class="info-panel">
  <div class="info-cell">
    <div class="info-label">OPEN BETS</div>
    <div class="info-value">{open_count}</div>
    <div class="info-sub">${wagered:,} at risk</div>
  </div>
  <div class="info-cell">
    <div class="info-label">POTENTIAL PAYOUT</div>
    <div class="info-value green">${payout:,}</div>
    <div class="info-sub">if all bets hit</div>
  </div>
</div>"""

    # ── Build HTML ────────────────────────────────────────────────────────
    body = f"""<style>{_SPORTSBOOK_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">\U0001f4ca</div>
    <div class="game-title-group">
      <div class="game-title">BETTOR PROFILE</div>
      <div class="game-subtitle">YOUR STATS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Hero Balance -->
<div class="hero-section">
  <div class="hero-label">YOUR BALANCE</div>
  <div class="hero-value">${balance:,}</div>
  <div class="hero-delta {delta_class}">{esc(delta_str)} this week</div>
</div>

<!-- Sparkline -->
<div class="sparkline-section">
  <div class="sparkline-label">30-DAY</div>
  {sparkline_html}
</div>

<div class="gold-divider"></div>

<!-- Win/Loss Ticker -->
{ticker}

<div class="gold-divider"></div>

<!-- Stat Grid (2 columns) -->
<div class="stat-grid-2col">
  <div class="stat-cell">
    <div class="stat-label">LIFETIME RECORD</div>
    <div class="stat-value">{wins}-{losses}-{pushes}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">WIN RATE</div>
    <div class="stat-value {wr_class}">{win_rate:.1f}%</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">TOTAL WAGERED</div>
    <div class="stat-value">${total_wagered:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">TOTAL WON</div>
    <div class="stat-value green">${total_won:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">ROI</div>
    <div class="stat-value {roi_class}">{roi:+.1f}%</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">LEADERBOARD</div>
    <div class="stat-value {lb_class}">#{rank} of {total_users}</div>
  </div>
</div>
{open_bets_html}
"""

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(png_bytes: bytes, filename: str = "card.png") -> discord.File:
    """Convert PNG bytes to a discord.File."""
    return discord.File(io.BytesIO(png_bytes), filename=filename)

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
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import discord

from atlas_html_engine import render_card, wrap_card, esc

# ── Config ────────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
STARTING_BALANCE = 1000


# ═════════════════════════════════════════════════════════════════════════════
#  DATA QUERIES
# ═════════════════════════════════════════════════════════════════════════════

def _get_balance(user_id: int) -> int:
    """Get user's current balance."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT balance FROM users_table WHERE discord_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else STARTING_BALANCE


def _get_season_start_balance(user_id: int) -> int:
    """Get user's balance at season start (for status bar calculation)."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT season_start_balance FROM users_table WHERE discord_id = ?",
            (user_id,)
        ).fetchone()
    return row[0] if row else STARTING_BALANCE


def _get_weekly_delta(user_id: int) -> int:
    """Calculate balance change over the past 7 days from snapshots."""
    with sqlite3.connect(DB_PATH) as con:
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        row = con.execute(
            """SELECT balance FROM balance_snapshots
               WHERE discord_id = ? AND snapshot_date <= ?
               ORDER BY snapshot_date DESC LIMIT 1""",
            (user_id, week_ago)
        ).fetchone()
    if row:
        return _get_balance(user_id) - row[0]
    return 0


def _get_sparkline_data(user_id: int, days: int = 7) -> list[int]:
    """Get balance snapshots for sparkline rendering."""
    with sqlite3.connect(DB_PATH) as con:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = con.execute(
            """SELECT balance FROM balance_snapshots
               WHERE discord_id = ? AND snapshot_date >= ?
               ORDER BY snapshot_date ASC""",
            (user_id, cutoff)
        ).fetchall()
    points = [r[0] for r in rows]
    # Always include current balance as the last point
    current = _get_balance(user_id)
    if not points or points[-1] != current:
        points.append(current)
    # Need at least 2 points for a sparkline
    if len(points) < 2:
        points = [current, current]
    return points


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


def _get_leaderboard_rank(user_id: int) -> tuple[int, int]:
    """Get (rank, total_users) on the leaderboard."""
    # NOTE: Could use SQL RANK() window function for O(1) lookup instead of
    # fetching all rows, but user count is small (~31 owners) so linear scan is fine.
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT discord_id, balance FROM users_table ORDER BY balance DESC"
        ).fetchall()
    total = len(rows)
    rank = 1
    for did, bal in rows:
        if did == user_id:
            return rank, total
        rank += 1
    return total, total


def _get_lifetime_record(user_id: int) -> tuple[int, int, int]:
    """Get (wins, losses, pushes) across all settled bets."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """SELECT
                 SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Push' THEN 1 ELSE 0 END)
               FROM bets_table
               WHERE discord_id = ? AND parlay_id IS NULL""",
            (user_id,)
        ).fetchone()
    return (row[0] or 0, row[1] or 0, row[2] or 0)


def _get_total_wagered(user_id: int) -> int:
    """Sum of all wagers ever placed."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """SELECT COALESCE(SUM(wager_amount), 0) FROM bets_table
               WHERE discord_id = ? AND parlay_id IS NULL""",
            (user_id,)
        ).fetchone()
        parlay_row = con.execute(
            """SELECT COALESCE(SUM(wager_amount), 0) FROM parlays_table
               WHERE discord_id = ?""",
            (user_id,)
        ).fetchone()
    return (row[0] or 0) + (parlay_row[0] or 0)


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


def _determine_status(user_id: int) -> str:
    """Determine status bar: 'top10', 'positive', or 'negative'."""
    rank, _ = _get_leaderboard_rank(user_id)
    if rank <= 10:
        return "top10"
    balance = _get_balance(user_id)
    start = _get_season_start_balance(user_id)
    return "positive" if balance >= start else "negative"


# ═════════════════════════════════════════════════════════════════════════════
#  SPARKLINE SVG HELPER
# ═════════════════════════════════════════════════════════════════════════════

def _sparkline_svg(data: list[int], width: int = 440, height: int = 40) -> str:
    """Convert balance history to an SVG polyline."""
    if len(data) < 2:
        return ""
    min_v, max_v = min(data), max(data)
    rng = max_v - min_v or 1
    points = []
    for i, v in enumerate(data):
        x = round(i / (len(data) - 1) * width, 1)
        y = round(height - (v - min_v) / rng * (height - 4) - 2, 1)
        points.append(f"{x},{y}")
    color = "var(--win)" if data[-1] >= data[0] else "var(--loss)"
    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;height:{height}px;">
      <polyline points="{' '.join(points)}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>'''


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED CSS FOR SPORTSBOOK CARDS
# ═════════════════════════════════════════════════════════════════════════════

_SPORTSBOOK_CSS = """\
.hero-section { padding: 20px; text-align: center; }
.hero-label { font-family: 'Outfit'; font-weight: 700; font-size: var(--font-sm); color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; }
.hero-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-display-size); color: var(--text-primary); }
.hero-delta { font-family: 'JetBrains Mono'; font-weight: 600; font-size: var(--font-sm); margin-top: 4px; }
.hero-delta.positive { color: var(--win); }
.hero-delta.negative { color: var(--loss); }

.sparkline-section { padding: 0 20px 12px; text-align: center; }
.sparkline-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; }

.ticker-section { padding: 4px 20px 12px; text-align: center; }
.ticker-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; margin-bottom: 6px; }
.ticker-dots { display: flex; justify-content: center; gap: 6px; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.dot.win { background: var(--win); }
.dot.loss { background: var(--loss); }
.dot.push { background: var(--push); }

.info-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.info-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 12px; }
.info-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.info-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-xl); color: var(--text-primary); margin-top: 4px; }
.info-value.green { color: var(--win); }
.info-sub { font-family: 'Outfit'; font-weight: 600; font-size: 11px; color: var(--text-muted); margin-top: 2px; }

.sport-footer { display: flex; justify-content: center; gap: 8px; padding: 8px 20px 14px; }
.sport-pill { font-family: 'Outfit'; font-weight: 700; font-size: 10px; letter-spacing: 1px; padding: 4px 12px; border-radius: 12px; color: var(--text-dim); background: rgba(255,255,255,0.03); }
.sport-pill.active { color: var(--gold); background: rgba(212,175,55,0.12); border: 1px solid rgba(212,175,55,0.25); }

.stat-grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.stat-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.stat-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 4px; }
.stat-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-lg); color: var(--text-primary); }
.stat-value.green { color: var(--win); }
.stat-value.red { color: var(--loss); }
.stat-value.gold { color: var(--gold); }
"""

# Status bar mapping: internal status → shared CSS class
_STATUS_MAP = {
    "top10": "jackpot",
    "positive": "win",
    "negative": "loss",
}


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
#  MATCH DETAIL CARD
# ═════════════════════════════════════════════════════════════════════════════

_MATCH_DETAIL_CSS = """\
.matchup-hero { text-align: center; padding: 8px 20px 4px; }
.matchup-teams { font-family: 'Outfit'; font-weight: 800; font-size: 24px; color: var(--text-primary); letter-spacing: 1px; }
.matchup-teams .at { color: var(--gold); margin: 0 8px; font-size: 18px; }
.matchup-sub { font-family: 'Outfit'; font-weight: 600; font-size: 12px; color: var(--text-muted); letter-spacing: 1px; margin-top: 2px; }

.status-badge { font-family: 'JetBrains Mono'; font-weight: 700; font-size: 11px; letter-spacing: 1px; padding: 4px 10px; border-radius: 12px; text-transform: uppercase; }
.status-badge.open { color: var(--win); background: rgba(74,222,128,0.12); border: 1px solid rgba(74,222,128,0.35); }
.status-badge.locked { color: var(--loss); background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.35); }

.odds-section { padding: 0 20px 16px; }
.odds-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
.odds-col-header { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; text-align: center; padding-bottom: 8px; }
.odds-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 12px 8px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.odds-team { font-family: 'Outfit'; font-weight: 700; font-size: 12px; color: var(--text-sub); letter-spacing: 0.5px; margin-bottom: 4px; }
.odds-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: 22px; color: var(--text-primary); }
.odds-value.fav { color: var(--win); }
.odds-value.dog { color: var(--loss); }
.odds-juice { font-family: 'JetBrains Mono'; font-weight: 600; font-size: 10px; color: var(--text-dim); margin-top: 2px; }

.match-footer { text-align: center; padding: 0 20px 14px; }
.match-footer span { font-family: 'JetBrains Mono'; font-weight: 600; font-size: 10px; color: var(--text-dim); letter-spacing: 0.5px; }
"""


async def build_match_detail_card(game: dict, *, locked: bool = False) -> bytes:
    """
    Render match detail card as PNG.
    `game` is a dict from _build_game_lines().
    """
    away = esc(game["away"])
    home = esc(game["home"])
    week = game.get("bet_week", "")
    status_class = "loss" if locked else "win"
    badge_class = "locked" if locked else "open"
    badge_text = "\u25cf LOCKED" if locked else "\u25cf OPEN"
    admin_note = " \u00b7 LINE ADJUSTED" if game.get("_overridden") else ""

    # Moneyline color: negative = favorite (green), positive = underdog (red)
    away_ml_class = "fav" if game["away_ml_val"] < 0 else "dog"
    home_ml_class = "fav" if game["home_ml_val"] < 0 else "dog"

    body = f"""<style>{_MATCH_DETAIL_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">\U0001f3df\ufe0f</div>
    <div class="game-title-group">
      <div class="game-title">MATCH DETAIL</div>
      <div class="game-subtitle">WEEK {esc(str(week))} \u00b7 TSL</div>
    </div>
  </div>
  <div class="status-badge {badge_class}">{badge_text}</div>
</div>

<div class="gold-divider"></div>

<!-- Matchup Hero -->
<div class="matchup-hero">
  <div class="matchup-teams">
    {away} <span class="at">@</span> {home}
  </div>
  <div class="matchup-sub">{away} {esc(game['away_spread'])} \u00b7 O/U {game['ou_line']}{admin_note}</div>
</div>

<div class="gold-divider"></div>

<!-- Odds Grid -->
<div class="odds-section">
  <div class="odds-grid">
    <!-- Column Headers -->
    <div class="odds-col-header">Moneyline</div>
    <div class="odds-col-header">Spread</div>
    <div class="odds-col-header">Total</div>

    <!-- Away Row -->
    <div class="odds-cell">
      <div class="odds-team">{away}</div>
      <div class="odds-value {away_ml_class}">{esc(game['away_ml'])}</div>
    </div>
    <div class="odds-cell">
      <div class="odds-team">{away}</div>
      <div class="odds-value">{esc(game['away_spread'])}</div>
      <div class="odds-juice">(-110)</div>
    </div>
    <div class="odds-cell">
      <div class="odds-team">Over</div>
      <div class="odds-value">{game['ou_line']}</div>
      <div class="odds-juice">(-110)</div>
    </div>

    <!-- Home Row -->
    <div class="odds-cell">
      <div class="odds-team">{home}</div>
      <div class="odds-value {home_ml_class}">{esc(game['home_ml'])}</div>
    </div>
    <div class="odds-cell">
      <div class="odds-team">{home}</div>
      <div class="odds-value">{esc(game['home_spread'])}</div>
      <div class="odds-juice">(-110)</div>
    </div>
    <div class="odds-cell">
      <div class="odds-team">Under</div>
      <div class="odds-value">{game['ou_line']}</div>
      <div class="odds-juice">(-110)</div>
    </div>
  </div>
</div>

<!-- Footer -->
<div class="match-footer">
  <span>ATLAS SPORTSBOOK \u00b7 ELO-POWERED ODDS</span>
</div>
"""

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(png_bytes: bytes, filename: str = "card.png") -> discord.File:
    """Convert PNG bytes to a discord.File."""
    return discord.File(io.BytesIO(png_bytes), filename=filename)

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

import asyncio
import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import discord

from atlas_html_engine import render_card, wrap_card, esc, icon_pill
from odds_utils import american_to_str, payout_calc

# ── Sport icon mapping ────────────────────────────────────────────────────────
SPORT_ICON_MAP = {
    # By sport_key prefix (e.g., "americanfootball_nfl" → "americanfootball")
    "americanfootball": "nfl",
    "basketball": "basketball",
    "baseball": "mlb",
    "icehockey": "nhl",
    "mma": "ufc",
    "soccer": "soccer",
}

# By source label (e.g., "NFL", "NCAAB", "NHL")
_LABEL_ICON_MAP = {
    "NFL": "nfl", "NCAAF": "nfl",
    "NBA": "basketball", "NCAAB": "basketball", "WNBA": "basketball",
    "MLB": "mlb",
    "NHL": "nhl",
    "UFC": "ufc", "MMA": "ufc",
    "EPL": "soccer", "MLS": "soccer",
    "TSL": "nfl",
}


def _sport_icon(sport_key: str = "", source: str = "") -> str:
    """Return icon_pill for the sport, falling back to sportsbook."""
    # Try sport_key prefix first
    if sport_key:
        prefix = sport_key.split("_")[0]
        icon_name = SPORT_ICON_MAP.get(prefix)
        if icon_name:
            return icon_pill(icon_name, "\U0001f3df\ufe0f")
    # Try source label
    if source:
        icon_name = _LABEL_ICON_MAP.get(source.upper())
        if icon_name:
            return icon_pill(icon_name, "\U0001f3df\ufe0f")
    return icon_pill("sportsbook", "\U0001f3df\ufe0f")


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
.hero-label { font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; }
.hero-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-display-size); color: var(--text-primary); }
.hero-delta { font-family: var(--font-mono); font-weight: 600; font-size: var(--font-sm); margin-top: var(--space-xs); }
.hero-delta.positive { color: var(--win); }
.hero-delta.negative { color: var(--loss); }

.sparkline-section { padding: 0 20px var(--space-md); text-align: center; }
.sparkline-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; }

.ticker-section { padding: var(--space-xs) 20px var(--space-md); text-align: center; }
.ticker-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; margin-bottom: 6px; }
.ticker-dots { display: flex; justify-content: center; gap: 6px; }
.dot { width: 12px; height: 12px; border-radius: 50%; }
.dot.win { background: var(--win); }
.dot.loss { background: var(--loss); }
.dot.push { background: var(--push); }

.info-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px var(--space-md); }
.info-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: var(--space-md); }
.info-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.info-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-xl); color: var(--text-primary); margin-top: var(--space-xs); }
.info-value.green { color: var(--win); }
.info-sub { font-family: var(--font-display); font-weight: 600; font-size: var(--font-xs); color: var(--text-muted); margin-top: 2px; }

.sport-footer { display: flex; justify-content: center; gap: var(--space-sm); padding: var(--space-sm) 20px 14px; }
.sport-pill { font-family: var(--font-display); font-weight: 700; font-size: 10px; letter-spacing: 1px; padding: var(--space-xs) var(--space-md); border-radius: 12px; color: var(--text-dim); background: rgba(255,255,255,0.03); }
.sport-pill.active { color: var(--gold); background: rgba(212,175,55,0.12); border: 1px solid rgba(212,175,55,0.25); }

.stat-grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px var(--space-md); }
.stat-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.stat-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: var(--space-xs); }
.stat-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-lg); color: var(--text-primary); }
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

def _gather_sportsbook_data(user_id: int) -> dict:
    """Sync: collect all DB data for sportsbook hub card."""
    return {
        "balance": _get_balance(user_id),
        "delta": _get_weekly_delta(user_id),
        "spark_data": _get_sparkline_data(user_id, days=7),
        "results_record": _get_last_n_results(user_id, n=5),
        "open_bets": _get_open_bets(user_id),
        "status": _determine_status(user_id),
    }


async def build_sportsbook_card(user_id: int) -> bytes:
    """
    Build the main sportsbook hub card for a user.
    Returns PNG bytes.
    """
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_sportsbook_data, user_id)
    balance = d["balance"]
    delta = d["delta"]
    spark_data = d["spark_data"]
    results, record = d["results_record"]
    open_count, wagered, payout = d["open_bets"]
    status = d["status"]

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
    <div class="game-icon-pill">{icon_pill("sportsbook", "\U0001f3c6")}</div>
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

def _gather_stats_data(user_id: int) -> dict:
    """Sync: collect all DB data for stats card."""
    wins, losses, pushes = _get_lifetime_record(user_id)
    return {
        "balance": _get_balance(user_id),
        "delta": _get_weekly_delta(user_id),
        "spark_data": _get_sparkline_data(user_id, days=30),
        "results_record": _get_last_n_results(user_id, n=10),
        "wins": wins, "losses": losses, "pushes": pushes,
        "total_wagered": _get_total_wagered(user_id),
        "total_won": _get_total_won(user_id),
        "rank_total": _get_leaderboard_rank(user_id),
        "open_bets": _get_open_bets(user_id),
        "status": _determine_status(user_id),
    }


async def build_stats_card(user_id: int) -> bytes:
    """
    Build the detailed bettor stats card for a user.
    Returns PNG bytes.
    """
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_stats_data, user_id)
    balance = d["balance"]
    delta = d["delta"]
    spark_data = d["spark_data"]
    results, record = d["results_record"]
    wins, losses, pushes = d["wins"], d["losses"], d["pushes"]
    total_wagered = d["total_wagered"]
    total_won = d["total_won"]
    rank, total_users = d["rank_total"]
    open_count, wagered, payout = d["open_bets"]
    status = d["status"]

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
    <div class="game-icon-pill">{icon_pill("sportsbook", "\U0001f4ca")}</div>
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
.matchup-hero { text-align: center; padding: var(--space-sm) 20px var(--space-xs); }
.matchup-teams { font-family: var(--font-display); font-weight: 800; font-size: var(--font-xl); color: var(--text-primary); letter-spacing: 1px; }
.matchup-teams .at { color: var(--gold); margin: 0 var(--space-sm); font-size: var(--font-lg); }
.matchup-sub { font-family: var(--font-display); font-weight: 600; font-size: 12px; color: var(--text-muted); letter-spacing: 1px; margin-top: 2px; }

.status-badge { font-family: var(--font-mono); font-weight: 700; font-size: var(--font-xs); letter-spacing: 1px; padding: var(--space-xs) 10px; border-radius: 12px; text-transform: uppercase; }
.status-badge.open { color: var(--win); background: rgba(74,222,128,0.12); border: 1px solid rgba(74,222,128,0.35); }
.status-badge.locked { color: var(--loss); background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.35); }

.odds-section { padding: 0 20px var(--space-lg); }
.odds-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; }
.odds-col-header { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; text-align: center; padding-bottom: var(--space-sm); }
.odds-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: var(--space-md) var(--space-sm); text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.odds-team { font-family: var(--font-display); font-weight: 700; font-size: 12px; color: var(--text-sub); letter-spacing: 0.5px; margin-bottom: var(--space-xs); }
.odds-value { font-family: var(--font-mono); font-weight: 800; font-size: 22px; color: var(--text-primary); }
.odds-value.fav { color: var(--win); }
.odds-value.dog { color: var(--loss); }
.odds-juice { font-family: var(--font-mono); font-weight: 600; font-size: 10px; color: var(--text-dim); margin-top: 2px; }

.match-footer { text-align: center; padding: 0 20px 14px; }
.match-footer span { font-family: var(--font-mono); font-weight: 600; font-size: 10px; color: var(--text-dim); letter-spacing: 0.5px; }
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
    <div class="game-icon-pill">{icon_pill("nfl", "\U0001f3c8")}</div>
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
#  REAL SPORTSBOOK MATCH DETAIL CARD
# ═════════════════════════════════════════════════════════════════════════════


async def build_real_match_detail_card(
    event: dict, odds_rows: list[dict], *, sport_key: str = ""
) -> bytes:
    """
    Render a match detail card for real sports (NCAAB, NFL, etc.).
    `event` has 'away_team', 'home_team', 'commence_time'.
    `odds_rows` has dicts with 'market', 'outcome_name', 'price', 'point'.
    """
    away = esc(event.get("away_team", "Away"))
    home = esc(event.get("home_team", "Home"))

    # Parse sport label from sport_key (e.g. "basketball_ncaab" → "NCAAB")
    sport_label = sport_key.split("_")[-1].upper() if sport_key else "SPORTS"

    # Group odds by market
    markets: dict[str, list[dict]] = {}
    for row in odds_rows:
        markets.setdefault(row["market"], []).append(row)

    # Build ML cells
    h2h = markets.get("h2h", [])
    ml_cells = ""
    for o in h2h:
        name = esc(o["outcome_name"])
        price = o["price"]
        cls = "fav" if price < 0 else "dog"
        ml_cells += f"""<div class="odds-cell">
      <div class="odds-team">{name}</div>
      <div class="odds-value {cls}">{american_to_str(price)}</div>
    </div>"""
    # Pad if only 1 or 0 outcomes
    while ml_cells.count("odds-cell") < 2:
        ml_cells += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'

    # Build spread cells
    spreads = markets.get("spreads", [])
    spread_cells = ""
    for o in spreads:
        name = esc(o["outcome_name"])
        point = f"{o['point']:+g}" if o.get("point") is not None else ""
        price = american_to_str(o["price"])
        spread_cells += f"""<div class="odds-cell">
      <div class="odds-team">{name}</div>
      <div class="odds-value">{point}</div>
      <div class="odds-juice">({price})</div>
    </div>"""
    while spread_cells.count("odds-cell") < 2:
        spread_cells += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'

    # Build total cells
    totals = markets.get("totals", [])
    total_cells = ""
    for o in totals:
        label = esc(o["outcome_name"])  # "Over" / "Under"
        point = f"{o['point']}" if o.get("point") is not None else ""
        price = american_to_str(o["price"])
        total_cells += f"""<div class="odds-cell">
      <div class="odds-team">{label}</div>
      <div class="odds-value">{point}</div>
      <div class="odds-juice">({price})</div>
    </div>"""
    while total_cells.count("odds-cell") < 2:
        total_cells += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'

    # Interleave rows: away ML, away spread, over | home ML, home spread, under
    # We need row-by-row: [ml_0, spread_0, total_0], [ml_1, spread_1, total_1]
    ml_list = h2h[:2] if len(h2h) >= 2 else h2h + [None] * (2 - len(h2h))
    spread_list = spreads[:2] if len(spreads) >= 2 else spreads + [None] * (2 - len(spreads))
    total_list = totals[:2] if len(totals) >= 2 else totals + [None] * (2 - len(totals))

    grid_rows = ""
    for ml, sp, tot in zip(ml_list, spread_list, total_list):
        # ML cell
        if ml:
            cls = "fav" if ml["price"] < 0 else "dog"
            grid_rows += f'<div class="odds-cell"><div class="odds-team">{esc(ml["outcome_name"])}</div><div class="odds-value {cls}">{american_to_str(ml["price"])}</div></div>'
        else:
            grid_rows += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'
        # Spread cell
        if sp:
            pt = f"{sp['point']:+g}" if sp.get("point") is not None else ""
            grid_rows += f'<div class="odds-cell"><div class="odds-team">{esc(sp["outcome_name"])}</div><div class="odds-value">{pt}</div><div class="odds-juice">({american_to_str(sp["price"])})</div></div>'
        else:
            grid_rows += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'
        # Total cell
        if tot:
            pt = f"{tot['point']}" if tot.get("point") is not None else ""
            grid_rows += f'<div class="odds-cell"><div class="odds-team">{esc(tot["outcome_name"])}</div><div class="odds-value">{pt}</div><div class="odds-juice">({american_to_str(tot["price"])})</div></div>'
        else:
            grid_rows += '<div class="odds-cell"><div class="odds-team">—</div><div class="odds-value">—</div></div>'

    body = f"""<style>{_MATCH_DETAIL_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{_sport_icon(sport_key)}</div>
    <div class="game-title-group">
      <div class="game-title">MATCH DETAIL</div>
      <div class="game-subtitle">{esc(sport_label)} \u00b7 LIVE ODDS</div>
    </div>
  </div>
  <div class="status-badge open">\u25cf OPEN</div>
</div>

<div class="gold-divider"></div>

<!-- Matchup Hero -->
<div class="matchup-hero">
  <div class="matchup-teams">
    {away} <span class="at">@</span> {home}
  </div>
</div>

<div class="gold-divider"></div>

<!-- Odds Grid -->
<div class="odds-section">
  <div class="odds-grid">
    <div class="odds-col-header">Moneyline</div>
    <div class="odds-col-header">Spread</div>
    <div class="odds-col-header">Total</div>
    {grid_rows}
  </div>
</div>

<!-- Footer -->
<div class="match-footer">
  <span>ATLAS SPORTSBOOK \u00b7 LIVE ODDS</span>
</div>
"""

    full_html = wrap_card(body, status_class="win")
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  BET CONFIRMATION CARD
# ═════════════════════════════════════════════════════════════════════════════

_BET_CONFIRM_CSS = """\
.confirm-pick { text-align: center; padding: var(--space-md) 20px var(--space-xs); }
.confirm-pick-name { font-family: var(--font-display); font-weight: 800; font-size: 22px; color: var(--text-primary); letter-spacing: 0.5px; }
.confirm-pick-detail { font-family: var(--font-mono); font-weight: 600; font-size: var(--font-sm); color: var(--text-muted); margin-top: var(--space-xs); }

.confirm-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; padding: 0 20px var(--space-lg); }
.confirm-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: var(--space-md) var(--space-sm); text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.confirm-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: var(--space-xs); }
.confirm-value { font-family: var(--font-mono); font-weight: 800; font-size: 20px; color: var(--text-primary); }
.confirm-value.green { color: var(--win); }
.confirm-value.gold { color: var(--gold); }

.parlay-legs { padding: 0 20px var(--space-sm); }
.parlay-leg { display: flex; align-items: baseline; gap: var(--space-sm); padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
.parlay-leg:last-child { border-bottom: none; }
.leg-num { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-xs); color: var(--gold-dim); min-width: var(--font-lg); }
.leg-pick { font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--text-primary); }
.leg-info { font-family: var(--font-mono); font-weight: 600; font-size: var(--font-xs); color: var(--text-muted); margin-left: auto; white-space: nowrap; }
"""


async def build_bet_confirm_card(
    pick: str, bet_type: str, odds: int, risk: int, to_win: int,
    balance: int, *, matchup: str = "", week: int = 0,
    line: float | None = None, source: str = "TSL"
) -> bytes:
    """Render a straight bet confirmation card as PNG."""
    odds_str = f"+{odds}" if odds > 0 else str(odds)

    # Build detail line: "Spread -3.5 · (-110)" or "Moneyline · (-165)"
    detail_parts = [esc(bet_type)]
    if line is not None:
        detail_parts[0] += f" {line:+g}"
    detail_parts.append(f"({odds_str})")
    detail_line = " \u00b7 ".join(detail_parts)

    subtitle = f"WEEK {week} \u00b7 {esc(source)}" if week else esc(source)

    body = f"""<style>{_BET_CONFIRM_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{_sport_icon(source=source)}</div>
    <div class="game-title-group">
      <div class="game-title">BET CONFIRMED</div>
      <div class="game-subtitle">{subtitle}</div>
    </div>
  </div>
  <div class="status-badge open">{esc(bet_type)}</div>
</div>

<div class="gold-divider"></div>

<div class="confirm-pick">
  <div class="confirm-pick-name">{esc(pick)}</div>
  <div class="confirm-pick-detail">{detail_line}</div>
</div>

<div class="gold-divider"></div>

<div class="confirm-grid">
  <div class="confirm-cell">
    <div class="confirm-label">Risk</div>
    <div class="confirm-value">${risk:,}</div>
  </div>
  <div class="confirm-cell">
    <div class="confirm-label">To Win</div>
    <div class="confirm-value green">${to_win:,}</div>
  </div>
  <div class="confirm-cell">
    <div class="confirm-label">Balance</div>
    <div class="confirm-value">${balance:,}</div>
  </div>
</div>

<div class="match-footer">
  <span>ATLAS SPORTSBOOK · BET CONFIRMED</span>
</div>
"""
    return await render_card(wrap_card(body, status_class="jackpot"))


async def build_parlay_confirm_card(
    legs: list[dict], combined_odds: int, risk: int, to_win: int,
    *, week: int = 0
) -> bytes:
    """Render a parlay confirmation card as PNG."""
    odds_str = f"+{combined_odds}" if combined_odds > 0 else str(combined_odds)

    # Build leg rows
    leg_html = ""
    for i, leg in enumerate(legs, 1):
        pick = esc(leg.get("pick", ""))
        bt = esc(leg.get("bet_type", ""))
        lo = leg.get("odds", 0)
        lo_str = f"+{lo}" if lo > 0 else str(lo)
        matchup = esc(leg.get("matchup", ""))
        leg_html += f"""<div class="parlay-leg">
  <span class="leg-num">{i}.</span>
  <span class="leg-pick">{pick} ({bt})</span>
  <span class="leg-info">{lo_str}</span>
</div>"""

    subtitle = f"WEEK {week} \u00b7 TSL" if week else "TSL"

    body = f"""<style>{_BET_CONFIRM_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("parlay", "\U0001f3b0")}</div>
    <div class="game-title-group">
      <div class="game-title">PARLAY CONFIRMED</div>
      <div class="game-subtitle">{subtitle}</div>
    </div>
  </div>
  <div class="status-badge open">{len(legs)}-LEG</div>
</div>

<div class="gold-divider"></div>

<div class="parlay-legs">
{leg_html}
</div>

<div class="gold-divider"></div>

<div class="confirm-grid">
  <div class="confirm-cell">
    <div class="confirm-label">Odds</div>
    <div class="confirm-value gold">{odds_str}</div>
  </div>
  <div class="confirm-cell">
    <div class="confirm-label">Risk</div>
    <div class="confirm-value">${risk:,}</div>
  </div>
  <div class="confirm-cell">
    <div class="confirm-label">To Win</div>
    <div class="confirm-value green">${to_win:,}</div>
  </div>
</div>

<div class="match-footer">
  <span>ALL LEGS MUST HIT{' \u00b7 WEEK ' + str(week) if week else ''}</span>
</div>
"""
    return await render_card(wrap_card(body, status_class="jackpot"))


# ═════════════════════════════════════════════════════════════════════════════
#  CASINO HUB CARD
# ═════════════════════════════════════════════════════════════════════════════

_CASINO_HUB_CSS = """\
.casino-balance { text-align: center; padding: var(--space-md) 20px var(--space-xs); }
.casino-bal-label { font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; }
.casino-bal-value { font-family: var(--font-mono); font-weight: 800; font-size: 40px; color: var(--text-primary); }
.casino-bal-sub { font-family: var(--font-display); font-weight: 600; font-size: var(--font-xs); color: var(--text-muted); margin-top: 2px; }

.jackpot-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; padding: 0 20px var(--space-md); }
.jackpot-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px 6px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.jackpot-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: var(--space-xs); }
.jackpot-label.mini { color: var(--blue-sky); }
.jackpot-label.major { color: var(--push); }
.jackpot-label.grand { color: var(--gold-bright); }
.jackpot-value { font-family: var(--font-mono); font-weight: 800; font-size: var(--font-lg); color: var(--text-primary); }

.game-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px var(--space-md); }
.game-tile { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px var(--space-md); display: flex; align-items: center; gap: var(--space-sm); border-top: 1px solid rgba(255,255,255,0.06); }
.game-emoji { font-size: var(--font-lg); }
.game-name { font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--text-primary); }
.game-payout { font-family: var(--font-mono); font-weight: 600; font-size: 10px; color: var(--text-dim); }

.streak-badge { font-family: var(--font-mono); font-weight: 700; font-size: var(--font-xs); letter-spacing: 1px; padding: var(--space-xs) 10px; border-radius: 12px; text-transform: uppercase; }
.streak-badge.hot { color: var(--orange); background: rgba(251,146,60,0.12); border: 1px solid rgba(251,146,60,0.35); }
.streak-badge.cold { color: var(--blue-sky); background: rgba(125,211,252,0.12); border: 1px solid rgba(125,211,252,0.35); }
.streak-badge.neutral { color: var(--text-muted); background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); }
"""


async def build_casino_hub_card(
    balance: int, max_bet: int, tier_name: str,
    streak: dict, jackpots: dict
) -> bytes:
    """Render the casino hub landing card as PNG."""
    # Streak badge
    s_count = streak.get("count", 0)
    s_type = streak.get("type", "")  # "win" or "loss"
    if s_type == "win" and s_count >= 3:
        streak_cls = "hot"
        streak_text = f"\U0001f525 {s_count} WIN"
    elif s_type == "loss" and s_count >= 3:
        streak_cls = "cold"
        streak_text = f"\u2744\ufe0f {s_count} LOSS"
    else:
        streak_cls = "neutral"
        streak_text = "NO STREAK"

    mini = jackpots.get("mini", 0)
    major = jackpots.get("major", 0)
    grand = jackpots.get("grand", 0)

    body = f"""<style>{_CASINO_HUB_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("slots", "\U0001f3b0")}</div>
    <div class="game-title-group">
      <div class="game-title">TSL CASINO</div>
      <div class="game-subtitle">THE SIM LEAGUE</div>
    </div>
  </div>
  <div class="streak-badge {streak_cls}">{streak_text}</div>
</div>

<div class="gold-divider"></div>

<div class="casino-balance">
  <div class="casino-bal-label">YOUR BALANCE</div>
  <div class="casino-bal-value">${balance:,}</div>
  <div class="casino-bal-sub">Max Bet: ${max_bet:,} \u00b7 {esc(tier_name)} Tier</div>
</div>

<div class="gold-divider"></div>

<div style="padding:0 20px 6px;">
  <div style="font-family:var(--font-display);font-weight:700;font-size:10px;color:var(--gold-dim);letter-spacing:1.5px;text-transform:uppercase;">JACKPOTS</div>
</div>
<div class="jackpot-grid">
  <div class="jackpot-cell">
    <div class="jackpot-label mini">MINI</div>
    <div class="jackpot-value">${mini:,}</div>
  </div>
  <div class="jackpot-cell">
    <div class="jackpot-label major">MAJOR</div>
    <div class="jackpot-value">${major:,}</div>
  </div>
  <div class="jackpot-cell">
    <div class="jackpot-label grand">GRAND</div>
    <div class="jackpot-value">${grand:,}</div>
  </div>
</div>

<div class="gold-divider"></div>

<div class="game-grid">
  <div class="game-tile">
    <span class="game-emoji">\U0001f0cf</span>
    <div><div class="game-name">Blackjack</div><div class="game-payout">6:5 PAYOUT</div></div>
  </div>
  <div class="game-tile">
    <span class="game-emoji">\U0001f3b0</span>
    <div><div class="game-name">Slots</div><div class="game-payout">UP TO 50x</div></div>
  </div>
  <div class="game-tile">
    <span class="game-emoji">\U0001f680</span>
    <div><div class="game-name">Crash</div><div class="game-payout">NO LIMIT</div></div>
  </div>
  <div class="game-tile">
    <span class="game-emoji">\U0001fa99</span>
    <div><div class="game-name">Coin Flip</div><div class="game-payout">2x PAYOUT</div></div>
  </div>
</div>

<div class="match-footer">
  <span>TSL CASINO \u00b7 THE SIM LEAGUE</span>
</div>
"""
    return await render_card(wrap_card(body, status_class="jackpot"))


# ═════════════════════════════════════════════════════════════════════════════
#  PARLAY ANALYTICS CARD
# ═════════════════════════════════════════════════════════════════════════════

_PARLAY_ANALYTICS_CSS = """\
.section-title { font-family: var(--font-display); font-weight: 700; font-size: 11px; color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; padding: 0 20px var(--space-xs); }
.bar-row { display: flex; align-items: center; gap: 8px; padding: 4px 20px; }
.bar-label { font-family: var(--font-display); font-weight: 700; font-size: 11px; color: var(--text-sub); width: 80px; text-align: right; flex-shrink: 0; }
.bar-track { flex: 1; height: 14px; background: rgba(255,255,255,0.06); border-radius: 7px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 7px; background: var(--gold); }
.bar-fill.green { background: var(--win); }
.bar-stat { font-family: var(--font-mono); font-weight: 700; font-size: 11px; color: var(--text-muted); width: 110px; flex-shrink: 0; }
.recent-row { display: flex; align-items: center; gap: 8px; padding: 3px 20px; }
.recent-icon { font-size: 14px; width: 18px; text-align: center; flex-shrink: 0; }
.recent-icon.won { color: var(--win); }
.recent-icon.lost { color: var(--loss); }
.recent-info { font-family: var(--font-mono); font-weight: 600; font-size: 12px; color: var(--text-sub); flex: 1; }
.recent-amount { font-family: var(--font-mono); font-weight: 700; font-size: 12px; }
.recent-amount.won { color: var(--win); }
.recent-amount.lost { color: var(--text-dim); }
.recent-section { padding: var(--space-xs) 0 var(--space-md); }
.empty-msg { font-family: var(--font-display); font-weight: 600; font-size: 13px; color: var(--text-dim); text-align: center; padding: 20px; }
"""


def _gather_parlay_analytics_data(user_id: int) -> dict:
    """Sync: collect all DB data for parlay analytics card."""
    with sqlite3.connect(DB_PATH) as con:
        # 1. Parlay-level aggregates
        agg = con.execute(
            "SELECT "
            "  COUNT(*), "
            "  SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN status='Push' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN status='Pending' THEN 1 ELSE 0 END), "
            "  SUM(wager_amount) "
            "FROM parlays_table WHERE discord_id=?",
            (user_id,),
        ).fetchone()
        total_parlays = agg[0] or 0
        wins = agg[1] or 0
        losses = agg[2] or 0
        pushes = agg[3] or 0
        pending = agg[4] or 0
        total_wagered = agg[5] or 0

        # 2. Won parlay payouts (compute via payout_calc in Python)
        won_rows = con.execute(
            "SELECT wager_amount, combined_odds "
            "FROM parlays_table WHERE discord_id=? AND status='Won'",
            (user_id,),
        ).fetchall()
        total_won = sum(payout_calc(w, o) for w, o in won_rows)

        # 3. Per-leg bet type breakdown (excludes Unknown/Pending/Cancelled)
        leg_stats_rows = con.execute(
            "SELECT pl.bet_type, "
            "  COUNT(*), "
            "  SUM(CASE WHEN pl.status='Won' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN pl.status='Lost' THEN 1 ELSE 0 END) "
            "FROM parlay_legs pl "
            "JOIN parlays_table p ON p.parlay_id = pl.parlay_id "
            "WHERE p.discord_id=? AND pl.status IN ('Won','Lost') "
            "GROUP BY pl.bet_type ORDER BY COUNT(*) DESC",
            (user_id,),
        ).fetchall()
        leg_stats = []
        for bt, total, won_ct, lost_ct in leg_stats_rows:
            wr = (won_ct / total * 100) if total > 0 else 0
            leg_stats.append({"bet_type": bt, "total": total, "won": won_ct, "lost": lost_ct, "win_rate": wr})

        # 4. Avg/max legs per parlay
        legs_agg = con.execute(
            "SELECT AVG(cnt), MAX(cnt) FROM ("
            "  SELECT COUNT(*) as cnt FROM parlay_legs pl "
            "  JOIN parlays_table p ON p.parlay_id = pl.parlay_id "
            "  WHERE p.discord_id=? GROUP BY pl.parlay_id"
            ")",
            (user_id,),
        ).fetchone()
        avg_legs = legs_agg[0] or 0
        max_legs = legs_agg[1] or 0

        # 5. Recent 5 settled parlays
        recent_parlays_raw = con.execute(
            "SELECT parlay_id, status, wager_amount, combined_odds "
            "FROM parlays_table "
            "WHERE discord_id=? AND status IN ('Won','Lost','Push') "
            "ORDER BY rowid DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        # Batch-fetch legs for recent parlays
        recent_parlays = []
        if recent_parlays_raw:
            pids = [r[0] for r in recent_parlays_raw]
            ph = ",".join("?" * len(pids))
            leg_rows = con.execute(
                f"SELECT parlay_id, pick, bet_type, status FROM parlay_legs "
                f"WHERE parlay_id IN ({ph}) ORDER BY parlay_id, leg_index",
                pids,
            ).fetchall()
            legs_map: dict[str, list[dict]] = {}
            for pid, pick, bt, st in leg_rows:
                legs_map.setdefault(pid, []).append({"pick": pick, "bet_type": bt, "status": st})
            for pid, status, wager, c_odds in recent_parlays_raw:
                recent_parlays.append({
                    "parlay_id": pid,
                    "status": status,
                    "wager": wager,
                    "combined_odds": c_odds,
                    "legs": legs_map.get(pid, []),
                })

    return {
        "total_parlays": total_parlays, "wins": wins, "losses": losses,
        "pushes": pushes, "pending": pending,
        "total_wagered": total_wagered, "total_won": total_won,
        "net_pnl": total_won - total_wagered,
        "leg_stats": leg_stats,
        "avg_legs": avg_legs, "max_legs": max_legs,
        "recent_parlays": recent_parlays,
    }


async def build_parlay_analytics_card(user_id: int) -> bytes:
    """Build the parlay analytics card. Returns PNG bytes."""
    d = await asyncio.get_running_loop().run_in_executor(
        None, _gather_parlay_analytics_data, user_id
    )
    total = d["total_parlays"]
    wins, losses, pushes, pending = d["wins"], d["losses"], d["pushes"], d["pending"]
    total_wagered = d["total_wagered"]
    total_won = d["total_won"]
    net_pnl = d["net_pnl"]
    leg_stats = d["leg_stats"]
    avg_legs = d["avg_legs"]
    max_legs = d["max_legs"]
    recent_parlays = d["recent_parlays"]

    # Derived values
    settled = wins + losses + pushes
    win_rate = (wins / settled * 100) if settled > 0 else 0
    pnl_class = "green" if net_pnl >= 0 else "red"
    pnl_str = f"+${net_pnl:,}" if net_pnl >= 0 else f"-${abs(net_pnl):,}"

    # Status bar
    if total == 0:
        status_class = "jackpot"
    elif win_rate >= 55:
        status_class = "win"
    else:
        status_class = "loss"

    # Empty state
    if total == 0:
        body = f"""<style>{_SPORTSBOOK_CSS}{_PARLAY_ANALYTICS_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("sportsbook", "\U0001f4ca")}</div>
    <div class="game-title-group">
      <div class="game-title">PARLAY ANALYTICS</div>
      <div class="game-subtitle">YOUR PARLAYS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<div class="empty-msg">No parlays placed yet. Build a parlay from the Sportsbook!</div>
"""
        return await render_card(wrap_card(body, status_class="jackpot"))

    # ── Bar chart HTML ─────────────────────────────────────────────────────
    bars_html = ""
    if leg_stats:
        for ls in leg_stats:
            wr = ls["win_rate"]
            bar_class = "green" if wr >= 50 else ""
            bars_html += f"""<div class="bar-row">
  <span class="bar-label">{esc(ls['bet_type'])}</span>
  <div class="bar-track"><div class="bar-fill {bar_class}" style="width:{wr:.0f}%"></div></div>
  <span class="bar-stat">{wr:.1f}% ({ls['won']}/{ls['total']})</span>
</div>\n"""
    else:
        bars_html = '<div class="empty-msg">No graded leg data yet</div>'

    # ── Recent parlays HTML ────────────────────────────────────────────────
    recent_html = ""
    if recent_parlays:
        for rp in recent_parlays:
            is_won = rp["status"] == "Won"
            icon = "✔" if is_won else "✗"
            icon_cls = "won" if is_won else "lost"
            n_legs = len(rp["legs"])
            odds_str = f"{int(rp['combined_odds']):+d}" if rp["combined_odds"] else ""
            if is_won:
                payout_val = payout_calc(rp["wager"], rp["combined_odds"])
                amt_str = f"${rp['wager']:,} → ${payout_val:,}"
                amt_cls = "won"
            else:
                amt_str = f"${rp['wager']:,} → $0"
                amt_cls = "lost"
            recent_html += f"""<div class="recent-row">
  <span class="recent-icon {icon_cls}">{icon}</span>
  <span class="recent-info">{n_legs}-leg {odds_str}</span>
  <span class="recent-amount {amt_cls}">{amt_str}</span>
</div>\n"""
    else:
        recent_html = '<div class="empty-msg">No settled parlays yet</div>'

    body = f"""<style>{_SPORTSBOOK_CSS}{_PARLAY_ANALYTICS_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("sportsbook", "\U0001f4ca")}</div>
    <div class="game-title-group">
      <div class="game-title">PARLAY ANALYTICS</div>
      <div class="game-subtitle">YOUR PARLAYS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Record -->
<div class="hero-section">
  <div class="hero-label">PARLAY RECORD</div>
  <div class="hero-value">{wins}-{losses}-{pushes}</div>
  <div class="hero-delta {'positive' if win_rate >= 50 else 'negative'}">{win_rate:.1f}% WIN RATE</div>
</div>

<div class="gold-divider"></div>

<!-- Stats Grid -->
<div class="stat-grid-2col">
  <div class="stat-cell">
    <div class="stat-label">TOTAL WAGERED</div>
    <div class="stat-value">${total_wagered:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">TOTAL WON</div>
    <div class="stat-value green">${total_won:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">NET P&amp;L</div>
    <div class="stat-value {pnl_class}">{esc(pnl_str)}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">AVG LEGS</div>
    <div class="stat-value">{avg_legs:.1f}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">BIGGEST PARLAY</div>
    <div class="stat-value">{max_legs} legs</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">PENDING</div>
    <div class="stat-value">{pending}</div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Leg Win Rate by Type -->
<div class="section-title">LEG WIN RATE BY TYPE</div>
{bars_html}

<div class="gold-divider"></div>

<!-- Recent Parlays -->
<div class="section-title">RECENT PARLAYS</div>
<div class="recent-section">
{recent_html}
</div>
"""

    return await render_card(wrap_card(body, status_class=status_class))


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(png_bytes: bytes, filename: str = "card.png") -> discord.File:
    """Convert PNG bytes to a discord.File."""
    return discord.File(io.BytesIO(png_bytes), filename=filename)

"""
flow_cards.py — ATLAS Flow Hub Card Builder
═══════════════════════════════════════════════════════════════════════════════
Uses the ATLAS HTML engine to build the unified Flow Hub card showing a user's
complete financial overview: balance, betting record, active positions, and
cross-module stats (sportsbook + casino + prediction markets).

Integration:
    from flow_cards import build_flow_card, card_to_file

    png = await build_flow_card(user_id=interaction.user.id)
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
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT balance FROM users_table WHERE discord_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row else STARTING_BALANCE


def _get_season_start_balance(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT season_start_balance FROM users_table WHERE discord_id = ?",
            (user_id,)
        ).fetchone()
    return row[0] if row else STARTING_BALANCE


def _get_weekly_delta(user_id: int) -> int:
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
    with sqlite3.connect(DB_PATH) as con:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = con.execute(
            """SELECT balance FROM balance_snapshots
               WHERE discord_id = ? AND snapshot_date >= ?
               ORDER BY snapshot_date ASC""",
            (user_id, cutoff)
        ).fetchall()
    points = [r[0] for r in rows]
    current = _get_balance(user_id)
    if not points or points[-1] != current:
        points.append(current)
    if len(points) < 2:
        points = [current, current]
    return points


def _get_lifetime_record(user_id: int) -> tuple[int, int, int]:
    """Get (wins, losses, pushes) across all settled straight bets."""
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


def _get_active_positions(user_id: int) -> dict:
    """Get counts of active positions across all modules."""
    result = {"bets": 0, "contracts": 0}
    with sqlite3.connect(DB_PATH) as con:
        # Open sportsbook bets (straight + parlays)
        row = con.execute(
            "SELECT COUNT(*) FROM bets_table WHERE discord_id=? AND status='Pending' AND parlay_id IS NULL",
            (user_id,)
        ).fetchone()
        parlay_row = con.execute(
            "SELECT COUNT(*) FROM parlays_table WHERE discord_id=? AND status='Pending'",
            (user_id,)
        ).fetchone()
        result["bets"] = (row[0] or 0) + (parlay_row[0] or 0)

        # Open prediction contracts
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM prediction_contracts WHERE discord_id=? AND status='open'",
                (user_id,)
            ).fetchone()
            result["contracts"] = row[0] or 0
        except sqlite3.OperationalError:
            pass  # table may not exist

    return result


def _get_leaderboard_rank(user_id: int) -> tuple[int, int]:
    # NOTE: Could use SQL RANK() window function for O(1) lookup instead of
    # fetching all rows, but user count is small (~31 owners) so linear scan is fine.
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT discord_id FROM users_table ORDER BY balance DESC"
        ).fetchall()
    total = len(rows)
    for i, (did,) in enumerate(rows, 1):
        if did == user_id:
            return i, total
    return total, total


def _determine_status(user_id: int) -> str:
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
    # Color based on trend
    color = "var(--win)" if data[-1] >= data[0] else "var(--loss)"
    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;height:{height}px;">
      <polyline points="{' '.join(points)}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>'''


# ═════════════════════════════════════════════════════════════════════════════
#  FLOW HUB CARD
# ═════════════════════════════════════════════════════════════════════════════

_FLOW_CSS = """\
.hero-section { padding: 20px; text-align: center; }
.hero-label { font-family: 'Outfit'; font-weight: 700; font-size: var(--font-sm); color: var(--gold-dim); letter-spacing: 2px; text-transform: uppercase; }
.hero-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-display-size); color: var(--text-primary); }
.hero-delta { font-family: 'JetBrains Mono'; font-weight: 600; font-size: var(--font-sm); margin-top: 4px; }
.hero-delta.positive { color: var(--win); }
.hero-delta.negative { color: var(--loss); }

.sparkline-section { padding: 0 20px 12px; text-align: center; }
.sparkline-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; }

.stat-grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.stat-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 10px; text-align: center; border-top: 1px solid rgba(255,255,255,0.06); }
.stat-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 4px; }
.stat-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-lg); color: var(--text-primary); }
.stat-value.green { color: var(--win); }
.stat-value.red { color: var(--loss); }
.stat-value.gold { color: var(--gold); }

.info-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 0 20px 12px; }
.info-cell { background: rgba(255,255,255,0.03); border-radius: var(--border-radius-sm); padding: 12px; }
.info-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.info-value { font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-xl); color: var(--text-primary); margin-top: 4px; }
.info-sub { font-family: 'Outfit'; font-weight: 600; font-size: 11px; color: var(--text-muted); margin-top: 2px; }

.nav-footer { text-align: center; padding: 8px 20px 14px; font-family: 'Outfit'; font-weight: 600; font-size: 10px; color: var(--text-dim); letter-spacing: 1px; }
"""

# Status bar mapping: internal status → shared CSS class
_STATUS_MAP = {
    "top10": "jackpot",
    "positive": "win",
    "negative": "loss",
}


async def build_flow_card(user_id: int) -> bytes:
    """
    Build the unified Flow Hub card for a user.
    Returns PNG bytes.
    """
    # ── Gather data ───────────────────────────────────────────────────────
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    spark_data = _get_sparkline_data(user_id, days=7)
    wins, losses, pushes = _get_lifetime_record(user_id)
    total_wagered = _get_total_wagered(user_id)
    positions = _get_active_positions(user_id)
    rank, total_users = _get_leaderboard_rank(user_id)
    status = _determine_status(user_id)

    total_bets = wins + losses + pushes
    win_rate = (wins / total_bets * 100) if total_bets > 0 else 0
    season_start = _get_season_start_balance(user_id)
    roi = ((balance - season_start) / season_start * 100) if season_start > 0 else 0

    # ── Delta string ──────────────────────────────────────────────────────
    delta_str = f"+${delta:,}" if delta >= 0 else f"-${abs(delta):,}"
    delta_class = "positive" if delta >= 0 else "negative"

    # ── Win rate color ────────────────────────────────────────────────────
    if total_bets > 0:
        wr_class = "green" if win_rate >= 50 else "red"
    else:
        wr_class = ""

    # ── Leaderboard color ─────────────────────────────────────────────────
    if rank <= 3:
        lb_class = "gold"
    elif rank <= 10:
        lb_class = "green"
    else:
        lb_class = ""

    # ── Active positions summary ──────────────────────────────────────────
    parts = []
    if positions["bets"]:
        parts.append(f"{positions['bets']} open bet{'s' if positions['bets'] != 1 else ''}")
    if positions["contracts"]:
        parts.append(f"{positions['contracts']} contract{'s' if positions['contracts'] != 1 else ''}")
    if not parts:
        parts.append("No active positions")
    pos_summary = " \u00b7 ".join(parts)
    pos_count = positions["bets"] + positions["contracts"]

    # ── ROI color ─────────────────────────────────────────────────────────
    roi_class = "green" if roi >= 0 else "red"

    # ── Sparkline SVG ─────────────────────────────────────────────────────
    sparkline_html = _sparkline_svg(spark_data)

    # ── Status bar CSS class ──────────────────────────────────────────────
    status_class = _STATUS_MAP.get(status, "win")

    # ── Build HTML ────────────────────────────────────────────────────────
    body = f"""<style>{_FLOW_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">\U0001f4b0</div>
    <div class="game-title-group">
      <div class="game-title">ATLAS FLOW</div>
      <div class="game-subtitle">ECONOMY HUB</div>
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
    <div class="stat-label">LEADERBOARD</div>
    <div class="stat-value {lb_class}">#{rank} of {total_users}</div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Info Panel -->
<div class="info-panel">
  <div class="info-cell">
    <div class="info-label">ACTIVE POSITIONS</div>
    <div class="info-value">{pos_count}</div>
    <div class="info-sub">{esc(pos_summary)}</div>
  </div>
  <div class="info-cell">
    <div class="info-label">ROI</div>
    <div class="info-value {roi_class}">{roi:+.1f}%</div>
    <div class="info-sub">from ${STARTING_BALANCE:,} start</div>
  </div>
</div>

<div class="gold-divider"></div>

<!-- Navigation Footer -->
<div class="nav-footer">Sportsbook \u00b7 Casino \u00b7 Markets \u00b7 Wallet</div>
"""

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPER
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(png_bytes: bytes, filename: str = "flow.png") -> discord.File:
    """Convert PNG bytes to a discord.File."""
    return discord.File(io.BytesIO(png_bytes), filename=filename)

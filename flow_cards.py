"""
flow_cards.py — ATLAS Flow Hub Card Builder
═══════════════════════════════════════════════════════════════════════════════
Uses ATLASCard renderer to build the unified Flow Hub card showing a user's
complete financial overview: balance, betting record, active positions, and
cross-module stats (sportsbook + casino + prediction markets).

Integration:
    from flow_cards import build_flow_card

    img = build_flow_card(user_id=interaction.user.id)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    file = discord.File(buf, filename="flow.png")
═══════════════════════════════════════════════════════════════════════════════
"""

import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import discord

from PIL import Image
from atlas_card_renderer import ATLASCard, CardSection, ICON_DIR

# ── Config ────────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
STARTING_BALANCE = 1000

FLOW_ICON = ICON_DIR / "flow.png"
# Fallback to sportsbook icon if flow icon not created yet
if not FLOW_ICON.exists():
    FLOW_ICON = ICON_DIR / "sportsbook.png"


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
#  FLOW HUB CARD
# ═════════════════════════════════════════════════════════════════════════════

def build_flow_card(user_id: int) -> Image.Image:
    """
    Build the unified Flow Hub card for a user.
    Returns a Pillow Image.
    """
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
    roi = ((balance - STARTING_BALANCE) / STARTING_BALANCE * 100) if STARTING_BALANCE > 0 else 0

    card = ATLASCard(
        module_icon=FLOW_ICON,
        module_title="ATLAS FLOW",
        module_subtitle="ECONOMY HUB",
        version="v1.0",
    )

    # 1. Hero balance
    delta_str = f"+${delta}" if delta >= 0 else f"-${abs(delta)}"
    card.add_section(CardSection.hero_number(
        "YOUR BALANCE",
        f"${balance:,}",
        delta=f"{delta_str} this week",
        delta_positive=delta >= 0,
    ))

    # 2. Sparkline (drawn inline with hero)
    card.add_section(CardSection.sparkline("7-DAY", spark_data))

    # 3. Stat grid
    card.add_section(CardSection.stat_grid([
        {"label": "LIFETIME RECORD", "value": f"{wins}-{losses}-{pushes}"},
        {"label": "WIN RATE", "value": f"{win_rate:.1f}%",
         "value_color": "green" if win_rate >= 50 else ("red" if total_bets > 0 else None)},
        {"label": "TOTAL WAGERED", "value": f"${total_wagered:,}"},
        {"label": "LEADERBOARD", "value": f"#{rank} of {total_users}",
         "value_color": "gold" if rank <= 3 else ("green" if rank <= 10 else None)},
    ], columns=2))

    # 4. Active positions info panel
    parts = []
    if positions["bets"]:
        parts.append(f"{positions['bets']} open bet{'s' if positions['bets'] != 1 else ''}")
    if positions["contracts"]:
        parts.append(f"{positions['contracts']} contract{'s' if positions['contracts'] != 1 else ''}")
    if not parts:
        parts.append("No active positions")

    pos_summary = " \u00b7 ".join(parts)  # middle dot separator

    card.add_section(CardSection.info_panel([
        {
            "label": "ACTIVE POSITIONS",
            "value": str(positions["bets"] + positions["contracts"]),
            "sub": pos_summary,
        },
        {
            "label": "ROI",
            "value": f"{roi:+.1f}%",
            "sub": f"from ${STARTING_BALANCE:,} start",
            "value_color": "green" if roi >= 0 else "red",
        },
    ]))

    # 5. Footer with navigation hint
    card.add_section(CardSection.text_block(
        "Sportsbook \u00b7 Casino \u00b7 Markets \u00b7 Wallet",
        centered=True,
        font_size=10,
    ))

    card.set_status_bar(status)
    return card.render()


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPER
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(img, filename: str = "flow.png") -> discord.File:
    """Convert a Pillow Image to a discord.File."""
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return discord.File(buf, filename=filename)

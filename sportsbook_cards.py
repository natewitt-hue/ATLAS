"""
sportsbook_cards.py — Sportsbook Card Builders
═══════════════════════════════════════════════════════════════════════════════
Uses ATLASCard renderer to build the main sportsbook hub card and the
stats/profile card. These functions query the sportsbook DB and return
a Pillow Image ready for discord.py.

Integration:
    from sportsbook_cards import build_sportsbook_card, build_stats_card

    # In your sportsbook cog:
    img = build_sportsbook_card(user_id=interaction.user.id)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    file = discord.File(buf, filename="sportsbook.png")
    embed.set_image(url="attachment://sportsbook.png")
    await interaction.response.send_message(embed=embed, file=file, view=view)
═══════════════════════════════════════════════════════════════════════════════
"""

import io
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

# Import the renderer (adjust path as needed for your project layout)
from atlas_card_renderer import ATLASCard, CardSection, ICON_DIR

# ── Config ────────────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
SPORTSBOOK_ICON = ICON_DIR / "sportsbook.png"
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
#  MAIN SPORTSBOOK CARD
# ═════════════════════════════════════════════════════════════════════════════

def build_sportsbook_card(user_id: int) -> "Image.Image":
    """
    Build the main sportsbook hub card for a user.
    Returns a Pillow Image.
    """
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    spark_data = _get_sparkline_data(user_id, days=7)
    results, record = _get_last_n_results(user_id, n=5)
    open_count, wagered, payout = _get_open_bets(user_id)
    status = _determine_status(user_id)

    card = ATLASCard(
        module_icon=SPORTSBOOK_ICON,
        module_title="ATLAS SPORTSBOOK",
        module_subtitle="GLOBAL WAGERING",
        version="v5.0",
    )

    # Hero balance
    delta_str = f"+${delta}" if delta >= 0 else f"-${abs(delta)}"
    card.add_section(CardSection.hero_number(
        "YOUR BALANCE",
        f"${balance:,}",
        delta=f"{delta_str} this week",
        delta_positive=delta >= 0,
    ))

    # Sparkline (drawn inline with hero)
    card.add_section(CardSection.sparkline("7-DAY", spark_data))

    # Win/Loss ticker
    if results:
        card.add_section(CardSection.win_loss_ticker(results, record=record))

    # Open bets / Potential payout
    card.add_section(CardSection.info_panel([
        {
            "label": "OPEN BETS",
            "value": str(open_count),
            "sub": f"${wagered:,} wagered",
            "sub_highlight": f"${wagered:,}",
        },
        {
            "label": "POTENTIAL PAYOUT",
            "value": f"${payout:,}",
            "sub": "if all bets hit",
            "value_color": "green",
        },
    ]))

    # Sport footer
    card.add_section(CardSection.sport_footer(
        sports=["TSL", "NFL", "NBA", "MLB", "NHL"],
        active="TSL",
        controller_icon=True,
    ))

    card.set_status_bar(status)
    return card.render()


# ═════════════════════════════════════════════════════════════════════════════
#  STATS / PROFILE CARD
# ═════════════════════════════════════════════════════════════════════════════

def build_stats_card(user_id: int) -> "Image.Image":
    """
    Build the detailed bettor stats card for a user.
    Returns a Pillow Image.
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

    card = ATLASCard(
        module_icon=SPORTSBOOK_ICON,
        module_title="BETTOR PROFILE",
        module_subtitle="YOUR STATS",
        version="v5.0",
    )

    # Hero balance
    delta_str = f"+${delta}" if delta >= 0 else f"-${abs(delta)}"
    card.add_section(CardSection.hero_number(
        "YOUR BALANCE",
        f"${balance:,}",
        delta=f"{delta_str} this week",
        delta_positive=delta >= 0,
    ))

    # 30-day sparkline
    card.add_section(CardSection.sparkline("30-DAY", spark_data))

    # Win/Loss ticker (last 10)
    if results:
        card.add_section(CardSection.win_loss_ticker(results, record=record))

    # Stat grid
    card.add_section(CardSection.stat_grid([
        {"label": "LIFETIME RECORD", "value": f"{wins}-{losses}-{pushes}"},
        {"label": "WIN RATE", "value": f"{win_rate:.1f}%",
         "value_color": "green" if win_rate >= 50 else "red"},
        {"label": "TOTAL WAGERED", "value": f"${total_wagered:,}"},
        {"label": "TOTAL WON", "value": f"${total_won:,}", "value_color": "green"},
        {"label": "ROI", "value": f"{roi:+.1f}%",
         "value_color": "green" if roi >= 0 else "red"},
        {"label": "LEADERBOARD", "value": f"#{rank} of {total_users}",
         "value_color": "gold" if rank <= 10 else None},
    ], columns=2))

    # Open bets summary
    if open_count > 0:
        card.add_section(CardSection.info_panel([
            {"label": "OPEN BETS", "value": str(open_count),
             "sub": f"${wagered:,} at risk", "sub_highlight": f"${wagered:,}"},
            {"label": "POTENTIAL PAYOUT", "value": f"${payout:,}",
             "sub": "if all bets hit", "value_color": "green"},
        ]))

    card.set_status_bar(status)
    return card.render()


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(img, filename: str = "card.png") -> discord.File:
    """Convert a Pillow Image to a discord.File."""
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return discord.File(buf, filename=filename)

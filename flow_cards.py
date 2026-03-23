"""
flow_cards.py — ATLAS Flow Hub Card Builders
═══════════════════════════════════════════════════════════════════════════════
Uses the ATLAS HTML engine to build all Flow economy dashboard cards:
  - Dashboard (overview)   - My Bets (sportsbook positions)
  - Portfolio (predictions) - Wallet (transaction ledger)
  - Leaderboard (rankings)

Integration:
    from flow_cards import build_flow_card, build_my_bets_card, card_to_file

    png = await build_flow_card(user_id=interaction.user.id)
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


def _get_lifetime_record(user_id: int) -> tuple[int, int, int]:
    """Get (wins, losses, pushes) across all settled straight bets AND parlays."""
    with sqlite3.connect(DB_PATH) as con:
        # Straight bets
        s = con.execute(
            """SELECT
                 SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Push' THEN 1 ELSE 0 END)
               FROM bets_table
               WHERE discord_id = ? AND parlay_id IS NULL""",
            (user_id,)
        ).fetchone()
        # Parlays
        p = con.execute(
            """SELECT
                 SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='Push' THEN 1 ELSE 0 END)
               FROM parlays_table
               WHERE discord_id = ?""",
            (user_id,)
        ).fetchone()
    return (
        (s[0] or 0) + (p[0] or 0),
        (s[1] or 0) + (p[1] or 0),
        (s[2] or 0) + (p[2] or 0),
    )


def _get_last_n_results(user_id: int, n: int = 10) -> list[str]:
    """Get last N bet results (newest first) as list of 'Won'/'Lost'/'Push'."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """SELECT status, created_at FROM (
                 SELECT status, created_at FROM bets_table
                   WHERE discord_id = ? AND status IN ('Won','Lost','Push')
                     AND parlay_id IS NULL
                 UNION ALL
                 SELECT status, created_at FROM parlays_table
                   WHERE discord_id = ? AND status IN ('Won','Lost','Push')
               ) ORDER BY created_at DESC LIMIT ?""",
            (user_id, user_id, n),
        ).fetchall()
    return [r[0] for r in rows]


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
        row = con.execute(
            "SELECT rank, total FROM ("
            "  SELECT discord_id, RANK() OVER (ORDER BY balance DESC) AS rank, "
            "  COUNT(*) OVER () AS total FROM users_table"
            ") WHERE discord_id = ?",
            (user_id,),
        ).fetchone()
    if row:
        return row[0], row[1]
    total = con.execute("SELECT COUNT(*) FROM users_table").fetchone()[0]
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

_FLOW_CSS = """\
.hero-section { padding: 24px 28px 18px; text-align: center; }
.hero-label {
  font-family: var(--font-display); font-weight: 700; font-size: 14px;
  color: var(--gold-dim); letter-spacing: 2.5px; text-transform: uppercase;
}
.hero-value {
  font-family: var(--font-mono); font-weight: 800; font-size: 44px;
  color: var(--text-primary); line-height: 1.1; margin-top: 4px;
}
.hero-delta {
  display: inline-block; font-family: var(--font-mono); font-weight: 600;
  font-size: 13px; margin-top: 8px; padding: 3px 10px;
  border-radius: 20px;
}
.hero-delta.positive { color: var(--win); background: rgba(52,211,153,0.08); }
.hero-delta.negative { color: var(--loss); background: rgba(251,113,133,0.08); }

.stat-grid {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  gap: 8px; padding: 0 28px 20px;
}
.stat-box {
  background: linear-gradient(180deg, var(--panel-bg) 0%, var(--bg) 100%);
  border-radius: var(--border-radius-sm); padding: 14px 12px;
  text-align: center;
  border: 1px solid var(--panel-border);
  border-top: 1px solid var(--panel-border-top);
}
.stat-box-label {
  font-family: var(--font-display); font-weight: 700; font-size: 10px;
  color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase;
  margin-bottom: 6px;
}
.stat-box-value {
  font-family: var(--font-mono); font-weight: 800; font-size: 20px;
  color: var(--text-primary);
}
.stat-box-value.green { color: var(--win); }
.stat-box-value.red { color: var(--loss); }
.stat-box-value.gold { color: var(--gold); }

.flow-footer {
  padding: 10px 28px 14px; text-align: center;
  font-family: var(--font-display); font-weight: 600; font-size: 10px;
  color: var(--text-dim); letter-spacing: 1.5px; text-transform: uppercase;
}

.results-strip {
  display: flex; justify-content: center; gap: 6px;
  padding: 6px 28px 2px;
}
.results-strip .dot {
  width: 10px; height: 10px; border-radius: 50%;
}
.results-strip .dot.win { background: var(--win); box-shadow: 0 0 4px rgba(74,222,128,0.4); }
.results-strip .dot.loss { background: var(--loss); box-shadow: 0 0 4px rgba(248,113,113,0.4); }
.results-strip .dot.push { background: var(--push, #FBBF24); box-shadow: 0 0 4px rgba(251,191,36,0.3); }
.results-strip .dot.empty { background: rgba(255,255,255,0.06); }

.streak-badge {
  display: inline-block; font-family: var(--font-mono); font-weight: 700;
  font-size: 10px; padding: 1px 6px; border-radius: 3px; margin-left: 4px;
  vertical-align: middle;
}
.streak-badge.hot { background: rgba(74,222,128,0.15); color: var(--win); }
.streak-badge.cold { background: rgba(248,113,113,0.15); color: var(--loss); }
"""

# Status bar mapping: internal status → shared CSS class
_STATUS_MAP = {
    "top10": "jackpot",
    "positive": "win",
    "negative": "loss",
}


def _gather_flow_data(user_id: int) -> dict:
    """Sync: collect all DB data for flow card in one executor dispatch."""
    from flow_wallet import get_theme
    balance = _get_balance(user_id)
    wins, losses, pushes = _get_lifetime_record(user_id)
    last_results = _get_last_n_results(user_id, 10)
    return {
        "balance": balance,
        "delta": _get_weekly_delta(user_id),
        "wins": wins, "losses": losses, "pushes": pushes,
        "last_results": last_results,
        "total_wagered": _get_total_wagered(user_id),
        "positions": _get_active_positions(user_id),
        "rank_total": _get_leaderboard_rank(user_id),
        "status": _determine_status(user_id),
        "season_start": _get_season_start_balance(user_id),
        "theme_id": get_theme(user_id),
    }


async def build_flow_card(user_id: int) -> bytes:
    """
    Build the unified Flow Hub card for a user.
    Returns PNG bytes.  700px wide, 2× DPI, theme-aware.
    """
    from atlas_themes import get_theme

    # ── Gather data (dispatched to thread pool) ───────────────────────────
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_flow_data, user_id)
    balance = d["balance"]
    delta = d["delta"]
    wins, losses, pushes = d["wins"], d["losses"], d["pushes"]
    last_results = d["last_results"]
    positions = d["positions"]
    rank, total_users = d["rank_total"]
    status = d["status"]
    theme_id = d["theme_id"]

    total_bets = wins + losses + pushes
    win_rate = (wins / total_bets * 100) if total_bets > 0 else 0
    season_start = d["season_start"]
    roi = ((balance - season_start) / season_start * 100) if season_start > 0 else 0
    net_pnl = balance - season_start

    # ── Streak from last results ──────────────────────────────────────────
    streak_text = ""
    if last_results:
        first = last_results[0]
        count = 0
        for r in last_results:
            if r == first:
                count += 1
            else:
                break
        if count >= 2:
            prefix = "W" if first == "Won" else ("L" if first == "Lost" else "P")
            streak_cls = "hot" if first == "Won" else "cold"
            streak_text = f'<span class="streak-badge {streak_cls}">{prefix}{count}</span>'

    # ── Results dot strip HTML ────────────────────────────────────────────
    dots_html = ""
    for r in reversed(last_results):  # oldest→newest left→right
        cls = "win" if r == "Won" else ("loss" if r == "Lost" else "push")
        dots_html += f'<span class="dot {cls}"></span>'
    # Pad to 10 with empty dots
    for _ in range(10 - len(last_results)):
        dots_html = f'<span class="dot empty"></span>' + dots_html

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

    # ── Open bets count ───────────────────────────────────────────────────
    open_bets = positions["bets"] + positions["contracts"]

    # ── P&L / ROI colors ─────────────────────────────────────────────────
    pnl_class = "green" if net_pnl >= 0 else "red"
    roi_class = "green" if roi >= 0 else "red"

    # ── Status bar CSS class ──────────────────────────────────────────────
    status_class = _STATUS_MAP.get(status, "win")

    # ── Theme-driven inline styles ────────────────────────────────────────
    theme = get_theme(theme_id)
    hero_class = theme.get("hero_class", "")
    divider_bg = theme.get("divider_style", "linear-gradient(90deg, transparent, var(--gold-deep), transparent)")
    default_border = theme.get("stat_left_border_default", "2px solid var(--gold-deep)")
    accent_border = theme.get("stat_left_border_accent", "2px solid var(--gold)")
    win_border = theme.get("stat_left_border_win", "2px solid var(--win)")
    box_shadow = theme.get("stat_box_shadow", "none")
    win_shadow = theme.get("stat_box_shadow_win", box_shadow)

    # P&L / ROI boxes get win glow when positive, loss border when negative
    pnl_border = win_border if net_pnl >= 0 else "2px solid var(--loss)"
    pnl_shadow = win_shadow if net_pnl >= 0 else box_shadow
    roi_border = win_border if roi >= 0 else "2px solid var(--loss)"
    roi_shadow = win_shadow if roi >= 0 else box_shadow

    # ── Net P&L display string ────────────────────────────────────────────
    pnl_str = f"+${net_pnl:,}" if net_pnl >= 0 else f"-${abs(net_pnl):,}"

    # ── Build HTML ────────────────────────────────────────────────────────
    body = f"""<style>{_FLOW_CSS}</style>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("wallet", "\U0001f4b0")}</div>
    <div class="game-title-group">
      <div class="game-title">ATLAS FLOW</div>
      <div class="game-subtitle">ECONOMY HUB</div>
    </div>
  </div>
</div>

<div class="gold-divider" style="background:{divider_bg};"></div>

<!-- Hero Balance -->
<div class="hero-section">
  <div class="hero-label">YOUR BALANCE</div>
  <div class="hero-value {hero_class}">${balance:,}</div>
  <div class="hero-delta {delta_class}">{esc(delta_str)} this week</div>
</div>

<!-- Results dot strip -->
<div class="results-strip">{dots_html}</div>

<div class="gold-divider" style="background:{divider_bg};"></div>

<!-- Stat Grid Row 1 -->
<div class="stat-grid">
  <div class="stat-box" style="border-left:{default_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">RECORD</div>
    <div class="stat-box-value">{wins}-{losses}-{pushes}{streak_text}</div>
  </div>
  <div class="stat-box" style="border-left:{default_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">WIN RATE</div>
    <div class="stat-box-value {wr_class}">{win_rate:.1f}%</div>
  </div>
  <div class="stat-box" style="border-left:{accent_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">RANK</div>
    <div class="stat-box-value gold">#{rank}/{total_users}</div>
  </div>
</div>

<!-- Stat Grid Row 2 -->
<div class="stat-grid">
  <div class="stat-box" style="border-left:{pnl_border};box-shadow:{pnl_shadow};">
    <div class="stat-box-label">NET P&amp;L</div>
    <div class="stat-box-value {pnl_class}">{pnl_str}</div>
  </div>
  <div class="stat-box" style="border-left:{default_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">OPEN BETS</div>
    <div class="stat-box-value">{open_bets}</div>
  </div>
  <div class="stat-box" style="border-left:{roi_border};box-shadow:{roi_shadow};">
    <div class="stat-box-label">ROI</div>
    <div class="stat-box-value {roi_class}">{roi:+.1f}%</div>
  </div>
</div>

<div class="flow-footer">ATLAS Flow Economy</div>
"""

    full_html = wrap_card(body, status_class=status_class, theme_id=theme_id)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  DISCORD HELPER
# ═════════════════════════════════════════════════════════════════════════════

def card_to_file(png_bytes: bytes, filename: str = "flow.png") -> discord.File:
    """Convert PNG bytes to a discord.File."""
    return discord.File(io.BytesIO(png_bytes), filename=filename)


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED CSS — used by multiple tab cards
# ═════════════════════════════════════════════════════════════════════════════

_TAB_CSS = """\
.section-label { font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; padding: 0 20px; margin-bottom: 6px; }
.bet-row { padding: var(--space-sm) 20px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.bet-row:last-child { border-bottom: none; }
.bet-team { font-family: var(--font-display); font-weight: 700; font-size: var(--font-base); color: var(--text-primary); }
.bet-type { font-family: var(--font-display); font-weight: 600; font-size: var(--font-sm); color: var(--text-muted); margin-left: var(--space-xs); }
.bet-details { display: flex; justify-content: space-between; margin-top: 2px; font-family: var(--font-mono); font-size: var(--font-sm); }
.bet-wager { color: var(--text-sub); }
.bet-potential { color: var(--win); font-weight: 700; }
.parlay-header { font-family: var(--font-display); font-weight: 700; font-size: var(--font-base); color: var(--text-primary); }
.parlay-odds { font-family: var(--font-mono); font-weight: 700; color: var(--gold); }
.parlay-legs { display: flex; flex-wrap: wrap; gap: var(--space-xs); margin-top: var(--space-xs); }
.parlay-leg { font-family: var(--font-display); font-size: var(--font-xs); padding: 2px 6px; border-radius: 3px; }
.parlay-leg.won { background: rgba(74,222,128,0.15); color: var(--win); }
.parlay-leg.lost { background: rgba(248,113,113,0.15); color: var(--loss); }
.parlay-leg.pending { background: rgba(255,255,255,0.06); color: var(--text-muted); }
.empty-state { text-align: center; padding: var(--space-xl) 20px; font-family: var(--font-display); font-weight: 600; font-size: var(--font-base); color: var(--text-muted); }

.side-badge { display: inline-block; font-family: var(--font-display); font-weight: 700; font-size: var(--font-xs); padding: 2px var(--space-sm); border-radius: 3px; text-transform: uppercase; }
.side-badge.yes { background: rgba(74,222,128,0.15); color: var(--win); }
.side-badge.no { background: rgba(248,113,113,0.15); color: var(--loss); }
.position-row { padding: var(--space-sm) 20px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.position-row:last-child { border-bottom: none; }
.position-title { font-family: var(--font-display); font-weight: 600; font-size: var(--font-sm); color: var(--text-primary); margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.position-meta { display: flex; justify-content: space-between; margin-top: 2px; font-family: var(--font-mono); font-size: var(--font-xs); }
.position-cost { color: var(--text-sub); }
.position-payout { color: var(--win); font-weight: 700; }
.status-dot { display: inline-block; font-size: 12px; margin-left: 6px; }

.txn-table { width: 100%; padding: 0 var(--space-md); }
.txn-row { display: flex; align-items: center; padding: 5px var(--space-sm); border-bottom: 1px solid rgba(255,255,255,0.03); gap: var(--space-sm); }
.txn-row:last-child { border-bottom: none; }
.txn-source { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
.txn-source.casino { background: rgba(168,85,247,0.2); }
.txn-source.sportsbook { background: rgba(74,222,128,0.2); }
.txn-source.prediction { background: rgba(96,165,250,0.2); }
.txn-source.admin { background: rgba(212,175,55,0.2); }
.txn-source.stipend { background: rgba(45,212,191,0.2); }
.txn-source.other { background: rgba(255,255,255,0.06); }
.txn-desc { flex: 1; font-family: var(--font-display); font-weight: 600; font-size: var(--font-xs); color: var(--text-sub); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.txn-amount { font-family: var(--font-mono); font-weight: 700; font-size: var(--font-sm); min-width: 60px; text-align: right; }
.txn-amount.credit { color: var(--win); }
.txn-amount.debit { color: var(--loss); }
.txn-bal { font-family: var(--font-mono); font-weight: 600; font-size: var(--font-xs); color: var(--text-dim); min-width: 55px; text-align: right; }

.lb-table { width: 100%; padding: 0 var(--space-lg); }
.lb-header { display: flex; padding: 6px var(--space-sm); font-family: var(--font-display); font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.lb-row { display: flex; padding: 7px var(--space-sm); border-bottom: 1px solid rgba(255,255,255,0.03); align-items: center; }
.lb-row:last-child { border-bottom: none; }
.lb-row.viewer { background: rgba(212,175,55,0.08); border: 1px solid rgba(212,175,55,0.2); border-radius: var(--border-radius-sm); }
.lb-rank { width: 36px; font-family: var(--font-mono); font-weight: 800; font-size: var(--font-sm); color: var(--text-sub); flex-shrink: 0; }
.lb-rank.gold { color: var(--gold); }
.lb-name { flex: 1; font-family: var(--font-display); font-weight: 700; font-size: var(--font-sm); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lb-stat { width: 60px; font-family: var(--font-mono); font-weight: 700; font-size: var(--font-xs); text-align: right; }
.lb-stat.green { color: var(--win); }
.lb-stat.red { color: var(--loss); }
.lb-stat.neutral { color: var(--text-sub); }

.footer-text { text-align: center; padding: var(--space-sm) 20px 14px; font-family: var(--font-display); font-weight: 600; font-size: 10px; color: var(--text-dim); letter-spacing: 1px; }
"""


# ═════════════════════════════════════════════════════════════════════════════
#  PAYOUT HELPER
# ═════════════════════════════════════════════════════════════════════════════

from odds_utils import american_to_str as _american_to_str, payout_calc as _payout_calc  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════════
#  MY BETS CARD
# ═════════════════════════════════════════════════════════════════════════════

def _relative_time(ts_str: str | None) -> str:
    """Convert a SQLite timestamp string to a relative time like '2h ago'."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        secs = int(diff.total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        days = secs // 86400
        return f"{days}d ago"
    except (ValueError, TypeError):
        return ""


def _gather_my_bets_data(user_id: int) -> dict:
    """Sync: collect DB data for My Bets card (open + recently settled)."""
    balance = _get_balance(user_id)
    wins, losses, pushes = _get_lifetime_record(user_id)
    with sqlite3.connect(DB_PATH) as con:
        # Open straight bets
        open_straight = con.execute(
            "SELECT matchup, bet_type, pick, wager_amount, odds, line, status, week, created_at "
            "FROM bets_table WHERE discord_id=? AND status='Pending' AND parlay_id IS NULL "
            "ORDER BY bet_id DESC",
            (user_id,),
        ).fetchall()
        # Recently settled straight bets (last 48h, max 8)
        settled_straight = con.execute(
            "SELECT matchup, bet_type, pick, wager_amount, odds, line, status, week, created_at "
            "FROM bets_table WHERE discord_id=? AND status IN ('Won','Lost','Push') "
            "AND parlay_id IS NULL AND created_at >= datetime('now', '-2 days') "
            "ORDER BY created_at DESC LIMIT 8",
            (user_id,),
        ).fetchall()
        # Open parlays
        open_parlays = con.execute(
            "SELECT parlay_id, week, combined_odds, wager_amount, status, created_at "
            "FROM parlays_table WHERE discord_id=? AND status='Pending' "
            "ORDER BY rowid DESC",
            (user_id,),
        ).fetchall()
        # Recently settled parlays
        settled_parlays = con.execute(
            "SELECT parlay_id, week, combined_odds, wager_amount, status, created_at "
            "FROM parlays_table WHERE discord_id=? AND status IN ('Won','Lost','Push') "
            "AND created_at >= datetime('now', '-2 days') "
            "ORDER BY created_at DESC LIMIT 4",
            (user_id,),
        ).fetchall()
        # Batch-fetch legs for ALL parlays (open + settled)
        all_parlays = list(open_parlays) + list(settled_parlays)
        legs_map: dict[str, list[dict]] = {}
        if all_parlays:
            pids = [p[0] for p in all_parlays]
            placeholders = ",".join("?" * len(pids))
            leg_rows = con.execute(
                f"SELECT parlay_id, pick, bet_type, line, status FROM parlay_legs "
                f"WHERE parlay_id IN ({placeholders}) ORDER BY parlay_id, leg_index",
                pids,
            ).fetchall()
            for parlay_id, pick, btype, line, status in leg_rows:
                legs_map.setdefault(parlay_id, []).append({
                    "pick": pick, "bet_type": btype, "line": line, "status": status,
                })
        # Real sportsbook bets (NFL/NBA/MLB/NHL etc.)
        open_real: list = []
        settled_real: list = []
        try:
            open_real = con.execute(
                "SELECT sport_key, bet_type, pick, wager_amount, odds, line, status, created_at "
                "FROM real_bets WHERE discord_id=? AND status='Pending' "
                "ORDER BY bet_id DESC",
                (user_id,),
            ).fetchall()
            settled_real = con.execute(
                "SELECT sport_key, bet_type, pick, wager_amount, odds, line, status, created_at "
                "FROM real_bets WHERE discord_id=? AND status IN ('Won','Lost','Push') "
                "AND created_at >= datetime('now', '-2 days') "
                "ORDER BY created_at DESC LIMIT 6",
                (user_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            pass  # real_bets table may not exist yet
    return {
        "balance": balance, "record": (wins, losses, pushes),
        "open_straight": open_straight, "settled_straight": settled_straight,
        "open_parlays": open_parlays, "settled_parlays": settled_parlays,
        "legs_map": legs_map,
        "open_real": open_real, "settled_real": settled_real,
    }


_SPORT_LABELS = {
    "americanfootball_nfl": "NFL", "basketball_nba": "NBA",
    "baseball_mlb": "MLB", "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB", "mma_ufc": "UFC",
    "soccer_epl": "EPL", "soccer_mls": "MLS",
    "basketball_wnba": "WNBA",
}

# ── My Bets CSS ──────────────────────────────────────────────────────────
_MY_BETS_CSS = """\
.bet-card {
  background: linear-gradient(180deg, var(--panel-bg) 0%, var(--bg) 100%);
  border: 1px solid var(--panel-border);
  border-top: 1px solid var(--panel-border-top);
  border-radius: var(--border-radius-sm);
  padding: 10px 14px;
  margin: 0 20px var(--space-sm);
}
.bet-card-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 4px;
}
.status-badge {
  display: inline-block; font-family: var(--font-display); font-weight: 800;
  font-size: 9px; padding: 2px 8px; border-radius: 3px;
  letter-spacing: 1px; text-transform: uppercase;
}
.status-badge.open { background: rgba(212,175,55,0.15); color: var(--gold); }
.status-badge.won { background: rgba(74,222,128,0.15); color: var(--win); }
.status-badge.lost { background: rgba(248,113,113,0.15); color: var(--loss); }
.status-badge.push { background: rgba(251,191,36,0.15); color: var(--push, #FBBF24); }
.bet-time {
  font-family: var(--font-mono); font-size: var(--font-xs);
  color: var(--text-dim);
}
.bet-card-pick {
  font-family: var(--font-display); font-weight: 700;
  font-size: var(--font-base); color: var(--text-primary);
  margin-bottom: 2px;
}
.bet-card-meta {
  font-family: var(--font-display); font-weight: 600;
  font-size: var(--font-sm); color: var(--text-muted);
  margin-bottom: 4px;
}
.bet-card-amounts {
  display: flex; justify-content: space-between;
  font-family: var(--font-mono); font-size: var(--font-sm);
}
.bet-card-amounts .wager { color: var(--text-sub); }
.bet-card-amounts .payout { font-weight: 700; }
.bet-card-amounts .payout.win { color: var(--win); }
.bet-card-amounts .payout.loss { color: var(--loss); }
.bet-card-amounts .payout.push-val { color: var(--push, #FBBF24); }
.bet-card-amounts .payout.pending { color: var(--gold); }

.parlay-title {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 4px;
}
.parlay-title .leg-count {
  font-family: var(--font-display); font-weight: 700;
  font-size: var(--font-base); color: var(--text-primary);
}
.parlay-title .parlay-odds {
  font-family: var(--font-mono); font-weight: 700;
  font-size: var(--font-sm); color: var(--gold);
}

.leg-tree { padding-left: 4px; margin-bottom: 4px; }
.leg-item {
  display: flex; align-items: center; gap: 6px;
  padding: 2px 0;
  font-family: var(--font-display); font-size: var(--font-sm);
}
.leg-icon { font-size: 11px; flex-shrink: 0; width: 14px; text-align: center; }
.leg-icon.won { color: var(--win); }
.leg-icon.lost { color: var(--loss); }
.leg-icon.pending { color: var(--text-muted); }
.leg-text { color: var(--text-sub); }
.leg-connector {
  color: var(--text-dim); font-family: var(--font-mono);
  font-size: 11px; flex-shrink: 0; width: 14px; text-align: center;
}

.section-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px; margin-bottom: var(--space-sm);
}
.section-header-label {
  font-family: var(--font-display); font-weight: 700; font-size: 10px;
  color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase;
}
.section-header-count {
  font-family: var(--font-mono); font-weight: 700; font-size: var(--font-xs);
  color: var(--text-muted);
}

.stat-grid-3col {
  display: grid; grid-template-columns: 1fr 1fr 1fr;
  gap: 8px; padding: 0 20px;
}
"""


def _build_straight_bet_card(
    pick, btype, matchup, wager, odds, line, status, created_at,
    theme, *, sport_tag: str = "",
) -> str:
    """Build HTML for a single straight bet panel."""
    # Status
    st_lower = status.lower() if status != "Pending" else "open"
    st_label = "OPEN" if status == "Pending" else status.upper()

    # Line string
    line_str = ""
    if btype in ("Spread", "Over/Under") and line is not None:
        line_str = f" ({line:+g})" if btype == "Spread" else f" ({line})"

    # Meta line
    meta_parts = []
    if sport_tag:
        meta_parts.append(sport_tag)
    meta_parts.append(f"{btype}{line_str}")
    meta_parts.append(_american_to_str(int(odds)))
    meta_text = " \u00b7 ".join(meta_parts)

    # Amounts
    potential = _payout_calc(wager, odds)
    if status == "Won":
        profit = potential - wager
        payout_text = f"+${profit:,}"
        payout_cls = "win"
    elif status == "Lost":
        payout_text = f"-${wager:,}"
        payout_cls = "loss"
    elif status == "Push":
        payout_text = "PUSH"
        payout_cls = "push-val"
    else:
        payout_text = f"${potential:,}"
        payout_cls = "pending"

    # Theme-aware border
    if status == "Won":
        border = theme.get("stat_left_border_win", "2px solid var(--win)")
    elif status == "Lost":
        border = "2px solid var(--loss)"
    elif status == "Push":
        border = "2px solid var(--push, #FBBF24)"
    else:
        border = theme.get("stat_left_border_default", "2px solid var(--gold-deep)")
    shadow = theme.get("stat_box_shadow", "none")

    time_str = _relative_time(created_at)

    # Matchup as subtitle if different from pick
    matchup_line = ""
    if matchup and str(matchup) != str(pick):
        matchup_line = f'<div class="bet-card-meta" style="color:var(--text-dim);font-size:var(--font-xs);">{esc(str(matchup))}</div>'

    return f'''<div class="bet-card" style="border-left:{border};box-shadow:{shadow};">
  <div class="bet-card-header">
    <span class="status-badge {st_lower}">{st_label}</span>
    <span class="bet-time">{esc(time_str)}</span>
  </div>
  <div class="bet-card-pick">{esc(str(pick))}</div>
  {matchup_line}
  <div class="bet-card-meta">{esc(meta_text)}</div>
  <div class="bet-card-amounts">
    <span class="wager">Wager: ${wager:,}</span>
    <span class="payout {payout_cls}">{payout_text}</span>
  </div>
</div>\n'''


def _build_parlay_card(pid, week, c_odds, wager, status, created_at, legs, theme) -> str:
    """Build HTML for a parlay panel with expanded legs."""
    st_lower = status.lower() if status != "Pending" else "open"
    st_label = "OPEN" if status == "Pending" else status.upper()
    n_legs = len(legs)

    potential = _payout_calc(wager, c_odds)
    if status == "Won":
        profit = potential - wager
        payout_text = f"+${profit:,}"
        payout_cls = "win"
    elif status == "Lost":
        payout_text = f"-${wager:,}"
        payout_cls = "loss"
    elif status == "Push":
        payout_text = "PUSH"
        payout_cls = "push-val"
    else:
        payout_text = f"${potential:,}"
        payout_cls = "pending"

    if status == "Won":
        border = theme.get("stat_left_border_win", "2px solid var(--win)")
    elif status == "Lost":
        border = "2px solid var(--loss)"
    elif status == "Push":
        border = "2px solid var(--push, #FBBF24)"
    else:
        border = theme.get("stat_left_border_accent", "2px solid var(--gold)")
    shadow = theme.get("stat_box_shadow", "none")

    time_str = _relative_time(created_at)

    # Build leg tree
    legs_html = ""
    for i, leg in enumerate(legs):
        leg_pick = leg["pick"]
        leg_status = leg["status"]
        leg_btype = leg.get("bet_type", "")
        leg_line = leg.get("line")
        if leg_status == "Won":
            icon, icon_cls = "\u2714", "won"
        elif leg_status == "Lost":
            icon, icon_cls = "\u2718", "lost"
        else:
            icon, icon_cls = "\u25cb", "pending"
        # Connector symbol
        connector = "\u2514" if i == len(legs) - 1 else "\u251c"
        # Leg meta
        leg_meta = ""
        if leg_btype:
            line_str = ""
            if leg_btype == "Spread" and leg_line is not None:
                line_str = f" {leg_line:+g}"
            elif leg_btype in ("Over/Under", "Totals") and leg_line is not None:
                line_str = f" {leg_line}"
            leg_meta = f" ({leg_btype}{line_str})"
        legs_html += f'''<div class="leg-item">
  <span class="leg-connector">{connector}</span>
  <span class="leg-icon {icon_cls}">{icon}</span>
  <span class="leg-text">{esc(str(leg_pick))}{esc(leg_meta)}</span>
</div>\n'''

    return f'''<div class="bet-card" style="border-left:{border};box-shadow:{shadow};">
  <div class="bet-card-header">
    <span class="status-badge {st_lower}">{st_label}</span>
    <span class="bet-time">{esc(time_str)}</span>
  </div>
  <div class="parlay-title">
    <span class="leg-count">{n_legs}-Leg Parlay</span>
    <span class="parlay-odds">{_american_to_str(int(c_odds))}</span>
  </div>
  <div class="leg-tree">{legs_html}</div>
  <div class="bet-card-amounts">
    <span class="wager">Wager: ${wager:,}</span>
    <span class="payout {payout_cls}">{payout_text}</span>
  </div>
</div>\n'''


async def build_my_bets_card(user_id: int, *, theme_id: str | None = None) -> bytes:
    """Build the My Bets card with themed panels, timestamps, and settled section."""
    from atlas_themes import get_theme
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_my_bets_data, user_id)
    balance = d["balance"]
    wins, losses, pushes = d["record"]
    legs_map = d["legs_map"]
    theme = get_theme(theme_id)

    open_straight = d["open_straight"]
    open_parlays = d["open_parlays"]
    open_real = d["open_real"]
    settled_straight = d["settled_straight"]
    settled_parlays = d["settled_parlays"]
    settled_real = d["settled_real"]

    open_count = len(open_straight) + len(open_parlays) + len(open_real)
    settled_count = len(settled_straight) + len(settled_parlays) + len(settled_real)

    total_risk = (
        sum(b[3] for b in open_straight)
        + sum(p[3] for p in open_parlays)
        + sum(r[3] for r in open_real)
    )
    max_payout = (
        sum(_payout_calc(b[3], b[4]) for b in open_straight)
        + sum(_payout_calc(p[3], p[2]) for p in open_parlays)
        + sum(_payout_calc(r[3], r[4]) for r in open_real)
    )

    # Status bar
    if not open_straight and not open_parlays and not open_real:
        status_class = "jackpot"
    elif max_payout > total_risk:
        status_class = "win"
    else:
        status_class = "loss"

    # ── Open positions section ────────────────────────────────────────────
    open_html = ""
    if open_count > 0:
        open_html += f'''<div class="section-header">
  <span class="section-header-label">OPEN POSITIONS</span>
  <span class="section-header-count">{open_count}</span>
</div>\n'''
        for b in open_straight[:8]:
            matchup, btype, pick, wager, odds, line, status, week, created_at = b
            open_html += _build_straight_bet_card(
                pick, btype, matchup, wager, odds, line, status, created_at, theme,
            )
        for p in open_parlays[:4]:
            pid, week, c_odds, wager, status, created_at = p
            legs = legs_map.get(pid, [])
            open_html += _build_parlay_card(pid, week, c_odds, wager, status, created_at, legs, theme)
        for rb in open_real[:6]:
            sport_key, btype, pick, wager, odds, line, status, created_at = rb
            sport_tag = _SPORT_LABELS.get(sport_key, sport_key.split("_")[-1].upper())
            open_html += _build_straight_bet_card(
                pick, btype, "", wager, odds, line, status, created_at, theme,
                sport_tag=sport_tag,
            )

    # ── Recently settled section ──────────────────────────────────────────
    settled_html = ""
    if settled_count > 0:
        settled_html += '<div class="gold-divider"></div>\n'
        settled_html += f'''<div class="section-header">
  <span class="section-header-label">RECENTLY SETTLED</span>
  <span class="section-header-count">{settled_count}</span>
</div>\n'''
        for b in settled_straight:
            matchup, btype, pick, wager, odds, line, status, week, created_at = b
            settled_html += _build_straight_bet_card(
                pick, btype, matchup, wager, odds, line, status, created_at, theme,
            )
        for p in settled_parlays:
            pid, week, c_odds, wager, status, created_at = p
            legs = legs_map.get(pid, [])
            settled_html += _build_parlay_card(pid, week, c_odds, wager, status, created_at, legs, theme)
        for rb in settled_real:
            sport_key, btype, pick, wager, odds, line, status, created_at = rb
            sport_tag = _SPORT_LABELS.get(sport_key, sport_key.split("_")[-1].upper())
            settled_html += _build_straight_bet_card(
                pick, btype, "", wager, odds, line, status, created_at, theme,
                sport_tag=sport_tag,
            )

    # ── Empty state ───────────────────────────────────────────────────────
    if open_count == 0 and settled_count == 0:
        open_html = '<div class="empty-state">No active bets. Hit /sportsbook to place some!</div>'

    # ── Theme-aware footer boxes ──────────────────────────────────────────
    default_border = theme.get("stat_left_border_default", "2px solid var(--gold-deep)")
    box_shadow = theme.get("stat_box_shadow", "none")
    win_border = theme.get("stat_left_border_win", "2px solid var(--win)")

    body = f"""<style>{_FLOW_CSS}{_TAB_CSS}{_MY_BETS_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("sportsbook", "\U0001f4cb")}</div>
    <div class="game-title-group">
      <div class="game-title">MY BETS</div>
      <div class="game-subtitle">POSITIONS &amp; RESULTS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

{open_html}
{settled_html}

<div class="gold-divider"></div>

<div class="stat-grid-3col">
  <div class="stat-box" style="border-left:2px solid var(--loss);box-shadow:{box_shadow};">
    <div class="stat-box-label">AT RISK</div>
    <div class="stat-box-value red">${total_risk:,}</div>
  </div>
  <div class="stat-box" style="border-left:{win_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">MAX PAYOUT</div>
    <div class="stat-box-value green">${max_payout:,}</div>
  </div>
  <div class="stat-box" style="border-left:{default_border};box-shadow:{box_shadow};">
    <div class="stat-box-label">RECORD</div>
    <div class="stat-box-value">{wins}-{losses}-{pushes}</div>
  </div>
</div>

<div class="footer-text">All Sportsbooks · Open &amp; Recently Settled</div>
"""

    full_html = wrap_card(body, status_class=status_class, theme_id=theme_id)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO CARD
# ═════════════════════════════════════════════════════════════════════════════

def _gather_portfolio_data(user_id: int) -> dict:
    """Sync: collect DB data for Portfolio card."""
    balance = _get_balance(user_id)
    with sqlite3.connect(DB_PATH) as con:
        try:
            rows = con.execute(
                "SELECT c.side, c.quantity, c.cost_bucks, c.potential_payout, c.status, "
                "       m.title "
                "FROM prediction_contracts c "
                "JOIN prediction_markets m ON c.market_id = m.market_id "
                "WHERE c.user_id = ? AND c.status IN ('open', 'won', 'lost') "
                "ORDER BY CASE c.status WHEN 'open' THEN 0 WHEN 'won' THEN 1 ELSE 2 END, "
                "         c.rowid DESC "
                "LIMIT 10",
                (str(user_id),),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    return {"balance": balance, "rows": rows}


async def build_portfolio_card(user_id: int, *, theme_id: str | None = None) -> bytes:
    """Build the Portfolio card showing prediction market positions."""
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_portfolio_data, user_id)
    balance = d["balance"]
    rows = d["rows"]

    open_count = sum(1 for r in rows if r[4] == "open")
    total_invested = sum(r[2] for r in rows if r[4] == "open")
    total_potential = sum(r[3] for r in rows if r[4] == "open")

    # Status
    if not rows:
        status_class = "jackpot"
    elif total_potential > total_invested:
        status_class = "win"
    else:
        status_class = "loss"

    # Build position rows
    positions_html = ""
    if rows:
        for side, qty, cost, payout, status, title in rows:
            side_cls = "yes" if side.upper() == "YES" else "no"
            status_icon = {"open": "\U0001f7e2", "won": "\U0001f3c6", "lost": "\U0001f480"}.get(status, "\u25cf")
            positions_html += f'''<div class="position-row">
  <span class="side-badge {side_cls}">{esc(side.upper())}</span>
  <span class="status-dot">{status_icon}</span>
  <div class="position-title">{esc(title or "Unknown Market")}</div>
  <div class="position-meta">
    <span class="position-cost">{qty} × ${cost:,} paid</span>
    <span class="position-payout">→ ${payout:,}</span>
  </div>
</div>\n'''
    else:
        positions_html = '<div class="empty-state">No open positions. Browse /markets to find opportunities!</div>'

    body = f"""<style>{_FLOW_CSS}{_TAB_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("predictions", "\U0001f4c8")}</div>
    <div class="game-title-group">
      <div class="game-title">PORTFOLIO</div>
      <div class="game-subtitle">{open_count} OPEN POSITION{'S' if open_count != 1 else ''}</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

{positions_html}

<div class="gold-divider"></div>

<div class="stat-grid-2col">
  <div class="stat-cell">
    <div class="stat-label">TOTAL INVESTED</div>
    <div class="stat-value">${total_invested:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">MAX PAYOUT</div>
    <div class="stat-value green">${total_potential:,}</div>
  </div>
</div>

<div class="hero-section" style="padding: var(--space-sm) 20px 14px;">
  <div class="stat-label">BALANCE</div>
  <div class="stat-value">${balance:,}</div>
</div>

<div class="footer-text">ATLAS Prediction Markets</div>
"""

    full_html = wrap_card(body, status_class=status_class, theme_id=theme_id)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  WALLET / LEDGER CARD
# ═════════════════════════════════════════════════════════════════════════════

# Source type → (emoji, CSS class)
_SOURCE_MAP = {
    "CASINO":     ("\U0001f3b0", "casino"),
    "TSL_BET":    ("\U0001f3c8", "sportsbook"),
    "REAL_BET":   ("\U0001f3c6", "sportsbook"),
    "PREDICTION": ("\U0001f52e", "prediction"),
    "ADMIN":      ("\U0001f451", "admin"),
    "STIPEND":    ("\U0001f4b5", "stipend"),
}


def _gather_wallet_data(user_id: int) -> dict:
    """Sync: collect DB data for Wallet card."""
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        txns = con.execute(
            "SELECT amount, source, description, balance_after, created_at "
            "FROM transactions WHERE discord_id = ? "
            "ORDER BY created_at DESC LIMIT 15",
            (user_id,),
        ).fetchall()
        txns = [dict(t) for t in txns]  # sqlite3.Row can't cross thread boundary
    return {"balance": balance, "delta": delta, "txns": txns}


async def build_wallet_card(user_id: int, *, theme_id: str | None = None) -> bytes:
    """Build the Wallet/Ledger card showing balance and recent transactions."""
    d = await asyncio.get_running_loop().run_in_executor(None, _gather_wallet_data, user_id)
    balance = d["balance"]
    delta = d["delta"]
    txns = d["txns"]

    # Status
    status_class = "win" if balance >= STARTING_BALANCE else "loss"

    # Delta string
    delta_str = f"+${delta:,}" if delta >= 0 else f"-${abs(delta):,}"
    delta_class = "positive" if delta >= 0 else "negative"

    # Build transaction rows
    txn_html = ""
    if txns:
        txn_html = '<div class="txn-table">\n'
        for t in txns:
            amt = t["amount"]
            source = t["source"] or ""
            desc = (t["description"] or source)[:35]
            bal_after = t["balance_after"]
            emoji, src_cls = _SOURCE_MAP.get(source, ("\U0001f4b0", "other"))
            amt_cls = "credit" if amt >= 0 else "debit"
            amt_str = f"+${amt:,}" if amt >= 0 else f"-${abs(amt):,}"
            txn_html += f'''  <div class="txn-row">
    <div class="txn-source {src_cls}">{emoji}</div>
    <div class="txn-desc">{esc(desc)}</div>
    <div class="txn-amount {amt_cls}">{amt_str}</div>
    <div class="txn-bal">${bal_after:,}</div>
  </div>\n'''
        txn_html += "</div>\n"
    else:
        txn_html = '<div class="empty-state">No transactions yet.</div>'

    body = f"""<style>{_FLOW_CSS}{_TAB_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("wallet", "\U0001f4b0")}</div>
    <div class="game-title-group">
      <div class="game-title">WALLET</div>
      <div class="game-subtitle">TRANSACTION LEDGER</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<div class="hero-section">
  <div class="hero-label">BALANCE</div>
  <div class="hero-value">${balance:,}</div>
  <div class="hero-delta {delta_class}">{esc(delta_str)} this week</div>
</div>

<div class="gold-divider"></div>

<div class="section-label">RECENT TRANSACTIONS</div>
{txn_html}

<div class="footer-text">ATLAS Flow Economy</div>
"""

    full_html = wrap_card(body, status_class=status_class, theme_id=theme_id)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  LEADERBOARD CARD
# ═════════════════════════════════════════════════════════════════════════════

def _gather_leaderboard_data() -> dict:
    """Sync: collect DB data for Leaderboard card."""
    with sqlite3.connect(DB_PATH) as con:
        users = con.execute(
            "SELECT discord_id, balance, season_start_balance FROM users_table "
            "ORDER BY balance DESC"
        ).fetchall()
        win_rates = {}
        wr_rows = con.execute(
            """SELECT discord_id, SUM(w) as wins, SUM(t) as total FROM (
                 SELECT discord_id,
                   SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN status IN ('Won','Lost') THEN 1 ELSE 0 END) as t
                 FROM bets_table WHERE parlay_id IS NULL
                 GROUP BY discord_id
                 UNION ALL
                 SELECT discord_id,
                   SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN status IN ('Won','Lost') THEN 1 ELSE 0 END) as t
                 FROM parlays_table
                 GROUP BY discord_id
               ) GROUP BY discord_id"""
        ).fetchall()
        for did, wins, total in wr_rows:
            win_rates[did] = (wins / total * 100) if total > 0 else 0.0
    return {"users": users, "win_rates": win_rates}


async def build_leaderboard_card(viewer_id: int, name_resolver=None, *, theme_id: str | None = None) -> bytes:
    """Build the Leaderboard card showing top 10 users with multi-stat columns.

    Args:
        viewer_id: Discord user ID of the viewer (highlighted in the table).
        name_resolver: Optional callable(discord_id) -> str that resolves
            Discord IDs to display names. Falls back to truncated ID.
    """

    d = await asyncio.get_running_loop().run_in_executor(None, _gather_leaderboard_data)
    users = d["users"]
    win_rates = d["win_rates"]

    # Build user entries
    entries = []
    viewer_entry = None
    viewer_rank = len(users)

    for i, (did, bal, season_start) in enumerate(users):
        rank = i + 1
        season_start = season_start or STARTING_BALANCE
        roi = ((bal - season_start) / season_start * 100) if season_start > 0 else 0
        wr = win_rates.get(did, 0.0)
        entry = {"rank": rank, "discord_id": did, "balance": bal, "roi": roi, "win_rate": wr}
        entries.append(entry)
        if did == viewer_id:
            viewer_entry = entry
            viewer_rank = rank

    medals = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}

    # Build table rows (top 10)
    rows_html = ""
    # Table header
    rows_html += '''<div class="lb-header">
  <span class="lb-rank">#</span>
  <span class="lb-name">NAME</span>
  <span class="lb-stat">BAL</span>
  <span class="lb-stat">ROI</span>
  <span class="lb-stat">WIN%</span>
</div>\n'''

    for entry in entries[:10]:
        is_viewer = entry["discord_id"] == viewer_id
        row_cls = "lb-row viewer" if is_viewer else "lb-row"
        rank_str = medals.get(entry["rank"], f"#{entry['rank']}")
        rank_cls = "lb-rank gold" if entry["rank"] <= 3 else "lb-rank"

        # Abbreviate balance
        bal = entry["balance"]
        if bal >= 10000:
            bal_str = f"${bal/1000:.1f}k"
        else:
            bal_str = f"${bal:,}"

        roi = entry["roi"]
        roi_cls = "lb-stat green" if roi >= 0 else "lb-stat red"
        wr = entry["win_rate"]
        wr_cls = "lb-stat green" if wr >= 50 else ("lb-stat red" if wr > 0 else "lb-stat neutral")

        rows_html += f'''<div class="{row_cls}">
  <span class="{rank_cls}">{rank_str}</span>
  <span class="lb-name">{"▶ " if is_viewer else ""}{esc(name_resolver(entry["discord_id"]) if name_resolver else f"User {str(entry['discord_id'])[-4:]}")}</span>
  <span class="lb-stat neutral">{bal_str}</span>
  <span class="{roi_cls}">{roi:+.0f}%</span>
  <span class="{wr_cls}">{wr:.0f}%</span>
</div>\n'''

    # If viewer is outside top 10, show their row at bottom
    if viewer_entry and viewer_rank > 10:
        entry = viewer_entry
        bal = entry["balance"]
        bal_str = f"${bal/1000:.1f}k" if bal >= 10000 else f"${bal:,}"
        roi = entry["roi"]
        roi_cls = "lb-stat green" if roi >= 0 else "lb-stat red"
        wr = entry["win_rate"]
        wr_cls = "lb-stat green" if wr >= 50 else ("lb-stat red" if wr > 0 else "lb-stat neutral")
        rows_html += f'''<div style="padding: var(--space-xs) var(--space-sm); margin-top: var(--space-xs); border-top: 1px solid rgba(255,255,255,0.08);"></div>
<div class="lb-row viewer">
  <span class="lb-rank gold">#{viewer_rank}</span>
  <span class="lb-name">▶ YOU</span>
  <span class="lb-stat neutral">{bal_str}</span>
  <span class="{roi_cls}">{roi:+.0f}%</span>
  <span class="{wr_cls}">{wr:.0f}%</span>
</div>\n'''

    body = f"""<style>{_FLOW_CSS}{_TAB_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">{icon_pill("leaderboard", "\U0001f3c6")}</div>
    <div class="game-title-group">
      <div class="game-title">LEADERBOARD</div>
      <div class="game-subtitle">TSL FLOW RANKINGS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<div class="lb-table">
{rows_html}
</div>

<div class="footer-text">ATLAS Flow Economy</div>
"""

    full_html = wrap_card(body, status_class="jackpot", theme_id=theme_id)
    return await render_card(full_html)

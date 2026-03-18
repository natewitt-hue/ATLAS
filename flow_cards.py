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

import io
import json
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
#  DATA QUERIES (flow-specific)
# ═════════════════════════════════════════════════════════════════════════════

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
"""

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

"""

    full_html = wrap_card(body, status_class=status_class)
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
.section-label { font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; padding: 0 20px; margin-bottom: 6px; }
.bet-row { padding: 8px 20px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.bet-row:last-child { border-bottom: none; }
.bet-team { font-family: 'Outfit'; font-weight: 700; font-size: var(--font-base); color: var(--text-primary); }
.bet-type { font-family: 'Outfit'; font-weight: 600; font-size: var(--font-sm); color: var(--text-muted); margin-left: 4px; }
.bet-details { display: flex; justify-content: space-between; margin-top: 2px; font-family: 'JetBrains Mono'; font-size: var(--font-sm); }
.bet-wager { color: var(--text-sub); }
.bet-potential { color: var(--win); font-weight: 700; }
.parlay-header { font-family: 'Outfit'; font-weight: 700; font-size: var(--font-base); color: var(--text-primary); }
.parlay-odds { font-family: 'JetBrains Mono'; font-weight: 700; color: var(--gold); }
.parlay-legs { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.parlay-leg { font-family: 'Outfit'; font-size: 11px; padding: 2px 6px; border-radius: 3px; }
.parlay-leg.won { background: rgba(74,222,128,0.15); color: var(--win); }
.parlay-leg.lost { background: rgba(248,113,113,0.15); color: var(--loss); }
.parlay-leg.pending { background: rgba(255,255,255,0.06); color: var(--text-muted); }
.empty-state { text-align: center; padding: 24px 20px; font-family: 'Outfit'; font-weight: 600; font-size: var(--font-base); color: var(--text-muted); }

.side-badge { display: inline-block; font-family: 'Outfit'; font-weight: 700; font-size: 11px; padding: 2px 8px; border-radius: 3px; text-transform: uppercase; }
.side-badge.yes { background: rgba(74,222,128,0.15); color: var(--win); }
.side-badge.no { background: rgba(248,113,113,0.15); color: var(--loss); }
.position-row { padding: 8px 20px; border-bottom: 1px solid rgba(255,255,255,0.04); }
.position-row:last-child { border-bottom: none; }
.position-title { font-family: 'Outfit'; font-weight: 600; font-size: var(--font-sm); color: var(--text-primary); margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.position-meta { display: flex; justify-content: space-between; margin-top: 2px; font-family: 'JetBrains Mono'; font-size: var(--font-xs); }
.position-cost { color: var(--text-sub); }
.position-payout { color: var(--win); font-weight: 700; }
.status-dot { display: inline-block; font-size: 12px; margin-left: 6px; }

.txn-table { width: 100%; padding: 0 12px; }
.txn-row { display: flex; align-items: center; padding: 5px 8px; border-bottom: 1px solid rgba(255,255,255,0.03); gap: 8px; }
.txn-row:last-child { border-bottom: none; }
.txn-source { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
.txn-source.casino { background: rgba(168,85,247,0.2); }
.txn-source.sportsbook { background: rgba(74,222,128,0.2); }
.txn-source.prediction { background: rgba(96,165,250,0.2); }
.txn-source.admin { background: rgba(212,175,55,0.2); }
.txn-source.stipend { background: rgba(45,212,191,0.2); }
.txn-source.other { background: rgba(255,255,255,0.06); }
.txn-desc { flex: 1; font-family: 'Outfit'; font-weight: 600; font-size: var(--font-xs); color: var(--text-sub); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.txn-amount { font-family: 'JetBrains Mono'; font-weight: 700; font-size: var(--font-sm); min-width: 60px; text-align: right; }
.txn-amount.credit { color: var(--win); }
.txn-amount.debit { color: var(--loss); }
.txn-bal { font-family: 'JetBrains Mono'; font-weight: 600; font-size: var(--font-xs); color: var(--text-dim); min-width: 55px; text-align: right; }

.lb-table { width: 100%; padding: 0 16px; }
.lb-header { display: flex; padding: 6px 8px; font-family: 'Outfit'; font-weight: 700; font-size: 10px; color: var(--gold-dim); letter-spacing: 1.5px; text-transform: uppercase; }
.lb-row { display: flex; padding: 7px 8px; border-bottom: 1px solid rgba(255,255,255,0.03); align-items: center; }
.lb-row:last-child { border-bottom: none; }
.lb-row.viewer { background: rgba(212,175,55,0.08); border: 1px solid rgba(212,175,55,0.2); border-radius: var(--border-radius-sm); }
.lb-rank { width: 36px; font-family: 'JetBrains Mono'; font-weight: 800; font-size: var(--font-sm); color: var(--text-sub); flex-shrink: 0; }
.lb-rank.gold { color: var(--gold); }
.lb-name { flex: 1; font-family: 'Outfit'; font-weight: 700; font-size: var(--font-sm); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lb-stat { width: 60px; font-family: 'JetBrains Mono'; font-weight: 700; font-size: var(--font-xs); text-align: right; }
.lb-stat.green { color: var(--win); }
.lb-stat.red { color: var(--loss); }
.lb-stat.neutral { color: var(--text-sub); }

.footer-text { text-align: center; padding: 8px 20px 14px; font-family: 'Outfit'; font-weight: 600; font-size: 10px; color: var(--text-dim); letter-spacing: 1px; }
"""


# ═════════════════════════════════════════════════════════════════════════════
#  PAYOUT HELPER
# ═════════════════════════════════════════════════════════════════════════════

def _payout_calc(wager: int, odds: int) -> int:
    """Return total payout (wager + profit) from American odds."""
    odds = int(odds)
    if odds == 0:
        return wager
    if odds > 0:
        return int(wager + wager * (odds / 100))
    return int(wager + wager * (100 / abs(odds)))


def _american_to_str(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


# ═════════════════════════════════════════════════════════════════════════════
#  MY BETS CARD
# ═════════════════════════════════════════════════════════════════════════════

async def build_my_bets_card(user_id: int) -> bytes:
    """Build the My Bets card showing active sportsbook positions."""
    balance = _get_balance(user_id)

    with sqlite3.connect(DB_PATH) as con:
        straight = con.execute(
            "SELECT matchup, bet_type, pick, wager_amount, odds, line, status, week "
            "FROM bets_table WHERE discord_id=? AND status='Pending' AND parlay_id IS NULL "
            "ORDER BY bet_id DESC",
            (user_id,),
        ).fetchall()
        parlays = con.execute(
            "SELECT parlay_id, week, legs, combined_odds, wager_amount, status "
            "FROM parlays_table WHERE discord_id=? AND status='Pending' "
            "ORDER BY rowid DESC",
            (user_id,),
        ).fetchall()

    pending_count = len(straight) + len(parlays)
    total_risk = sum(b[3] for b in straight) + sum(p[4] for p in parlays)
    max_payout = (
        sum(_payout_calc(b[3], b[4]) for b in straight)
        + sum(_payout_calc(p[4], p[3]) for p in parlays)
    )

    # Status bar
    if not straight and not parlays:
        status_class = "jackpot"
    elif max_payout > total_risk:
        status_class = "win"
    else:
        status_class = "loss"

    # Build bet rows HTML
    bets_html = ""
    if straight:
        bets_html += '<div class="section-label">STRAIGHT BETS</div>\n'
        for b in straight[:8]:
            matchup, btype, pick, wager, odds, line, status, week = b
            potential = _payout_calc(wager, odds)
            line_str = ""
            if btype in ("Spread", "Over/Under") and line is not None:
                line_str = f" ({line:+g})" if btype == "Spread" else f" ({line})"
            bets_html += f'''<div class="bet-row">
  <span class="bet-team">{esc(str(pick))}</span>
  <span class="bet-type">{esc(btype)}{esc(line_str)} {_american_to_str(int(odds))}</span>
  <div class="bet-details">
    <span class="bet-wager">Wager: ${wager:,}</span>
    <span class="bet-potential">Win: ${potential:,}</span>
  </div>
</div>\n'''

    if parlays:
        bets_html += '<div class="section-label" style="margin-top:8px;">PARLAYS</div>\n'
        for p in parlays[:4]:
            pid, week, legs_json, c_odds, wager, status = p
            try:
                legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else []
            except Exception:
                legs = []
            potential = _payout_calc(wager, c_odds)
            legs_html = ""
            for leg in legs:
                leg_pick = leg.get("pick", "?")
                # Check if individual leg is graded
                leg_status = leg.get("status", "Pending")
                if leg_status == "Won":
                    cls = "won"
                    icon = "✔"
                elif leg_status == "Lost":
                    cls = "lost"
                    icon = "✗"
                else:
                    cls = "pending"
                    icon = "○"
                legs_html += f'<span class="parlay-leg {cls}">{icon} {esc(str(leg_pick))}</span>'

            bets_html += f'''<div class="bet-row">
  <div><span class="parlay-header">{len(legs)}-Leg Parlay</span> · <span class="parlay-odds">{_american_to_str(int(c_odds))}</span></div>
  <div class="bet-details">
    <span class="bet-wager">Wager: ${wager:,}</span>
    <span class="bet-potential">Win: ${potential:,}</span>
  </div>
  <div class="parlay-legs">{legs_html}</div>
</div>\n'''

    if not straight and not parlays:
        bets_html = '<div class="empty-state">No active bets. Hit /sportsbook to place some!</div>'

    body = f"""<style>{_FLOW_CSS}{_TAB_CSS}</style>

<div class="header">
  <div class="header-left">
    <div class="game-icon-pill">\U0001f4cb</div>
    <div class="game-title-group">
      <div class="game-title">MY BETS</div>
      <div class="game-subtitle">ACTIVE POSITIONS</div>
    </div>
  </div>
</div>

<div class="gold-divider"></div>

<div class="stat-grid-2col">
  <div class="stat-cell">
    <div class="stat-label">BALANCE</div>
    <div class="stat-value">${balance:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">PENDING</div>
    <div class="stat-value">{pending_count} bet{'s' if pending_count != 1 else ''}</div>
  </div>
</div>

<div class="gold-divider"></div>

{bets_html}

<div class="gold-divider"></div>

<div class="stat-grid-2col">
  <div class="stat-cell">
    <div class="stat-label">TOTAL AT RISK</div>
    <div class="stat-value red">${total_risk:,}</div>
  </div>
  <div class="stat-cell">
    <div class="stat-label">MAX PAYOUT</div>
    <div class="stat-value green">${max_payout:,}</div>
  </div>
</div>

<div class="footer-text">TSL Sportsbook · Pending bets only</div>
"""

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  PORTFOLIO CARD
# ═════════════════════════════════════════════════════════════════════════════

async def build_portfolio_card(user_id: int) -> bytes:
    """Build the Portfolio card showing prediction market positions."""
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
    <div class="game-icon-pill">\U0001f4c8</div>
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

<div class="hero-section" style="padding: 8px 20px 14px;">
  <div class="stat-label">BALANCE</div>
  <div class="stat-value">${balance:,}</div>
</div>

<div class="footer-text">ATLAS Prediction Markets</div>
"""

    full_html = wrap_card(body, status_class=status_class)
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


async def build_wallet_card(user_id: int) -> bytes:
    """Build the Wallet/Ledger card showing balance and recent transactions."""
    balance = _get_balance(user_id)
    delta = _get_weekly_delta(user_id)

    # Use sync sqlite3 to match existing patterns in this file
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        txns = con.execute(
            "SELECT amount, source, description, balance_after, created_at "
            "FROM transactions WHERE discord_id = ? "
            "ORDER BY created_at DESC LIMIT 15",
            (user_id,),
        ).fetchall()

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
    <div class="game-icon-pill">\U0001f4b0</div>
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

    full_html = wrap_card(body, status_class=status_class)
    return await render_card(full_html)


# ═════════════════════════════════════════════════════════════════════════════
#  LEADERBOARD CARD
# ═════════════════════════════════════════════════════════════════════════════

async def build_leaderboard_card(viewer_id: int, name_resolver=None) -> bytes:
    """Build the Leaderboard card showing top 10 users with multi-stat columns.

    Args:
        viewer_id: Discord user ID of the viewer (highlighted in the table).
        name_resolver: Optional callable(discord_id) -> str that resolves
            Discord IDs to display names. Falls back to truncated ID.
    """

    with sqlite3.connect(DB_PATH) as con:
        # Get all users for ranking
        users = con.execute(
            "SELECT discord_id, balance, season_start_balance FROM users_table "
            "ORDER BY balance DESC"
        ).fetchall()

        # Pre-compute win rates for all users in one query
        win_rates = {}
        wr_rows = con.execute(
            "SELECT discord_id, "
            "  SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as wins, "
            "  SUM(CASE WHEN status IN ('Won','Lost') THEN 1 ELSE 0 END) as total "
            "FROM bets_table WHERE parlay_id IS NULL "
            "GROUP BY discord_id"
        ).fetchall()
        for did, wins, total in wr_rows:
            win_rates[did] = (wins / total * 100) if total > 0 else 0.0

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
        rows_html += f'''<div style="padding: 4px 8px; margin-top: 4px; border-top: 1px solid rgba(255,255,255,0.08);"></div>
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
    <div class="game-icon-pill">\U0001f3c6</div>
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

    full_html = wrap_card(body, status_class="jackpot")
    return await render_card(full_html)

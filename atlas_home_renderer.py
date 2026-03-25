"""
atlas_home_renderer.py — ATLAS User Home Baseball Card
──────────────────────────────────────────────────────
Renders a personalized PNG "baseball card" for /atlas.

Pipeline: gather_home_data → _build_home_html → wrap_card → render_card → PNG
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from atlas_html_engine import esc, render_card, wrap_card

_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))
_DB_TIMEOUT = 10


def gather_home_data(user_id: int) -> dict:
    """
    Gather all stats for the home card from flow_economy.db.
    Runs synchronously — call via run_in_executor.
    Returns dict with safe defaults for all fields.
    """
    data = {
        "user_id": user_id,
        # Hero
        "display_name": "Unknown",
        "role_badge": "",
        "rank": 0, "total_users": 0,
        "balance": 0, "weekly_delta": 0,
        "season_roi": 0.0, "streak": "—",
        # Economy
        "record_w": 0, "record_l": 0, "record_p": 0,
        "win_rate": 0.0, "net_pnl": 0,
        # Sportsbook
        "tsl_bet_w": 0, "tsl_bet_l": 0,
        "best_parlay_odds": 0.0,
        "real_bet_w": 0, "real_bet_l": 0,
        # Casino
        "casino_sessions": 0, "biggest_win": 0, "fav_game": "—",
        # Predictions
        "pred_accuracy": 0.0, "pred_markets": 0, "pred_pnl": 0,
        # Footer
        "theme_name": "Obsidian Gold", "season": 0,
    }

    try:
        con = sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT)

        # Hero — balance, season_start_balance
        row = con.execute(
            "SELECT balance, season_start_balance FROM users_table WHERE discord_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            balance = int(row[0] or 0)
            season_start = int(row[1] or 0)
            data["balance"] = balance
            if season_start > 0:
                pnl = balance - season_start
                data["season_roi"] = round(pnl / season_start * 100, 1)
                data["net_pnl"] = pnl

        # Rank
        ranks = con.execute(
            "SELECT discord_id FROM users_table ORDER BY balance DESC"
        ).fetchall()
        data["total_users"] = len(ranks)
        for i, (uid,) in enumerate(ranks, 1):
            if uid == user_id:
                data["rank"] = i
                break

        # Weekly delta from transactions
        try:
            delta_row = con.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM transactions
                   WHERE discord_id = ? AND created_at >= datetime('now', '-7 days')""",
                (user_id,),
            ).fetchone()
            if delta_row:
                data["weekly_delta"] = int(delta_row[0] or 0)
        except Exception:
            pass

        # TSL Sportsbook bets (bets_table, status: won/lost/push)
        try:
            tsl_rows = con.execute(
                "SELECT status FROM bets_table WHERE discord_id = ? AND status IN ('won','lost','push')",
                (user_id,),
            ).fetchall()
            for (s,) in tsl_rows:
                if s == "won": data["tsl_bet_w"] += 1
                elif s == "lost": data["tsl_bet_l"] += 1
                elif s == "push": data["record_p"] += 1
        except Exception:
            pass

        # Real sports bets (real_bets table, separate)
        try:
            real_rows = con.execute(
                "SELECT status FROM real_bets WHERE discord_id = ? AND status IN ('won','lost')",
                (user_id,),
            ).fetchall()
            for (s,) in real_rows:
                if s == "won": data["real_bet_w"] += 1
                elif s == "lost": data["real_bet_l"] += 1
        except Exception:
            pass

        # Casino sessions, biggest win, favorite game
        try:
            sess_count = con.execute(
                "SELECT COUNT(*) FROM casino_sessions WHERE discord_id = ?",
                (user_id,),
            ).fetchone()
            data["casino_sessions"] = int(sess_count[0] or 0) if sess_count else 0

            # Biggest win = max(payout - wager) for wins
            big_win = con.execute(
                """SELECT MAX(payout - wager) FROM casino_sessions
                   WHERE discord_id = ? AND outcome = 'win'""",
                (user_id,),
            ).fetchone()
            if big_win and big_win[0]:
                data["biggest_win"] = int(big_win[0])

            fav = con.execute(
                """SELECT game_type, COUNT(*) AS cnt FROM casino_sessions
                   WHERE discord_id = ?
                   GROUP BY game_type ORDER BY cnt DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if fav:
                data["fav_game"] = str(fav[0]).capitalize()
        except Exception:
            pass

        # Predictions (prediction_contracts, user_id is TEXT)
        try:
            uid_str = str(user_id)
            pred_rows = con.execute(
                """SELECT status, cost_bucks, potential_payout FROM prediction_contracts
                   WHERE user_id = ?""",
                (uid_str,),
            ).fetchall()
            if pred_rows:
                data["pred_markets"] = len(pred_rows)
                resolved = [(s, c, p) for s, c, p in pred_rows if s == "resolved"]
                if resolved:
                    # Approximate: resolved + payout means won
                    wins = sum(1 for s, c, p in resolved if (p or 0) > (c or 0))
                    data["pred_accuracy"] = round(wins / len(resolved) * 100, 1) if resolved else 0.0
                total_cost = sum(c or 0 for _, c, _ in pred_rows)
                total_payout = sum(p or 0 for s, _, p in pred_rows if s == "resolved")
                data["pred_pnl"] = total_payout - total_cost
        except Exception:
            pass

        # Streak — last N resolved TSL bets
        try:
            streak_rows = con.execute(
                """SELECT status FROM bets_table
                   WHERE discord_id = ? AND status IN ('won','lost')
                   ORDER BY created_at DESC LIMIT 20""",
                (user_id,),
            ).fetchall()
            if streak_rows:
                first = streak_rows[0][0]
                is_win = (first == "won")
                count = 0
                for (s,) in streak_rows:
                    if (s == "won") == is_win:
                        count += 1
                    else:
                        break
                data["streak"] = f"W{count}" if is_win else f"L{count}"
        except Exception:
            pass

        # Best parlay odds (parlays_table, status='won')
        try:
            parlay_row = con.execute(
                """SELECT MAX(combined_odds) FROM parlays_table
                   WHERE discord_id = ? AND status = 'won'""",
                (user_id,),
            ).fetchone()
            if parlay_row and parlay_row[0]:
                # combined_odds is stored as American odds (e.g. 450 = +450)
                # Convert to decimal multiplier for display
                american = int(parlay_row[0])
                if american > 0:
                    data["best_parlay_odds"] = round(american / 100 + 1, 2)
                else:
                    data["best_parlay_odds"] = round(100 / abs(american) + 1, 2)
        except Exception:
            pass

        # Economy record totals
        total_w = data["tsl_bet_w"] + data["real_bet_w"]
        total_l = data["tsl_bet_l"] + data["real_bet_l"]
        data["record_w"] = total_w
        data["record_l"] = total_l
        total = total_w + total_l + data["record_p"]
        if total > 0:
            data["win_rate"] = round(total_w / total * 100, 1)

        con.close()
    except Exception:
        pass

    return data


def _stat_cell(label: str, value: str, accent: str = "#d4a843") -> str:
    """One cell in a 3-col stat grid."""
    return (
        f'<div style="background:rgba(255,255,255,0.04);border-radius:8px;'
        f'padding:10px 8px;text-align:center;">'
        f'<div style="font-size:9px;font-weight:700;color:{accent};'
        f'text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;">'
        f"{esc(label)}</div>"
        f'<div style="font-size:16px;font-weight:800;color:#fff;">'
        f"{esc(str(value))}</div>"
        f"</div>"
    )


def _section(title: str, cells_html: str, accent: str = "#d4a843") -> str:
    """A labeled section with a 3-col stat grid."""
    return (
        f'<div style="margin-bottom:14px;">'
        f'<div style="font-size:10px;font-weight:700;color:{accent};'
        f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;'
        f'padding-bottom:4px;border-bottom:1px solid {accent}33;">'
        f"{esc(title)}</div>"
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">'
        f"{cells_html}"
        f"</div>"
        f"</div>"
    )


def _build_home_html(data: dict) -> str:
    """Build the inner HTML body for the baseball card."""
    accent = "#d4a843"
    balance_sign = "+" if data["weekly_delta"] >= 0 else ""
    roi_sign = "+" if data["season_roi"] >= 0 else ""

    hero = (
        f'<div style="padding:20px;text-align:center;'
        f'background:linear-gradient(135deg,rgba(0,0,0,0.4),rgba(20,20,30,0.9));">'
        f'<div style="font-size:22px;font-weight:900;color:#fff;'
        f'letter-spacing:-0.5px;">{esc(data["display_name"])}</div>'
        + (
            f'<div style="display:inline-block;background:{accent}22;border:1px solid {accent}44;'
            f'border-radius:12px;padding:2px 12px;font-size:10px;font-weight:700;'
            f'color:{accent};margin-top:4px;">{esc(data["role_badge"])}</div>'
            if data["role_badge"] else ""
        )
        + f'<div style="margin-top:10px;display:grid;grid-template-columns:1fr 1fr 1fr 1fr;'
        f'gap:8px;max-width:500px;margin-left:auto;margin-right:auto;">'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Rank</div>'
        f'<div style="font-size:18px;font-weight:900;color:{accent};">#{data["rank"]}</div>'
        f'<div style="font-size:9px;color:#666;">of {data["total_users"]}</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Balance</div>'
        f'<div style="font-size:18px;font-weight:900;color:#fff;">{data["balance"]:,}</div>'
        f'<div style="font-size:9px;color:{"#4caf50" if data["weekly_delta"] >= 0 else "#f44336"};">'
        f'{balance_sign}{data["weekly_delta"]:,} wk</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Season ROI</div>'
        f'<div style="font-size:18px;font-weight:900;'
        f'color:{"#4caf50" if data["season_roi"] >= 0 else "#f44336"};">'
        f'{roi_sign}{data["season_roi"]}%</div></div>'
        f'<div style="text-align:center;">'
        f'<div style="font-size:9px;color:#888;text-transform:uppercase;">Streak</div>'
        f'<div style="font-size:18px;font-weight:900;color:#fff;">{esc(data["streak"])}</div>'
        f"</div></div></div>"
    )

    economy = _section("Economy", (
        _stat_cell("Record", f'{data["record_w"]}-{data["record_l"]}-{data["record_p"]}', accent)
        + _stat_cell("Win Rate", f'{data["win_rate"]}%', accent)
        + _stat_cell("Net P&L", f'{data["net_pnl"]:+,}', accent)
    ), accent)

    sportsbook = _section("Sportsbook", (
        _stat_cell("TSL Bets", f'{data["tsl_bet_w"]}-{data["tsl_bet_l"]}', accent)
        + _stat_cell("Best Parlay", f'{data["best_parlay_odds"]}x' if data["best_parlay_odds"] else "—", accent)
        + _stat_cell("Real Sports", f'{data["real_bet_w"]}-{data["real_bet_l"]}', accent)
    ), accent)

    casino = _section("Casino", (
        _stat_cell("Sessions", str(data["casino_sessions"]), accent)
        + _stat_cell("Biggest Win", f'{data["biggest_win"]:,}', accent)
        + _stat_cell("Fav Game", data["fav_game"], accent)
    ), accent)

    predictions = _section("Predictions", (
        _stat_cell("Accuracy", f'{data["pred_accuracy"]}%', accent)
        + _stat_cell("Markets", str(data["pred_markets"]), accent)
        + _stat_cell("Pred P&L", f'{data["pred_pnl"]:+,}', accent)
    ), accent)

    footer = (
        f'<div style="text-align:center;padding:10px;font-size:9px;color:#555;">'
        f'ATLAS™ · {esc(data["theme_name"])} · Season {data["season"]}'
        f"</div>"
    )

    return f"{hero}<div style='padding:16px 20px 0;'>{economy}{sportsbook}{casino}{predictions}</div>{footer}"


async def render_home_card(data: dict, *, theme_id: str | None = None) -> bytes:
    """Render the home card to PNG bytes."""
    body_html = _build_home_html(data)
    full_html = wrap_card(body_html, "", theme_id=theme_id)
    return await render_card(full_html)


async def render_home_card_to_file(data: dict, *, theme_id: str | None = None, filename: str = "atlas_home.png"):
    """Render and return a discord.File."""
    import io
    import discord
    png_bytes = await render_home_card(data, theme_id=theme_id)
    buf = io.BytesIO(png_bytes)
    buf.seek(0)
    return discord.File(buf, filename=filename)

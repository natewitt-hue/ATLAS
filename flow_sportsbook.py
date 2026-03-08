"""
flow_sportsbook.py — ATLAS · Flow Module · Sportsbook v4.0
─────────────────────────────────────────────────────────────────────────────
ODDS ENGINE v3 — Power Rating Formula
  · _power_rating(): composite power number per team
    Inputs: season W%, power rank, OVR, off rank, def rank, career W%
    Weights calibrated so CURRENT season performance dominates career history
  · _calc_spread(): home_power − away_power + HOME_FIELD_EDGE, capped ±14.5
  · _spread_to_ml(): spread → American ML odds (tightened table)
  · _calc_ou(): historical avg pts ± rank adjustments, calibrated to actual
    TSL league average from tsl_history.db (fallback 27.0 pts/team)
  · _build_game_lines(): admin line_overrides applied first, engine as fallback

ADMIN MANAGEMENT SUITE (Commissioner role or ADMIN_USER_IDS):
  /sb_status    — Overview of all current-week games, lines, locks, bet counts
  /sb_lines     — Debug view: per-game power ratings + component breakdown
  /sb_setspread — Override spread (auto-recalculates ML from new spread)
  /sb_setml     — Override moneylines manually
  /sb_setou     — Override O/U total
  /sb_lock      — Lock / unlock a single game
  /sb_lockall   — Lock all current-week games at once
  /sb_unlockall — Unlock all current-week games at once
  /sb_cancelgame — Void & refund all pending bets on one game
  /sb_refund    — Refund a single bet by ID
  /sb_balance   — Manually adjust a member's TSL Bucks
  /sb_resetlines — Wipe all admin line overrides for the current week
  /sb_addprop   — Create a custom prop bet with two options
  /sb_settleprop — Settle a prop bet and pay out winners
  /grade_bets   — Manual commissioner bet settlement (unchanged)

USER COMMANDS:
  /sportsbook   — Current-week betting board (spread, ML, O/U, parlay)
  /mybets       — Active bets + balance dashboard
  /bethistory   — Full season P&L history
  /leaderboard  — Season P&L rankings
  /props        — View and bet available prop bets

CHANGES v4.0 vs v3.3:
  BREAK career_win_pct overweight: 10× → 4×   (Commanders +14 at home bug)
  BREAK O/U flat clustering: rank multipliers 0.18/0.12 → 0.28/0.18
  BREAK status '3' type mismatch in history query → now handles TEXT and INT
  ADD  line_overrides table: admin can override spread / ML / O/U per game
  ADD  prop_bets + prop_wagers tables for custom bets
  ADD  _power_rating() unified formula instead of ad-hoc components
  ADD  league avg calibration from tsl_history.db (fallback 27.0)
  ADD  13 /sb_* admin commands replacing /lockgame + /setline
  ADD  /props user command
  KEEP /lockgame, /setline — backward compat, delegate to new override system
  FIX  _is_admin() checks Commissioner role AND ADMIN_USER_IDS env var
  FIX  SPREAD_CAP tightened to ±14.5 (was ±17)
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import math
import os
import sqlite3
import uuid

import discord
from discord import app_commands
from discord.ext import commands, tasks

import data_manager as dm

# ── Configuration ─────────────────────────────────────────────────────────────
_DIR              = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(_DIR, "sportsbook.db")
HISTORY_DB_PATH   = os.path.join(_DIR, "tsl_history.db")

TSL_GOLD          = 0xD4AF37
TSL_BLACK         = 0x1A1A1A
TSL_RED           = 0xC0392B
TSL_GREEN         = 0x27AE60

ADMIN_ROLE_NAME   = "Commissioner"
ADMIN_USER_IDS    = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

STARTING_BALANCE  = 1000
MIN_BET           = 10
MAX_PARLAY_LEGS   = 6
HOME_FIELD_EDGE   = 2.0       # pts advantage for home team
SPREAD_CAP        = 14.5      # max absolute spread value
_DB_TIMEOUT       = 10

SPORTSBOOK_VERSION = "v4.0"
print(f"[SPORTSBOOK] Loading {SPORTSBOOK_VERSION}")


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN HELPER
# ═════════════════════════════════════════════════════════════════════════════

def _is_admin(interaction: discord.Interaction) -> bool:
    """True if user has Commissioner role OR is in ADMIN_USER_IDS."""
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    return any(r.name == ADMIN_ROLE_NAME for r in getattr(interaction.user, "roles", []))


# ═════════════════════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ═════════════════════════════════════════════════════════════════════════════

def _db_con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def setup_db():
    with _db_con() as con:
        # ── Core tables ───────────────────────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS users_table (
                discord_id           INTEGER PRIMARY KEY,
                balance              INTEGER DEFAULT 1000,
                season_start_balance INTEGER DEFAULT 1000
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bets_table (
                bet_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id   INTEGER,
                week         INTEGER,
                matchup      TEXT,
                bet_type     TEXT,
                wager_amount INTEGER,
                odds         INTEGER,
                pick         TEXT,
                line         REAL,
                status       TEXT DEFAULT 'Pending',
                parlay_id    TEXT DEFAULT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS parlays_table (
                parlay_id     TEXT PRIMARY KEY,
                discord_id    INTEGER,
                week          INTEGER,
                legs          TEXT,
                combined_odds INTEGER,
                wager_amount  INTEGER,
                status        TEXT DEFAULT 'Pending',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS games_state (
                game_id TEXT PRIMARY KEY,
                locked  INTEGER DEFAULT 0
            )
        """)

        # ── NEW v4.0: Line override table ─────────────────────────────────
        # Stores admin-set lines; NULL = use engine value
        con.execute("""
            CREATE TABLE IF NOT EXISTS line_overrides (
                game_id     TEXT PRIMARY KEY,
                home_spread REAL    DEFAULT NULL,
                away_spread REAL    DEFAULT NULL,
                home_ml     INTEGER DEFAULT NULL,
                away_ml     INTEGER DEFAULT NULL,
                ou_line     REAL    DEFAULT NULL,
                set_by      TEXT    DEFAULT NULL,
                set_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── NEW v4.0: Prop bets ───────────────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS prop_bets (
                prop_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                week        INTEGER,
                description TEXT,
                option_a    TEXT,
                option_b    TEXT,
                odds_a      INTEGER DEFAULT -110,
                odds_b      INTEGER DEFAULT -110,
                status      TEXT    DEFAULT 'Open',
                result      TEXT    DEFAULT NULL,
                created_by  TEXT    DEFAULT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS prop_wagers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                prop_id      INTEGER,
                discord_id   INTEGER,
                pick         TEXT,
                wager_amount INTEGER,
                odds         INTEGER,
                status       TEXT DEFAULT 'Pending',
                placed_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Migration guards ──────────────────────────────────────────────
        for stmt in [
            "ALTER TABLE users_table ADD COLUMN season_start_balance INTEGER DEFAULT 1000",
            "ALTER TABLE bets_table ADD COLUMN parlay_id TEXT DEFAULT NULL",
            "ALTER TABLE bets_table ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        ]:
            try:
                con.execute(stmt)
            except Exception:
                pass

        # Migrate old ou_line column from games_state → line_overrides
        try:
            old_rows = con.execute(
                "SELECT game_id, ou_line FROM games_state WHERE ou_line IS NOT NULL AND ou_line > 0"
            ).fetchall()
            for gid, ou in old_rows:
                con.execute(
                    "INSERT OR IGNORE INTO line_overrides (game_id, ou_line, set_by) VALUES (?, ?, 'migrated')",
                    (gid, ou)
                )
            if old_rows:
                print(f"[SB] Migrated {len(old_rows)} ou_line entries → line_overrides")
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _get_balance(uid: int) -> int:
    with _db_con() as con:
        res = con.execute("SELECT balance FROM users_table WHERE discord_id=?", (uid,)).fetchone()
        if not res:
            con.execute(
                "INSERT INTO users_table (discord_id, balance, season_start_balance) VALUES (?, ?, ?)",
                (uid, STARTING_BALANCE, STARTING_BALANCE)
            )
            return STARTING_BALANCE
        return res[0]


def _update_balance(uid: int, delta: int, con=None):
    def _run(c):
        if not c.execute("SELECT 1 FROM users_table WHERE discord_id=?", (uid,)).fetchone():
            c.execute(
                "INSERT INTO users_table (discord_id, balance, season_start_balance) VALUES (?, ?, ?)",
                (uid, STARTING_BALANCE + delta, STARTING_BALANCE)
            )
        else:
            c.execute("UPDATE users_table SET balance = balance + ? WHERE discord_id=?", (delta, uid))
    if con:
        _run(con)
    else:
        with _db_con() as c:
            _run(c)


def _is_locked(game_id: str) -> bool:
    with _db_con() as con:
        res = con.execute("SELECT locked FROM games_state WHERE game_id=?", (game_id,)).fetchone()
        return bool(res[0]) if res else False


def _set_locked(game_id: str, locked: bool):
    with _db_con() as con:
        con.execute(
            "INSERT OR REPLACE INTO games_state (game_id, locked) VALUES (?, ?)",
            (game_id, int(locked))
        )


def _get_line_override(game_id: str) -> dict | None:
    """Return admin line overrides for a game, or None if not set."""
    with _db_con() as con:
        res = con.execute(
            "SELECT home_spread, away_spread, home_ml, away_ml, ou_line "
            "FROM line_overrides WHERE game_id=?",
            (game_id,)
        ).fetchone()
    if not res:
        return None
    return {
        "home_spread": res[0],
        "away_spread": res[1],
        "home_ml":     res[2],
        "away_ml":     res[3],
        "ou_line":     res[4],
    }


def _set_line_override(game_id: str, set_by: str, **kwargs):
    """
    Upsert line override fields. Pass only the fields you want to change.
    Accepted kwargs: home_spread, away_spread, home_ml, away_ml, ou_line
    """
    allowed = {"home_spread", "away_spread", "home_ml", "away_ml", "ou_line"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return

    with _db_con() as con:
        con.execute(
            "INSERT OR IGNORE INTO line_overrides (game_id, set_by) VALUES (?, ?)",
            (game_id, set_by)
        )
        for col, val in updates.items():
            con.execute(
                f"UPDATE line_overrides SET {col}=?, set_by=?, set_at=CURRENT_TIMESTAMP "
                f"WHERE game_id=?",
                (val, set_by, game_id)
            )


def _clear_line_overrides_for_week(week: int):
    """Remove all line overrides for games in the given week."""
    # Get game_ids for the given week from dm.df_games (weekIndex is 0-based in API)
    gdf = dm.df_games
    if gdf.empty:
        return
    week_col = "weekIndex" if "weekIndex" in gdf.columns else "week"
    if week_col not in gdf.columns:
        return
    week_games = gdf[gdf[week_col] == (week - 1)]  # API weekIndex is 0-based
    id_col = "gameId" if "gameId" in gdf.columns else "id"
    if id_col not in week_games.columns:
        return
    game_ids = [str(gid) for gid in week_games[id_col].tolist()]
    if not game_ids:
        return
    with _db_con() as con:
        placeholders = ",".join("?" * len(game_ids))
        con.execute(f"DELETE FROM line_overrides WHERE game_id IN ({placeholders})", game_ids)
    print(f"[SB] Cleared line overrides for week {week} ({len(game_ids)} games)")


# ═════════════════════════════════════════════════════════════════════════════
#  ODDS ENGINE v3 — Owner History + League Average
# ═════════════════════════════════════════════════════════════════════════════

_OWNER_STATS_CACHE: dict   = {}
_LEAGUE_AVG_SCORE:  float  = 27.0   # updated from DB; fallback if DB empty


def _invalidate_owner_cache():
    global _OWNER_STATS_CACHE
    _OWNER_STATS_CACHE = {}


def _get_owner_history_stats() -> dict:
    """
    Query tsl_history.db for per-owner career stats.
    Returns { userName: { career_win_pct, career_games, avg_pts_scored, avg_pts_allowed } }

    FIX v4.0: status check handles both TEXT '3' and INTEGER 3 in the DB.
    FIX v4.0: computes _LEAGUE_AVG_SCORE from actual game data for O/U calibration.
    """
    global _OWNER_STATS_CACHE, _LEAGUE_AVG_SCORE
    if _OWNER_STATS_CACHE:
        return _OWNER_STATS_CACHE

    stats: dict = {}

    try:
        con = sqlite3.connect(HISTORY_DB_PATH, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")

        # FIX: CAST status to TEXT so '3' and 3 both match
        rows = con.execute("""
            SELECT homeUser, awayUser,
                   CAST(homeScore AS INTEGER) AS hs,
                   CAST(awayScore AS INTEGER) AS aws
            FROM games
            WHERE CAST(status AS TEXT) IN ('2', '3')
              AND homeUser  IS NOT NULL AND homeUser  != ''
              AND awayUser  IS NOT NULL AND awayUser  != ''
              AND homeScore IS NOT NULL AND CAST(homeScore AS INTEGER) >= 0
        """).fetchall()
        con.close()

        total_pts = 0
        total_games = 0

        for home_user, away_user, hs, aws in rows:
            for user, scored, allowed, won in [
                (home_user, hs,  aws, hs > aws),
                (away_user, aws, hs,  aws > hs),
            ]:
                d = stats.setdefault(user, {
                    "wins": 0, "losses": 0, "games": 0,
                    "pts_scored": 0, "pts_allowed": 0
                })
                d["games"]       += 1
                d["pts_scored"]  += scored
                d["pts_allowed"] += allowed
                if won:
                    d["wins"]   += 1
                else:
                    d["losses"] += 1

            total_pts   += hs + aws
            total_games += 1

        # Calibrate league average from actual data
        if total_games >= 10:
            _LEAGUE_AVG_SCORE = round((total_pts / total_games) / 2, 2)
            print(f"[ODDS] League avg pts/team: {_LEAGUE_AVG_SCORE:.1f} ({total_games} games)")
        else:
            _LEAGUE_AVG_SCORE = 27.0
            print(f"[ODDS] Not enough games for avg — using fallback {_LEAGUE_AVG_SCORE}")

    except Exception as e:
        print(f"[ODDS] owner history query failed: {e}")
        _LEAGUE_AVG_SCORE = 27.0

    for user, d in stats.items():
        g = max(d["games"], 1)
        d["career_win_pct"]  = round(d["wins"] / g, 4)
        d["avg_pts_scored"]  = round(d["pts_scored"]  / g, 2)
        d["avg_pts_allowed"] = round(d["pts_allowed"] / g, 2)

    _OWNER_STATS_CACHE = stats
    print(f"[ODDS] Owner history loaded: {len(stats)} owners")
    return stats


def _owner_defaults() -> dict:
    return {
        "career_win_pct":  0.500,
        "avg_pts_scored":  _LEAGUE_AVG_SCORE,
        "avg_pts_allowed": _LEAGUE_AVG_SCORE,
        "games":           0,
    }


def _resolve_owner(username: str, history: dict) -> dict:
    """Fuzzy username lookup — handles underscore/case differences between API and DB."""
    if not username:
        return _owner_defaults()
    if username in history:
        return history[username]
    norm = username.lower().replace("_", "").replace(" ", "")
    for key, val in history.items():
        if key.lower().replace("_", "").replace(" ", "") == norm:
            return val
    return _owner_defaults()


def _get_power_map() -> dict:
    """Build { teamName: { ovr, win_pct, rank, off_rank, def_rank, userName } } from df_power."""
    pm = {}
    if dm.df_power.empty:
        return pm
    for _, row in dm.df_power.iterrows():
        name = row.get("teamName", "")
        if not name:
            continue
        pm[name] = {
            "ovr":      float(row.get("ovrRating",     78) or 78),
            "win_pct":  float(row.get("winPct",       0.5) or 0.5),
            "rank":     int(row.get("rank",              16) or 16),
            "off_rank": int(row.get("offTotalRank",      16) or 16),
            "def_rank": int(row.get("defTotalRank",      16) or 16),
            "userName": str(row.get("userName",          "")),
        }
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# POWER RATING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

_MIDPOINT_RANK = 16.5   # midpoint of 1–32

def _power_rating(team_data: dict, owner_data: dict) -> float:
    """
    Compute a single composite power rating for one team. Higher = stronger.
    Typical range: −8 to +8. Extremes can reach ±12.

    Component breakdown and max per-team contribution:
      Season W%:     ×10   →  ±5.0    (PRIMARY — current form dominates)
      Power rank:    ×0.12 →  ±1.86
      OVR rating:    ×0.15 →  ±~1.5 (norm at 78; typical range 70-90)
      Offense rank:  ×0.12 →  ±1.86
      Defense rank:  ×0.12 →  ±1.86
      Career W%:     ×4.0  →  ±1.0    (ANCHOR — not primary driver)
    ─────────────────────────────────────────────────────────────────────────
    Rationale for career W% demotion (×10 → ×4):
      The v3.3 bug that produced "Commanders +14 at home" came from career W%
      dominating. A .700 vs .300 career owner was worth 4 spread points on its
      own. Career history should anchor the line, not dictate it.
    """
    # Season win% — most direct measure of current performance
    win_pct = float(team_data.get("win_pct", 0.5) or 0.5)
    wp_score = (win_pct - 0.500) * 10.0

    # Current power ranking (1 = best, 32 = worst)
    rank = int(team_data.get("rank", 16) or 16)
    rank_score = (_MIDPOINT_RANK - rank) * 0.12

    # OVR roster quality (centered at 78; each +1 OVR ≈ 0.15 pts)
    ovr = float(team_data.get("ovr", 78) or 78)
    ovr_score = (ovr - 78.0) * 0.15

    # Offensive rank (best offense scores more)
    off_rank = int(team_data.get("off_rank", 16) or 16)
    off_score = (_MIDPOINT_RANK - off_rank) * 0.12

    # Defensive rank (best defense limits opponent)
    def_rank = int(team_data.get("def_rank", 16) or 16)
    def_score = (_MIDPOINT_RANK - def_rank) * 0.12

    # Career owner win% — historical skill anchor, intentionally light
    career_wp = float(owner_data.get("career_win_pct", 0.500) or 0.500)
    career_score = (career_wp - 0.500) * 4.0

    return wp_score + rank_score + ovr_score + off_score + def_score + career_score


def _calc_spread(away_data: dict, home_data: dict,
                 away_owner: dict, home_owner: dict) -> float:
    """
    Spread from HOME team's perspective. Negative = home favored.

    spread = home_power − away_power + HOME_FIELD_EDGE

    Example: home PR = 3.2, away PR = 1.5 → raw = 1.7 + 2.0 = 3.7
             rounds to 3.5 → home is favored by 3.5 (displayed as -3.5)
    """
    home_pr = _power_rating(home_data, home_owner)
    away_pr = _power_rating(away_data, away_owner)
    raw = home_pr - away_pr + HOME_FIELD_EDGE
    spread = round(raw * 2) / 2                        # round to nearest 0.5
    return max(-SPREAD_CAP, min(SPREAD_CAP, spread))   # cap at ±14.5


def _spread_to_ml(spread: float) -> int:
    """
    Convert a point spread to American moneyline for that team.
    Negative spread (favored) → negative ML. Positive spread (dog) → positive ML.

    Calibrated to real NFL implied-probability curves.
    """
    if spread == 0:
        return -110
    abs_s = abs(spread)
    if   abs_s <= 1.0:  base = 115
    elif abs_s <= 2.0:  base = 125
    elif abs_s <= 3.0:  base = 145
    elif abs_s <= 4.0:  base = 165
    elif abs_s <= 5.0:  base = 185
    elif abs_s <= 6.5:  base = 200 + int((abs_s - 5.0) * 10)
    elif abs_s <= 10.0: base = 215 + int((abs_s - 6.5) * 12)
    else:               base = 257 + int((abs_s - 10.0) * 8)
    base = min(base, 600)
    return -base if spread < 0 else base


def _calc_ou(away_data: dict, home_data: dict,
             away_owner: dict, home_owner: dict) -> float:
    """
    Over/Under total points.

    Formula:
      Each team's expected score = owner historical avg (or league avg if < 5 games)
        + offensive rank bonus/penalty  (±3.88 max per team at 0.25/rank)
        − opponent defensive rank penalty (±2.33 max per team at 0.15/rank)

    Total = home_expected + away_expected, clamped 35–72, rounded to nearest 0.5

    v4.0 changes vs v3.3:
      · off multiplier: 0.18 → 0.28  (was too flat; rank 1 vs 32 only ±2.79 before)
      · def multiplier: 0.12 → 0.18  (similar reason)
      · Result: elite off vs elite def matchup ~44, two bad offenses ~38
                two high-powered offenses ~58, typical matchup ~48-54
    """
    LAR = _MIDPOINT_RANK   # League Avg Rank = 16.5

    # Use owner historical avg if at least 5 games; otherwise league average
    h_base = (home_owner["avg_pts_scored"]
              if home_owner.get("games", 0) >= 5 else _LEAGUE_AVG_SCORE)
    a_base = (away_owner["avg_pts_scored"]
              if away_owner.get("games", 0) >= 5 else _LEAGUE_AVG_SCORE)

    # Offensive rank boost/penalty on own team's expected score
    h_off_adj = (LAR - home_data["off_rank"]) * 0.28
    a_off_adj = (LAR - away_data["off_rank"]) * 0.28

    # Defensive rank quality suppresses opponent's expected score
    # Rank 1 defense (best) → most suppression
    h_def_qual = (LAR - home_data["def_rank"]) * 0.18   # home def suppresses away
    a_def_qual = (LAR - away_data["def_rank"]) * 0.18   # away def suppresses home

    home_expected = max(7.0, h_base + h_off_adj - a_def_qual)
    away_expected = max(7.0, a_base + a_off_adj - h_def_qual)

    total = round((home_expected + away_expected) * 2) / 2
    return max(35.0, min(72.0, total))


def _american_to_str(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _payout_calc(wager: int, odds: int) -> int:
    """Return total payout (wager + profit)."""
    odds = int(odds)
    if odds == 0:
        return wager
    if odds > 0:
        return int(wager + wager * (odds / 100))
    return int(wager + wager * (100 / abs(odds)))


def _combine_parlay_odds(odds_list: list[int]) -> int:
    """Combine multiple American odds into a single parlay American odds value."""
    decimal = 1.0
    for o in odds_list:
        o = int(o)
        decimal *= (1 + o / 100) if o > 0 else (1 + 100 / abs(o))
    return int((decimal - 1) * 100) if decimal >= 2.0 else int(-100 / (decimal - 1))


def _build_game_lines(games_raw: list) -> list[dict]:
    """
    Build fully-calculated game lines. Admin line_overrides applied first;
    engine calculates any field not overridden.
    """
    power_map     = _get_power_map()
    owner_history = _get_owner_history_stats()

    _FALLBACK_TEAM = {
        "ovr": 78.0, "win_pct": 0.5, "rank": 16,
        "off_rank": 16, "def_rank": 16, "userName": ""
    }

    def fmt_spread(s: float) -> str:
        if s == 0: return "PK"
        return f"+{s}" if s > 0 else str(s)

    ui_games = []

    for rg in games_raw:
        home     = rg.get("homeTeamName", rg.get("home", ""))
        away     = rg.get("awayTeamName", rg.get("away", ""))
        game_id  = str(rg.get("gameId", rg.get("id", rg.get("matchup_key", f"{away}@{home}"))))
        status   = int(rg.get("status", 1) or 1)
        week_idx = int(rg.get("weekIndex", 99))

        # Auto-lock finished or past-week games
        if status >= 2 or week_idx < dm.CURRENT_WEEK:
            _set_locked(game_id, True)

        home_data  = power_map.get(home, _FALLBACK_TEAM)
        away_data  = power_map.get(away, _FALLBACK_TEAM)
        home_owner = _resolve_owner(home_data["userName"], owner_history)
        away_owner = _resolve_owner(away_data["userName"], owner_history)

        # ── Compute engine values first ───────────────────────────────────
        engine_home_spread = _calc_spread(away_data, home_data, away_owner, home_owner)
        engine_away_spread = -engine_home_spread
        engine_home_ml     = _spread_to_ml(engine_home_spread)
        engine_away_ml     = _spread_to_ml(engine_away_spread)
        engine_ou          = _calc_ou(away_data, home_data, away_owner, home_owner)

        # ── Apply admin overrides ─────────────────────────────────────────
        ov = _get_line_override(game_id) or {}

        # If admin set spread but not ML, auto-derive ML from new spread
        if ov.get("home_spread") is not None and ov.get("home_ml") is None:
            ov["home_spread"] = float(ov["home_spread"])
            ov["away_spread"] = -ov["home_spread"]
            ov["home_ml"]     = _spread_to_ml(ov["home_spread"])
            ov["away_ml"]     = _spread_to_ml(ov["away_spread"])

        home_spread = ov.get("home_spread") if ov.get("home_spread") is not None else engine_home_spread
        away_spread = ov.get("away_spread") if ov.get("away_spread") is not None else engine_away_spread
        home_ml     = ov.get("home_ml")     if ov.get("home_ml")     is not None else engine_home_ml
        away_ml     = ov.get("away_ml")     if ov.get("away_ml")     is not None else engine_away_ml
        ou_line     = ov.get("ou_line")     if ov.get("ou_line")     is not None else engine_ou

        print(
            f"[ODDS] {away}({away_owner['career_win_pct']:.3f}cW% "
            f"PR={_power_rating(away_data,away_owner):.1f}) "
            f"@ {home}({home_owner['career_win_pct']:.3f}cW% "
            f"PR={_power_rating(home_data,home_owner):.1f}) "
            f"→ spread {fmt_spread(home_spread)}  O/U {ou_line}"
            + (" [ADMIN OVERRIDE]" if ov else "")
        )

        ui_games.append({
            "game_id":         game_id,
            "away":            away,
            "home":            home,
            "away_spread":     fmt_spread(away_spread),
            "home_spread":     fmt_spread(home_spread),
            "away_spread_val": away_spread,
            "home_spread_val": home_spread,
            "away_ml":         _american_to_str(away_ml),
            "home_ml":         _american_to_str(home_ml),
            "away_ml_val":     away_ml,
            "home_ml_val":     home_ml,
            "ou_line":         ou_line,
            "matchup_key":     f"{away} @ {home}",
            "status":          status,
            "bet_week":        dm.CURRENT_WEEK + 1,
            # Store engine values for debug view
            "_engine_spread":  engine_home_spread,
            "_away_pr":        _power_rating(away_data, away_owner),
            "_home_pr":        _power_rating(home_data, home_owner),
            "_overridden":     bool(ov),
        })

    return ui_games


# ═════════════════════════════════════════════════════════════════════════════
#  PARLAY CART
# ═════════════════════════════════════════════════════════════════════════════

_parlay_carts: dict[int, list[dict]] = {}

def _get_cart(uid):    return _parlay_carts.setdefault(uid, [])
def _clear_cart(uid):  _parlay_carts[uid] = []

def _add_to_cart(uid: int, leg: dict) -> int:
    cart = _get_cart(uid)
    if any(e["game_id"] == leg["game_id"] for e in cart):
        return -1
    cart.append(leg)
    return len(cart)


# ═════════════════════════════════════════════════════════════════════════════
#  GRADING LOGIC
# ═════════════════════════════════════════════════════════════════════════════

def _build_score_lookup(week: int) -> dict:
    results = dm.get_weekly_results(week)
    lookup  = {}
    fuzzy   = []
    for g in results:
        home = str(g.get("home", "")).strip()
        away = str(g.get("away", "")).strip()
        if not home or not away:
            continue
        key = f"{away} @ {home}".lower().strip()
        lookup[key] = g
        lookup[f"{home} @ {away}".lower().strip()] = g
        fuzzy.append((set(home.lower().split()), set(away.lower().split()), g))
    lookup["__fuzzy__"] = fuzzy
    return lookup


def _fuzzy_match(matchup_key: str, lookup: dict) -> dict | None:
    key = matchup_key.lower().strip()
    if key in lookup:
        return lookup[key]
    query = set(key.replace(" @ ", " ").split())
    for home_toks, away_toks, game in lookup.get("__fuzzy__", []):
        if (query & home_toks) and (query & away_toks):
            return game
    return None


def _grade_single_bet(bet_type: str, pick: str, line: float,
                      home_name: str, away_name: str,
                      h_score: int, a_score: int) -> str:
    """Grade one bet. Returns 'Won', 'Lost', 'Push', or 'Pending'."""
    if bet_type == "Moneyline":
        if h_score == a_score:
            return "Push"
        winner = home_name if h_score > a_score else away_name
        return "Won" if pick.strip().lower() == winner.strip().lower() else "Lost"

    elif bet_type == "Spread":
        pick_l  = pick.strip().lower()
        home_l  = home_name.strip().lower()
        away_l  = away_name.strip().lower()
        if   pick_l == away_l:          covered = (a_score + line) - h_score
        elif pick_l == home_l:          covered = (h_score + line) - a_score
        elif pick_l in away_l:          covered = (a_score + line) - h_score
        elif pick_l in home_l:          covered = (h_score + line) - a_score
        else:                           return "Pending"
        if covered > 0:  return "Won"
        if covered < 0:  return "Lost"
        return "Push"

    elif bet_type == "Over":
        total = h_score + a_score
        if total > line: return "Won"
        if total < line: return "Lost"
        return "Push"

    elif bet_type == "Under":
        total = h_score + a_score
        if total < line: return "Won"
        if total > line: return "Lost"
        return "Push"

    return "Pending"


# ═════════════════════════════════════════════════════════════════════════════
#  AUTO-GRADE
# ═════════════════════════════════════════════════════════════════════════════

async def _run_autograde(bot) -> None:
    def _grade_sync():
        results = []
        try:
            with _db_con() as con:
                ungraded = con.execute(
                    "SELECT DISTINCT week FROM bets_table "
                    "WHERE status NOT IN ('Won','Lost','Push','Cancelled') AND week <= ?",
                    (dm.CURRENT_WEEK,)
                ).fetchall()
            if not ungraded:
                return results

            for (week,) in ungraded:
                scores     = _build_score_lookup(week)
                real_games = len([k for k in scores if k != "__fuzzy__"])
                if real_games == 0:
                    continue

                settled = wins = losses = pushes = 0
                total_paid = 0

                with _db_con() as con:
                    pending = con.execute(
                        "SELECT bet_id, discord_id, matchup, bet_type, wager_amount, odds, pick, line "
                        "FROM bets_table WHERE week=? AND status NOT IN ('Won','Lost','Push','Cancelled')",
                        (week,)
                    ).fetchall()

                    for b in pending:
                        bid, uid, matchup, btype, amt, odds, pick, line = b
                        gd = _fuzzy_match(matchup.lower().strip(), scores)
                        if not gd:
                            continue
                        try:
                            line_val = float(line)
                        except (ValueError, TypeError):
                            line_val = 0.0
                        res = _grade_single_bet(btype, pick, line_val,
                                                gd["home"], gd["away"],
                                                gd["home_score"], gd["away_score"])
                        if res == "Pending":
                            continue
                        if res == "Won":
                            payout = _payout_calc(amt, int(odds))
                            _update_balance(uid, payout, con)
                            total_paid += payout - amt
                            wins += 1
                        elif res == "Push":
                            _update_balance(uid, amt, con)
                            pushes += 1
                        elif res == "Lost":
                            losses += 1
                        con.execute("UPDATE bets_table SET status=? WHERE bet_id=?", (res, bid))
                        settled += 1

                    # ── Parlays ───────────────────────────────────────────
                    parlays = con.execute(
                        "SELECT parlay_id, discord_id, legs, combined_odds, wager_amount "
                        "FROM parlays_table WHERE week=? AND status='Pending'",
                        (week,)
                    ).fetchall()

                    for pid, uid, legs_json, c_odds, amt in parlays:
                        try:
                            legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else legs_json
                            if not isinstance(legs, list) or not legs:
                                con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                                continue
                        except Exception:
                            continue

                        all_won    = True
                        any_lost   = False
                        unresolved = 0
                        any_pushed = False

                        for leg in legs:
                            gd = _fuzzy_match(leg["matchup"].lower().strip(), scores)
                            if not gd:
                                unresolved += 1
                                all_won = False
                                continue
                            res = _grade_single_bet(
                                leg["bet_type"], leg["pick"], float(leg.get("line", 0)),
                                gd["home"], gd["away"], gd["home_score"], gd["away_score"]
                            )
                            if res == "Lost":
                                all_won  = False
                                any_lost = True
                                break
                            elif res == "Push":
                                all_won    = False
                                any_pushed = True
                            elif res == "Pending":
                                all_won    = False
                                unresolved += 1

                        # Leave Pending if any leg still unresolved (not yet final)
                        if unresolved > 0 and not any_lost:
                            continue

                        if all_won:
                            payout = _payout_calc(amt, c_odds)
                            _update_balance(uid, payout, con)
                            total_paid += payout - amt
                            con.execute("UPDATE parlays_table SET status='Won' WHERE parlay_id=?", (pid,))
                            wins += 1
                        elif any_lost:
                            con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                            losses += 1
                        elif any_pushed:
                            # One leg pushed, rest won → return wager (standard parlay push rule)
                            _update_balance(uid, amt, con)
                            con.execute("UPDATE parlays_table SET status='Push' WHERE parlay_id=?", (pid,))
                            pushes += 1

                if settled > 0 or wins + losses + pushes > 0:
                    results.append({
                        "week": week, "settled": settled,
                        "wins": wins, "losses": losses, "pushes": pushes,
                        "total_paid": total_paid,
                    })
        except Exception as e:
            print(f"[AUTO-GRADE] Error: {e}")
        return results

    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _grade_sync)

    for r in results:
        print(
            f"[AUTO-GRADE] Week {r['week']} — Settled {r['settled']} | "
            f"W{r['wins']} L{r['losses']} P{r['pushes']} | Paid ${r['total_paid']:,}"
        )
        try:
            from setup_cog import get_channel_id as _get_ch_id
            ch_id = _get_ch_id("sportsbook")
            channel = bot.get_channel(ch_id) if ch_id else None
        except ImportError:
            channel = discord.utils.get(bot.get_all_channels(), name="sportsbook")
        if channel:
            try:
                embed = discord.Embed(
                    title=f"✅ Week {r['week']} Bets Auto-Graded", color=TSL_GOLD
                )
                embed.add_field(name="Settled",      value=str(r["settled"]),       inline=True)
                embed.add_field(name="✅ Won",       value=str(r["wins"]),          inline=True)
                embed.add_field(name="❌ Lost",      value=str(r["losses"]),        inline=True)
                embed.add_field(name="🔁 Push",     value=str(r["pushes"]),        inline=True)
                embed.add_field(name="💸 Paid Out", value=f"${r['total_paid']:,}", inline=True)
                embed.set_footer(text="TSL Sportsbook • Auto-graded")
                await channel.send(embed=embed)
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════════
#  UI COMPONENTS
# ═════════════════════════════════════════════════════════════════════════════

class BetSlipModal(discord.ui.Modal):
    def __init__(self, team, line, odds, game_id, bet_type,
                 matchup_key, away_name, home_name, bet_week=None):
        super().__init__(title=f"📋 Bet Slip — {bet_type}")
        self.team        = team
        self.line        = line
        self.odds        = odds
        self.game_id     = game_id
        self.bet_type    = bet_type
        self.matchup_key = matchup_key
        self.away_name   = away_name
        self.home_name   = home_name
        self.bet_week    = bet_week if bet_week is not None else (dm.CURRENT_WEEK + 1)

        self.amount_input = discord.ui.TextInput(
            label=f"Wager Amount  |  Odds: {_american_to_str(odds)}",
            placeholder=f"Min ${MIN_BET}  —  Pick: {team}",
            min_length=1, max_length=8
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        if _is_locked(self.game_id):
            return await interaction.response.send_message(
                "🔴 Game is **locked**.", ephemeral=True
            )
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Enter a valid number.", ephemeral=True)

        balance = _get_balance(interaction.user.id)
        if amt < MIN_BET:
            return await interaction.response.send_message(f"❌ Minimum bet is **${MIN_BET}**.", ephemeral=True)
        if amt > balance:
            return await interaction.response.send_message(
                f"❌ Insufficient balance. You have **${balance:,}**.", ephemeral=True
            )

        with _db_con() as con:
            _update_balance(interaction.user.id, -amt, con)
            safe_line = self.line if isinstance(self.line, (int, float)) else 0.0
            con.execute(
                "INSERT INTO bets_table "
                "(discord_id, week, matchup, bet_type, wager_amount, odds, pick, line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(interaction.user.id), int(self.bet_week), self.matchup_key,
                 self.bet_type, int(amt), int(self.odds), self.team, float(safe_line))
            )

        profit  = _payout_calc(amt, self.odds) - amt
        new_bal = balance - amt

        embed = discord.Embed(title="✅ Bet Confirmed", color=TSL_GOLD)
        embed.add_field(name="Pick",    value=f"**{self.team}**",          inline=True)
        embed.add_field(name="Type",    value=self.bet_type,               inline=True)
        embed.add_field(name="Odds",    value=_american_to_str(self.odds), inline=True)
        embed.add_field(name="Risk",    value=f"**${amt:,}**",             inline=True)
        embed.add_field(name="To Win",  value=f"**${profit:,}**",          inline=True)
        embed.add_field(name="Balance", value=f"${new_bal:,}",             inline=True)
        embed.set_footer(text=f"TSL Sportsbook • Week {self.bet_week}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ParlayWagerModal(discord.ui.Modal):
    def __init__(self, uid, legs, combined_odds):
        super().__init__(title=f"🎰 Submit Parlay ({len(legs)} Legs)")
        self.uid           = uid
        self.legs          = legs
        self.combined_odds = combined_odds

        leg_summary = " | ".join(l["pick"] for l in legs)
        self.amount_input = discord.ui.TextInput(
            label=f"Wager  |  Combined Odds: {_american_to_str(combined_odds)}",
            placeholder=f"Legs: {leg_summary[:60]}",
            min_length=1, max_length=8
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Enter a valid number.", ephemeral=True)

        balance = _get_balance(interaction.user.id)
        if amt < MIN_BET:
            return await interaction.response.send_message(f"❌ Min bet is **${MIN_BET}**.", ephemeral=True)
        if amt > balance:
            return await interaction.response.send_message(
                f"❌ Insufficient funds. Balance: **${balance:,}**.", ephemeral=True
            )

        parlay_id = str(uuid.uuid4())[:8].upper()
        with _db_con() as con:
            _update_balance(interaction.user.id, -amt, con)
            con.execute(
                "INSERT INTO parlays_table "
                "(parlay_id, discord_id, week, legs, combined_odds, wager_amount, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'Pending')",
                (parlay_id, int(interaction.user.id), int(dm.CURRENT_WEEK + 1),
                 json.dumps(self.legs), int(self.combined_odds), int(amt))
            )

        potential = _payout_calc(amt, self.combined_odds) - amt
        embed = discord.Embed(title="🎰 Parlay Confirmed!", color=TSL_GOLD)
        embed.description = f"**Parlay ID:** `{parlay_id}`"
        for i, leg in enumerate(self.legs, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['matchup']}",
                value=f"**{leg['pick']}** ({leg['bet_type']}) @ {_american_to_str(leg['odds'])}",
                inline=False
            )
        embed.add_field(name="Combined Odds", value=_american_to_str(self.combined_odds), inline=True)
        embed.add_field(name="Risk",          value=f"**${amt:,}**",                      inline=True)
        embed.add_field(name="To Win",        value=f"**${potential:,}**",                inline=True)
        embed.set_footer(text=f"TSL Sportsbook • Week {dm.CURRENT_WEEK + 1} • All legs must hit")
        _clear_cart(interaction.user.id)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PropBetModal(discord.ui.Modal):
    """Modal for placing a prop bet wager."""
    def __init__(self, prop_id, description, pick, odds):
        super().__init__(title=f"📋 Prop Bet — {pick}")
        self.prop_id     = prop_id
        self.description = description
        self.pick        = pick
        self.odds        = odds

        self.amount_input = discord.ui.TextInput(
            label=f"Wager Amount  |  Odds: {_american_to_str(odds)}",
            placeholder=f"Min ${MIN_BET}  —  Pick: {pick}",
            min_length=1, max_length=8
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.amount_input.value.replace(",", "").replace("$", ""))
        except ValueError:
            return await interaction.response.send_message("❌ Enter a valid number.", ephemeral=True)

        balance = _get_balance(interaction.user.id)
        if amt < MIN_BET:
            return await interaction.response.send_message(f"❌ Min bet is **${MIN_BET}**.", ephemeral=True)
        if amt > balance:
            return await interaction.response.send_message(
                f"❌ Insufficient funds. Balance: **${balance:,}**.", ephemeral=True
            )

        # Verify prop is still open
        with _db_con() as con:
            prop = con.execute(
                "SELECT status FROM prop_bets WHERE prop_id=?", (self.prop_id,)
            ).fetchone()
            if not prop or prop[0] != 'Open':
                return await interaction.response.send_message(
                    "❌ This prop bet is no longer open.", ephemeral=True
                )
            _update_balance(interaction.user.id, -amt, con)
            con.execute(
                "INSERT INTO prop_wagers (prop_id, discord_id, pick, wager_amount, odds) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.prop_id, int(interaction.user.id), self.pick, int(amt), int(self.odds))
            )

        profit = _payout_calc(amt, self.odds) - amt
        embed = discord.Embed(title="✅ Prop Bet Confirmed", color=TSL_GOLD)
        embed.add_field(name="Prop",    value=self.description[:50], inline=False)
        embed.add_field(name="Pick",    value=f"**{self.pick}**",    inline=True)
        embed.add_field(name="Odds",    value=_american_to_str(self.odds), inline=True)
        embed.add_field(name="Risk",    value=f"**${amt:,}**",       inline=True)
        embed.add_field(name="To Win",  value=f"**${profit:,}**",    inline=True)
        embed.add_field(name="Balance", value=f"${balance - amt:,}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GameCardViewWithParlay(discord.ui.View):
    """Full game card with straight + parlay buttons."""
    def __init__(self, game: dict):
        super().__init__(timeout=180)
        self.game    = game
        self.game_id = game["game_id"]

        away, home = game["away"], game["home"]
        ou = game["ou_line"]

        bets = [
            (f"{away} {game['away_spread']} (−110)", away,         game["away_spread_val"], -110,               "Spread",    0),
            (f"{home} {game['home_spread']} (−110)", home,         game["home_spread_val"], -110,               "Spread",    0),
            (f"{away} ML {game['away_ml']}",         away,         0.0,                    game["away_ml_val"], "Moneyline", 1),
            (f"{home} ML {game['home_ml']}",         home,         0.0,                    game["home_ml_val"], "Moneyline", 1),
            (f"OVER {ou} (−110)",                    f"OVER {ou}", ou,                     -110,                "Over",      2),
            (f"UNDER {ou} (−110)",                   f"UNDER {ou}",ou,                     -110,                "Under",     2),
        ]

        for label, pick, line, odds, bet_type, row in bets:
            btn = discord.ui.Button(
                label=label[:80],
                style=(discord.ButtonStyle.secondary if row == 0 else
                       discord.ButtonStyle.primary   if row == 1 else
                       discord.ButtonStyle.success   if "OVER" in label else
                       discord.ButtonStyle.danger),
                row=row
            )
            btn.callback = self._make_straight_cb(pick, line, odds, bet_type)
            self.add_item(btn)

            p_btn = discord.ui.Button(label="🎰+", style=discord.ButtonStyle.secondary, row=row)
            p_btn.callback = self._make_parlay_cb(pick, line, odds, bet_type)
            self.add_item(p_btn)

    def _make_straight_cb(self, pick, line, odds, bet_type):
        game = self.game
        async def callback(interaction: discord.Interaction):
            if _is_locked(game["game_id"]):
                return await interaction.response.send_message("🔴 Game is **locked**.", ephemeral=True)
            modal = BetSlipModal(
                team=pick, line=line, odds=odds, game_id=game["game_id"],
                bet_type=bet_type, matchup_key=game["matchup_key"],
                away_name=game["away"], home_name=game["home"],
                bet_week=game.get("bet_week", dm.CURRENT_WEEK + 1)
            )
            await interaction.response.send_modal(modal)
        return callback

    def _make_parlay_cb(self, pick, line, odds, bet_type):
        game = self.game
        async def callback(interaction: discord.Interaction):
            if _is_locked(game["game_id"]):
                return await interaction.response.send_message("🔴 Game is **locked**.", ephemeral=True)
            uid = interaction.user.id
            leg = {"game_id": game["game_id"], "matchup": game["matchup_key"],
                   "pick": pick, "line": line, "odds": odds, "bet_type": bet_type}
            result = _add_to_cart(uid, leg)
            if result == -1:
                return await interaction.response.send_message(
                    "⚠️ You already have a leg from this game in your parlay cart.", ephemeral=True
                )
            cart     = _get_cart(uid)
            combined = _combine_parlay_odds([l["odds"] for l in cart])
            legs_text = "\n".join(
                f"**{i+1}.** {l['pick']} ({l['bet_type']}) @ {_american_to_str(l['odds'])}"
                for i, l in enumerate(cart)
            )
            embed = discord.Embed(title="🎰 Parlay Cart", color=TSL_GOLD)
            embed.description = legs_text
            embed.add_field(name="Legs",          value=str(len(cart)),             inline=True)
            embed.add_field(name="Combined Odds", value=_american_to_str(combined), inline=True)
            view = ParlayCartView(uid, cart, combined)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        return callback


class ParlayCartView(discord.ui.View):
    def __init__(self, uid, cart, combined_odds):
        super().__init__(timeout=120)
        self.uid           = uid
        self.cart          = cart
        self.combined_odds = combined_odds

    @discord.ui.button(label="💰 Submit Parlay", style=discord.ButtonStyle.success, row=0)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.cart) < 2:
            return await interaction.response.send_message(
                "❌ A parlay requires at least **2 legs**.", ephemeral=True
            )
        await interaction.response.send_modal(
            ParlayWagerModal(self.uid, self.cart, self.combined_odds)
        )

    @discord.ui.button(label="🗑️ Clear Cart", style=discord.ButtonStyle.danger, row=0)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        _clear_cart(self.uid)
        await interaction.response.send_message("🗑️ Parlay cart cleared.", ephemeral=True)


class SportsbookSelectView(discord.ui.View):
    def __init__(self, games: list[dict]):
        super().__init__(timeout=None)
        self.games = games

        options = []
        for i, g in enumerate(games):
            locked  = "🔴 " if _is_locked(g["game_id"]) else "🟢 "
            over    = " ⚠️" if g.get("_overridden") else ""
            options.append(discord.SelectOption(
                label=f"{locked}{g['away']} @ {g['home']}{over}",
                value=str(i),
                description=f"Spread: {g['away']} {g['away_spread']} | O/U {g['ou_line']}"
            ))

        sel = discord.ui.Select(
            placeholder="━━ SELECT A GAME TO BET ━━",
            options=options[:25],
            min_values=1, max_values=1
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        game = self.games[int(interaction.data["values"][0])]
        locked_str = ("🔴 **LOCKED — Betting Closed**"
                      if _is_locked(game["game_id"]) else "🟢 **OPEN — Place Your Bets**")
        admin_note = " *(line adjusted)*" if game.get("_overridden") else ""

        embed = discord.Embed(
            title=f"🏟️  {game['away']} @ {game['home']}",
            color=TSL_RED if _is_locked(game["game_id"]) else TSL_GOLD
        )
        embed.add_field(
            name="📊 Spread",
            value=(f"`{game['away']}` **{game['away_spread']}** (-110)\n"
                   f"`{game['home']}` **{game['home_spread']}** (-110)"),
            inline=True
        )
        embed.add_field(
            name="💰 Moneyline",
            value=(f"`{game['away']}` **{game['away_ml']}**\n"
                   f"`{game['home']}` **{game['home_ml']}**"),
            inline=True
        )
        embed.add_field(
            name="🎯 Over/Under",
            value=f"**{game['ou_line']}** pts\nOver / Under (-110 each)",
            inline=True
        )
        embed.add_field(name="Status", value=locked_str + admin_note, inline=False)
        embed.set_footer(text="Click a bet button below  •  🎰+ adds to your parlay cart")

        await interaction.response.send_message(
            embed=embed, view=GameCardViewWithParlay(game), ephemeral=True
        )


# ═════════════════════════════════════════════════════════════════════════════
#  PROP BET UI
# ═════════════════════════════════════════════════════════════════════════════

class PropListView(discord.ui.View):
    """Select menu showing open prop bets."""
    def __init__(self, props: list):
        super().__init__(timeout=120)
        self.props = props

        if not props:
            return

        options = [
            discord.SelectOption(
                label=f"#{p[0]}: {p[2][:50]}",
                value=str(i),
                description=f"{p[3]} vs {p[4]}"
            )
            for i, p in enumerate(props)
        ]
        sel = discord.ui.Select(
            placeholder="━━ SELECT A PROP BET ━━",
            options=options[:25],
            min_values=1, max_values=1
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        prop = self.props[int(interaction.data["values"][0])]
        pid, week, desc, opt_a, opt_b, odds_a, odds_b = prop[:7]

        embed = discord.Embed(title=f"📋  Prop #{pid}", color=TSL_GOLD)
        embed.description = f"**{desc}**"
        embed.add_field(name=f"Option A: {opt_a}", value=f"Odds: {_american_to_str(odds_a)}", inline=True)
        embed.add_field(name=f"Option B: {opt_b}", value=f"Odds: {_american_to_str(odds_b)}", inline=True)
        embed.set_footer(text=f"Week {week} Prop")

        view = PropBetButtons(pid, desc, opt_a, opt_b, odds_a, odds_b)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PropBetButtons(discord.ui.View):
    def __init__(self, prop_id, description, opt_a, opt_b, odds_a, odds_b):
        super().__init__(timeout=60)
        self.prop_id     = prop_id
        self.description = description

        btn_a = discord.ui.Button(
            label=f"{opt_a} ({_american_to_str(odds_a)})",
            style=discord.ButtonStyle.primary, row=0
        )
        btn_b = discord.ui.Button(
            label=f"{opt_b} ({_american_to_str(odds_b)})",
            style=discord.ButtonStyle.secondary, row=0
        )
        btn_a.callback = self._make_bet_cb(opt_a, odds_a)
        btn_b.callback = self._make_bet_cb(opt_b, odds_b)
        self.add_item(btn_a)
        self.add_item(btn_b)

    def _make_bet_cb(self, pick, odds):
        async def callback(interaction: discord.Interaction):
            modal = PropBetModal(self.prop_id, self.description, pick, odds)
            await interaction.response.send_modal(modal)
        return callback


# ═════════════════════════════════════════════════════════════════════════════
#  THE COG
# ═════════════════════════════════════════════════════════════════════════════

class SportsbookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        setup_db()
        dm._autograde_callback = self._on_data_refresh
        self.auto_grade.start()

    def cog_unload(self):
        self.auto_grade.cancel()
        dm._autograde_callback = None

    async def _on_data_refresh(self):
        _invalidate_owner_cache()
        await _run_autograde(self.bot)

    @tasks.loop(minutes=60)
    async def auto_grade(self):
        await _run_autograde(self.bot)

    @auto_grade.before_loop
    async def before_auto_grade(self):
        await self.bot.wait_until_ready()
        await discord.utils.sleep_until(
            discord.utils.utcnow().replace(minute=0, second=0, microsecond=0)
        )

    # ── Autocomplete helper ───────────────────────────────────────────────────
    async def _matchup_autocomplete(self, interaction: discord.Interaction, current: str):
        if dm.df_games.empty:
            return []
        choices = []
        for _, g in dm.df_games.iterrows():
            key = str(g.get("matchup_key", ""))
            if not key:
                continue
            if current.lower() in key.lower():
                choices.append(app_commands.Choice(name=key[:100], value=key[:100]))
        return choices[:25]

    # ─────────────────────────────────────────────────────────────────────────
    # USER COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sportsbook", description="Open the TSL Interactive Sportsbook")
    async def sportsbook(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            bet_week = dm.CURRENT_WEEK + 1
            src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
            if src.empty:
                return await interaction.followup.send("❌ No game data loaded. Try again shortly.")

            df = src.copy()
            for col in ["weekIndex", "seasonIndex", "stageIndex"]:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: int(float(x)) if x not in (None, "", "nan") else -1
                    )

            if "weekIndex" in df.columns:
                mask = df["weekIndex"] == (bet_week - 1)
                if "seasonIndex" in df.columns:
                    mask = mask & (df["seasonIndex"] == dm.CURRENT_SEASON)
                games_df = df[mask]
            elif "week" in df.columns:
                games_df = df[df["week"].apply(
                    lambda x: int(float(x)) if x not in (None, "") else -1
                ) == bet_week]
            else:
                games_df = df

            raw_games = games_df.to_dict("records")
            if not raw_games:
                return await interaction.followup.send(
                    f"❌ No games found for Week {bet_week}. Schedule may not be posted yet."
                )

            loop = asyncio.get_running_loop()
            ui_games = await loop.run_in_executor(None, _build_game_lines, raw_games)

        except Exception as e:
            return await interaction.followup.send(f"❌ Error loading games: `{e}`")

        balance = _get_balance(interaction.user.id)
        embed = discord.Embed(title="🏆  TSL GLOBAL SPORTSBOOK", color=TSL_GOLD)
        if interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.description = (
            f"```\n"
            f"WEEK {bet_week} BOARD  •  SEASON {dm.CURRENT_SEASON}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Your Balance:  ${balance:,}\n"
            f"🎮 Games:         {len(ui_games)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SELECT A GAME TO PLACE WAGER\n"
            f"```"
        )
        embed.set_footer(
            text="Lines: Season W% · Power Rank · OVR · Off/Def Rank · Career W% · Home Edge"
        )
        await interaction.followup.send(embed=embed, view=SportsbookSelectView(ui_games))

    @app_commands.command(name="mybets", description="View your active bets and current balance")
    async def mybets(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        uid     = interaction.user.id
        balance = _get_balance(uid)

        with _db_con() as con:
            straight = con.execute(
                "SELECT matchup, bet_type, pick, wager_amount, odds, line, status, week "
                "FROM bets_table WHERE discord_id=? AND status='Pending' ORDER BY bet_id DESC",
                (uid,)
            ).fetchall()
            parlays = con.execute(
                "SELECT parlay_id, week, legs, combined_odds, wager_amount, status "
                "FROM parlays_table WHERE discord_id=? AND status='Pending' ORDER BY rowid DESC",
                (uid,)
            ).fetchall()

        embed = discord.Embed(
            title=f"📋  {interaction.user.display_name}'s Active Bets", color=TSL_GOLD
        )
        embed.add_field(name="💰 Balance", value=f"**${balance:,}**",                        inline=True)
        embed.add_field(name="🎯 Pending", value=f"**{len(straight) + len(parlays)}** bets", inline=True)

        if not straight and not parlays:
            embed.description = "_No pending bets. Hit `/sportsbook` to place some!_"
        else:
            if straight:
                lines = []
                for b in straight[:8]:
                    matchup, btype, pick, amt, odds, line, status, week = b
                    profit = _payout_calc(amt, odds) - amt
                    lines.append(
                        f"**{pick}** ({btype}) @ {_american_to_str(int(odds))} | "
                        f"${amt:,} → +${profit:,} | W{week}"
                    )
                embed.add_field(name="🎯 Straight Bets", value="\n".join(lines), inline=False)
            if parlays:
                lines = []
                for p in parlays[:4]:
                    pid, week, legs_json, c_odds, amt, status = p
                    try:
                        legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else []
                    except Exception:
                        legs = []
                    potential = _payout_calc(amt, c_odds) - amt
                    picks = ", ".join(l["pick"] for l in legs) if legs else "—"
                    lines.append(
                        f"`{pid}` — {len(legs)} legs ({picks[:40]}) | ${amt:,} → +${potential:,}"
                    )
                embed.add_field(name="🎰 Parlays", value="\n".join(lines), inline=False)

        cart = _get_cart(uid)
        if cart:
            combined = _combine_parlay_odds([l["odds"] for l in cart])
            embed.add_field(
                name="🛒 Parlay Cart",
                value=(f"{len(cart)} leg(s) in cart | Combined: {_american_to_str(combined)}\n"
                       "Use **🎰+** buttons in `/sportsbook` to submit."),
                inline=False
            )
        embed.set_footer(text="TSL Sportsbook — Pending bets only")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="bethistory", description="View your full season bet history and P&L")
    @app_commands.describe(weeks="How many weeks to look back (default: all)")
    async def bethistory(self, interaction: discord.Interaction, weeks: int = 99):
        await interaction.response.defer(thinking=True, ephemeral=True)
        uid = interaction.user.id

        with _db_con() as con:
            bets = con.execute(
                "SELECT matchup, bet_type, pick, wager_amount, odds, status, week "
                "FROM bets_table WHERE discord_id=? AND week >= ? ORDER BY week DESC, bet_id DESC",
                (uid, max(1, dm.CURRENT_WEEK - weeks))
            ).fetchall()
            parlays = con.execute(
                "SELECT parlay_id, week, legs, combined_odds, wager_amount, status "
                "FROM parlays_table WHERE discord_id=? AND week >= ? ORDER BY week DESC",
                (uid, max(1, dm.CURRENT_WEEK - weeks))
            ).fetchall()

        total_wagered = total_profit = wins = losses = pushes = 0
        for b in bets:
            matchup, btype, pick, amt, odds, status, week = b
            total_wagered += amt
            if status == "Won":
                total_profit += _payout_calc(amt, int(odds)) - amt; wins += 1
            elif status == "Lost":
                total_profit -= amt; losses += 1
            elif status == "Push":
                pushes += 1

        for p in parlays:
            pid, week, legs_json, c_odds, amt, status = p
            total_wagered += amt
            if status == "Won":
                total_profit += _payout_calc(amt, c_odds) - amt; wins += 1
            elif status == "Lost":
                total_profit -= amt; losses += 1

        embed = discord.Embed(
            title=f"📊  {interaction.user.display_name}'s Bet History", color=TSL_GOLD
        )
        embed.add_field(name="🏆 Record",     value=f"**{wins}W - {losses}L - {pushes}P**", inline=True)
        embed.add_field(name="💸 Total Risk", value=f"${total_wagered:,}",                   inline=True)
        net_icon = "📈" if total_profit >= 0 else "📉"
        embed.add_field(name=f"{net_icon} Net P&L", value=f"**${total_profit:+,}**",          inline=True)

        if bets:
            lines = []
            for b in bets[:10]:
                matchup, btype, pick, amt, odds, status, week = b
                icon = {"Won": "✅", "Lost": "❌", "Push": "🔁", "Pending": "⏳"}.get(status, "•")
                lines.append(
                    f"{icon} W{week} | **{pick}** ({btype}) | ${amt:,} | {_american_to_str(int(odds))}"
                )
            embed.add_field(name="🕐 Recent Bets", value="\n".join(lines), inline=False)

        embed.set_footer(text="TSL Sportsbook • Season History")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="TSL Sportsbook season P&L leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        with _db_con() as con:
            users = con.execute(
                "SELECT discord_id, balance, season_start_balance "
                "FROM users_table ORDER BY balance DESC"
            ).fetchall()

        if not users:
            return await interaction.followup.send("No bettors found yet.")

        embed = discord.Embed(title="🏆  TSL SPORTSBOOK LEADERBOARD", color=TSL_GOLD)
        embed.description = f"**Season {dm.CURRENT_SEASON} • Week {dm.CURRENT_WEEK}**\n"

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (uid, balance, start) in enumerate(users[:15]):
            pnl    = balance - (start or STARTING_BALANCE)
            sign   = "+" if pnl >= 0 else ""
            arrow  = "📈" if pnl >= 0 else "📉"
            medal  = medals[i] if i < 3 else f"**#{i+1}**"
            member = interaction.guild.get_member(uid) if interaction.guild else None
            name   = member.display_name if member else f"<@{uid}>"
            lines.append(f"{medal} {name}\n   💰 ${balance:,}  •  {arrow} {sign}${pnl:,}")

        embed.description += "\n".join(lines)
        embed.set_footer(text=f"Starting balance: ${STARTING_BALANCE:,} • Updated live")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="props", description="Browse and bet on TSL prop bets")
    async def props(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        with _db_con() as con:
            prop_list = con.execute(
                "SELECT prop_id, week, description, option_a, option_b, odds_a, odds_b "
                "FROM prop_bets WHERE status='Open' ORDER BY prop_id DESC"
            ).fetchall()

        if not prop_list:
            return await interaction.followup.send(
                "No open prop bets right now. Check back later!", ephemeral=True
            )

        embed = discord.Embed(title="📋  TSL Prop Bets", color=TSL_GOLD)
        embed.description = f"**{len(prop_list)} open prop(s)** — select one below to bet"
        embed.set_footer(text="TSL Sportsbook • Prop Bets")

        await interaction.followup.send(
            embed=embed, view=PropListView(prop_list), ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # SETTLEMENT COMMANDS
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="grade_bets", description="[Commish] Settle all pending bets for a week")
    @app_commands.describe(week="Week number to settle")
    async def grade_bets(self, interaction: discord.Interaction, week: int):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        await interaction.response.defer(thinking=True)
        scores     = _build_score_lookup(week)
        real_games = len([k for k in scores if k != "__fuzzy__"])
        if real_games == 0:
            return await interaction.followup.send(
                f"❌ No final scores found for Week {week}. "
                f"Check that games are status==3 in the MM export."
            )

        settled = wins = losses = pushes = 0
        total_paid = 0
        bet_log    = []

        with _db_con() as con:
            pending = con.execute(
                "SELECT bet_id, discord_id, matchup, bet_type, wager_amount, odds, pick, line "
                "FROM bets_table WHERE week=? AND status NOT IN ('Won','Lost','Push','Cancelled')",
                (week,)
            ).fetchall()
            print(f"[GRADE] {len(pending)} ungraded bets for week {week}")

            for b in pending:
                bid, uid, matchup, btype, amt, odds, pick, line = b
                key = matchup.lower().strip()
                gd  = _fuzzy_match(key, scores)
                if not gd:
                    continue
                try:
                    line_val = float(line)
                except (ValueError, TypeError):
                    line_val = 0.0
                res = _grade_single_bet(btype, pick, line_val,
                                        gd["home"], gd["away"],
                                        gd["home_score"], gd["away_score"])
                print(f"[GRADE] bid={bid} {btype} pick='{pick}' → {res}")
                if res == "Pending":
                    continue
                if res == "Won":
                    payout = _payout_calc(amt, int(odds))
                    _update_balance(uid, payout, con)
                    total_paid += payout - amt
                    wins += 1
                elif res == "Push":
                    _update_balance(uid, amt, con)
                    pushes += 1
                elif res == "Lost":
                    losses += 1
                con.execute("UPDATE bets_table SET status=? WHERE bet_id=?", (res, bid))
                settled += 1
                profit = (_payout_calc(amt, int(odds)) - amt) if res == "Won" else 0
                bet_log.append({"uid": uid, "result": res, "pick": pick,
                                  "bet_type": btype, "matchup": matchup,
                                  "wager": amt, "profit": profit})

            # Grade parlays
            parlays = con.execute(
                "SELECT parlay_id, discord_id, legs, combined_odds, wager_amount "
                "FROM parlays_table WHERE week=? AND status='Pending'",
                (week,)
            ).fetchall()
            for pid, uid, legs_json, c_odds, amt in parlays:
                try:
                    legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else legs_json
                    if not isinstance(legs, list):
                        continue
                except Exception:
                    continue

                all_won = True; any_lost = False; unresolved = 0; any_pushed = False
                for leg in legs:
                    gd = _fuzzy_match(leg["matchup"].lower().strip(), scores)
                    if not gd:
                        all_won = False; unresolved += 1; continue
                    res = _grade_single_bet(
                        leg["bet_type"], leg["pick"], float(leg.get("line", 0)),
                        gd["home"], gd["away"], gd["home_score"], gd["away_score"]
                    )
                    if res == "Lost":
                        all_won = False; any_lost = True; break
                    elif res == "Push":
                        all_won = False; any_pushed = True
                    elif res == "Pending":
                        all_won = False; unresolved += 1

                if unresolved > 0 and not any_lost:
                    continue
                if all_won:
                    payout = _payout_calc(amt, c_odds)
                    _update_balance(uid, payout, con)
                    total_paid += payout - amt
                    con.execute("UPDATE parlays_table SET status='Won' WHERE parlay_id=?", (pid,))
                    wins += 1
                elif any_lost:
                    con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                    losses += 1
                elif any_pushed:
                    _update_balance(uid, amt, con)
                    con.execute("UPDATE parlays_table SET status='Push' WHERE parlay_id=?", (pid,))
                    pushes += 1

        embed = discord.Embed(title=f"✅  Week {week} Bets Graded", color=TSL_GOLD)
        embed.add_field(name="Settled",      value=str(settled),       inline=True)
        embed.add_field(name="✅ Won",       value=str(wins),          inline=True)
        embed.add_field(name="❌ Lost",      value=str(losses),        inline=True)
        embed.add_field(name="🔁 Push",     value=str(pushes),        inline=True)
        embed.add_field(name="💸 Paid Out", value=f"${total_paid:,}", inline=True)
        embed.add_field(name="\u200b",      value="\u200b",           inline=True)

        if bet_log:
            lines = []
            for entry in bet_log[:15]:
                icon   = {"Won": "✅", "Lost": "❌", "Push": "🔁"}.get(entry["result"], "•")
                member = interaction.guild.get_member(entry["uid"]) if interaction.guild else None
                name   = member.display_name if member else f"<@{entry['uid']}>"
                money  = (f"+${entry['profit']:,}" if entry["result"] == "Won"
                          else f"-${entry['wager']:,}" if entry["result"] == "Lost"
                          else f"↩️ ${entry['wager']:,}")
                lines.append(
                    f"{icon} **{name}** — {entry['pick']} ({entry['bet_type']}) | "
                    f"{entry['matchup']} | {money}"
                )
            embed.add_field(name="📋 Bet Results", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — OVERVIEW
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_status", description="[Commish] Sportsbook overview — lines, locks, pending bets")
    async def sb_status(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        bet_week = dm.CURRENT_WEEK + 1
        try:
            src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
            df  = src.copy()
            for col in ["weekIndex", "seasonIndex", "stageIndex"]:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: int(float(x)) if x not in (None, "", "nan") else -1
                    )
            if "weekIndex" in df.columns:
                mask = df["weekIndex"] == (bet_week - 1)
                if "seasonIndex" in df.columns:
                    mask = mask & (df["seasonIndex"] == dm.CURRENT_SEASON)
                games_df = df[mask]
            else:
                games_df = df
            raw_games = games_df.to_dict("records")
        except Exception as e:
            return await interaction.followup.send(f"❌ Error loading games: `{e}`", ephemeral=True)

        loop = asyncio.get_running_loop()
        ui_games = await loop.run_in_executor(None, _build_game_lines, raw_games)

        with _db_con() as con:
            bet_counts = {}
            for (matchup, cnt) in con.execute(
                "SELECT matchup, COUNT(*) FROM bets_table WHERE week=? AND status='Pending' "
                "GROUP BY matchup",
                (bet_week,)
            ).fetchall():
                bet_counts[matchup.lower().strip()] = cnt

        embed = discord.Embed(
            title=f"📊  SPORTSBOOK STATUS — Week {bet_week}",
            color=TSL_GOLD
        )
        embed.description = f"Season {dm.CURRENT_SEASON}  •  League Avg: **{_LEAGUE_AVG_SCORE:.1f} pts/team**\n"

        lines = []
        for g in ui_games:
            lock = "🔴" if _is_locked(g["game_id"]) else "🟢"
            ov   = " ⚡" if g.get("_overridden") else ""
            key  = g["matchup_key"].lower().strip()
            bets = bet_counts.get(key, 0)

            lines.append(
                f"{lock} **{g['away']}** @ **{g['home']}**{ov}\n"
                f"   Spread: `{g['away']} {g['away_spread']}` / `{g['home']} {g['home_spread']}`\n"
                f"   ML: `{g['away']} {g['away_ml']}` / `{g['home']} {g['home_ml']}`\n"
                f"   O/U: **{g['ou_line']}**  •  Pending bets: **{bets}**"
            )

        if lines:
            # Split into chunks to avoid field length limit
            chunk = []
            for line in lines:
                chunk.append(line)
                if len(chunk) == 4:
                    embed.add_field(name="\u200b", value="\n\n".join(chunk), inline=False)
                    chunk = []
            if chunk:
                embed.add_field(name="\u200b", value="\n\n".join(chunk), inline=False)

        embed.set_footer(text="🟢 = Open  🔴 = Locked  ⚡ = Admin Override")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sb_lines", description="[Commish] Debug power ratings driving each game's spread")
    async def sb_lines(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        bet_week = dm.CURRENT_WEEK + 1
        try:
            src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
            df  = src.copy()
            for col in ["weekIndex", "seasonIndex"]:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: int(float(x)) if x not in (None, "", "nan") else -1
                    )
            if "weekIndex" in df.columns:
                mask = df["weekIndex"] == (bet_week - 1)
                if "seasonIndex" in df.columns:
                    mask = mask & (df["seasonIndex"] == dm.CURRENT_SEASON)
                raw_games = df[mask].to_dict("records")
            else:
                raw_games = df.to_dict("records")
        except Exception as e:
            return await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

        loop = asyncio.get_running_loop()
        ui_games = await loop.run_in_executor(None, _build_game_lines, raw_games)

        power_map     = _get_power_map()
        owner_history = _get_owner_history_stats()
        _FB = {"ovr": 78, "win_pct": 0.5, "rank": 16, "off_rank": 16, "def_rank": 16, "userName": ""}

        embed = discord.Embed(
            title=f"🔬  Line Debug — Week {bet_week}",
            color=discord.Color.blurple()
        )
        for g in ui_games[:8]:   # Discord embed field limit
            hd = power_map.get(g["home"], _FB)
            ad = power_map.get(g["away"], _FB)
            ho = _resolve_owner(hd["userName"], owner_history)
            ao = _resolve_owner(ad["userName"], owner_history)
            h_pr = _power_rating(hd, ho)
            a_pr = _power_rating(ad, ao)
            ov_note = " **[OVERRIDE]**" if g.get("_overridden") else ""

            embed.add_field(
                name=f"{g['away']} @ {g['home']}{ov_note}",
                value=(
                    f"PR: **{g['away']}** {a_pr:+.2f} vs **{g['home']}** {h_pr:+.2f}\n"
                    f"Spread: home {g['home_spread']} (engine {g['_engine_spread']:+.1f})\n"
                    f"ML: {g['away']} {g['away_ml']} / {g['home']} {g['home_ml']}\n"
                    f"O/U: {g['ou_line']}  "
                    f"(H cW%:{ho['career_win_pct']:.3f} | A cW%:{ao['career_win_pct']:.3f})"
                ),
                inline=False
            )

        embed.set_footer(
            text=f"PR = Power Rating  •  League Avg Score: {_LEAGUE_AVG_SCORE:.1f} pts/team"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — LINE OVERRIDES
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_setspread", description="[Commish] Override the spread for a game")
    @app_commands.describe(
        matchup="e.g. 'Cowboys @ Eagles'",
        home_spread="Home team's spread (negative = home favored, e.g. -3.5)"
    )
    @app_commands.autocomplete(matchup=_matchup_autocomplete)
    async def sb_setspread(self, interaction: discord.Interaction,
                           matchup: str, home_spread: float):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        away_spread = -home_spread
        home_ml     = _spread_to_ml(home_spread)
        away_ml     = _spread_to_ml(away_spread)

        _set_line_override(
            matchup.strip(),
            set_by=interaction.user.display_name,
            home_spread=home_spread,
            away_spread=away_spread,
            home_ml=home_ml,
            away_ml=away_ml,
        )

        def fmt(s):
            return "PK" if s == 0 else (f"+{s}" if s > 0 else str(s))

        await interaction.response.send_message(
            f"✅ **Spread override set** for `{matchup}`\n"
            f"Home: **{fmt(home_spread)}** ({_american_to_str(home_ml)})\n"
            f"Away: **{fmt(away_spread)}** ({_american_to_str(away_ml)})\n"
            f"*(ML auto-calculated from spread)*",
            ephemeral=True
        )

    @app_commands.command(name="sb_setml", description="[Commish] Override moneylines for a game")
    @app_commands.describe(
        matchup="e.g. 'Cowboys @ Eagles'",
        home_ml="Home team moneyline (e.g. -145 or +125)",
        away_ml="Away team moneyline (e.g. +125 or -145)"
    )
    @app_commands.autocomplete(matchup=_matchup_autocomplete)
    async def sb_setml(self, interaction: discord.Interaction,
                       matchup: str, home_ml: int, away_ml: int):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        _set_line_override(
            matchup.strip(),
            set_by=interaction.user.display_name,
            home_ml=home_ml,
            away_ml=away_ml,
        )
        await interaction.response.send_message(
            f"✅ **ML override set** for `{matchup}`\n"
            f"Home: **{_american_to_str(home_ml)}**  |  Away: **{_american_to_str(away_ml)}**",
            ephemeral=True
        )

    @app_commands.command(name="sb_setou", description="[Commish] Override the Over/Under total for a game")
    @app_commands.describe(
        matchup="e.g. 'Cowboys @ Eagles'",
        ou_line="Over/Under total points (e.g. 47.5)"
    )
    @app_commands.autocomplete(matchup=_matchup_autocomplete)
    async def sb_setou(self, interaction: discord.Interaction, matchup: str, ou_line: float):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        _set_line_override(
            matchup.strip(),
            set_by=interaction.user.display_name,
            ou_line=ou_line,
        )
        await interaction.response.send_message(
            f"✅ **O/U override set** for `{matchup}`: **{ou_line}**",
            ephemeral=True
        )

    @app_commands.command(name="sb_resetlines", description="[Commish] Clear ALL admin line overrides — revert to engine")
    async def sb_resetlines(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        with _db_con() as con:
            count = con.execute("SELECT COUNT(*) FROM line_overrides").fetchone()[0]
            con.execute("DELETE FROM line_overrides")

        await interaction.response.send_message(
            f"✅ Cleared **{count}** line override(s). All games now use the ATLAS odds engine.",
            ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — GAME LOCKS
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_lock", description="[Commish] Lock or unlock betting for one game")
    @app_commands.describe(
        matchup="Game ID or 'Away @ Home'",
        locked="True to lock, False to unlock"
    )
    @app_commands.autocomplete(matchup=_matchup_autocomplete)
    async def sb_lock(self, interaction: discord.Interaction, matchup: str, locked: bool):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        _set_locked(matchup.strip(), locked)
        status = "🔴 **LOCKED**" if locked else "🟢 **UNLOCKED**"
        await interaction.response.send_message(
            f"{status} — `{matchup}` betting is now {'closed' if locked else 'open'}.",
            ephemeral=True
        )

    @app_commands.command(name="sb_lockall", description="[Commish] Lock ALL games for the current week")
    async def sb_lockall(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games
        if src.empty:
            return await interaction.followup.send("❌ No game data loaded.", ephemeral=True)

        bet_week = dm.CURRENT_WEEK + 1
        df = src.copy()
        for col in ["weekIndex", "seasonIndex"]:
            if col in df.columns:
                df[col] = df[col].apply(
                    lambda x: int(float(x)) if x not in (None, "", "nan") else -1
                )
        if "weekIndex" in df.columns:
            mask = df["weekIndex"] == (bet_week - 1)
            if "seasonIndex" in df.columns:
                mask = mask & (df["seasonIndex"] == dm.CURRENT_SEASON)
            games_df = df[mask]
        else:
            games_df = df

        count = 0
        for _, g in games_df.iterrows():
            game_id = str(g.get("gameId", g.get("id", g.get("matchup_key", ""))))
            if game_id:
                _set_locked(game_id, True)
                count += 1

        await interaction.followup.send(
            f"🔴 **Locked {count} game(s)** for Week {bet_week}. No new bets accepted.",
            ephemeral=True
        )

    @app_commands.command(name="sb_unlockall", description="[Commish] Unlock ALL games for the current week")
    async def sb_unlockall(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        with _db_con() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM games_state WHERE locked=1"
            ).fetchone()[0]
            con.execute("UPDATE games_state SET locked=0")

        await interaction.response.send_message(
            f"🟢 **Unlocked {count} game(s)**. Betting is now open.",
            ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — BET MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_cancelgame",
                          description="[Commish] Void & refund all pending bets on a game")
    @app_commands.describe(matchup="Matchup key, e.g. 'Cowboys @ Eagles'")
    @app_commands.autocomplete(matchup=_matchup_autocomplete)
    async def sb_cancelgame(self, interaction: discord.Interaction, matchup: str):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        key = matchup.strip().lower()
        refunded = 0
        total_refunded = 0

        with _db_con() as con:
            pending = con.execute(
                "SELECT bet_id, discord_id, wager_amount "
                "FROM bets_table WHERE LOWER(matchup)=? AND status='Pending'",
                (key,)
            ).fetchall()
            for bid, uid, amt in pending:
                _update_balance(uid, amt, con)
                con.execute(
                    "UPDATE bets_table SET status='Cancelled' WHERE bet_id=?", (bid,)
                )
                refunded      += 1
                total_refunded += amt

            # Also refund parlays containing this matchup
            parlay_rows = con.execute(
                "SELECT parlay_id, discord_id, legs, wager_amount "
                "FROM parlays_table WHERE status='Pending'"
            ).fetchall()
            parlay_refunds = 0
            for pid, uid, legs_json, amt in parlay_rows:
                try:
                    legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else []
                except Exception:
                    continue
                if any(key in leg.get("matchup", "").lower() for leg in legs):
                    _update_balance(uid, amt, con)
                    con.execute(
                        "UPDATE parlays_table SET status='Cancelled' WHERE parlay_id=?", (pid,)
                    )
                    parlay_refunds += 1
                    total_refunded += amt

            # Lock the game so no new bets come in
            _set_locked(matchup.strip(), True)

        await interaction.followup.send(
            f"✅ **Cancelled & refunded** bets for `{matchup}`\n"
            f"Straight bets: **{refunded}** refunded\n"
            f"Parlays: **{parlay_refunds}** refunded\n"
            f"Total returned: **${total_refunded:,}**\n"
            f"*(Game has been locked)*",
            ephemeral=True
        )

    @app_commands.command(name="sb_refund", description="[Commish] Refund a single bet by ID")
    @app_commands.describe(bet_id="Bet ID number (from /mybets or the DB)")
    async def sb_refund(self, interaction: discord.Interaction, bet_id: int):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        with _db_con() as con:
            bet = con.execute(
                "SELECT discord_id, wager_amount, pick, bet_type, matchup, status "
                "FROM bets_table WHERE bet_id=?",
                (bet_id,)
            ).fetchone()
            if not bet:
                return await interaction.response.send_message(
                    f"❌ Bet ID `{bet_id}` not found.", ephemeral=True
                )
            uid, amt, pick, btype, matchup, status = bet
            if status not in ('Pending',):
                return await interaction.response.send_message(
                    f"❌ Bet `{bet_id}` is already **{status}** and cannot be refunded.",
                    ephemeral=True
                )
            _update_balance(uid, amt, con)
            con.execute(
                "UPDATE bets_table SET status='Cancelled' WHERE bet_id=?", (bet_id,)
            )

        member = interaction.guild.get_member(uid) if interaction.guild else None
        name   = member.display_name if member else f"<@{uid}>"
        await interaction.response.send_message(
            f"✅ Refunded bet `#{bet_id}` — **{name}** gets **${amt:,}** back\n"
            f"*(was: {pick} {btype} on {matchup})*",
            ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — BALANCE MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_balance", description="[Commish] Manually adjust a member's TSL Bucks")
    @app_commands.describe(
        member="Discord member to adjust",
        adjustment="Amount to add (positive) or remove (negative)",
        reason="Optional reason for the audit log"
    )
    async def sb_balance(self, interaction: discord.Interaction,
                         member: discord.Member,
                         adjustment: int,
                         reason: str = "Commissioner adjustment"):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        old_balance = _get_balance(member.id)
        _update_balance(member.id, adjustment)
        new_balance = _get_balance(member.id)

        sign = f"+${adjustment:,}" if adjustment >= 0 else f"-${abs(adjustment):,}"
        await interaction.response.send_message(
            f"✅ Balance adjusted for **{member.display_name}**\n"
            f"${old_balance:,} → **${new_balance:,}** ({sign})\n"
            f"Reason: *{reason}*",
            ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADMIN — PROP BET MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="sb_addprop",
                          description="[Commish] Create a custom prop bet for the current week")
    @app_commands.describe(
        description="Full prop bet description, e.g. 'Will JT score 30+ pts this week?'",
        option_a="First option label (e.g. 'Yes' or 'Ravens win big')",
        option_b="Second option label (e.g. 'No' or 'Ravens win close')",
        odds_a="American odds for Option A (default -110)",
        odds_b="American odds for Option B (default -110)"
    )
    async def sb_addprop(self, interaction: discord.Interaction,
                         description: str,
                         option_a: str,
                         option_b: str,
                         odds_a: int = -110,
                         odds_b: int = -110):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)

        with _db_con() as con:
            cur = con.execute(
                "INSERT INTO prop_bets (week, description, option_a, option_b, odds_a, odds_b, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dm.CURRENT_WEEK + 1, description, option_a, option_b,
                 odds_a, odds_b, interaction.user.display_name)
            )
            prop_id = cur.lastrowid

        await interaction.response.send_message(
            f"✅ **Prop bet #{prop_id} created!**\n"
            f"**{description}**\n"
            f"A: **{option_a}** ({_american_to_str(odds_a)})\n"
            f"B: **{option_b}** ({_american_to_str(odds_b)})\n\n"
            f"Members can bet via `/props`.",
            ephemeral=True
        )

    @app_commands.command(name="sb_settleprop",
                          description="[Commish] Settle a prop bet and pay out winners")
    @app_commands.describe(
        prop_id="Prop bet ID number",
        result="Winning option: 'a', 'b', or 'push'"
    )
    @app_commands.choices(result=[
        app_commands.Choice(name="Option A wins",  value="a"),
        app_commands.Choice(name="Option B wins",  value="b"),
        app_commands.Choice(name="Push (refund all)", value="push"),
    ])
    async def sb_settleprop(self, interaction: discord.Interaction,
                            prop_id: int, result: str):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        await interaction.response.defer(thinking=True, ephemeral=True)

        with _db_con() as con:
            prop = con.execute(
                "SELECT description, option_a, option_b, odds_a, odds_b, status "
                "FROM prop_bets WHERE prop_id=?",
                (prop_id,)
            ).fetchone()
            if not prop:
                return await interaction.followup.send(
                    f"❌ Prop #{prop_id} not found.", ephemeral=True
                )
            desc, opt_a, opt_b, odds_a, odds_b, status = prop
            if status != 'Open':
                return await interaction.followup.send(
                    f"❌ Prop #{prop_id} is already **{status}**.", ephemeral=True
                )

            wagers = con.execute(
                "SELECT id, discord_id, pick, wager_amount, odds "
                "FROM prop_wagers WHERE prop_id=? AND status='Pending'",
                (prop_id,)
            ).fetchall()

            wins = losses = pushes = 0
            total_paid = 0

            for wid, uid, pick, amt, odds in wagers:
                pick_lower = pick.lower().strip()
                if result == "push":
                    _update_balance(uid, amt, con)
                    con.execute("UPDATE prop_wagers SET status='Push' WHERE id=?", (wid,))
                    pushes += 1
                else:
                    winning_pick = opt_a if result == "a" else opt_b
                    if pick_lower == winning_pick.lower().strip():
                        payout = _payout_calc(amt, int(odds))
                        _update_balance(uid, payout, con)
                        total_paid += payout - amt
                        con.execute("UPDATE prop_wagers SET status='Won' WHERE id=?", (wid,))
                        wins += 1
                    else:
                        con.execute("UPDATE prop_wagers SET status='Lost' WHERE id=?", (wid,))
                        losses += 1

            result_label = (opt_a if result == "a" else opt_b if result == "b" else "PUSH")
            con.execute(
                "UPDATE prop_bets SET status='Settled', result=? WHERE prop_id=?",
                (result_label, prop_id)
            )

        embed = discord.Embed(title=f"✅  Prop #{prop_id} Settled", color=TSL_GREEN)
        embed.description = f"**{desc}**\n**Result: {result_label}**"
        embed.add_field(name="✅ Won",       value=str(wins),           inline=True)
        embed.add_field(name="❌ Lost",      value=str(losses),         inline=True)
        embed.add_field(name="🔁 Push",     value=str(pushes),         inline=True)
        embed.add_field(name="💸 Paid Out", value=f"${total_paid:,}",  inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # LEGACY COMMANDS (backward compat — delegate to new system)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="lockgame",
                          description="[Commish] Lock a game to stop new bets (use /sb_lock instead)")
    @app_commands.describe(matchup="e.g. 'Cowboys @ Eagles'", locked="True to lock, False to unlock")
    async def lockgame(self, interaction: discord.Interaction, matchup: str, locked: bool):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        _set_locked(matchup.strip(), locked)
        status = "🔴 **LOCKED**" if locked else "🟢 **UNLOCKED**"
        await interaction.response.send_message(
            f"{status} — `{matchup}` betting is now {'closed' if locked else 'open'}.",
            ephemeral=True
        )

    @app_commands.command(name="setline",
                          description="[Commish] Override O/U line for a game (use /sb_setou instead)")
    @app_commands.describe(game_id="Game ID or 'Away @ Home'", ou_line="Over/Under total points")
    async def setline(self, interaction: discord.Interaction, game_id: str, ou_line: float):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Commissioner only.", ephemeral=True)
        _set_line_override(
            game_id.strip(),
            set_by=interaction.user.display_name,
            ou_line=ou_line,
        )
        await interaction.response.send_message(
            f"✅ O/U for `{game_id}` set to **{ou_line}**.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SportsbookCog(bot))
